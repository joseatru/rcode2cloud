"""
migration.paths
---------------
Filesystem locations every phase reads and writes, and the one-time directory
creation that has to happen before logging opens its file handler.

All paths are relative to the process working directory, which is the v1/
directory the operator runs `gitlab_migration.py` from.
"""

from pathlib import Path


# ------------------------------------------------------------------ #
# File Paths
# ------------------------------------------------------------------ #
CHECKPOINT_FILE      = Path("checkpoints/migration_state.json")
CHECKPOINT_JOURNAL   = Path("checkpoints/migration_state.jsonl")
USER_CACHE_FILE      = Path("cache/user_id_cache.json")
UPLOAD_CACHE_FILE    = Path("cache/upload_map.json")
LOG_FILE             = Path("logs/migration.log")
REPORT_JSON          = Path("reports/migration_report.json")
REPORT_CSV           = Path("reports/migration_report.csv")
FAILURES_JSON        = Path("reports/failures.json")
VERIFICATION_REPORT  = Path("reports/verification_report.json")

# Ensure all required directories exist before logging initializes
for _path in [CHECKPOINT_FILE, CHECKPOINT_JOURNAL, USER_CACHE_FILE,
              UPLOAD_CACHE_FILE, LOG_FILE, REPORT_JSON, FAILURES_JSON,
              VERIFICATION_REPORT]:
    _path.parent.mkdir(parents=True, exist_ok=True)
