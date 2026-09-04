#!/usr/bin/env python3
"""
config.py
---------
Configuration loader for the GitLab Migration Script.

Responsibilities:
  - Parse all CLI arguments and runtime mode flags
  - Optionally load a .env file via --env-source <path>
  - Validate that required tokens are present and not left as placeholders
  - Return a unified runtime config dict consumed by gitlab_migration.py

Runtime Modes (mutually exclusive — exactly one is REQUIRED):
  --sanity-check     Validate PATs, Owner roles, user mappings, project access.
                     Fails if any PAT named in mappings.yml is missing or is
                     not an Owner on the destination project.
  --dry-run          Simulate migration output. No API writes.
  --test-run         Pilot: migrate the first 50 issues by IID (stable set).
  --validation-only  Post-migration verification only. No migration.
  --full-run         Migrate all issues. With --limit, a forward-moving window.

Read-back fidelity guard (enabled by default, disabled with
--no-fidelity-guard):
  GitLab returns HTTP 201 whether or not it honoured the `iid` and
  `created_at` it was sent, so a non-Owner PAT produces a renumbered,
  re-dated migration that looks like a clean success. The guard compares the
  created issue back against what was requested and aborts the run on the
  first mismatch.

Environment Variables (shell export or via --env-source file):
  SOURCE_GITLAB_URL       Base URL of source GitLab (default: https://rcode.rockefeller.edu)
  DEST_GITLAB_URL         Base URL of destination GitLab (default: https://gitlab.com)
  SOURCE_GITLAB_TOKEN     Admin PAT for source project  [REQUIRED]
  DEST_GITLAB_TOKEN       Owner PAT for destination project [REQUIRED]
  PAT_<NAME>              One per user in mappings.yml, named by its pat_env
                          key. Required for every mapped user: --sanity-check
                          fails when one is missing.
  MAX_CONCURRENT_REQUESTS Async worker concurrency limit (default: 10)
  REQUEST_TIMEOUT         HTTP timeout in seconds (default: 60)
  RETRY_COUNT             Max retry attempts on rate-limit / server errors (default: 5)
  RETRY_BACKOFF           Base backoff in seconds, doubles each retry (default: 2.0)

See .env.example for a fill-in-the-blanks template.
"""

import os
import re
import sys
import argparse
from pathlib import Path


# Values that look like `<token>` or `<your-pat-here>` are unsubstituted
# template placeholders, not credentials. Treated as "not set".
PLACEHOLDER_RE = re.compile(r"^<.*>$")


# ------------------------------------------------------------------ #
# .env File Loader
# ------------------------------------------------------------------ #

def load_env_file(env_path: str) -> None:
    """
    Read a KEY=VALUE file and inject variables into os.environ.

    Rules:
      - Lines beginning with '#' are comments — skipped.
      - Empty lines are skipped.
      - Surrounding single or double quotes are stripped from values.
      - Variables already present in the environment are NOT overwritten
        (uses os.environ.setdefault so shell exports always win).

    Args:
        env_path: Path string to the .env file.

    Exits with code 1 if the file does not exist.
    """
    path = Path(env_path)

    if not path.exists():
        print(
            f"[ERROR] --env-source file not found: {env_path}",
            file=sys.stderr
        )
        sys.exit(1)

    loaded = 0
    with open(path) as f:
        for line_num, line in enumerate(f, start=1):
            line = line.strip()

            # Skip blank lines and comments
            if not line or line.startswith("#"):
                continue

            if "=" not in line:
                print(
                    f"[WARN] Skipping malformed line {line_num} in {env_path}: {line!r}",
                    file=sys.stderr
                )
                continue

            key, _, value = line.partition("=")
            key   = key.strip()
            value = value.strip()

            # Strip surrounding quotes
            if len(value) >= 2 and value[0] in ('"', "'") and value[0] == value[-1]:
                value = value[1:-1]

            os.environ.setdefault(key, value)
            loaded += 1

    print(f"[CONFIG] Loaded {loaded} variable(s) from {env_path}")


# ------------------------------------------------------------------ #
# Token Helpers
# ------------------------------------------------------------------ #

def is_placeholder(value) -> bool:
    """
    Return True if value is empty or an unsubstituted `<...>` placeholder.

    .env.example ships every secret as `<token>`. A run that inherits those
    values would fail with a confusing 401 several API calls in, so they are
    treated as absent instead.
    """
    if not value:
        return True
    return bool(PLACEHOLDER_RE.match(value.strip()))


def read_token(env_key: str):
    """
    Read a PAT from the environment, returning None for placeholder values.

    Args:
        env_key: Environment variable name (e.g. "PAT_JBANFELDER").

    Returns:
        The token string, or None if unset or still a `<...>` placeholder.
    """
    value = os.getenv(env_key)
    return None if is_placeholder(value) else value.strip()


# ------------------------------------------------------------------ #
# Argument Parser
# ------------------------------------------------------------------ #

def build_arg_parser() -> argparse.ArgumentParser:
    """
    Build and return the CLI argument parser.

    Runtime mode flags are mutually exclusive and one is required, so a bare
    invocation cannot silently start a real full migration.

    Override flags (--source-project, --dest-project, --limit) apply in
    addition to any mode flag.

    Returns:
        argparse.ArgumentParser: Fully configured parser.
    """
    parser = argparse.ArgumentParser(
        description="GitLab Issue Migration Tool — scicomp-kanban",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
RUNTIME MODES (exactly one is required):
  --sanity-check      Validate PATs, Owner roles, users, URLs. No migration.
  --dry-run           Simulate migration. Print what would happen. No writes.
  --test-run          Pilot mode: first 50 issues by IID. Full pipeline.
  --validation-only   Post-migration check: compare source vs dest counts.
  --full-run          Full migration of all issues.

--limit SEMANTICS:
  --test-run / --dry-run  first N issues by ascending IID, applied BEFORE the
                          checkpoint filter — the same set on every run.
  --full-run              next N not-yet-migrated issues, applied AFTER the
                          checkpoint filter — a forward-moving window.

--ca-bundle and --insecure are mutually exclusive.

READ-BACK FIDELITY GUARD:
  Enabled by default. Every created issue is compared back against the iid,
  created_at, and author that were requested, and the first successful create
  of each run is additionally re-fetched to confirm it. Any mismatch aborts
  the run with exit code 1. --no-fidelity-guard turns this off and is NOT
  recommended: GitLab answers 201 even when it silently discards iid and
  created_at, so without the guard a full run can report success having
  renumbered and re-dated every issue.

EXAMPLES:
  # Step 1 — validate credentials, Owner roles, and connectivity
  python gitlab_migration.py --env-source .env --sanity-check

  # Step 2 — simulate pilot with first 50 issues (no writes)
  python gitlab_migration.py --env-source .env --dry-run --limit 50

  # Step 3 — run pilot migration against a scratch destination project
  python gitlab_migration.py --env-source .env --test-run --dest-project 12345

  # Step 4 — verify pilot results
  python gitlab_migration.py --env-source .env --validation-only

  # Step 5 — full migration (re-run the same command to resume)
  python gitlab_migration.py --env-source .env --full-run

  # Step 5 alternative — 2000 issues per scheduled window; each run advances
  python gitlab_migration.py --env-source .env --full-run --limit 2000

  # Self-signed or internal CA on the source instance
  python gitlab_migration.py --env-source .env --full-run \\
      --ca-bundle /path/to/rockefeller-ca.pem

  # Verbose per-page / per-note progress
  python gitlab_migration.py --env-source .env --full-run -v

EXIT CODES:
  0    success — the selected mode passed with zero recorded failures
  1    failure, missing/placeholder token, or invalid mappings.yml
  2    argparse usage error (no mode flag, or two mode flags)
  130  interrupted with Ctrl-C; progress is journaled, re-run to resume
        """
    )

    # ---------------------------------------------------------------- #
    # .env file override
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "--env-source",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Path to a .env file (KEY=VALUE format) whose variables are "
            "injected into the environment before the script runs. "
            "Variables already set in the shell are NOT overwritten."
        )
    )

    # ---------------------------------------------------------------- #
    # Runtime mode flags — mutually exclusive, one required
    # ---------------------------------------------------------------- #
    mode_group = parser.add_mutually_exclusive_group(required=True)

    mode_group.add_argument(
        "--sanity-check",
        action="store_true",
        default=False,
        help=(
            "SANITY CHECK: Validate PATs, source/destination project access, "
            "all user mappings, and that every mapped PAT holds Owner on the "
            "destination project. A user listed in mappings.yml whose PAT is "
            "missing, still a <...> placeholder, or not an Owner is a hard "
            "failure here. No migration is performed. "
            "Run this first before any migration."
        )
    )

    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help=(
            "DRY RUN: Simulate the migration. Print what would be "
            "created/updated without making any API writes. "
            "Use with --limit 50 for a safe pilot preview."
        )
    )

    mode_group.add_argument(
        "--test-run",
        action="store_true",
        default=False,
        help=(
            "TEST RUN (pilot): Migrate only the first 50 issues by ascending "
            "IID. Runs the full migration pipeline with real API writes. "
            "The limit is applied BEFORE checkpoint filtering, so the pilot "
            "set is the same first N issues on every run and a resumed pilot "
            "does not drift forward into unpiloted issues. "
            "Default limit: 50 (override with --limit)."
        )
    )

    mode_group.add_argument(
        "--validation-only",
        action="store_true",
        default=False,
        help=(
            "VALIDATION ONLY: Run post-migration verification. "
            "Compares source vs destination issue counts and metadata. "
            "No migration is performed."
        )
    )

    mode_group.add_argument(
        "--full-run",
        action="store_true",
        default=False,
        help=(
            "FULL RUN: Migrate all issues from source to destination. "
            "No issue limit is applied unless --limit is given, in which case "
            "it caps the issues migrated per run and each subsequent run "
            "advances through the backlog. Re-run the same command to resume "
            "from the checkpoint."
        )
    )

    # ---------------------------------------------------------------- #
    # Optional project ID overrides
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "--source-project",
        type=int,
        default=None,
        metavar="PROJECT_ID",
        help=(
            "Override the source project ID from mappings.yml. "
            "Useful for testing against a different project."
        )
    )

    parser.add_argument(
        "--dest-project",
        type=int,
        default=None,
        metavar="PROJECT_ID",
        help=(
            "Override the destination project ID from mappings.yml. "
            "Useful for testing against a scratch destination."
        )
    )

    # ---------------------------------------------------------------- #
    # Issue limit
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Limit the number of issues to process. Its meaning depends on the "
            "mode. With --test-run or --dry-run the limit is applied BEFORE "
            "checkpoint filtering: the first N issues by ascending IID, the "
            "same set on every run. With --full-run it is applied AFTER "
            "checkpoint filtering: the next N issues that are not yet fully "
            "migrated, so consecutive windowed runs make forward progress "
            "through the backlog. Overrides the --test-run default of 50; "
            "unset means no limit."
        )
    )

    # ---------------------------------------------------------------- #
    # Resume / no-resume (boolean pair)
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help=(
            "Resume from the last checkpoint (default: True). "
            "Issues already recorded in migration_state.json are skipped."
        )
    )

    parser.add_argument(
        "--no-resume",
        action="store_false",
        dest="resume",
        help=(
            "Ignore the checkpoint and re-attempt every issue. "
            "WARNING: against a populated destination every issue is re-probed "
            "and adopted rather than created, which wastes a round-trip per "
            "issue and fills the report with already_present rows. Only use it "
            "against a genuinely empty destination project."
        )
    )

    # ---------------------------------------------------------------- #
    # Read-back fidelity guard
    # ---------------------------------------------------------------- #
    # GitLab answers 201 whether or not it honoured `iid` and `created_at`, so
    # silent rejection is indistinguishable from success at the HTTP layer.
    # The guard is therefore on by default and only switchable off.
    parser.add_argument(
        "--no-fidelity-guard",
        action="store_false",
        dest="fidelity_guard",
        default=True,
        help=(
            "Disable the read-back fidelity guard. NOT RECOMMENDED. The guard "
            "compares every created issue's returned iid, created_at, and "
            "author against what was requested, re-fetches the first "
            "successful create of the run to confirm it, and aborts on the "
            "first mismatch. GitLab returns 201 even when it silently "
            "discards iid and created_at for a non-Owner caller, so with the "
            "guard off a --full-run can exit 0 having renumbered every issue "
            "and stamped every one with today's date — and authorship and "
            "IIDs cannot be corrected after the fact."
        )
    )

    # ---------------------------------------------------------------- #
    # TLS / certificate handling
    # ---------------------------------------------------------------- #
    # aiohttp does NOT honour REQUESTS_CA_BUNDLE (that variable belongs to
    # `requests`), so an explicit path has to be threaded into the connector.
    parser.add_argument(
        "--ca-bundle",
        type=str,
        default=None,
        metavar="FILE",
        help=(
            "Path to a PEM CA bundle used to verify TLS certificates. "
            "Required when the source instance presents a certificate signed "
            "by an internal CA. REQUESTS_CA_BUNDLE has no effect here."
        )
    )

    parser.add_argument(
        "--insecure",
        action="store_true",
        default=False,
        help=(
            "Disable TLS certificate verification entirely. "
            "Last resort for self-signed certificates; prefer --ca-bundle."
        )
    )

    # ---------------------------------------------------------------- #
    # Verbosity
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        default=False,
        help=(
            "Enable DEBUG logging: per-page fetch progress, per-note results, "
            "cache hits, and skip reasons."
        )
    )

    # ---------------------------------------------------------------- #
    # Mappings file path
    # ---------------------------------------------------------------- #
    parser.add_argument(
        "--mappings",
        type=str,
        default="mappings.yml",
        metavar="FILE",
        help="Path to the mappings.yml configuration file (default: mappings.yml)."
    )

    return parser


# ------------------------------------------------------------------ #
# Runtime Config Builder
# ------------------------------------------------------------------ #

def get_config(args: argparse.Namespace) -> dict:
    """
    Build and return the unified runtime configuration dictionary.

    Reads connection settings from environment variables (after any
    optional .env file has been loaded). CLI flags take priority over
    environment-variable defaults.

    The returned dict is the single source of truth passed into
    GitLabMigration.__init__().

    Args:
        args: Namespace from argparse.parse_args().

    Returns:
        dict: Runtime configuration used throughout gitlab_migration.py.

    Exits with code 1 if a required token is missing, is still a `<...>`
    placeholder, or if mutually exclusive TLS flags are combined.
    """

    # ---- Apply test-run default limit ---- #
    # --test-run implies a limit of 50 unless --limit overrides it.
    limit = args.limit
    if args.test_run and limit is None:
        limit = 50

    # ---- Read and validate required tokens ---- #
    # Every mode reads from both instances (even --dry-run diffs destination
    # labels and milestones), so both admin tokens are unconditionally
    # required. A missing token is fatal here rather than a 401 an hour in.
    source_token = read_token("SOURCE_GITLAB_TOKEN")
    dest_token   = read_token("DEST_GITLAB_TOKEN")

    missing = []
    if not source_token:
        missing.append("SOURCE_GITLAB_TOKEN")
    if not dest_token:
        missing.append("DEST_GITLAB_TOKEN")

    if missing:
        print(
            "[ERROR] Required token(s) not set (or still a <...> placeholder): "
            + ", ".join(missing),
            file=sys.stderr
        )
        print(
            "        Export them in your shell, or copy .env.example to .env, "
            "fill it in, and pass --env-source .env",
            file=sys.stderr
        )
        sys.exit(1)

    # ---- TLS flag sanity ---- #
    if args.ca_bundle and args.insecure:
        print(
            "[ERROR] --ca-bundle and --insecure are mutually exclusive.",
            file=sys.stderr
        )
        sys.exit(1)

    if args.ca_bundle and not Path(args.ca_bundle).exists():
        print(
            f"[ERROR] --ca-bundle file not found: {args.ca_bundle}",
            file=sys.stderr
        )
        sys.exit(1)

    return {
        # ---- GitLab Instance URLs ---- #
        "SOURCE_GITLAB_URL": os.getenv(
            "SOURCE_GITLAB_URL",
            "https://rcode.rockefeller.edu"
        ),
        "DEST_GITLAB_URL": os.getenv(
            "DEST_GITLAB_URL",
            "https://gitlab.com"
        ),

        # ---- Admin PATs ---- #
        # SOURCE_TOKEN: used for all reads on the source project.
        # DEST_TOKEN:   Owner PAT used for project-level writes (labels,
        #               milestones, boards, IID bump) and as the authorship
        #               fallback for source users absent from mappings.yml.
        # Per-user PATs are read on demand from each mapping's pat_env key.
        "SOURCE_TOKEN": source_token,
        "DEST_TOKEN":   dest_token,

        # ---- Concurrency and Timeouts ---- #
        "MAX_CONCURRENT_REQUESTS": int(
            os.getenv("MAX_CONCURRENT_REQUESTS", "10")
        ),
        "REQUEST_TIMEOUT": int(
            os.getenv("REQUEST_TIMEOUT", "60")
        ),

        # ---- Retry / Exponential Backoff ---- #
        # RETRY_COUNT:   max retries on 429 / 5xx / transient transport errors.
        # RETRY_BACKOFF: base wait time in seconds (doubles each retry, unless
        #                the server sends Retry-After).
        "RETRY_COUNT":   int(os.getenv("RETRY_COUNT", "5")),
        "RETRY_BACKOFF": float(os.getenv("RETRY_BACKOFF", "2.0")),

        # ---- Runtime Mode Booleans ---- #
        # Exactly one of these is True; argparse enforces it.
        "DRY_RUN":         args.dry_run,
        "TEST_RUN":        args.test_run,
        "SANITY_CHECK":    args.sanity_check,
        "VALIDATION_ONLY": args.validation_only,
        "FULL_RUN":        args.full_run,

        # ---- Resume Behavior ---- #
        # True  → skip issues already in migration_state.json (default).
        # False → re-attempt every issue (--no-resume).
        "RESUME": args.resume,

        # ---- Read-Back Fidelity Guard ---- #
        # True (default) → verify each created issue's iid / created_at /
        #                  author against the request and abort on mismatch.
        # False          → --no-fidelity-guard; not recommended.
        "FIDELITY_GUARD": args.fidelity_guard,

        # ---- Project ID Overrides ---- #
        # If set via CLI, these override the IDs in mappings.yml.
        "SOURCE_PROJECT_OVERRIDE": args.source_project,
        "DEST_PROJECT_OVERRIDE":   args.dest_project,

        # ---- Issue Processing Limit ---- #
        # None = no limit (full-run); 50 = test-run default.
        "LIMIT": limit,

        # ---- TLS ---- #
        "CA_BUNDLE": args.ca_bundle,
        "INSECURE":  args.insecure,

        # ---- Logging ---- #
        "VERBOSE": args.verbose,

        # ---- File Paths ---- #
        "MAPPINGS_FILE": args.mappings,
    }
