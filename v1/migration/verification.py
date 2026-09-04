"""
migration.verification
----------------------
`--validation-only` mode and the post-run comparison.

Counts alone can match while the IIDs are wrong, so the IID set comparison is
the check that actually proves preservation worked. Results land in
reports/verification_report.json.
"""

import aiohttp

from .logging_setup import log
from .paths import VERIFICATION_REPORT
from .timeutils import utc_now_iso


class VerificationMixin:
    """
    Source vs destination verification for GitLabMigration.

    Expects the host object to provide both URLs, both tokens, and both
    project IDs.
    """

    # ---------------------------------------------------------------- #
    # [7] Verification Mode
    # ---------------------------------------------------------------- #

    async def run_verification(self, session: aiohttp.ClientSession) -> bool:
        """
        Post-migration verification: compare source vs destination.

        Verifies:
          - Total / open / closed issue counts
          - Label and milestone counts
          - Board count
          - IID set equality (which issues are missing or unexpected)

        Counting alone can pass while the IIDs are wrong, so the IID set
        comparison is the check that actually proves preservation worked.

        Results are written to reports/verification_report.json.

        Args:
            session: Active aiohttp session.

        Returns:
            True if every comparison matched.
        """
        log.info("=" * 60)
        log.info("  VERIFICATION MODE")
        log.info("=" * 60)

        results = {}

        # Define what to compare: (label, src_path, dest_path)
        checks = [
            ("total_issues",  "issues?state=all",     "issues?state=all"),
            ("open_issues",   "issues?state=opened",  "issues?state=opened"),
            ("closed_issues", "issues?state=closed",  "issues?state=closed"),
            ("labels",        "labels",               "labels"),
            ("milestones",    "milestones?state=all", "milestones?state=all"),
            ("boards",        "boards",               "boards"),
        ]

        for label, src_path, dest_path in checks:
            try:
                src_items  = await self._get_all_pages(
                    session,
                    f"{self.source_url}/api/v4/projects/"
                    f"{self.source_project}/{src_path}",
                    self.source_token
                )
                dest_items = await self._get_all_pages(
                    session,
                    f"{self.dest_url}/api/v4/projects/"
                    f"{self.dest_project}/{dest_path}",
                    self.dest_token
                )
            except Exception as exc:
                log.warning(f"  Could not verify {label}: {exc}")
                results[label] = {"source": "?", "destination": "?", "match": False}
                continue

            src_count  = len(src_items)
            dest_count = len(dest_items)
            is_match   = src_count == dest_count

            results[label] = {
                "source":      src_count,
                "destination": dest_count,
                "match":       is_match,
            }

            icon   = "✓" if is_match else "✗"
            status = "MATCH" if is_match else "MISMATCH"
            log.info(
                f"  {icon}  {label:<20} "
                f"source={src_count:<6} dest={dest_count:<6} {status}"
            )

            if label == "total_issues":
                results["iid_preservation"] = self._compare_iids(
                    src_items, dest_items
                )

        # ---- Write verification report ---- #
        report = {
            "verified_at":         utc_now_iso(),
            "source_project_id":   self.source_project,
            "dest_project_id":     self.dest_project,
            "results":             results,
            "all_match":           all(
                entry.get("match") for entry in results.values()
            ),
        }
        self._save_json(VERIFICATION_REPORT, report)

        overall = "PASSED ✓" if report["all_match"] else "FAILED ✗"
        log.info("─" * 60)
        log.info(f"  Verification result: {overall}")
        log.info(f"  Report saved to:     {VERIFICATION_REPORT}")
        log.info("=" * 60)

        return bool(report["all_match"])

    def _compare_iids(self, src_issues: list, dest_issues: list) -> dict:
        """
        Compare source and destination IID sets.

        Args:
            src_issues:  Source issue dicts.
            dest_issues: Destination issue dicts.

        Returns:
            dict with counts, the match flag, and up to 25 example IIDs on each
            side of the difference (enough to diagnose, short enough to read).
        """
        src_iids  = {int(issue["iid"]) for issue in src_issues}
        dest_iids = {int(issue["iid"]) for issue in dest_issues}

        missing = sorted(src_iids - dest_iids)
        extra   = sorted(dest_iids - src_iids)
        matched = src_iids == dest_iids

        icon = "✓" if matched else "✗"
        log.info(
            f"  {icon}  {'iid_preservation':<20} "
            f"missing={len(missing):<6} unexpected={len(extra):<6} "
            f"{'MATCH' if matched else 'MISMATCH'}"
        )
        if missing:
            log.warning(f"      Missing IIDs (first 25): {missing[:25]}")
        if extra:
            log.warning(f"      Unexpected IIDs (first 25): {extra[:25]}")

        return {
            "source":            len(src_iids),
            "destination":       len(dest_iids),
            "match":             matched,
            "missing_count":     len(missing),
            "unexpected_count":  len(extra),
            "missing_sample":    missing[:25],
            "unexpected_sample": extra[:25],
        }
