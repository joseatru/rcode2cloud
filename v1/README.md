# scicomp-kanban GitLab Issue Migration

Production-grade async, full-fidelity migration of GitLab issues from:

| | Project |
|---|---|
| **Source** | `rcode.rockefeller.edu` — ID `641` — `jbanfelder/scicomp-kanban` |
| **Destination** | `gitlab.com` — ID `83486174` — `rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration` |

Full fidelity means the destination reproduces the original **issue numbers**,
**authors**, and **timestamps** — not a flattened copy attributed to an admin
account. That requirement drives the permission model described below, so read
[Fidelity and Authorship](#fidelity-and-authorship) before collecting tokens.

For the operational procedure — pre-flight, pilot, full run, verification, and
cleanup — see **[RUNBOOK.md](RUNBOOK.md)**.

---

## Features

| Feature | Status |
|---------|--------|
| Async concurrent migration (`aiohttp`) | ✓ |
| Original issue number (`iid`) preservation | ✓ |
| Original author preservation (per-user PATs) | ✓ |
| Original `created_at` / `updated_at` preservation (issues and notes) | ✓ |
| Original `closed_at` preservation | ✗ The issues API has no settable `closed_at`. A closed issue records the migration time; the source value is kept in the report/CSV only |
| Admin fallback with markdown attribution for unmapped authors | ✓ |
| Label migration | ✓ |
| Milestone migration | ✓ |
| Comment / note migration | ✓ |
| Attachment download, re-upload, and markdown rewriting | ✓ |
| Issue link migration (same-project) | ✓ |
| Board and board-list (Kanban column) migration | ✓ |
| IID counter bump (prevents post-migration 409s) | ✓ |
| Open/closed state sync | ✓ |
| Adopt (rather than duplicate) an issue already at the target IID | ✓ |
| Assignee migration | ⚠ Single assignee on Free tier (`assignee_ids` is Premium-only) |
| Multiple boards | ⚠ One board per project on gitlab.com Free; extras reported as skipped |
| Cross-project issue links | ✗ Out of scope — skipped and reported |
| Merge requests, wikis, repository content, CI configuration | ✗ Out of scope |
| Epics, iterations, other Premium/Ultimate-only objects | ✗ Out of scope |
| Resumable via checkpoint file | ✓ |
| Idempotent re-runs (every phase checks before creating) | ✓ |
| Dry-run mode (zero API writes) | ✓ |
| Sanity-check mode (PATs, projects, users, **Owner roles**) | ✓ |
| Post-migration verification mode | ✓ |
| Read-back fidelity guard (aborts on a silently ignored `iid`/`created_at`/author) | ✓ |
| Full pagination (`X-Next-Page` header) | ✓ |
| Destination user ID caching | ✓ |
| Attachment upload caching | ✓ |
| Retry + backoff on 429/5xx, timeouts, and connection errors | ✓ |
| JSON + CSV migration reports with a fidelity column | ✓ |
| `.env` file support (`--env-source`) | ✓ |
| Custom CA bundle / insecure TLS options | ✓ |
| Mandatory runtime mode flag | ✓ |
| Meaningful process exit codes (`0`/`1`/`2`/`130`) | ✓ |

The out-of-scope rows above mirror the `Not covered` block in the
`gitlab_migration.py` module docstring. If the two ever disagree, the docstring
is next to the code and wins.

---

## Repository Layout

```
gitlab-migration/
│
├── .gitignore                   # Repo root — keeps .env and generated output out of git
│
└── v1/
    │
    ├── gitlab_migration.py      # Entry point — thin shim over migration.cli
    ├── config.py                # Config loader, arg parser, runtime flags
    │
    ├── migration/               # The migration pipeline, one module per phase
    │   ├── __init__.py          # Package docstring + "where to change what" table
    │   ├── paths.py             # Checkpoint / cache / log / report file locations
    │   ├── logging_setup.py     # The single logging config and shared `log`
    │   ├── timeutils.py         # Timestamp parsing and UTC-now formatting
    │   ├── mappings.py          # mappings.yml loading and schema validation
    │   ├── http_client.py       # TLS, session, HTTP verbs, retry/backoff
    │   ├── state.py             # Checkpoint journal, snapshots, atomic JSON, caches
    │   ├── users.py             # Authorship/token resolution, dest user lookup
    │   ├── fidelity.py          # Read-back fidelity guard
    │   ├── sanity.py            # --sanity-check mode
    │   ├── labels.py            # Label phase, label name → ID map
    │   ├── milestones.py        # Milestone phase and the milestone map
    │   ├── issues.py            # Issue selection, --limit semantics, create, adopt
    │   ├── attachments.py       # Upload re-hosting and markdown rewriting
    │   ├── notes.py             # Note (comment) phase
    │   ├── links.py             # Issue link phase
    │   ├── boards.py            # Board and board-list phase
    │   ├── bump.py              # IID counter priming
    │   ├── verification.py      # --validation-only and post-run comparison
    │   ├── reporting.py         # Report rows, REPORT_FIELDNAMES, JSON/CSV output
    │   ├── orchestrator.py      # GitLabMigration composition and run() phase order
    │   └── cli.py               # main(), exit codes
    │
    ├── mappings.yml             # Project paths, IDs, and user mappings
    ├── requirements.txt         # Python dependencies
    ├── .env.example             # Template for the token file — copy to .env
    ├── verify_offline.py        # Offline logic checks — no network, no credentials
    │
    ├── README.md                # This file
    ├── RUNBOOK.md               # Step-by-step operational procedure
    ├── AGENTS.md                # Guidance for AI agents working in this repo
    │
    ├── checkpoints/
    │   ├── migration_state.json  # Compacted checkpoint snapshot (auto-created)
    │   └── migration_state.jsonl # Append-only checkpoint journal (auto-created)
    │
    ├── cache/
    │   ├── user_id_cache.json    # Destination user ID cache (auto-created)
    │   └── upload_map.json       # Attachment upload cache (auto-created)
    │
    ├── logs/
    │   └── migration.log         # Full execution log (auto-created)
    │
    └── reports/
        ├── migration_report.json    # Stats + per-issue records (incl. fidelity)
        ├── migration_report.csv     # Tabular report (for spreadsheet review)
        ├── failures.json            # Failed-issue details
        └── verification_report.json # Source vs destination comparison
```

`gitlab_migration.py` remains the entry point at its original path, with an
unchanged CLI. Every command in this file and in
[RUNBOOK.md](RUNBOOK.md) works exactly as written — the `migration/` package is
a structural arrangement of the same code, not a change to its behavior.

`.gitignore` lives at the **repository root** (`gitlab-migration/.gitignore`),
one level above this directory, so its `.env` rule covers `v1/.env`. Verify with
`git check-ignore -v v1/.env` from the repo root.

`.env` is deliberately absent from this listing. It is created locally from
`.env.example` and is never committed.

---

## Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Sanity-check the install without any credentials:

```bash
python verify_offline.py
```

`verify_offline.py` exercises the pure-logic surfaces — `--limit` window
selection, checkpoint semantics, authorship/fidelity resolution, the read-back
fidelity guard (against an in-memory fake HTTP layer), and the report column
set — with no network calls and no tokens. It exits `0` when every assertion
passes. Run it after any change to `_apply_limit()`, `token_for_author()`,
`REPORT_FIELDNAMES`, or anything in `migration/fidelity.py`.

---

## Fidelity and Authorship

GitLab honors the `iid` and `created_at` parameters on issue and note creation
only when the calling token belongs to an **administrator or a project owner**.
gitlab.com SaaS provides neither an instance administrator nor `sudo`
impersonation. Two things follow:

1. **Each user's own PAT creates that user's own issues and notes.** That is the
   only way the destination shows the true author.
2. **Each of those PATs must hold Owner on the destination project** for the
   migration window. Without Owner, GitLab accepts the request but silently
   drops `iid` and `created_at` — you lose IID alignment and every timestamp
   reads "just now".

`--sanity-check` probes each mapped user's role and fails if any PAT is not an
Owner, so this surfaces in seconds rather than hours into a run.

### The read-back fidelity guard

`--sanity-check` is a pre-flight, and a pre-flight can be skipped or can pass
against a role that is revoked minutes later. The guard is the backstop that
catches it at write time.

The failure it exists for is invisible at the HTTP layer: **GitLab does not
error when it ignores `iid` and `created_at`.** A rejected `iid` comes back as
HTTP 201 with a *different* `iid` in the response body. Without a read-back
check, a `--full-run` can exit `0` having renumbered every issue and stamped
every one of them with today's date — and neither IIDs nor authorship can be
corrected after creation.

So every created issue is compared against what was requested:

| Field | Comparison |
|-------|------------|
| `iid` | Exact match |
| `created_at` | Equal instants within 2 seconds, parsed — not string-compared, since GitLab may echo a different precision or offset |
| `author.username` | Must equal the mapped `dest_username` for `fidelity=full`. Not enforced for `admin_attributed`, which is the supported fallback |

The check reads the `POST /issues` response body first, which costs zero extra
API calls. The **first successfully created issue of each run** is then
re-fetched with `GET /projects/:id/issues/:iid` to rule out a create response
that echoes the request optimistically; once that confirmation passes, the rest
of the run is checked from the response body alone.

Any mismatch **aborts the entire run at that issue** with exit code `1` and a
diagnostic naming the field, the requested value, the returned value, and the
most likely cause. It does not continue to the next issue and it is not
recorded as a per-issue failure, because retrying cannot fix it and every later
write would be wrong the same way.

The guard is inert in `--dry-run` (nothing is written) and does not fire on an
**adopted** issue — an issue already sitting at its target IID is a resume, not
a fidelity failure. See [Checkpoint / Resume](#checkpoint--resume).

`--no-fidelity-guard` disables it. **Not recommended.** The only situation that
justifies it is a deliberate migration where preserved IIDs and timestamps are
not required, and in that case the reports will not tell you afterwards which
issues were affected.

### Two different "no PAT" cases — do not confuse them

| Situation | Behavior |
|-----------|----------|
| Source author has **no `mappings.yml` entry** | Legitimate fallback. Written with `DEST_GITLAB_TOKEN`, original IID and timestamp preserved, markdown attribution header naming the real author, reported as `fidelity=admin_attributed`. |
| Source author **is in `mappings.yml`** but their `PAT_*` is missing, still a `<token>` placeholder, or does not hold Owner | **Hard `--sanity-check` failure.** Not a downgrade. Silently attributing a mapped user's content to the admin account defeats the reason their PAT was collected, and authorship cannot be corrected after creation. |

If you genuinely do not have a user's PAT, remove them from `mappings.yml` —
that converts the situation from the second row into the first, deliberately and
visibly.

Skipping `--sanity-check` and running anyway does not abort mid-migration: the
run logs a loud `WARNING` per affected user and falls back to admin attribution.
That is a safety net, not a supported mode.

Because IIDs must be assigned monotonically, issue creation runs sequentially in
ascending IID order. Notes remain concurrent across issues.

### What is not preserved

`closed_at` cannot be set through the GitLab issues API — there is no parameter
for it. Closing a migrated issue therefore stamps it with the *migration* time.
The original close date is recorded in `reports/migration_report.json` and the
`closed_at` column of `reports/migration_report.csv` for audit, and nowhere on
the destination issue itself. `created_at` and `updated_at` *are* preserved on
both issues and notes.

[RUNBOOK.md](RUNBOOK.md) covers granting Owner before the run and downgrading
every user afterward.

---

## Configure

### Option A — `.env` File with `--env-source` (recommended)

```bash
cp .env.example .env
$EDITOR .env
```

```env
SOURCE_GITLAB_URL=https://rcode.rockefeller.edu
DEST_GITLAB_URL=https://gitlab.com
SOURCE_GITLAB_TOKEN=xxxxx
DEST_GITLAB_TOKEN=xxxxx
PAT_JHERRERA15=xxxxx
PAT_JBANFELDER=xxxxx
PAT_DEST2=xxxxx
PAT_DEST4=xxxxx
PAT_DEST5=xxxxx
```

Pass it at runtime:

```bash
python gitlab_migration.py --env-source .env --full-run
```

> ⚠ `.env` is covered by the repo-root `.gitignore`. Never store PATs in version
> control. `SOURCE_GITLAB_TOKEN` and `DEST_GITLAB_TOKEN` must be real values — a
> leftover `<token>` placeholder is rejected at startup with exit code `1`. A
> `PAT_*` left as a placeholder for a user listed in `mappings.yml` fails
> `--sanity-check`; it is not silently treated as unset.

### Option B — Export Shell Environment Variables

```bash
export SOURCE_GITLAB_URL=https://rcode.rockefeller.edu
export DEST_GITLAB_URL=https://gitlab.com

export SOURCE_GITLAB_TOKEN=<source-admin-pat>
export DEST_GITLAB_TOKEN=<dest-admin-pat>

# Per-user PATs — used for actual authorship of that user's issues and notes
export PAT_JHERRERA15=<token>
export PAT_JBANFELDER=<token>
export PAT_DEST2=<token>
export PAT_DEST4=<token>
export PAT_DEST5=<token>
```

Shell exports take precedence over values in a `--env-source` file.

---

## Runtime Modes

Modes are **mutually exclusive and one is required**. A bare
`python gitlab_migration.py` exits with a usage error rather than starting a
migration.

| Flag | Mode | Description |
|------|------|-------------|
| `--sanity-check` | Sanity Check | Validate PATs, user mappings, project access, and Owner roles. No writes. |
| `--dry-run` | Dry Run | Simulate. Report what would happen. Zero API writes. |
| `--test-run` | Test Run (Pilot) | Migrate the first 50 issues by ascending IID. Full pipeline, real writes. |
| `--validation-only` | Validation Only | Compare source vs destination counts. No writes. |
| `--full-run` | Full Run | Migrate all issues, resuming from the checkpoint. |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success — the selected mode passed with zero recorded failures |
| `1` | Failure: a check failed, an issue failed, a required token is missing or still a `<token>` placeholder, `mappings.yml` is missing/unparseable/invalid, the read-back fidelity guard aborted the run, `--ca-bundle` and `--insecure` were combined, or the `--ca-bundle` file does not exist |
| `2` | argparse usage error — no mode flag, or two mode flags together |
| `130` | Interrupted with Ctrl-C. Progress is journaled; re-run the same command to resume |

Safe to gate a shell script on: `python gitlab_migration.py --env-source .env --sanity-check && ...`

---

## Optional Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--env-source FILE` | — | Load environment variables from a `.env` file |
| `--source-project N` | from `mappings.yml` | Override source project ID |
| `--dest-project N` | from `mappings.yml` | Override destination project ID |
| `--limit N` | None (`--test-run`: 50) | Cap the issues processed. Mode-dependent — see below |
| `--resume` | `True` | Resume from checkpoint (default) |
| `--no-resume` | — | Ignore the checkpoint; re-attempt every issue |
| `--no-fidelity-guard` | guard **on** | Disable the read-back fidelity guard. Not recommended — see [The read-back fidelity guard](#the-read-back-fidelity-guard) |
| `--mappings FILE` | `mappings.yml` | Path to the mappings YAML |
| `-v`, `--verbose` | off | Emit per-item debug logging |
| `--ca-bundle PATH` | — | CA bundle for TLS verification (use for rcode's internal CA) |
| `--insecure` | off | Disable TLS verification entirely — diagnostic use only |

`--ca-bundle` and `--insecure` are **mutually exclusive**. Passing both exits `1`
with `[ERROR] --ca-bundle and --insecure are mutually exclusive.` rather than
silently picking one. A `--ca-bundle` path that does not exist also exits `1`.

### `--limit` means two different things

| Mode | When the limit is applied | Effect |
|------|---------------------------|--------|
| `--test-run`, `--dry-run` | **Before** checkpoint filtering | The first N issues by ascending IID — the *same* set on every run. A resumed pilot re-examines 1–50 rather than drifting to 31–80, so pilot results stay comparable. |
| `--full-run` | **After** checkpoint filtering | The next N issues that are not yet fully migrated. Consecutive `--full-run --limit N` invocations each advance N issues through the backlog, which is what makes scheduled migration windows work. |

"Not yet fully migrated" means no checkpoint record, or a record whose notes and
final state never completed — so an interrupted issue is picked up by the next
window instead of being stranded. With `--no-resume` the checkpoint is ignored
and both paths reduce to the first N issues.

`REQUESTS_CA_BUNDLE` has no effect here. That variable belongs to the `requests`
library; this script uses `aiohttp`, which ignores it. Use `--ca-bundle`.

---

## Usage Examples

### 1. Validate Everything First

```bash
python gitlab_migration.py --env-source .env --sanity-check
```

Checks tokens, project access, every user mapping, and that each mapped PAT
holds Owner on the destination. Safe to run any time. Exits `0` on success, `1`
on any failure.

### 2. Simulate Pilot (No Writes)

```bash
python gitlab_migration.py --env-source .env --dry-run --limit 50 -v
```

Shows the IIDs that would be created, the token each item would be authored
with, and the resulting fidelity — no API writes occur.

### 3. Run Pilot Migration (First 50 Issues)

```bash
python gitlab_migration.py --env-source .env --test-run --dest-project <scratch_id>
```

Point the pilot at a scratch project. IIDs cannot be reclaimed once consumed, so
piloting against the real destination forces a full cleanup before the real run.

### 4. Verify Results

```bash
python gitlab_migration.py --env-source .env --validation-only
```

Compares issue, label, and milestone counts and writes
`reports/verification_report.json`.

### 5. Full Migration

```bash
python gitlab_migration.py --env-source .env --full-run
```

To split the work across scheduled windows, add `--limit`. Each run migrates the
next N outstanding issues:

```bash
python gitlab_migration.py --env-source .env --full-run --limit 2000
```

### 6. Resume After Interruption

```bash
python gitlab_migration.py --env-source .env --full-run
```

`--resume` is the default. Already-migrated issues (tracked in
`checkpoints/migration_state.json`) are skipped, and every other phase checks
for existing objects before creating them, so re-running is safe.

### 7. Custom CA Bundle for rcode

```bash
python gitlab_migration.py --env-source .env --sanity-check \
  --ca-bundle /path/to/rockefeller-ca.pem
```

### 8. Override Project IDs (Staging Test)

```bash
python gitlab_migration.py --env-source .env \
  --dry-run \
  --source-project 641 \
  --dest-project 99999
```

---

## mappings.yml Reference

```yaml
source:
  path: "https://rcode.rockefeller.edu/jbanfelder/scicomp-kanban"
  project_id: 641
  name: "scicomp-kanban"

destination:
  path: "https://gitlab.com/rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration"
  project_id: 83486174
  name: "scicomp-kanban_cce-migration"

users:
  jherrera:
    dest_username: "jherrera15"
    pat_env: "PAT_JHERRERA15"

  jbanfelder:
    dest_username: "jbanfelder"
    pat_env: "PAT_JBANFELDER"

  source_username_2:
    dest_username: "dest2"
    pat_env: "PAT_DEST2"

  source_username_4:
    dest_username: "dest4"
    pat_env: "PAT_DEST4"

  source_username_5:
    dest_username: "dest5"
    pat_env: "PAT_DEST5"
```

The file is schema-validated on load, so a typo fails immediately with a clear
message instead of a deep `KeyError` mid-run.

---

## User Mapping

| Source Username | Destination Username | PAT Env Var | Dest role during migration |
|-----------------|----------------------|-------------|----------------------------|
| `jherrera` | `jherrera15` | `PAT_JHERRERA15` | Owner |
| `jbanfelder` | `jbanfelder` | `PAT_JBANFELDER` | Owner |
| `source_username_2` | `dest2` | `PAT_DEST2` | Owner |
| `source_username_4` | `dest4` | `PAT_DEST4` | Owner |
| `source_username_5` | `dest5` | `PAT_DEST5` | Owner |

Replace the `source_username_*` placeholders with the real rcode usernames.
Owner is temporary and required for IID/timestamp preservation — see
[RUNBOOK.md](RUNBOOK.md) Step 9 for the downgrade procedure.

---

## PAT Scopes Required

### Source (`rcode.rockefeller.edu`)

```
api
read_api
read_repository
```

### Destination (`gitlab.com`)

```
api
```

Every destination PAT — admin and per-user alike — needs `api` scope *and* the
Owner role on project `83486174`.

---

## Reports

| File | Description |
|------|-------------|
| `reports/migration_report.json` | `stats` block plus an `issues` array of per-issue records |
| `reports/migration_report.csv` | Tabular report — open in Excel/Sheets for review |
| `reports/failures.json` | Per-issue failure details with error messages |
| `reports/verification_report.json` | Source vs destination count comparison |
| `logs/migration.log` | Full timestamped log of all operations |

The CSV and the JSON `issues` records share the same 13 columns, in this order:

```
source_iid, dest_iid, title, state, author, fidelity,
created_at, closed_at, labels, notes, milestone, status, error
```

`fidelity` is `full` or `admin_attributed`. `status` is one of `migrated`,
`already_present` (an issue adopted at its target IID), `would_migrate`
(dry-run), `failed`, or `notes_failed`. `closed_at` is the **source** close
timestamp, retained here for audit because the destination issue cannot carry it.

Dry-run populates the report rows too, so you can review the intended plan as a
spreadsheet before writing anything.

---

## Checkpoint / Resume

`checkpoints/migration_state.jsonl` is the append-only write path — one line per
issue, so recording progress is O(1) instead of rewriting the whole state file.
`checkpoints/migration_state.json` is a periodic compacted snapshot of it. On
startup both are read, with later journal lines winning.

On any re-run, already-completed issues are skipped, and labels, milestones,
links, and board lists are checked for existence before creation — so the whole
pipeline is idempotent. An issue that already occupies its target IID (because a
prior run died before its checkpoint line was durable) is **adopted**, not
duplicated and not failed with a 409; the note pass then appends only the notes
it is missing.

```bash
python gitlab_migration.py --env-source .env --full-run --no-resume
```

> ⚠ Only use `--no-resume` against a genuinely empty destination. Against a
> populated project it re-attempts every issue; those issues get adopted rather
> than duplicated, but the run wastes an API round-trip per issue and the report
> fills with `already_present` rows.

---

## Environment Variable Reference

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SOURCE_GITLAB_URL` | No | `https://rcode.rockefeller.edu` | Source GitLab base URL |
| `DEST_GITLAB_URL` | No | `https://gitlab.com` | Destination GitLab base URL |
| `SOURCE_GITLAB_TOKEN` | **Yes** | — | Admin PAT for source reads |
| `DEST_GITLAB_TOKEN` | **Yes** | — | Admin PAT for destination writes and unmapped-author fallback |
| `MAX_CONCURRENT_REQUESTS` | No | `10` | Async worker concurrency limit (does not apply to issue creation, which is sequential) |
| `REQUEST_TIMEOUT` | No | `60` | HTTP timeout in seconds |
| `RETRY_COUNT` | No | `5` | Max retries on 429/5xx, timeouts, and connection errors |
| `RETRY_BACKOFF` | No | `2.0` | Base backoff in seconds; `Retry-After` wins when present |
| `PAT_JHERRERA15` | **Yes** | — | `jherrera15`'s PAT — authors that user's issues and notes |
| `PAT_JBANFELDER` | **Yes** | — | `jbanfelder`'s PAT — authors that user's issues and notes |
| `PAT_DEST2` | **Yes** | — | `dest2`'s PAT — authors that user's issues and notes |
| `PAT_DEST4` | **Yes** | — | `dest4`'s PAT — authors that user's issues and notes |
| `PAT_DEST5` | **Yes** | — | `dest5`'s PAT — authors that user's issues and notes |

Each `PAT_*` is the writing credential for that user's content, and each is
required *because that user is listed in `mappings.yml`*. `--sanity-check` fails
if any is missing, is still a `<token>` placeholder, authenticates as the wrong
user, or does not hold Owner on the destination project. To migrate without a
given user's PAT, remove them from `mappings.yml`; their content then falls back
to admin attribution deliberately rather than by accident.

Missing `SOURCE_GITLAB_TOKEN` or `DEST_GITLAB_TOKEN` is a hard startup failure
(exit `1`) in every mode.

---

## Step-by-Step Runbook

See **[RUNBOOK.md](RUNBOOK.md)** for the complete procedure: pre-flight
checklist and Owner grants, sanity check, dry run, pilot, full run, verification,
troubleshooting, and post-migration cleanup including the role downgrade.

Working on this repo with an AI agent? Start with **[AGENTS.md](AGENTS.md)**.
