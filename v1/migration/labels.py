"""
migration.labels
----------------
Label migration, and the destination label name -> ID map board lists need.

Labels are created before issues because issues reference them by name, and
board lists are created after because they reference them by numeric ID, which
differs between instances.
"""

import aiohttp

from .logging_setup import log


class LabelsMixin:
    """
    Label migration for GitLabMigration.

    Expects the host object to provide `cfg`, both URLs, both tokens, both
    project IDs, and `stats`.
    """

    # ---------------------------------------------------------------- #
    # Label Migration
    # ---------------------------------------------------------------- #

    async def migrate_labels(self, session: aiohttp.ClientSession) -> None:
        """
        Migrate all project labels from source to destination.

        Labels already present in the destination (matched by name) are
        skipped to avoid duplicates. Color and description are preserved.

        Args:
            session: Active aiohttp session.
        """
        log.info("─── Migrating labels ───")

        src_labels_url  = (
            f"{self.source_url}/api/v4/projects/{self.source_project}/labels"
        )
        dest_labels_url = (
            f"{self.dest_url}/api/v4/projects/{self.dest_project}/labels"
        )

        try:
            src_labels  = await self._get_all_pages(
                session, src_labels_url, self.source_token
            )
            dest_labels = await self._get_all_pages(
                session, dest_labels_url, self.dest_token
            )
        except Exception as exc:
            log.error(f"Failed to fetch labels: {exc}")
            return

        # Build a set of existing destination label names for O(1) lookup
        existing_names = {label["name"] for label in dest_labels}
        created = 0

        for label in src_labels:
            name = label["name"]

            if name in existing_names:
                log.debug(f"Label already exists (skipping): {name}")
                continue

            if self.cfg["DRY_RUN"]:
                log.info(f"  [DRY RUN] Would create label: {name}")
                created += 1
                continue

            try:
                await self._post(
                    session,
                    dest_labels_url,
                    self.dest_token,
                    {
                        "name":        name,
                        "color":       label.get("color", "#428BCA"),
                        "description": label.get("description", ""),
                    }
                )
                created += 1
                log.debug(f"  Created label: {name}")
            except Exception as exc:
                log.warning(f"  Failed to create label '{name}': {exc}")

        self.stats["labels_created"] = created
        log.info(
            f"Labels complete: {created} created, "
            f"{len(src_labels) - created} already existed."
        )

    async def dest_label_ids(self, session: aiohttp.ClientSession) -> dict:
        """
        Build a destination label name → label ID map.

        Board lists reference labels by numeric ID, which differs between
        instances, so the ID has to be looked up by name after labels have
        been migrated.

        Args:
            session: Active aiohttp session.

        Returns:
            dict mapping label name to destination label ID.
        """
        labels = await self._get_all_pages(
            session,
            f"{self.dest_url}/api/v4/projects/{self.dest_project}/labels",
            self.dest_token
        )
        return {label["name"]: label["id"] for label in labels}
