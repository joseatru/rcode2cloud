"""
migration.orchestrator
----------------------
GitLabMigration: the object every phase hangs off, and the run() dispatcher.

The phases are composed in as mixins, one module per phase, so this file holds
only what is genuinely cross-cutting: construction, the active-mode label, and
the fixed phase order in run(). That order is set by data dependencies — labels
and milestones before issues that reference them, every issue before links can
join them, labels before board lists can point at them, and the IID counter
only once the highest IID is final.
"""

import asyncio

from .attachments import AttachmentsMixin
from .boards import BoardsMixin
from .bump import BumpMixin
from .fidelity import FidelityGuardMixin
from .http_client import HttpMixin
from .issues import IssuesMixin
from .labels import LabelsMixin
from .links import LinksMixin
from .logging_setup import log
from .milestones import MilestonesMixin
from .notes import NotesMixin
from .paths import UPLOAD_CACHE_FILE, USER_CACHE_FILE
from .reporting import ReportingMixin
from .sanity import SanityCheckMixin
from .state import StateMixin
from .timeutils import utc_now_iso
from .users import AuthorshipMixin
from .verification import VerificationMixin


class GitLabMigration(
    HttpMixin,
    StateMixin,
    AuthorshipMixin,
    FidelityGuardMixin,
    SanityCheckMixin,
    LabelsMixin,
    MilestonesMixin,
    AttachmentsMixin,
    IssuesMixin,
    NotesMixin,
    LinksMixin,
    BoardsMixin,
    BumpMixin,
    ReportingMixin,
    VerificationMixin,
):
    """
    Orchestrates the full async migration of GitLab issues from a
    source project to a destination project.

    Public API:
      migration = GitLabMigration(mappings, cfg)
      ok = await migration.run()

    Attributes:
        mappings (dict):       Loaded mappings.yml content.
        cfg (dict):            Runtime config from config.get_config().
        source_project (int):  Source project ID (CLI override > mappings.yml).
        dest_project (int):    Destination project ID.
        source_url (str):      Source GitLab base URL.
        dest_url (str):        Destination GitLab base URL.
        source_token (str):    Admin PAT for source reads.
        dest_token (str):      Owner PAT for destination writes and fallback
                               authorship.
        checkpoint (dict):     Source IID -> migration record.
        user_cache (dict):     Destination username -> user ID.
        upload_map (dict):     Source upload path -> destination markdown.
        milestone_map (dict):  Source milestone IID -> destination milestone ID.
        author_tokens (dict):  Source username -> resolved PAT (or None).
        supports_multiple_assignees (bool):
                               Cleared the first time the destination rejects
                               assignee_ids (Premium/Ultimate only).
        fidelity_guard_enabled (bool):
                               False under --no-fidelity-guard.
        _fidelity_confirmed_by_refetch (bool):
                               Set once the first successfully created issue of
                               the run has been re-fetched and matched.
        stats (dict):          Running migration statistics.
        failures (list):       Records of issues that failed to migrate.
        report_rows (list):    Per-issue rows written to CSV/JSON reports.
    """

    def __init__(self, mappings: dict, cfg: dict):
        self.mappings = mappings
        self.cfg      = cfg

        # ---- Resolve project IDs (CLI override wins over mappings.yml) ---- #
        self.source_project = (
            cfg.get("SOURCE_PROJECT_OVERRIDE")
            or mappings["source"]["project_id"]
        )
        self.dest_project = (
            cfg.get("DEST_PROJECT_OVERRIDE")
            or mappings["destination"]["project_id"]
        )

        # ---- URLs and admin tokens ---- #
        self.source_url   = cfg["SOURCE_GITLAB_URL"].rstrip("/")
        self.dest_url     = cfg["DEST_GITLAB_URL"].rstrip("/")
        self.source_token = cfg["SOURCE_TOKEN"]
        self.dest_token   = cfg["DEST_TOKEN"]

        # ---- Retry settings ---- #
        self.retry_count   = cfg["RETRY_COUNT"]
        self.retry_backoff = cfg["RETRY_BACKOFF"]

        # ---- Persistent state ---- #
        # Checkpoint: {source_iid: {dest_iid, title, migrated_at, fidelity}}
        self.checkpoint = self._load_checkpoint()
        # [2] User ID cache: {dest_username: user_id}
        self.user_cache = self._load_json(USER_CACHE_FILE, default={})
        # [9] Attachment cache: {"/uploads/<secret>/<file>": "![f](/uploads/..)"}
        self.upload_map = self._load_json(UPLOAD_CACHE_FILE, default={})
        # [3] Milestone map built during migrate_milestones()
        self.milestone_map: dict = {}

        # ---- Authorship ---- #
        # Lazily populated by token_for_author(); None means "no usable PAT,
        # fall back to DEST_TOKEN with an attribution header".
        self.author_tokens: dict = {}

        # GitLab Free exposes only the singular assignee_id. The plural form
        # is Premium/Ultimate, so the first rejection flips this off and every
        # later issue uses the singular field without re-probing.
        self.supports_multiple_assignees = True

        # ---- [13] Read-back fidelity guard ---- #
        # On by default. The first successful create of each run is re-fetched
        # to confirm the create response is not echoing optimistically; once
        # that confirmation passes, later issues are checked from the create
        # response body alone, which costs no extra API calls.
        self.fidelity_guard_enabled = cfg.get("FIDELITY_GUARD", True)
        self._fidelity_confirmed_by_refetch = False

        # ---- TLS ---- #
        self.ssl_context = self._build_ssl_context()

        # ---- Reporting ---- #
        self.stats = {
            "total_issues":         0,
            "migrated":             0,
            "skipped":              0,
            "failed":               0,
            "notes_migrated":       0,
            "labels_created":       0,
            "milestones_created":   0,
            "attachments_uploaded": 0,
            "links_created":        0,
            "links_skipped":        0,
            "boards_created":       0,
            "board_lists_created":  0,
            "fidelity_full":        0,
            "fidelity_attributed":  0,
            "started_at":           utc_now_iso(),
            "completed_at":         None,
            "mode":                 self._active_mode(),
        }
        self.failures: list    = []
        self.report_rows: list = []
        # Source IID -> its row in report_rows, so the concurrent note pass can
        # amend the row the sequential create pass already emitted.
        self._row_index: dict  = {}


    # ---------------------------------------------------------------- #
    # Runtime Mode
    # ---------------------------------------------------------------- #

    def _active_mode(self) -> str:
        """Return a string label for the currently active runtime mode."""
        for key in ("DRY_RUN", "TEST_RUN", "SANITY_CHECK",
                    "VALIDATION_ONLY", "FULL_RUN"):
            if self.cfg.get(key):
                return key.lower().replace("_", "-")
        return "unknown"

    # ---------------------------------------------------------------- #
    # Main Orchestration
    # ---------------------------------------------------------------- #

    async def run(self) -> bool:
        """
        Top-level orchestration. Dispatches on the active runtime mode flag.

        Dispatch logic:
          SANITY_CHECK    → run_sanity_check() only
          VALIDATION_ONLY → run_verification() only
          DRY_RUN         → every phase in simulation, then reports
          TEST_RUN        → full pipeline over the first cfg["LIMIT"] issues
          FULL_RUN        → full pipeline; cfg["LIMIT"], when set, is a
                            scheduling window over the remaining backlog

        See _apply_limit() for why the same --limit integer means "first N by
        IID" under --test-run and "next N outstanding" under --full-run.

        Migration phase order is fixed by data dependencies: labels and
        milestones must exist before issues reference them, every issue must
        exist before links can join them, labels must exist before board lists
        can point at them, and the IID counter can only be primed once the
        highest IID is final.

        Issue creation is sequential (concurrency 1) because GitLab assigns
        preserved IIDs monotonically; note threads then run concurrently under
        the MAX_CONCURRENT_REQUESTS semaphore.

        Returns:
            True on success. False if a check failed or any issue failed, which
            main() turns into exit code 1.
        """
        semaphore = asyncio.Semaphore(self.cfg["MAX_CONCURRENT_REQUESTS"])

        async with self._build_session() as session:

            # -------------------------------------------------------- #
            # Sanity Check — no migration
            # -------------------------------------------------------- #
            if self.cfg["SANITY_CHECK"]:
                return await self.run_sanity_check(session)

            # -------------------------------------------------------- #
            # Validation Only — no migration
            # -------------------------------------------------------- #
            if self.cfg["VALIDATION_ONLY"]:
                return await self.run_verification(session)

            # -------------------------------------------------------- #
            # Migration Modes: Dry Run, Test Run, Full Run
            # -------------------------------------------------------- #
            mode_label = (
                "DRY RUN"
                if self.cfg["DRY_RUN"]
                else f"TEST RUN (first {self.cfg.get('LIMIT', 50)} issues)"
                if self.cfg["TEST_RUN"]
                else "FULL RUN"
            )
            log.info("=" * 60)
            log.info(f"  Starting migration — {mode_label}")
            log.info(
                f"  Source:      {self.source_url} "
                f"(project ID: {self.source_project})"
            )
            log.info(
                f"  Destination: {self.dest_url} "
                f"(project ID: {self.dest_project})"
            )
            log.info("=" * 60)

            if not self.fidelity_guard_enabled and not self.cfg["DRY_RUN"]:
                log.warning(
                    "Read-back fidelity guard is DISABLED "
                    "(--no-fidelity-guard). GitLab returns 201 even when it "
                    "silently discards iid and created_at, so this run can "
                    "renumber and re-date every issue and still exit 0. "
                    "Neither is correctable after creation."
                )

            # Step 1 — Migrate labels
            await self.migrate_labels(session)

            # Step 2 — Migrate milestones and build milestone_map
            await self.migrate_milestones(session)

            # Step 3 — Fetch all source issues (paginated, sorted, limited)
            issues = await self.fetch_issues(session)
            self.stats["total_issues"] = len(issues)

            if not issues:
                log.warning("No issues found to migrate. Exiting.")
                self.generate_reports()
                return self.stats["failed"] == 0

            # Step 4 — Create issues sequentially in ascending IID order
            log.info(f"─── Creating {len(issues)} issues (sequential) ───")
            for issue in issues:
                await self.migrate_issue(session, issue)
            self.save_checkpoint()

            # Step 5 — Notes, closing state, and updated_at (concurrent)
            if not self.cfg["DRY_RUN"]:
                log.info("─── Migrating notes and issue state ───")

                async def _content_worker(issue: dict):
                    """Semaphore-guarded per-issue notes/state worker."""
                    async with semaphore:
                        await self.migrate_issue_content(session, issue)

                await asyncio.gather(
                    *[_content_worker(issue) for issue in issues]
                )
                self.save_checkpoint()

            # Step 6 — Issue links (needs every issue to exist)
            await self.migrate_links(session, issues)

            # Step 7 — Boards and lists (needs destination labels to exist)
            await self.migrate_boards(session)

            # Step 8 — Prime the IID counter (needs the final highest IID)
            if not self.cfg["DRY_RUN"]:
                await self.bump_iid_counter(session)

            # Step 9 — Generate CSV + JSON reports
            self.generate_reports()

            # Step 10 — Post-migration verification (skip for dry-run)
            verified = True
            if not self.cfg["DRY_RUN"]:
                verified = await self.run_verification(session)

            return verified and self.stats["failed"] == 0
