"""
migration.milestones
--------------------
Milestone migration and the source-IID -> destination-ID map issues use.

Runs before the issue pass, since an issue payload carries `milestone_id` and
that ID only exists once the milestone does.
"""

import aiohttp

from .logging_setup import log


class MilestonesMixin:
    """
    Milestone migration for GitLabMigration.

    Expects the host object to provide `cfg`, both URLs, both tokens, both
    project IDs, `milestone_map`, and `stats`.
    """

    # ---------------------------------------------------------------- #
    # [3] Milestone Migration
    # ---------------------------------------------------------------- #

    async def migrate_milestones(self, session: aiohttp.ClientSession) -> None:
        """
        Migrate all project milestones from source to destination.

        Builds self.milestone_map ({src_iid: dest_id}) which is consumed
        by migrate_issue() to assign the correct milestone on each issue.

        Milestones already present in the destination (matched by title)
        are not re-created — their IDs are recorded in the map.

        Args:
            session: Active aiohttp session.
        """
        log.info("─── Migrating milestones ───")

        src_ms_url  = (
            f"{self.source_url}/api/v4/projects/{self.source_project}/milestones"
        )
        dest_ms_url = (
            f"{self.dest_url}/api/v4/projects/{self.dest_project}/milestones"
        )

        try:
            src_milestones  = await self._get_all_pages(
                session, f"{src_ms_url}?state=all", self.source_token
            )
            dest_milestones = await self._get_all_pages(
                session, f"{dest_ms_url}?state=all", self.dest_token
            )
        except Exception as exc:
            log.error(f"Failed to fetch milestones: {exc}")
            return

        # Build title → dest_id lookup for existing destination milestones
        dest_by_title = {m["title"]: m["id"] for m in dest_milestones}
        created = 0

        for ms in src_milestones:
            src_iid = str(ms["iid"])
            title   = ms["title"]

            if title in dest_by_title:
                # Record mapping without creating a duplicate
                self.milestone_map[src_iid] = dest_by_title[title]
                log.debug(f"Milestone already exists (mapped): {title}")
                continue

            if self.cfg["DRY_RUN"]:
                log.info(f"  [DRY RUN] Would create milestone: {title}")
                created += 1
                continue

            # Build payload, excluding None fields (GitLab rejects null dates)
            payload = {
                k: v for k, v in {
                    "title":       title,
                    "description": ms.get("description", ""),
                    "due_date":    ms.get("due_date"),
                    "start_date":  ms.get("start_date"),
                }.items() if v is not None
            }

            try:
                dest_ms = await self._post(
                    session, dest_ms_url, self.dest_token, payload
                )
                self.milestone_map[src_iid] = dest_ms["id"]
                created += 1
                log.debug(f"  Created milestone: {title}")

                # A closed source milestone stays closed on the destination.
                if ms.get("state") == "closed":
                    await self._put(
                        session,
                        f"{dest_ms_url}/{dest_ms['id']}",
                        self.dest_token,
                        {"state_event": "close"}
                    )
            except Exception as exc:
                log.warning(f"  Failed to create milestone '{title}': {exc}")

        self.stats["milestones_created"] = created
        log.info(
            f"Milestones complete: {created} created, "
            f"{len(src_milestones) - created} already existed."
        )
