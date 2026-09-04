"""
migration.logging_setup
-----------------------
The single logging configuration for the whole tool.

Imported (directly or transitively) by every other module so that basicConfig
runs exactly once, before any phase emits a line. The logger name is
"gitlab_migration" and the format is fixed: RUNBOOK.md quotes real log output,
so changing either breaks documented expectations.
"""

import logging
import sys

from .paths import LOG_FILE


# ------------------------------------------------------------------ #
# Logging — writes to both file and stdout
# ------------------------------------------------------------------ #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("gitlab_migration")
