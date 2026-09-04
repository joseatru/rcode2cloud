"""
migration.reporting
-------------------
Per-issue report rows and the JSON/CSV/failures output.

REPORT_FIELDNAMES is the documented 13-column contract shared by
migration_report.csv and the `issues` array in migration_report.json; the two
must not drift. `closed_at` is the *source* close timestamp and this is the
only place it survives, because the issues API has no settable `closed_at`.
"""

import csv

from .logging_setup import log
from .paths import FAILURES_JSON, REPORT_CSV, REPORT_JSON
from .timeutils import utc_now_iso


class ReportingMixin:
    """
    Report row construction and output for GitLabMigration.

    Expects the host object to provide `stats`, `failures`, `report_rows`, and
    `_row_index`.
    """

    def _count_fidelity(self, fidelity: str) -> None:
        """Increment the per-fidelity counter for a migrated issue."""
        key = "fidelity_full" if fidelity == "full" else "fidelity_attributed"
        self.stats[key] += 1

    def _add_report_row(
        self,
        issue: dict,
        dest_iid,
        notes: int,
        author: str,
        fidelity: str,
        status: str,
        error: str = ""
    ) -> dict:
        """
        Append a report row and index it by source IID.

        The index exists so the concurrent content pass can update the note
        count on a row the sequential creation pass already wrote, instead of
        emitting a second row for the same issue.

        Returns:
            The row dict that was appended.
        """
        row = self._report_row(issue, dest_iid=dest_iid, notes=notes,
                               author=author, fidelity=fidelity,
                               status=status, error=error)
        self.report_rows.append(row)
        self._row_index[str(issue["iid"])] = row
        return row

    def _report_row(
        self,
        issue: dict,
        dest_iid,
        notes: int,
        author: str,
        fidelity: str,
        status: str,
        error: str = ""
    ) -> dict:
        """
        Build one report row for an issue in any terminal state.

        Args:
            issue:    Source issue dict.
            dest_iid: Destination IID, or "" when nothing was created.
            notes:    Number of notes present on the destination issue.
            author:   Source author username.
            fidelity: "full" or "admin_attributed".
            status:   migrated | already_present | would_migrate | failed.
            error:    Error text for failed rows.

        Returns:
            dict matching the CSV field order in generate_reports().
        """
        return {
            "source_iid":     str(issue["iid"]),
            "dest_iid":       dest_iid,
            "title":          issue["title"],
            "state":          issue.get("state", ""),
            "author":         author,
            "fidelity":       fidelity,
            "created_at":     issue.get("created_at", ""),
            "closed_at":      issue.get("closed_at") or "",
            "labels":         ",".join(issue.get("labels") or []),
            "notes":          notes,
            "milestone":      (issue.get("milestone") or {}).get("title", ""),
            "status":         status,
            "error":          error,
        }
    # ---------------------------------------------------------------- #
    # [6] CSV + JSON Report Generation
    # ---------------------------------------------------------------- #

    REPORT_FIELDNAMES = [
        "source_iid", "dest_iid", "title", "state", "author", "fidelity",
        "created_at", "closed_at", "labels", "notes", "milestone",
        "status", "error",
    ]

    def generate_reports(self) -> None:
        """
        Write JSON and CSV migration reports to the reports/ directory.

        Reports produced:
          reports/migration_report.json  — Full stats and per-issue records.
          reports/migration_report.csv   — Tabular per-issue view (for review).
          reports/failures.json          — Detail on every failed issue.

        The fidelity column records, per issue, whether the content was written
        by its real author ("full") or by the admin token with an attribution
        header ("admin_attributed"), which is the audit trail for the
        preserve-authorship requirement.

        Summary is also logged to stdout / migration.log.
        """
        self.stats["completed_at"] = utc_now_iso()

        # ---- JSON report ---- #
        report = {
            "stats":   self.stats,
            "issues":  self.report_rows,
        }
        self._save_json(REPORT_JSON, report)
        log.info(f"JSON report saved:  {REPORT_JSON}")

        # ---- CSV report ---- #
        if self.report_rows:
            with open(REPORT_CSV, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(
                    f, fieldnames=self.REPORT_FIELDNAMES, extrasaction="ignore"
                )
                writer.writeheader()
                writer.writerows(self.report_rows)
            log.info(f"CSV report saved:   {REPORT_CSV}")
        else:
            log.info("No issue rows to write to CSV.")

        # ---- Failures report ---- #
        if self.failures:
            self._save_json(FAILURES_JSON, self.failures)
            log.warning(
                f"{len(self.failures)} issue(s) failed. "
                f"See {FAILURES_JSON} for details."
            )
        else:
            log.info("No failures recorded.")

        # ---- Console summary ---- #
        log.info("=" * 60)
        log.info("  MIGRATION SUMMARY")
        log.info("=" * 60)
        log.info(f"  Mode:                  {self.stats['mode']}")
        log.info(f"  Total issues found:    {self.stats['total_issues']}")
        log.info(f"  Migrated:              {self.stats['migrated']}")
        log.info(f"  Skipped (checkpoint):  {self.stats['skipped']}")
        log.info(f"  Failed:                {self.stats['failed']}")
        log.info(f"  Notes migrated:        {self.stats['notes_migrated']}")
        log.info(f"  Attachments uploaded:  {self.stats['attachments_uploaded']}")
        log.info(f"  Labels created:        {self.stats['labels_created']}")
        log.info(f"  Milestones created:    {self.stats['milestones_created']}")
        log.info(f"  Issue links created:   {self.stats['links_created']}")
        log.info(f"  Issue links skipped:   {self.stats['links_skipped']}")
        log.info(f"  Boards created:        {self.stats['boards_created']}")
        log.info(f"  Board lists created:   {self.stats['board_lists_created']}")
        log.info(f"  Fidelity full:         {self.stats['fidelity_full']}")
        log.info(f"  Fidelity attributed:   {self.stats['fidelity_attributed']}")
        log.info(f"  Started:               {self.stats['started_at']}")
        log.info(f"  Completed:             {self.stats['completed_at']}")
        log.info("=" * 60)
