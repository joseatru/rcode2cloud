"""
migration.issues
----------------
Source issue selection and the issue write path.

Two passes live here and they are deliberately different shapes:

  migrate_issue()          sequential, ascending IID, concurrency 1 — GitLab
                           assigns preserved IIDs monotonically, so this cannot
                           become a gather().
  migrate_issue_content()  concurrent across issues — note threads are
                           independent and are the bulk of the work.

Also here: the mode-dependent `--limit` semantics (_apply_limit), the adoption
path for an IID that is already occupied, and the final `updated_at` restore,
which must run after the note thread because every note write bumps it.
"""

import aiohttp

from typing import Optional

from .fidelity import FidelityGuardError
from .logging_setup import log
from .timeutils import utc_now_iso


class IssuesMixin:
    """
    Issue fetching, selection, creation, adoption, and finalisation.

    Expects the host object to provide `cfg`, `checkpoint`, `mappings`,
    `milestone_map`, both URLs, both tokens, both project IDs, `stats`,
    `failures`, `report_rows`, and `supports_multiple_assignees`.
    """

    # ---------------------------------------------------------------- #
    # Issue Fetching with Pagination
    # ---------------------------------------------------------------- #

    def _is_pending(self, issue: dict) -> bool:
        """
        Return True when an issue still has migration work outstanding.

        "Outstanding" means either no checkpoint record at all, or a record
        whose note thread and final state were never completed. Adopted issues
        and runs killed between creation and the note pass land in the second
        category, so a windowed run still finishes them instead of stranding
        them permanently.

        Args:
            issue: Source issue dict.

        Returns:
            True if the issue is not yet fully migrated.
        """
        record = self.checkpoint.get(str(issue["iid"]))
        return not record or not record.get("notes_done", False)

    def _apply_limit(self, issues: list) -> list:
        """
        Truncate the work list according to the active mode's limit semantics.

        The two modes need opposite behaviour from the same integer:

          --test-run (and --dry-run): the limit is applied BEFORE checkpoint
              filtering, so the pilot set is always the same first N issues by
              ascending IID. A resumed pilot re-examines issues 1–50 rather
              than drifting to 31–80, which is what makes pilot results
              comparable between runs.

          --full-run: the limit is applied AFTER checkpoint filtering, so it
              behaves as a scheduling window over the remaining backlog.
              Consecutive `--full-run --limit N` invocations each move N
              issues forward instead of re-scanning the same completed prefix
              and doing nothing.

        With --no-resume the checkpoint is ignored entirely, so the two paths
        collapse to the same first-N slice.

        Args:
            issues: All source issues, ascending by IID.

        Returns:
            The possibly-truncated list to process this run.
        """
        limit = self.cfg.get("LIMIT")
        if not limit:
            return issues

        windowed = self.cfg.get("FULL_RUN") and self.cfg.get("RESUME", True)

        if windowed:
            pending = [issue for issue in issues if self._is_pending(issue)]
            selected = pending[:limit]
            if selected:
                log.info(
                    f"Limit applied after checkpoint filtering: "
                    f"{len(selected)} of {len(pending)} outstanding issue(s) "
                    f"this window (IIDs {selected[0]['iid']}–"
                    f"{selected[-1]['iid']})."
                )
            else:
                log.info(
                    "Limit applied after checkpoint filtering: nothing "
                    "outstanding — every source issue is already migrated."
                )
            return selected

        selected = issues[:limit]
        log.info(
            f"Limit applied before checkpoint filtering: first "
            f"{len(selected)} issue(s) by ascending IID (stable across resumes)."
        )
        return selected

    async def fetch_issues(self, session: aiohttp.ClientSession) -> list:
        """
        Fetch all issues from the source project using full pagination.

        Sorting is done client-side. `order_by=iid` is NOT a valid GitLab
        parameter — the issues endpoint accepts only created_at, updated_at,
        priority, due_date, relative_position, label_priority, milestone_due,
        popularity, title, and weight — and passing it makes GitLab fall back
        to its default ordering, which is not what the caller asked for.
        Ascending IID order matters because preserved IIDs must be created
        monotonically.

        Args:
            session: Active aiohttp session.

        Returns:
            List of issue dicts in ascending IID order, truncated by
            cfg["LIMIT"] under the mode-dependent rules in _apply_limit().
        """
        url = (
            f"{self.source_url}/api/v4/projects/{self.source_project}"
            f"/issues?state=all&order_by=created_at&sort=asc"
        )

        log.info("─── Fetching source issues (paginated) ───")
        issues = await self._get_all_pages(session, url, self.source_token)
        issues.sort(key=lambda issue: int(issue["iid"]))
        log.info(f"Found {len(issues)} issues in source project.")

        return self._apply_limit(issues)
    # ---------------------------------------------------------------- #
    # [4] Issue Migration — Full Fidelity
    # ---------------------------------------------------------------- #

    async def migrate_issue(
        self,
        session: aiohttp.ClientSession,
        issue: dict
    ) -> None:
        """
        Create one destination issue, preserving its identity and timestamps.

        This is the sequential pass. Notes, closing state, and the updated_at
        restoration happen later in migrate_issue_content(), which can run
        concurrently across issues.

        Steps (per issue):
          1. Skip if already in the checkpoint (resumable).
          2. Adopt, rather than duplicate, an issue that already occupies the
             target IID on the destination.
          3. In dry-run: record what would happen and return.
          4. Resolve the authoring token from the source author.
          5. Re-host attachments referenced by the description.
          6. Resolve assignees and the destination milestone ID.
          7. POST the issue with its original iid and created_at.
          8. Verify the created issue really carries them (fidelity guard).
          9. Journal the checkpoint, statistics, and report row.

        `iid` and `created_at` are only honoured for a token with owner rights,
        which is why authorship and permissions are the same problem here: the
        author's own PAT must hold Owner. When it does not, DEST_TOKEN writes
        the issue with an attribution header and the row is recorded as
        fidelity=admin_attributed.

        Any per-issue failure is caught, logged, and added to failures.json
        without stopping the rest of the migration. A FidelityGuardError is the
        one exception: it means the destination did not honour what it was
        sent, so every later write would be wrong the same way and the run
        aborts instead.

        Args:
            session: Active aiohttp session.
            issue:   Issue dict from the source GitLab API.
        """
        iid    = str(issue["iid"])
        title  = issue["title"]
        author = (issue.get("author") or {}).get("username", "")

        # ---- [Resumable] Skip already-migrated issues ---- #
        if self.cfg.get("RESUME", True) and iid in self.checkpoint:
            log.debug(f"Skipping (already migrated): #{iid} — {title}")
            self.stats["skipped"] += 1
            return

        token, fidelity = self.token_for_author(author)

        # ---- Dry Run: describe what would happen ---- #
        if self.cfg["DRY_RUN"]:
            assignees = [
                a.get("username", "") for a in (issue.get("assignees") or [])
            ]
            dest_assignees = [
                self.mappings.get("users", {}).get(name, {})
                .get("dest_username", f"{name} (unmapped)")
                for name in assignees
            ] or ["unassigned"]
            milestone_title = (
                (issue.get("milestone") or {}).get("title", "none")
            )

            await self.rewrite_uploads(session, issue.get("description"), iid)

            log.info(
                f"  [DRY RUN] Issue #{iid}: {title}\n"
                f"            Author:    {author or 'unknown'} "
                f"(fidelity: {fidelity})\n"
                f"            Assignees: {', '.join(assignees) or 'none'} → "
                f"{', '.join(dest_assignees)}\n"
                f"            Labels:    {issue.get('labels', [])}\n"
                f"            Milestone: {milestone_title}\n"
                f"            Created:   {issue.get('created_at')}\n"
                f"            State:     {issue.get('state', 'unknown')}\n"
                f"            Notes:     {issue.get('user_notes_count', 0)}"
            )
            self.stats["migrated"] += 1
            self._count_fidelity(fidelity)
            self._add_report_row(
                issue, dest_iid="", notes=0, author=author,
                fidelity=fidelity, status="would_migrate"
            )
            return

        try:
            # ---- Adopt an issue already occupying this IID ---- #
            existing = await self._existing_dest_issue(session, iid)
            if existing is not None:
                await self._adopt_existing_issue(session, issue, existing,
                                                 author, fidelity)
                return

            # ---- Re-host attachments in the description ---- #
            description = await self.rewrite_uploads(
                session, issue.get("description"), iid
            )
            if fidelity == "admin_attributed":
                description = self.attribution_header(
                    author or "unknown", issue.get("created_at", "unknown"),
                    iid, "issue"
                ) + description

            # ---- Resolve assignees ---- #
            assignee_ids = []
            for assignee in (issue.get("assignees") or []):
                uid = await self.resolve_user(session, assignee.get("username", ""))
                if uid:
                    assignee_ids.append(uid)

            # ---- Resolve milestone ID ---- #
            milestone_id  = None
            src_milestone = issue.get("milestone")
            if src_milestone:
                milestone_id = self.milestone_map.get(str(src_milestone["iid"]))

            # ---- Build issue creation payload ---- #
            payload = {
                "iid":          int(issue["iid"]),
                "title":        title,
                "description":  description,
                "labels":       ",".join(issue.get("labels") or []),
                "confidential": bool(issue.get("confidential", False)),
            }
            if issue.get("created_at"):
                payload["created_at"] = issue["created_at"]
            if milestone_id:
                payload["milestone_id"] = milestone_id
            if issue.get("due_date"):
                payload["due_date"] = issue["due_date"]

            created  = await self._create_issue(session, token, payload,
                                                assignee_ids)
            dest_iid = created["iid"]

            # ---- [13] Read-back fidelity guard ---- #
            # Runs before the checkpoint is journaled, so an issue that failed
            # the check is never recorded as migrated. Raises past the handler
            # below and aborts the whole run.
            await self._assert_created_fidelity(
                session, iid, payload, created, author, fidelity
            )

            if str(dest_iid) != iid:
                log.warning(
                    f"  ⚠  #{iid} was created as #{dest_iid}: the destination "
                    f"ignored the requested iid. Verify the writing token holds "
                    f"Owner on the destination project."
                )

            # ---- Checkpoint the created issue ---- #
            # notes_done stays False until migrate_issue_content() has posted
            # the thread and restored updated_at, so an interrupted run resumes
            # at the note level rather than re-creating the issue.
            record = {
                "source_iid":  iid,
                "dest_iid":    dest_iid,
                "title":       title,
                "author":      author,
                "fidelity":    fidelity,
                "notes":       0,
                "notes_done":  False,
                "migrated_at": utc_now_iso(),
            }
            self.checkpoint[iid] = record
            self._journal_checkpoint(record)

            # ---- Update statistics ---- #
            self.stats["migrated"] += 1
            self._count_fidelity(fidelity)

            # ---- Add row to CSV/JSON report ---- #
            self._add_report_row(
                issue, dest_iid=dest_iid, notes=0, author=author,
                fidelity=fidelity, status="migrated"
            )

            log.info(
                f"  ✓  #{iid} → #{dest_iid}  [{fidelity}]  {title}"
            )

        except FidelityGuardError:
            # Not a per-issue failure: retrying cannot fix it, every later
            # write would be wrong the same way, and IIDs and authorship are
            # unrecoverable once consumed. Propagate to abort the run.
            raise

        except Exception as exc:
            # Log failure and continue — do NOT re-raise so other issues proceed
            log.error(f"  ✗  Failed to migrate issue #{iid}: {exc}")
            self.stats["failed"] += 1
            self.failures.append({
                "source_iid": iid,
                "title":      title,
                "author":     author,
                "error":      str(exc),
                "failed_at":  utc_now_iso(),
            })
            self.report_rows.append(
                self._report_row(issue, dest_iid="", notes=0, author=author,
                                 fidelity=fidelity, status="failed",
                                 error=str(exc))
            )

    async def migrate_issue_content(
        self,
        session: aiohttp.ClientSession,
        issue: dict
    ) -> None:
        """
        Post an issue's notes, then restore its state and updated_at.

        Split out from migrate_issue() because issue *creation* has to be
        strictly sequential — GitLab assigns preserved IIDs monotonically — but
        note threads are independent between issues and are the bulk of the
        work, so they run concurrently under the shared semaphore.

        The number of notes already on the destination is measured first, so a
        resumed run appends only the tail it never posted instead of
        duplicating the thread.

        Args:
            session: Active aiohttp session.
            issue:   Issue dict from the source GitLab API.
        """
        iid    = str(issue["iid"])
        record = self.checkpoint.get(iid)

        if not record or record.get("notes_done"):
            return

        dest_iid        = record["dest_iid"]
        author          = record.get("author") or ""
        token, fidelity = self.token_for_author(author)

        try:
            present = await self._dest_note_count(session, dest_iid)
            added   = await self.migrate_notes(
                session, issue["iid"], dest_iid, skip_existing=present
            )

            # Must run after the notes: every note write bumps updated_at.
            await self._finalize_issue(session, token, dest_iid, issue)

            record["notes"]        = present + added
            record["notes_done"]  = True
            record["completed_at"] = utc_now_iso()
            self._journal_checkpoint(record)

            self.stats["notes_migrated"] += added

            row = self._row_index.get(iid)
            if row is not None:
                row["notes"] = present + added

            log.debug(
                f"  ✓  #{iid} notes complete: {added} added "
                f"({present} already present)."
            )

        except Exception as exc:
            log.error(f"  ✗  Failed to migrate notes for issue #{iid}: {exc}")
            self.stats["failed"] += 1
            self.failures.append({
                "source_iid": iid,
                "title":      record.get("title", ""),
                "author":     author,
                "error":      f"notes/finalize: {exc}",
                "failed_at":  utc_now_iso(),
            })
            row = self._row_index.get(iid)
            if row is not None:
                row["status"] = "notes_failed"
                row["error"]  = str(exc)

    async def _create_issue(
        self,
        session: aiohttp.ClientSession,
        token: str,
        payload: dict,
        assignee_ids: list
    ) -> dict:
        """
        POST a new issue, degrading gracefully from plural to singular assignee.

        `assignee_ids` is a Premium/Ultimate field; GitLab Free accepts only
        `assignee_id`. The first rejection flips a flag so the remaining
        thousands of issues skip the doomed plural attempt.

        Args:
            session:      Active aiohttp session.
            token:        PAT that will author the issue.
            payload:      Issue creation payload (without assignee fields).
            assignee_ids: Destination user IDs, in source order.

        Returns:
            The created issue dict.
        """
        url = f"{self.dest_url}/api/v4/projects/{self.dest_project}/issues"

        if not assignee_ids:
            return await self._post(session, url, token, payload)

        if self.supports_multiple_assignees:
            try:
                return await self._post(
                    session, url, token,
                    {**payload, "assignee_ids": assignee_ids}
                )
            except aiohttp.ClientResponseError as exc:
                if exc.status not in (400, 403, 422):
                    raise
                self.supports_multiple_assignees = False
                log.warning(
                    "Destination rejected assignee_ids (Premium/Ultimate only). "
                    "Falling back to a single assignee_id for all issues; "
                    f"({exc.message})"
                )

        if len(assignee_ids) > 1:
            log.warning(
                f"Only the first of {len(assignee_ids)} assignees can be set "
                f"on GitLab Free."
            )
        return await self._post(
            session, url, token, {**payload, "assignee_id": assignee_ids[0]}
        )
    async def _finalize_issue(
        self,
        session: aiohttp.ClientSession,
        token: str,
        dest_iid,
        issue: dict
    ) -> None:
        """
        Apply the closing state and restore the original updated_at.

        This is the last write against an issue on purpose: creating notes
        bumps updated_at, so restoring it any earlier would be overwritten.

        `closed_at` has no settable parameter in the GitLab issues API, so a
        closed issue records the migration time there. The source value is kept
        in the report for audit.

        Args:
            session:   Active aiohttp session.
            token:     PAT authoring the update (must hold Owner for updated_at).
            dest_iid:  Destination issue IID.
            issue:     Source issue dict.
        """
        payload = {}
        if issue.get("state") == "closed":
            payload["state_event"] = "close"
        if issue.get("updated_at"):
            payload["updated_at"] = issue["updated_at"]

        if not payload:
            return

        await self._put(
            session,
            f"{self.dest_url}/api/v4/projects/{self.dest_project}"
            f"/issues/{dest_iid}",
            token,
            payload
        )

    async def _existing_dest_issue(
        self,
        session: aiohttp.ClientSession,
        iid: str
    ) -> Optional[dict]:
        """
        Return the destination issue already occupying an IID, if any.

        Args:
            session: Active aiohttp session.
            iid:     Target IID (which is preserved from the source).

        Returns:
            The destination issue dict, or None if the IID is free.
        """
        try:
            return await self._get(
                session,
                f"{self.dest_url}/api/v4/projects/{self.dest_project}"
                f"/issues/{iid}",
                self.dest_token
            )
        except aiohttp.ClientResponseError as exc:
            if exc.status == 404:
                return None
            raise

    async def _adopt_existing_issue(
        self,
        session: aiohttp.ClientSession,
        issue: dict,
        existing: dict,
        author: str,
        fidelity: str
    ) -> None:
        """
        Reconcile an issue that already exists on the destination.

        This happens when a previous run created the issue but was killed
        before its checkpoint record was durable. Rather than fail with a 409
        or duplicate the content, the existing issue is adopted; the content
        pass then appends only the notes it is still missing.

        Args:
            session:  Active aiohttp session.
            issue:    Source issue dict.
            existing: Destination issue dict already at that IID.
            author:   Source author username.
            fidelity: Fidelity label resolved for this issue's author.
        """
        iid      = str(issue["iid"])
        dest_iid = existing["iid"]

        log.info(
            f"  ↺  #{iid} already exists on the destination as #{dest_iid}; "
            f"adopting it instead of creating a duplicate."
        )

        record = {
            "source_iid":  iid,
            "dest_iid":    dest_iid,
            "title":       issue["title"],
            "author":      author,
            "fidelity":    fidelity,
            "notes":       0,
            "notes_done":  False,
            "adopted":     True,
            "migrated_at": utc_now_iso(),
        }
        self.checkpoint[iid] = record
        self._journal_checkpoint(record)

        self.stats["skipped"] += 1
        self._add_report_row(
            issue, dest_iid=dest_iid, notes=0, author=author,
            fidelity=fidelity, status="already_present"
        )
