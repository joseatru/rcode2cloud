"""
migration.bump
--------------
Destination IID counter priming.

Creating issues with an explicit `iid` does not advance GitLab's `internal_ids`
counter, so without this the first issue a user files after the migration is
offered IID 1 and fails with "Duplicated issue". Creating and deleting one
throwaway issue at max_iid + 1 forces the counter forward.
"""

import aiohttp

from .logging_setup import log


class BumpMixin:
    """
    IID counter bump for GitLabMigration.

    Expects the host object to provide `cfg`, `dest_url`, `dest_token`, and
    `dest_project`.
    """

    # ---------------------------------------------------------------- #
    # [12] IID Counter Bump
    # ---------------------------------------------------------------- #

    async def bump_iid_counter(self, session: aiohttp.ClientSession) -> None:
        """
        Initialise the destination project's internal IID counter.

        Creating issues with an explicit `iid` does not advance GitLab's
        `internal_ids` counter, so the first issue a user files after the
        migration is offered IID 1 and fails with "Duplicated issue". Creating
        one throwaway issue at max_iid + 1 forces the counter forward; deleting
        it leaves the range clean and the counter primed.

        Args:
            session: Active aiohttp session.
        """
        log.info("─── Bumping destination IID counter ───")

        issues_url = (
            f"{self.dest_url}/api/v4/projects/{self.dest_project}/issues"
        )

        try:
            dest_issues = await self._get_all_pages(
                session, f"{issues_url}?state=all", self.dest_token
            )
        except Exception as exc:
            log.warning(f"Could not read destination issues for bump: {exc}")
            return

        if not dest_issues:
            log.info("Destination has no issues — nothing to bump.")
            return

        target_iid = max(int(issue["iid"]) for issue in dest_issues) + 1

        if self.cfg["DRY_RUN"]:
            log.info(
                f"  [DRY RUN] Would create and delete a throwaway issue at "
                f"#{target_iid} to prime the IID counter."
            )
            return

        try:
            created = await self._post(
                session, issues_url, self.dest_token,
                {
                    "iid":   target_iid,
                    "title": "[migration] IID counter bump — safe to ignore",
                    "description": (
                        "Temporary issue created by the migration tool to "
                        "initialise this project's internal IID counter. "
                        "It is deleted immediately."
                    ),
                }
            )
        except Exception as exc:
            log.warning(
                f"Could not create the bump issue at #{target_iid}: {exc}. "
                f"The first issue filed after migration may return 409."
            )
            return

        try:
            await self._delete(session, f"{issues_url}/{created['iid']}",
                               self.dest_token)
            log.info(
                f"IID counter primed: next issue will be "
                f"#{int(created['iid']) + 1}."
            )
        except Exception as exc:
            log.warning(
                f"Bump issue #{created['iid']} was created but could not be "
                f"deleted: {exc}. Delete it manually."
            )
