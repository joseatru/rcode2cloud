"""
migration.cli
-------------
Argument parsing, configuration, and process exit codes.

Exit-code semantics are part of the operator contract (operators gate shell
scripts on them): 0 success with zero recorded failures, 1 something needs a
human, 2 an argparse usage error, 130 Ctrl-C.
"""

import asyncio
import logging
import sys

from pathlib import Path

import config as cfg_module

from .fidelity import FidelityGuardError
from .logging_setup import log
from .mappings import load_mappings
from .orchestrator import GitLabMigration


def main() -> None:
    """
    Script entry point.

    Execution order:
      1. Build and parse CLI arguments (a runtime mode flag is required).
      2. If --env-source is provided, load the .env file.
      3. Build the runtime configuration dict (validates tokens and TLS flags).
      4. Load and validate mappings.yml.
      5. Instantiate GitLabMigration and call run().

    Exits 1 when the selected mode reports failure or any issue failed, so the
    script is usable from a shell conditional or CI job. A read-back fidelity
    guard abort also exits 1, but stops the run at the offending issue rather
    than continuing.
    """
    # Step 1 — Parse args
    parser = cfg_module.build_arg_parser()
    args   = parser.parse_args()

    # Step 2 — Load .env file before reading any os.getenv() values
    if args.env_source:
        cfg_module.load_env_file(args.env_source)

    # Step 3 — Build runtime config (reads env vars set in step 2)
    cfg = cfg_module.get_config(args)

    if cfg["VERBOSE"]:
        logging.getLogger().setLevel(logging.DEBUG)
        log.setLevel(logging.DEBUG)
        log.debug("Verbose logging enabled.")

    # Step 4 — Load and validate mappings.yml
    mappings = load_mappings(Path(cfg["MAPPINGS_FILE"]))

    # Log any active project ID overrides
    if cfg.get("SOURCE_PROJECT_OVERRIDE"):
        log.info(
            f"[OVERRIDE] Source project ID: {cfg['SOURCE_PROJECT_OVERRIDE']}"
        )
    if cfg.get("DEST_PROJECT_OVERRIDE"):
        log.info(
            f"[OVERRIDE] Destination project ID: {cfg['DEST_PROJECT_OVERRIDE']}"
        )

    # Step 5 — Run migration
    migration = GitLabMigration(mappings=mappings, cfg=cfg)
    try:
        succeeded = asyncio.run(migration.run())
    except KeyboardInterrupt:
        log.warning(
            "Interrupted. Progress is journaled — re-run the same command to "
            "resume."
        )
        sys.exit(130)
    except FidelityGuardError as exc:
        log.error("=" * 60)
        log.error("  MIGRATION ABORTED — READ-BACK FIDELITY FAILURE")
        log.error("=" * 60)
        for line in str(exc).splitlines():
            log.error(line)
        log.error("=" * 60)
        sys.exit(1)

    if not succeeded:
        log.error("Run finished with failures. See the reports directory.")
        sys.exit(1)


if __name__ == "__main__":
    main()








