"""
migration.links
---------------
Issue-to-issue link migration.

Runs as its own pass after every issue exists, because a link needs both
endpoints. GitLab reports a link from both sides, so pairs are deduplicated;
cross-project links are out of scope and reported as skipped.
"""

import aiohttp

from .logging_setup import log


class LinksMixin:
    """
    Issue link migration for GitLabMigration.

    Expects the host object to provide `cfg`, `checkpoint`, both URLs, both
    tokens, both project IDs, and `stats`.
    """

    # ---------------------------------------------------------------- #
    # [10] Issue Link Migration
    # ---------------------------------------------------------------- #

    async def migrate_links(
        self,
        session: aiohttp.ClientSession,
        issues: list
    ) -> None:
        """
        Recreate issue-to-issue relationships once every issue exists.

        Links can only be created after both endpoints are present, so this
        runs as a separate pass after issue creation.

        Behaviour:
          - Bidirectional pairs are deduplicated: GitLab reports a link from
            both sides, and re-posting the mirror image creates nothing but an
            error.
          - Cross-project links are skipped and counted; migrating them would
            mean pulling in projects outside this migration's scope.
          - Links whose target was not migrated (for example under --limit) are
            skipped.
          - `blocks` / `is_blocked_by` are Premium link types; a rejection
            degrades that link to `relates_to` rather than losing it.

        Args:
            session: Active aiohttp session.
            issues:  Source issue dicts considered by this run.
        """
        log.info("─── Migrating issue links ───")

        iid_map = {
            str(src): record["dest_iid"]
            for src, record in self.checkpoint.items()
        }
        created = 0
        skipped = 0
        seen: set = set()

        for issue in issues:
            src_iid = str(issue["iid"])

            if self.cfg["DRY_RUN"]:
                dest_iid = src_iid
            else:
                dest_iid = iid_map.get(src_iid)
                if dest_iid is None:
                    continue

            try:
                links = await self._get_all_pages(
                    session,
                    f"{self.source_url}/api/v4/projects/{self.source_project}"
                    f"/issues/{src_iid}/links",
                    self.source_token
                )
            except Exception as exc:
                log.warning(f"  Could not read links for #{src_iid}: {exc}")
                continue

            if not links:
                continue

            existing_targets: set = set()
            if not self.cfg["DRY_RUN"]:
                try:
                    dest_links = await self._get_all_pages(
                        session,
                        f"{self.dest_url}/api/v4/projects/{self.dest_project}"
                        f"/issues/{dest_iid}/links",
                        self.dest_token
                    )
                    existing_targets = {str(link["iid"]) for link in dest_links}
                except Exception as exc:
                    log.debug(
                        f"  Could not read destination links for "
                        f"#{dest_iid}: {exc}"
                    )

            for link in links:
                target_iid = str(link.get("iid"))
                link_type  = link.get("link_type", "relates_to")

                if link.get("project_id") != self.source_project:
                    log.debug(
                        f"  Skipping cross-project link "
                        f"#{src_iid} → project {link.get('project_id')}"
                    )
                    skipped += 1
                    continue

                pair = frozenset((src_iid, target_iid))
                if pair in seen:
                    continue
                seen.add(pair)

                dest_target = (
                    target_iid if self.cfg["DRY_RUN"]
                    else iid_map.get(target_iid)
                )
                if dest_target is None:
                    log.debug(
                        f"  Skipping link #{src_iid} → #{target_iid}: "
                        f"target not migrated in this run."
                    )
                    skipped += 1
                    continue

                if str(dest_target) in existing_targets:
                    log.debug(
                        f"  Link already present: #{dest_iid} → #{dest_target}"
                    )
                    continue

                if self.cfg["DRY_RUN"]:
                    log.info(
                        f"  [DRY RUN] Would link #{src_iid} → #{target_iid} "
                        f"({link_type})"
                    )
                    created += 1
                    continue

                link_url = (
                    f"{self.dest_url}/api/v4/projects/{self.dest_project}"
                    f"/issues/{dest_iid}/links"
                )
                payload = {
                    "target_project_id": self.dest_project,
                    "target_issue_iid":  dest_target,
                    "link_type":         link_type,
                }
                try:
                    await self._post(session, link_url, self.dest_token, payload)
                    created += 1
                except aiohttp.ClientResponseError as exc:
                    if link_type != "relates_to" and exc.status in (400, 403, 422):
                        log.debug(
                            f"  link_type '{link_type}' rejected "
                            f"(Premium feature); retrying as relates_to."
                        )
                        try:
                            await self._post(
                                session, link_url, self.dest_token,
                                {**payload, "link_type": "relates_to"}
                            )
                            created += 1
                            continue
                        except Exception as retry_exc:
                            exc = retry_exc  # fall through to the warning
                    log.warning(
                        f"  Failed to link #{dest_iid} → #{dest_target}: {exc}"
                    )
                    skipped += 1
                except Exception as exc:
                    log.warning(
                        f"  Failed to link #{dest_iid} → #{dest_target}: {exc}"
                    )
                    skipped += 1

        self.stats["links_created"] = created
        self.stats["links_skipped"] = skipped
        log.info(f"Links complete: {created} created, {skipped} skipped.")
