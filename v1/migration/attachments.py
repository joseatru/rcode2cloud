"""
migration.attachments
---------------------
Attachment re-hosting and markdown rewriting.

Upload secrets are project-scoped, so a source project's
`/uploads/<secret>/<file>` path 404s under the destination project. Every
referenced file is downloaded with the source token, re-uploaded to the
destination, and the path rewritten in place. cache/upload_map.json keeps a
file referenced by twenty issues to a single transfer, and survives a resume.
"""

import re

import aiohttp

from typing import Optional

from .logging_setup import log


# ------------------------------------------------------------------ #
# Markdown upload references
# ------------------------------------------------------------------ #
# GitLab renders attachments as `[label](/uploads/<32-hex-secret>/<filename>)`.
# The secret is project-scoped, so the destination project cannot serve the
# source project's upload paths: each referenced file must be downloaded and
# re-uploaded, then the markdown rewritten to the new path.
UPLOAD_REF_RE = re.compile(r"/uploads/([0-9a-f]{32})/([^\s\)\"'<>]+)")


class AttachmentsMixin:
    """
    Attachment download, re-upload, and markdown rewriting.

    Expects the host object to provide `cfg`, `mappings`, both URLs, both
    tokens, both project IDs, `upload_map`, and `stats`.
    """

    # ---------------------------------------------------------------- #
    # [9] Attachment Migration
    # ---------------------------------------------------------------- #

    async def _fetch_source_upload(
        self,
        session: aiohttp.ClientSession,
        secret: str,
        filename: str
    ) -> Optional[bytes]:
        """
        Download one source-project upload by its markdown secret and filename.

        Two routes are attempted because upload download support depends on the
        source instance's version:
          1. GET /projects/:id/uploads/:secret/:filename  (REST, GitLab 16.6+)
          2. <project web path>/uploads/:secret/:filename  (web route)

        Args:
            session:  Active aiohttp session.
            secret:   32-character hex secret from the markdown path.
            filename: File name from the markdown path.

        Returns:
            File bytes, or None if every route failed.
        """
        candidates = [
            f"{self.source_url}/api/v4/projects/{self.source_project}"
            f"/uploads/{secret}/{filename}"
        ]

        src_web_path = (self.mappings.get("source") or {}).get("path")
        if src_web_path:
            candidates.append(
                f"{src_web_path.rstrip('/')}/uploads/{secret}/{filename}"
            )

        for url in candidates:
            try:
                return await self._download(session, url, self.source_token)
            except Exception as exc:
                log.debug(f"    Upload fetch failed via {url}: {exc}")

        return None

    async def rewrite_uploads(
        self,
        session: aiohttp.ClientSession,
        text: Optional[str],
        source_iid
    ) -> str:
        """
        Re-host every /uploads/ reference in a markdown body.

        Upload secrets are project-scoped, so a source project's
        `/uploads/<secret>/<file>` path 404s under the destination project.
        Each referenced file is downloaded from the source, re-uploaded to the
        destination, and the path rewritten in place — the surrounding markdown
        (including the `!` image prefix and the link label) is untouched.

        cache/upload_map.json maps source path → destination upload, so a file
        referenced by twenty issues is transferred once and survives a resume.

        A file that cannot be downloaded or re-uploaded leaves its original
        path in the text: a visibly broken link is preferable to silently
        dropping the reference, and the failure is logged.

        Args:
            session:    Active aiohttp session.
            text:       Markdown body (issue description or note body).
            source_iid: Source issue IID, for log context.

        Returns:
            The body with all successfully migrated paths rewritten.
        """
        if not text:
            return text or ""

        matches = list(UPLOAD_REF_RE.finditer(text))
        if not matches:
            return text

        result = text

        # De-duplicate within this body: the same path can appear twice.
        for source_path in dict.fromkeys(m.group(0) for m in matches):
            match = UPLOAD_REF_RE.match(source_path)
            if not match:
                continue
            secret, filename = match.group(1), match.group(2)

            cached = self.upload_map.get(source_path)
            if cached:
                dest_path = cached["url"] if isinstance(cached, dict) else cached
                result    = result.replace(source_path, dest_path)
                log.debug(f"    Attachment cache hit: {filename}")
                continue

            if self.cfg["DRY_RUN"]:
                log.info(
                    f"  [DRY RUN] Would re-upload attachment {filename} "
                    f"referenced by issue #{source_iid}"
                )
                self.stats["attachments_uploaded"] += 1
                continue

            content = await self._fetch_source_upload(session, secret, filename)
            if content is None:
                log.warning(
                    f"    Could not download attachment {filename} "
                    f"(issue #{source_iid}); leaving the original link in place."
                )
                continue

            try:
                uploaded = await self._upload_file(
                    session, self.dest_token, filename, content
                )
            except Exception as exc:
                log.warning(
                    f"    Could not re-upload attachment {filename} "
                    f"(issue #{source_iid}): {exc}"
                )
                continue

            dest_path = uploaded.get("url") or uploaded.get("full_path")
            if not dest_path:
                log.warning(
                    f"    Upload of {filename} returned no url field; "
                    f"leaving the original link in place."
                )
                continue

            self.upload_map[source_path] = {
                "url":      dest_path,
                "markdown": uploaded.get("markdown", ""),
            }
            self.save_upload_cache()
            self.stats["attachments_uploaded"] += 1
            result = result.replace(source_path, dest_path)
            log.debug(f"    Attachment re-hosted: {filename} → {dest_path}")

        return result
