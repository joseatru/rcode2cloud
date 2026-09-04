"""
migration.fidelity
------------------
The read-back fidelity guard.

GitLab honours `iid` and `created_at` on issue creation only for an
administrator or a project Owner, and returns no error when it ignores them: a
rejected `iid` comes back as HTTP 201 carrying a *different* `iid`. Silent
rejection is therefore indistinguishable from success at the HTTP layer, which
makes a permissions problem look like a clean run until someone opens the
destination project hours later.

Everything in this module exists to turn that into a loud failure at the first
issue. The guard runs on the create path only, which is what keeps a legitimate
IID adoption (a resume) distinct from a newly created issue landing on an
unexpected IID (a hard failure).
"""

import aiohttp

from typing import Optional

from .logging_setup import log
from .timeutils import parse_gitlab_timestamp


class FidelityGuardError(RuntimeError):
    """
    Raised when a newly created issue does not match what was requested.

    GitLab answers `POST .../issues` with 201 whether or not it honoured the
    `iid` and `created_at` it was sent: a rejected `iid` comes back as a
    *different* `iid` inside that same 201 body. Silent rejection is therefore
    indistinguishable from success at the HTTP layer, and a --full-run whose
    writing PAT is not an Owner can exit 0 having renumbered every issue and
    stamped every one of them with today's date.

    This is deliberately NOT recorded as a per-issue failure. Retrying cannot
    fix it, every subsequent write would be wrong in exactly the same way, and
    neither IIDs nor authorship can be corrected after creation — so it is
    raised past migrate_issue()'s per-issue handler and turned into exit code 1
    by main(), at the first issue rather than three hours in.
    """


# GitLab may echo a timestamp back at a different precision or UTC offset than
# it was sent, so created_at is compared as a parsed instant within a small
# tolerance rather than as a string.
CREATED_AT_TOLERANCE_SECONDS = 2.0


class FidelityGuardMixin:
    """
    Read-back verification of created issues for GitLabMigration.

    Expects the host object to provide `cfg`, `mappings`, `dest_url`,
    `dest_project`, `dest_token`, `fidelity_guard_enabled`, and
    `_fidelity_confirmed_by_refetch`.
    """

    # ---------------------------------------------------------------- #
    # [13] Read-Back Fidelity Guard
    # ---------------------------------------------------------------- #

    def _expected_dest_author(
        self,
        source_username: str,
        fidelity: str
    ) -> Optional[str]:
        """
        Return the destination username the created issue must be authored by.

        Only a fidelity=full write has an enforceable expectation: the mapped
        user's own PAT made the call, so the destination author has to be that
        user's dest_username. An admin_attributed write is the supported
        fallback for a source author absent from mappings.yml, and its author
        is legitimately the DEST_GITLAB_TOKEN identity — whose username is not
        known here without an extra API call, and is not a failure either way.

        Args:
            source_username: Source author username.
            fidelity:        "full" or "admin_attributed".

        Returns:
            The expected destination username, or None when there is nothing
            to enforce.
        """
        if fidelity != "full":
            return None

        mapping = self.mappings.get("users", {}).get(source_username) or {}
        return mapping.get("dest_username") or None

    def _fidelity_mismatches(
        self,
        requested: dict,
        observed: dict,
        expected_author: Optional[str]
    ) -> list:
        """
        Compare an observed issue against the payload that asked for it.

        Timestamps are compared as parsed instants within
        CREATED_AT_TOLERANCE_SECONDS rather than as strings, because GitLab may
        echo a value back at a different precision or UTC offset than it was
        sent — "2020-01-01T00:00:00Z" and "2020-01-01T00:00:00.000+00:00" are
        the same instant and must not read as a mismatch.

        An unparseable returned timestamp is a mismatch: the guard cannot
        confirm preservation, and "cannot confirm" must not pass silently.

        Args:
            requested:       The issue creation payload (iid, created_at, ...).
            observed:        An issue dict from GitLab — either the create
                             response body or an explicit re-fetch.
            expected_author: Destination username that must own the issue, or
                             None to skip the author comparison.

        Returns:
            List of (field, requested, returned) tuples. Empty means a match.
        """
        mismatches: list = []

        # ---- iid: exact match ---- #
        want_iid = requested.get("iid")
        got_iid  = observed.get("iid")
        if want_iid is not None and str(got_iid) != str(want_iid):
            mismatches.append(("iid", want_iid, got_iid))

        # ---- created_at: equal instants within tolerance ---- #
        want_created = requested.get("created_at")
        if want_created:
            want_dt = parse_gitlab_timestamp(want_created)
            got_dt  = parse_gitlab_timestamp(observed.get("created_at"))
            if want_dt is None:
                # Nothing sendable was requested; nothing to enforce.
                pass
            elif got_dt is None:
                mismatches.append((
                    "created_at", want_created,
                    observed.get("created_at") or "(absent)"
                ))
            elif abs((got_dt - want_dt).total_seconds()) > \
                    CREATED_AT_TOLERANCE_SECONDS:
                mismatches.append((
                    "created_at", want_created, observed.get("created_at")
                ))

        # ---- author.username: only enforced for fidelity=full ---- #
        if expected_author:
            got_author = (observed.get("author") or {}).get("username")
            if got_author != expected_author:
                mismatches.append((
                    "author.username", expected_author, got_author or "(absent)"
                ))

        return mismatches

    def _fidelity_abort_message(
        self,
        source_iid: str,
        fidelity: str,
        source_author: str,
        origin: str,
        mismatches: list
    ) -> str:
        """
        Build the operator-facing diagnostic for a fidelity guard abort.

        Names each offending field with the value requested and the value
        returned, states where the discrepancy was observed, and leads with the
        likely cause, because the overwhelmingly common one is a single missing
        role grant.

        Args:
            source_iid:    Source issue IID that failed.
            fidelity:      Fidelity label resolved for the write.
            source_author: Source author username.
            origin:        "the create response" or "an explicit re-fetch".
            mismatches:    Tuples from _fidelity_mismatches().

        Returns:
            A multi-line message ready to hand to log.error().
        """
        writer = (
            f"{source_author}'s own PAT" if fidelity == "full"
            else "DEST_GITLAB_TOKEN"
        )
        lines = [
            f"Read-back fidelity check FAILED on source issue "
            f"#{source_iid} (observed in {origin}).",
            "",
        ]
        for field, want, got in mismatches:
            lines.append(f"    {field}:")
            lines.append(f"      requested: {want}")
            lines.append(f"      returned:  {got}")
        lines += [
            "",
            f"  Most likely cause: the PAT that wrote this issue ({writer}) "
            f"does not hold",
            f"  Owner on destination project {self.dest_project}. GitLab "
            f"accepts iid and created_at",
            "  from an administrator or a project Owner only, and silently "
            "discards them for",
            "  everyone else while still answering 201 — so a rejected write "
            "is indistinguishable",
            "  from an accepted one without this check.",
            "",
            "  The run is aborting now rather than renumbering and re-dating "
            "the remaining",
            "  issues. Authorship and IIDs cannot be corrected after "
            "creation.",
            "",
            "  To fix: grant Owner to every mapped user on the destination "
            "project (RUNBOOK",
            "  Pre-Flight §3), re-run --sanity-check until it passes, delete "
            f"the issue(s) already",
            "  written by this run, clear both checkpoint files, and start "
            "again.",
        ]
        return "\n".join(lines)

    async def _assert_created_fidelity(
        self,
        session: aiohttp.ClientSession,
        source_iid: str,
        requested: dict,
        created: dict,
        source_author: str,
        fidelity: str
    ) -> None:
        """
        Verify a freshly created issue really carries what was requested.

        Two stages, cheapest first:

          1. The create response body already contains the resulting `iid`,
             `created_at`, and `author`, so the common case costs zero extra
             API calls.
          2. For the first successfully created issue of the run only, the
             issue is then re-fetched with GET /projects/:id/issues/:iid to
             catch a create response that echoes the request optimistically.
             Once that confirmation passes, later issues rely on stage 1
             alone.

        This runs only for issues this process *created*. An issue adopted at
        its target IID never reaches here — see migrate_issue(), which returns
        straight after _adopt_existing_issue(). Adoption is a resume path where
        the IID was already correct; a newly created issue landing on an
        unexpected IID is a hard failure. Keeping the guard on the create path
        only is what separates the two.

        Args:
            session:       Active aiohttp session.
            source_iid:    Source issue IID, for the diagnostic.
            requested:     The payload sent to POST .../issues.
            created:       The create response body.
            source_author: Source author username.
            fidelity:      "full" or "admin_attributed".

        Raises:
            FidelityGuardError: On any mismatch, to abort the entire run.
        """
        if not self.fidelity_guard_enabled or self.cfg.get("DRY_RUN"):
            return

        expected_author = self._expected_dest_author(source_author, fidelity)

        mismatches = self._fidelity_mismatches(requested, created,
                                               expected_author)
        if mismatches:
            raise FidelityGuardError(self._fidelity_abort_message(
                source_iid, fidelity, source_author,
                "the create response", mismatches
            ))

        if self._fidelity_confirmed_by_refetch:
            return

        # First create of the run: confirm the response was not optimistic.
        refetched = await self._get(
            session,
            f"{self.dest_url}/api/v4/projects/{self.dest_project}"
            f"/issues/{created['iid']}",
            self.dest_token
        )
        mismatches = self._fidelity_mismatches(requested, refetched,
                                               expected_author)
        if mismatches:
            raise FidelityGuardError(self._fidelity_abort_message(
                source_iid, fidelity, source_author,
                "an explicit re-fetch", mismatches
            ))

        self._fidelity_confirmed_by_refetch = True
        log.info(
            f"  ✓  Fidelity guard confirmed on re-fetch of #{created['iid']}: "
            f"iid, created_at, and author were honoured. Remaining issues are "
            f"checked from the create response."
        )
