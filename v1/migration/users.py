"""
migration.users
---------------
Authorship resolution and destination user lookup.

This is where the fidelity model is decided per item: which PAT writes a given
author's content, and therefore whether the row is recorded as `full` or
`admin_attributed`. See AGENTS.md "Fidelity and authorship model" before
changing any of it — authorship is immutable after creation.
"""

import aiohttp

from typing import Optional

import config as cfg_module

from .logging_setup import log


class AuthorshipMixin:
    """
    Per-author token resolution and destination user ID caching.

    Expects the host object to provide `mappings`, `dest_token`, `dest_url`,
    `author_tokens`, `user_cache`, `source_url`, and `source_project`.
    """

    # ---------------------------------------------------------------- #
    # [4] Authorship — Per-Author Token Resolution
    # ---------------------------------------------------------------- #

    def token_for_author(self, source_username: Optional[str]) -> tuple:
        """
        Resolve a source username to the PAT that should author its content.

        A mapped user with a populated pat_env writes their own issues and
        notes, which is the only way GitLab records them as the author while
        also permitting iid / created_at (both need owner rights). Everyone
        else is written by the admin Owner token and carries a markdown
        attribution header instead.

        A source author *absent* from mappings.yml is an expected fallback. A
        mapped user whose PAT is unset is not: --sanity-check fails on that
        case, so reaching it here means the pre-flight was skipped. The run
        still degrades rather than aborting mid-migration, but it says so
        loudly.

        Results are memoised, so a missing env var is logged once per user
        rather than once per note.

        Args:
            source_username: Username from the source instance, or None.

        Returns:
            (token, fidelity) where fidelity is "full" when the token belongs
            to the intended author and "admin_attributed" otherwise.
        """
        if not source_username:
            return self.dest_token, "admin_attributed"

        if source_username in self.author_tokens:
            token = self.author_tokens[source_username]
            if token:
                return token, "full"
            return self.dest_token, "admin_attributed"

        mapping = self.mappings.get("users", {}).get(source_username) or {}
        pat_env = mapping.get("pat_env", "")
        token   = cfg_module.read_token(pat_env) if pat_env else None

        self.author_tokens[source_username] = token

        if token:
            log.debug(
                f"Authorship: {source_username} writes with its own PAT "
                f"({pat_env})"
            )
            return token, "full"

        if mapping:
            log.warning(
                f"Authorship: {source_username} is mapped but {pat_env or 'pat_env'} "
                f"is unset — falling back to DEST_TOKEN with an attribution header. "
                f"--sanity-check rejects this; content authored now cannot be "
                f"reattributed later."
            )
        else:
            log.debug(
                f"Authorship: {source_username} is not in mappings.yml — "
                f"falling back to DEST_TOKEN with an attribution header."
            )
        return self.dest_token, "admin_attributed"

    def source_issue_url(self, source_iid) -> str:
        """
        Build a browser URL for a source issue, used in attribution headers.

        Args:
            source_iid: Source issue IID.

        Returns:
            Absolute URL string. Falls back to the API-style project path when
            mappings.yml does not define a browsable source path.
        """
        src_path = (self.mappings.get("source") or {}).get("path")
        base     = (src_path or f"{self.source_url}/projects/{self.source_project}")
        return f"{base.rstrip('/')}/-/issues/{source_iid}"

    def attribution_header(self, author: str, created_at: str,
                           source_iid, kind: str) -> str:
        """
        Build the markdown header prepended to admin-authored content.

        Args:
            author:     Original source username.
            created_at: Original ISO-8601 timestamp.
            source_iid: Source issue IID, for the traceability link.
            kind:       Human label, e.g. "issue" or "note".

        Returns:
            Markdown block ending in a horizontal rule.
        """
        return (
            f"> 🔁 **Migrated {kind}** &nbsp;|&nbsp; "
            f"Author: `{author}` &nbsp;|&nbsp; "
            f"Originally posted: `{created_at}` &nbsp;|&nbsp; "
            f"[View source issue]({self.source_issue_url(source_iid)})\n\n"
            f"---\n\n"
        )

    # ---------------------------------------------------------------- #
    # [2] User Resolution with Destination-User ID Caching
    # ---------------------------------------------------------------- #

    async def resolve_user(
        self,
        session: aiohttp.ClientSession,
        source_username: str
    ) -> Optional[int]:
        """
        Resolve a source GitLab username to a destination user ID.

        Lookup order (fastest first):
          1. In-memory cache (self.user_cache) — O(1)
          2. Persistent disk cache (cache/user_id_cache.json) — already loaded
          3. Live API call to destination GitLab /api/v4/users

        On a successful live lookup the result is stored in both the in-memory
        and disk caches so future calls are instant.

        Args:
            session:         Active aiohttp session.
            source_username: Username from the source project issue.

        Returns:
            Destination user ID (int), or None if not mapped or not found.
        """
        # Look up the user mapping in mappings.yml
        user_map = self.mappings.get("users", {})
        mapping  = user_map.get(source_username)

        if not mapping:
            log.debug(f"No user mapping defined for source user: {source_username}")
            return None

        dest_username = mapping.get("dest_username", "")

        # [2] Return from cache if already resolved (avoids redundant API calls)
        if dest_username in self.user_cache:
            return self.user_cache[dest_username]

        # Live lookup
        lookup_url = (
            f"{self.dest_url}/api/v4/users?username={dest_username}"
        )
        try:
            users = await self._get(session, lookup_url, self.dest_token)

            if users:
                user_id = users[0]["id"]
                # Store in both caches
                self.user_cache[dest_username] = user_id
                self.save_user_cache()
                log.debug(
                    f"Resolved: {source_username} → {dest_username} "
                    f"(ID: {user_id}) [cached]"
                )
                return user_id
            else:
                log.warning(
                    f"Destination user not found: {dest_username} "
                    f"(mapped from: {source_username})"
                )
                return None

        except Exception as exc:
            log.warning(f"User resolution failed for {dest_username}: {exc}")
            return None
