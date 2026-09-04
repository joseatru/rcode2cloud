#!/usr/bin/env python3
"""
gitlab_migration.py
-------------------
Production-grade async GitLab issue migration script.

Migrates issues (IIDs, authors, timestamps, labels, milestones, assignees,
notes, attachments, links, boards, open/closed state) from:

  Source:      rcode.rockefeller.edu  /  Project ID 641  (scicomp-kanban)
  Destination: gitlab.com             /  Project ID 83486174
               (scicomp-kanban_cce-migration)

This file is the entry point and the only supported way to run the migration.
The pipeline itself lives in the `migration/` package, one module per phase; see
`migration/__init__.py` for which module owns what. The CLI, log wording, report
columns, and exit codes are unchanged by that arrangement — every command in
README.md and RUNBOOK.md works exactly as documented.

Implemented capabilities:
  [1]  Full pagination     — follows X-Next-Page; handles >100 issues
                             (migration/http_client.py)
  [2]  User ID caching     — dest user IDs cached in cache/user_id_cache.json
                             (migration/users.py)
  [3]  Milestone migration — creates milestones; maps src IID -> dest ID
                             (migration/milestones.py)
  [4]  Full-fidelity writes— original iid, created_at, updated_at, and author,
                             written with the mapped author's own Owner PAT
                             (migration/issues.py, migration/notes.py)
  [5]  Retry / backoff     — 429 (honouring Retry-After), 5xx, and transient
                             transport errors (migration/http_client.py)
  [6]  CSV + JSON reports  — migration_report.{json,csv} + failures.json,
                             including a per-issue fidelity column
                             (migration/reporting.py)
  [7]  Verification mode   — --validation-only compares src vs dest counts
                             (migration/verification.py)
  [8]  Sanity-check mode   — validates PATs, users, projects, and that every
                             mapped PAT holds Owner on the destination
                             (migration/sanity.py)
  [9]  Attachments         — /uploads/... files re-uploaded and re-linked
                             (migration/attachments.py)
  [10] Issue links         — relationships recreated with link_type preserved
                             (migration/links.py)
  [11] Boards              — boards and lists recreated in position order
                             (migration/boards.py)
  [12] IID counter bump    — initialises internal_ids so the first post-
                             migration issue does not 409 (migration/bump.py)
  [13] Fidelity guard      — reads every created issue back and aborts the run
                             on the first iid / created_at / author mismatch
                             (migration/fidelity.py)

Fidelity model:
  Setting `iid` / `created_at` requires administrator or project owner rights,
  and gitlab.com SaaS offers neither instance admin nor `sudo` impersonation.
  Every mapped user therefore holds Owner on the destination project for the
  migration window and writes their own content with their own PAT
  (fidelity=full). A source author absent from mappings.yml is written with
  DEST_TOKEN plus a markdown attribution header (fidelity=admin_attributed);
  iid and created_at are still preserved.

  A user *present* in mappings.yml whose PAT is missing, still a placeholder,
  or not an Owner is a hard --sanity-check failure, not a silent downgrade:
  degrading a mapped user to admin_attributed defeats the point of the
  per-user PAT collection.

  Because GitLab answers 201 whether or not it honoured `iid` and
  `created_at`, the read-back fidelity guard re-checks every created issue
  against the request and aborts the whole run on the first mismatch. See
  migration/fidelity.py. It is on by default; --no-fidelity-guard disables it
  and is not recommended.

Not covered (deliberately out of scope):
  - Merge requests, wikis, repository content, CI configuration
  - Cross-project issue links (skipped and reported)
  - Epics, iterations, and other Premium/Ultimate-only objects
  - Multiple assignees on GitLab Free (falls back to a single assignee)
  - `closed_at` on closed issues: the issues API has no settable parameter for
    it, so a closed issue records the migration time. The source value is kept
    in the JSON/CSV report only.

Usage:
  See README.md or: python gitlab_migration.py --help

Dependencies:
  pip install -r requirements.txt
"""

import config as cfg_module

from migration.cli import main
from migration.fidelity import (
    CREATED_AT_TOLERANCE_SECONDS,
    FidelityGuardError,
)
from migration.http_client import api_call_with_retry
from migration.logging_setup import log
from migration.mappings import load_mappings, validate_mappings
from migration.orchestrator import GitLabMigration
from migration.paths import (
    CHECKPOINT_FILE,
    CHECKPOINT_JOURNAL,
    FAILURES_JSON,
    LOG_FILE,
    REPORT_CSV,
    REPORT_JSON,
    UPLOAD_CACHE_FILE,
    USER_CACHE_FILE,
    VERIFICATION_REPORT,
)
from migration.timeutils import parse_gitlab_timestamp, utc_now_iso

# Re-exported at module scope so `import gitlab_migration as gm` keeps working
# for verify_offline.py and any operator one-liner that reaches for a symbol
# here. The package modules are the source of truth; these are aliases.
__all__ = [
    "CHECKPOINT_FILE",
    "CHECKPOINT_JOURNAL",
    "CREATED_AT_TOLERANCE_SECONDS",
    "FAILURES_JSON",
    "FidelityGuardError",
    "GitLabMigration",
    "LOG_FILE",
    "REPORT_CSV",
    "REPORT_JSON",
    "UPLOAD_CACHE_FILE",
    "USER_CACHE_FILE",
    "VERIFICATION_REPORT",
    "api_call_with_retry",
    "cfg_module",
    "load_mappings",
    "log",
    "main",
    "parse_gitlab_timestamp",
    "utc_now_iso",
    "validate_mappings",
]


if __name__ == "__main__":
    main()
