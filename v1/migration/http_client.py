"""
migration.http_client
---------------------
The transport layer: TLS setup, the shared aiohttp session, one thin wrapper
per HTTP verb, and the retry/backoff policy they all route through.

Every API call in the tool goes through these helpers, so rate-limit handling
and transient-failure recovery live in exactly one place. Nothing here knows
anything about issues, notes, or fidelity.
"""

import asyncio
import re
import ssl

import aiohttp

from typing import Optional

from .logging_setup import log


# ================================================================== #
# [5] Retry / Exponential Backoff Helper
# ================================================================== #

# Transport-level failures that are worth another attempt. A dropped
# connection midway through a multi-hour migration is expected, not fatal.
RETRIABLE_EXCEPTIONS = (
    asyncio.TimeoutError,
    aiohttp.ServerDisconnectedError,
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ClientPayloadError,
)

RETRIABLE_STATUSES = {429, 500, 502, 503, 504}


def _retry_after_seconds(exc: aiohttp.ClientResponseError) -> Optional[float]:
    """
    Extract a Retry-After delay from a 429 response, if the server sent one.

    GitLab returns Retry-After (in seconds) alongside its rate-limit
    responses. Honouring it beats blind exponential doubling, which either
    waits far too long or hammers the endpoint again too soon.

    Args:
        exc: The raised ClientResponseError (its .headers may be None).

    Returns:
        Delay in seconds, or None if the header is absent or unparseable.
    """
    headers = getattr(exc, "headers", None) or {}
    raw     = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(str(raw).strip()))
    except (TypeError, ValueError):
        return None


async def api_call_with_retry(coro_fn, retry_count: int, backoff: float):
    """
    Execute an async API call with exponential backoff retry.

    Retries on:
      HTTP 429  — Too Many Requests (honouring Retry-After when present)
      HTTP 500  — Internal Server Error
      HTTP 502  — Bad Gateway
      HTTP 503  — Service Unavailable
      HTTP 504  — Gateway Timeout
      asyncio.TimeoutError, ServerDisconnectedError, ClientConnectorError,
      ClientOSError, ClientPayloadError — transient transport failures

    All other HTTP errors (401, 403, 404, 409, 422, ...) are raised
    immediately: they signal a configuration or data problem that retrying
    cannot resolve.

    Args:
        coro_fn:     Zero-argument async callable that performs one API call.
        retry_count: Maximum number of retry attempts after the first failure.
        backoff:     Base wait time in seconds (doubles on each retry).

    Returns:
        The return value of coro_fn() on success.

    Raises:
        The last exception encountered after all retry attempts are exhausted.
    """
    delay    = backoff
    last_exc = None

    for attempt in range(1, retry_count + 2):  # +1 for the initial attempt
        try:
            return await coro_fn()

        except aiohttp.ClientResponseError as exc:
            last_exc = exc

            if exc.status not in RETRIABLE_STATUSES:
                # Non-retriable (e.g. 401, 403, 404, 409) — raise immediately
                raise

            if attempt > retry_count:
                log.error(
                    f"HTTP {exc.status} — all {retry_count + 1} attempts failed."
                )
                raise

            wait = _retry_after_seconds(exc) if exc.status == 429 else None
            if wait is None:
                wait  = delay
                delay *= 2  # Exponential backoff
            else:
                log.debug(f"Honouring Retry-After: {wait:.1f}s")

            log.warning(
                f"HTTP {exc.status} on attempt {attempt}/{retry_count + 1}. "
                f"Retrying in {wait:.1f}s... ({exc.message})"
            )
            await asyncio.sleep(wait)

        except RETRIABLE_EXCEPTIONS as exc:
            last_exc = exc

            if attempt > retry_count:
                log.error(
                    f"{type(exc).__name__} — all {retry_count + 1} attempts "
                    f"failed: {exc}"
                )
                raise

            log.warning(
                f"{type(exc).__name__} on attempt {attempt}/{retry_count + 1}. "
                f"Retrying in {delay:.1f}s... ({exc})"
            )
            await asyncio.sleep(delay)
            delay *= 2

    # Unreachable: every loop exit either returns or re-raises. Kept as a
    # guard so a future edit to the loop bounds cannot silently return None.
    raise last_exc or RuntimeError("api_call_with_retry exhausted no attempts")


class HttpMixin:
    """
    HTTP transport for GitLabMigration.

    Expects the host object to provide `cfg`, `ssl_context`, `retry_count`,
    `retry_backoff`, `dest_url`, and `dest_project`.
    """

    # ---------------------------------------------------------------- #
    # TLS Context
    # ---------------------------------------------------------------- #

    def _build_ssl_context(self):
        """
        Build the SSL setting handed to the aiohttp TCP connector.

        aiohttp does not read REQUESTS_CA_BUNDLE — that variable is honoured
        only by `requests` — so an internal CA has to be loaded explicitly and
        attached to the connector.

        Returns:
            False to disable verification (--insecure), an ssl.SSLContext
            when --ca-bundle is given, or None to use aiohttp's defaults.
        """
        if self.cfg.get("INSECURE"):
            log.warning(
                "TLS certificate verification is DISABLED (--insecure). "
                "Traffic is encrypted but the server identity is unverified."
            )
            return False

        ca_bundle = self.cfg.get("CA_BUNDLE")
        if ca_bundle:
            context = ssl.create_default_context(cafile=ca_bundle)
            log.info(f"TLS verification using CA bundle: {ca_bundle}")
            return context

        return None

    # ---------------------------------------------------------------- #
    # Core HTTP Helpers — GET, paginated GET, POST, PUT, DELETE, upload
    # ---------------------------------------------------------------- #

    @staticmethod
    async def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
        """
        Raise ClientResponseError for a 4xx/5xx response, including its body.

        aiohttp's own raise_for_status() discards the response body, which is
        exactly where GitLab explains *why* a write was rejected (for example
        "iid: has already been taken" versus "assignee_ids is not available").
        """
        if resp.status < 400:
            return

        try:
            detail = (await resp.text())[:600].replace("\n", " ").strip()
        except Exception:
            detail = ""

        raise aiohttp.ClientResponseError(
            resp.request_info,
            resp.history,
            status=resp.status,
            message=f"{resp.reason} — {detail}" if detail else str(resp.reason),
            headers=resp.headers,
        )

    async def _get(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str
    ):
        """
        Single authenticated GET with retry/backoff.

        Args:
            session: Active aiohttp session.
            url:     Full API endpoint URL.
            token:   GitLab PRIVATE-TOKEN value.

        Returns:
            Parsed JSON (dict or list).
        """
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            async with session.get(url, headers=headers) as resp:
                await self._raise_for_status(resp)
                return await resp.json()

        return await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    # [1] Full Pagination Support
    async def _get_all_pages(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str
    ) -> list:
        """
        Fetch all pages from a paginated GitLab API endpoint.

        GitLab uses the X-Next-Page response header to indicate the next
        page number. This method requests per_page=100 (the GitLab maximum)
        and follows X-Next-Page until it is empty.

        Args:
            session: Active aiohttp session.
            url:     Base API URL. Query params may already be present.
            token:   GitLab PRIVATE-TOKEN value.

        Returns:
            Flat list of all items across all pages.
        """
        headers   = {"PRIVATE-TOKEN": token}
        results   = []

        # Append per_page and page=1 to the URL
        sep      = "&" if "?" in url else "?"
        page_url = f"{url}{sep}per_page=100&page=1"

        page_num = 1
        while page_url:
            current_url = page_url  # capture for closure

            async def _call(u=current_url):
                async with session.get(u, headers=headers) as resp:
                    await self._raise_for_status(resp)
                    data      = await resp.json()
                    next_page = resp.headers.get("X-Next-Page", "").strip()
                    total     = resp.headers.get("X-Total", "?")
                    return data, next_page, total

            data, next_page, total = await api_call_with_retry(
                _call, self.retry_count, self.retry_backoff
            )

            results.extend(data)
            log.debug(
                f"Page {page_num}: fetched {len(data)} items "
                f"(cumulative: {len(results)} / {total})"
            )

            if next_page:
                # Replace existing page param with the next page number
                base     = re.sub(r"([&?])page=\d+", "", page_url)
                sep_next = "&" if "?" in base else "?"
                page_url = f"{base}{sep_next}page={next_page}"
                page_num += 1
            else:
                page_url = None  # No more pages

        return results

    async def _post(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str,
        payload: dict
    ) -> dict:
        """
        Authenticated POST with retry/backoff.

        Args:
            session: Active aiohttp session.
            url:     Full API endpoint URL.
            token:   GitLab PRIVATE-TOKEN value.
            payload: JSON body dict.

        Returns:
            Parsed JSON response.
        """
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            async with session.post(url, headers=headers, json=payload) as resp:
                await self._raise_for_status(resp)
                return await resp.json()

        return await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    async def _put(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str,
        payload: dict
    ) -> dict:
        """
        Authenticated PUT with retry/backoff.

        Used to update issue state (open → closed) after issue creation.

        Args:
            session: Active aiohttp session.
            url:     Full API endpoint URL.
            token:   GitLab PRIVATE-TOKEN value.
            payload: JSON body dict (e.g. {"state_event": "close"}).

        Returns:
            Parsed JSON response.
        """
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            async with session.put(url, headers=headers, json=payload) as resp:
                await self._raise_for_status(resp)
                return await resp.json()

        return await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    async def _delete(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str
    ) -> None:
        """
        Authenticated DELETE with retry/backoff.

        Used only by the IID counter bump, which creates and immediately
        removes a throwaway issue.

        Args:
            session: Active aiohttp session.
            url:     Full API endpoint URL.
            token:   GitLab PRIVATE-TOKEN value.
        """
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            async with session.delete(url, headers=headers) as resp:
                await self._raise_for_status(resp)
                return None

        await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    async def _download(
        self,
        session: aiohttp.ClientSession,
        url: str,
        token: str
    ) -> bytes:
        """
        Download raw bytes from an authenticated URL.

        Args:
            session: Active aiohttp session.
            url:     Full URL (API or web route).
            token:   GitLab PRIVATE-TOKEN value.

        Returns:
            Response body as bytes.
        """
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            async with session.get(url, headers=headers) as resp:
                await self._raise_for_status(resp)
                return await resp.read()

        return await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    async def _upload_file(
        self,
        session: aiohttp.ClientSession,
        token: str,
        filename: str,
        content: bytes
    ) -> dict:
        """
        Upload a file to the destination project via POST /projects/:id/uploads.

        The endpoint expects multipart/form-data with the field named `file`
        and returns a `markdown` field containing the ready-to-embed link.

        Args:
            session:  Active aiohttp session.
            token:    GitLab PRIVATE-TOKEN value.
            filename: Name to store the upload under.
            content:  File bytes.

        Returns:
            Parsed JSON response (contains url, full_path, and markdown).
        """
        url     = f"{self.dest_url}/api/v4/projects/{self.dest_project}/uploads"
        headers = {"PRIVATE-TOKEN": token}

        async def _call():
            form = aiohttp.FormData()
            form.add_field(
                "file",
                content,
                filename=filename,
                content_type="application/octet-stream",
            )
            async with session.post(url, headers=headers, data=form) as resp:
                await self._raise_for_status(resp)
                return await resp.json()

        return await api_call_with_retry(_call, self.retry_count, self.retry_backoff)

    def _build_session(self) -> aiohttp.ClientSession:
        """
        Create the shared aiohttp session with the configured TLS settings.

        Returns:
            An aiohttp.ClientSession the caller owns and must close.
        """
        timeout   = aiohttp.ClientTimeout(total=self.cfg["REQUEST_TIMEOUT"])
        connector = aiohttp.TCPConnector(
            ssl=self.ssl_context,
            limit=max(self.cfg["MAX_CONCURRENT_REQUESTS"] * 2, 10),
        )
        return aiohttp.ClientSession(timeout=timeout, connector=connector)
