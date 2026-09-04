"""
migration.boards
----------------
Board and board-list (Kanban column) migration.

Lists reference labels by numeric ID, so this runs after the label pass.
gitlab.com Free permits exactly one board per project: the first source board
is mapped onto the destination's existing board and any further board is
reported as skipped, not failed — a tier limit is not a migration error.
"""

import aiohttp

from .logging_setup import log


class BoardsMixin:
    """
    Board migration for GitLabMigration.

    Expects the host object to provide `cfg`, both URLs, both tokens, both
    project IDs, and `stats`.
    """

    # ---------------------------------------------------------------- #
    # [11] Board Migration
    # ---------------------------------------------------------------- #

    async def migrate_boards(self, session: aiohttp.ClientSession) -> None:
        """
        Recreate issue boards and their label columns.

        Lists reference labels by numeric ID, which is instance-specific, so
        each source list's label name is resolved against the destination label
        map built after label migration.

        gitlab.com Free permits exactly one board per project. The first source
        board is therefore mapped onto the destination's existing board (Free
        projects always have one) and any additional source board is reported as
        skipped rather than failed — a tier limit is not a migration error.

        Args:
            session: Active aiohttp session.
        """
        log.info("─── Migrating boards ───")

        src_boards_url  = (
            f"{self.source_url}/api/v4/projects/{self.source_project}/boards"
        )
        dest_boards_url = (
            f"{self.dest_url}/api/v4/projects/{self.dest_project}/boards"
        )

        try:
            src_boards = await self._get_all_pages(
                session, src_boards_url, self.source_token
            )
        except Exception as exc:
            log.error(f"Failed to fetch source boards: {exc}")
            return

        if not src_boards:
            log.info("Source project has no boards.")
            return

        if self.cfg["DRY_RUN"]:
            for board in src_boards:
                names = [
                    (lst.get("label") or {}).get("name", "?")
                    for lst in sorted(
                        board.get("lists") or [],
                        key=lambda l: l.get("position") or 0
                    )
                ]
                log.info(
                    f"  [DRY RUN] Would ensure board '{board.get('name')}' "
                    f"with lists: {', '.join(names) or 'none'}"
                )
            return

        try:
            dest_boards = await self._get_all_pages(
                session, dest_boards_url, self.dest_token
            )
            label_ids = await self.dest_label_ids(session)
        except Exception as exc:
            log.error(f"Failed to read destination boards or labels: {exc}")
            return

        dest_by_name = {b.get("name"): b for b in dest_boards}
        boards_created = 0
        lists_created  = 0

        for index, src_board in enumerate(src_boards):
            name       = src_board.get("name") or "Development"
            dest_board = dest_by_name.get(name)

            if dest_board is None and index == 0 and dest_boards:
                # Free tier: adopt the single pre-existing board instead of
                # trying to create a second one.
                dest_board = dest_boards[0]
                log.info(
                    f"  Mapping source board '{name}' onto the existing "
                    f"destination board '{dest_board.get('name')}'."
                )

            if dest_board is None:
                try:
                    dest_board = await self._post(
                        session, dest_boards_url, self.dest_token,
                        {"name": name}
                    )
                    boards_created += 1
                    log.info(f"  Created board: {name}")
                except aiohttp.ClientResponseError as exc:
                    log.warning(
                        f"  Skipping board '{name}': the destination refused "
                        f"another board (gitlab.com Free allows one board per "
                        f"project). ({exc.message})"
                    )
                    continue
                except Exception as exc:
                    log.warning(f"  Skipping board '{name}': {exc}")
                    continue

            board_id       = dest_board["id"]
            existing_lists = {
                (lst.get("label") or {}).get("name")
                for lst in (dest_board.get("lists") or [])
            }

            src_lists = sorted(
                src_board.get("lists") or [],
                key=lambda lst: lst.get("position") or 0
            )

            for src_list in src_lists:
                label_name = (src_list.get("label") or {}).get("name")
                if not label_name:
                    # Assignee, milestone, and iteration lists are Premium
                    # features with no Free-tier equivalent.
                    log.debug("  Skipping non-label board list.")
                    continue

                if label_name in existing_lists:
                    log.debug(f"  Board list already present: {label_name}")
                    continue

                label_id = label_ids.get(label_name)
                if label_id is None:
                    log.warning(
                        f"  Cannot add board list '{label_name}': no such "
                        f"label on the destination."
                    )
                    continue

                try:
                    await self._post(
                        session,
                        f"{dest_boards_url}/{board_id}/lists",
                        self.dest_token,
                        {"label_id": label_id}
                    )
                    lists_created += 1
                    log.debug(f"  Added board list: {label_name}")
                except Exception as exc:
                    log.warning(
                        f"  Failed to add board list '{label_name}': {exc}"
                    )

        self.stats["boards_created"]      = boards_created
        self.stats["board_lists_created"] = lists_created
        log.info(
            f"Boards complete: {boards_created} board(s) created, "
            f"{lists_created} list(s) added."
        )
