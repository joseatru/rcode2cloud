# Migration Runbook: scicomp-kanban
## rcode.rockefeller.edu → gitlab.com

**Project:** `scicomp-kanban` (Issue Board Migration)
**Team:** CCE — Rockefeller University IT
**Contact:** cce@rockefeller.edu

---

## Overview

This runbook covers the full end-to-end migration of the `scicomp-kanban`
GitLab project's issue board from the on-prem rcode instance to gitlab.com.

| | Project |
|---|---|
| **Source** | `rcode.rockefeller.edu` — ID `641` — `jbanfelder/scicomp-kanban` |
| **Destination** | `gitlab.com` — ID `83486174` — `rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration` |

**Total estimated time:** 3–4 hours (mostly unattended)
**Risk level:** Low — source project is read-only; destination project is empty

The migration is driven by a single command, `python gitlab_migration.py`, plus
two configuration inputs:

| Input | Purpose | Committed to git? |
|-------|---------|-------------------|
| `mappings.yml` | Project IDs, paths, and the source→destination user map | Yes |
| `.env` | All tokens (`SOURCE_GITLAB_TOKEN`, `DEST_GITLAB_TOKEN`, `PAT_*`) | **Never** |

There is no `config.yml` and no `secrets.yml`. Tokens live only in the
environment, either exported in your shell or loaded from a `.env` file with
`--env-source`.

### Code layout

You do not need this to run the migration, only to read or change it.
`gitlab_migration.py` is the entry point; the pipeline lives in the
`migration/` package alongside it, one module per phase:

```
v1/
├── gitlab_migration.py   # entry point — run this
├── config.py             # CLI parser, .env loader, runtime config
└── migration/
    ├── http_client.py    # TLS, session, HTTP verbs, retry/backoff
    ├── state.py          # checkpoint journal + snapshots, caches
    ├── users.py          # authorship / token resolution
    ├── fidelity.py       # read-back fidelity guard
    ├── sanity.py         # --sanity-check
    ├── labels.py  milestones.py  issues.py  attachments.py
    ├── notes.py   links.py       boards.py  bump.py
    ├── verification.py   # --validation-only
    ├── reporting.py      # report rows and JSON/CSV output
    ├── orchestrator.py   # phase order
    └── cli.py            # main(), exit codes
```

Every command in this runbook is unaffected by that layout — it is the same
code, arranged so a single phase can be read without scrolling past the other
twelve.

---

## What Gets Migrated

| Migrated | Preserved | Not preserved / Excluded |
|----------|-----------|--------------------------|
| Issues and descriptions | Original issue number (`iid`) | `closed_at` — no settable API parameter; report only |
| Assignees | Original author | Merge requests |
| Labels | Original `created_at` and `updated_at` | Wikis |
| Due dates | Source project links | Repository content and CI configuration |
| Milestones | | Epics and iterations |
| Comments / notes | | Cross-project issue links |
| Issue state (open/closed) | | Pipelines |
| Attachments referenced in issues and notes | | |
| Issue links (same-project) | | |
| Boards and board lists (Kanban columns) | | |

A closed issue arrives on the destination as closed, but its close timestamp is
the *migration* time, not the original. The GitLab issues API exposes no
`closed_at` parameter, so there is nothing to send. The source value is recorded
in `reports/migration_report.json` and the `closed_at` column of
`reports/migration_report.csv` for audit.

---

## Fidelity Model — Read This Before Collecting Tokens

The migration preserves original IIDs, original authors, and original
timestamps. That capability is the reason the pre-flight permission
requirement is stricter than you might expect.

GitLab only honors the `iid` and `created_at` parameters on `POST .../issues`
and `POST .../notes` when the calling token belongs to an **administrator or a
project owner**. gitlab.com SaaS gives us neither an instance administrator nor
`sudo` impersonation. Therefore:

1. Each user's own PAT must create that user's own issues and notes — that is
   the only way the destination shows the correct author.
2. Every one of those PATs must hold **Owner** on the destination project for
   the duration of the migration window, or GitLab silently drops `iid` and
   `created_at` and you lose IID alignment and timestamps.

Consequences to plan around:

- All 5 mapped users need the **Owner** role on the destination project while
  the migration runs. Step 9 walks that back down afterward.
- A source author with **no `mappings.yml` entry** falls back to the admin
  `DEST_GITLAB_TOKEN`. The item is still created with the original IID and
  timestamp, and a markdown attribution header records the real author. These
  rows are marked `fidelity=admin_attributed` in the reports. This is the
  intended, supported fallback.
- A user who **is** in `mappings.yml` but whose `PAT_*` is missing, still a
  `<token>` placeholder, or not an Owner is a **hard `--sanity-check` failure**,
  not a downgrade. Silently attributing a mapped user's content to the admin
  account would defeat the reason their PAT was collected, and authorship cannot
  be corrected after creation. If you cannot get a user's PAT, remove them from
  `mappings.yml` — that converts the situation into the previous bullet,
  deliberately.
- Because IIDs must be assigned monotonically, issue creation is **sequential
  in ascending IID order**. Notes remain concurrent across issues. This is why
  the full run takes hours rather than minutes.

`--sanity-check` probes each mapped user's role on the destination project and
fails loudly if any PAT is not an Owner. Run it and read the output — that is
cheaper than discovering the problem three hours into a full run.

### The read-back fidelity guard — your backstop if Owner is wrong

`--sanity-check` is a pre-flight. A role can be revoked between the pre-flight
and the run, and the pre-flight can simply be skipped. The guard catches that
at write time.

The reason it has to exist: **GitLab does not return an error when it ignores
`iid` and `created_at`.** A rejected `iid` comes back as HTTP 201 with a
*different* `iid` in the response body. At the HTTP layer, silent rejection and
success look identical — which is why "timestamps show just now" was
historically discovered hours after the fact.

Every issue this tool creates is therefore compared back against what was
requested:

| Field | Comparison |
|-------|------------|
| `iid` | Exact match |
| `created_at` | Equal instants within 2 seconds, compared as parsed timestamps so a different precision or UTC offset is not a false alarm |
| `author.username` | Must equal the mapped `dest_username` for `fidelity=full`. Not checked for `admin_attributed` — that fallback is expected |

The create response body is checked first (no extra API calls). The **first
successfully created issue of each run** is additionally re-fetched with
`GET /projects/:id/issues/:iid`, in case the create response echoed the request
optimistically; after that passes you will see:

```
  ✓  Fidelity guard confirmed on re-fetch of #1: iid, created_at, and author were honoured. Remaining issues are checked from the create response.
```

On a mismatch the run **aborts at that issue** with exit code `1`:

```
============================================================
  MIGRATION ABORTED — READ-BACK FIDELITY FAILURE
============================================================
Read-back fidelity check FAILED on source issue #1 (observed in the create response).

    iid:
      requested: 1
      returned:  152
...
```

It does not continue to the next issue, and it is not written to
`failures.json` as a per-issue failure — retrying cannot fix it and every later
write would be wrong the same way. Fix the Owner grant (Pre-Flight §3), re-run
`--sanity-check`, delete whatever this run already wrote, clear **both**
checkpoint files, and start again.

The guard is inert under `--dry-run` and does not fire on an **adopted** issue,
because an issue already occupying its target IID is a resume, not a fidelity
failure — see Troubleshooting → "An issue already exists at the target IID".

`--no-fidelity-guard` turns it off. **Do not use it for this migration.**
Without it a full run can complete cleanly having scrambled every IID.

---

## Pre-Flight Checklist

Complete all items before starting any migration steps.

### 1. Access & Permissions
- [ ] You have admin API access to `rcode.rockefeller.edu`
- [ ] You have **Owner** access to the destination project on gitlab.com
      (`rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration`)
- [ ] Destination project has **zero issues** (critical for IID preservation)
- [ ] Network connectivity from the migration machine to both GitLab instances

### 2. Token Collection

| Token | Env variable | Who creates it | Where |
|-------|--------------|----------------|-------|
| Source admin | `SOURCE_GITLAB_TOKEN` | You | rcode → User Settings → Access Tokens (`api`, `read_api`, `read_repository`) |
| Destination admin | `DEST_GITLAB_TOKEN` | You | gitlab.com → User Settings → Access Tokens (`api` scope; Owner on dest project) |
| PAT for `jherrera15` | `PAT_JHERRERA15` | Jose Herrera | gitlab.com → User Settings → Access Tokens (`api` scope) |
| PAT for `jbanfelder` | `PAT_JBANFELDER` | Jason Banfelder | gitlab.com → User Settings → Access Tokens (`api` scope) |
| PAT for `dest2` | `PAT_DEST2` | User 3 | gitlab.com → User Settings → Access Tokens (`api` scope) |
| PAT for `dest4` | `PAT_DEST4` | User 4 | gitlab.com → User Settings → Access Tokens (`api` scope) |
| PAT for `dest5` | `PAT_DEST5` | User 5 | gitlab.com → User Settings → Access Tokens (`api` scope) |

> **Security:** Collect PATs via a secure channel (1Password share, encrypted
> email, or a temporary GitLab CI/CD variable). Never send plain text in Slack
> or email. Ask each user to name the token `scicomp-migration` so Step 9 is
> unambiguous.

### 3. Destination project membership — Owner role required

All 5 users must be **Owner** on the destination project on gitlab.com before
the migration starts. Reporter and Maintainer are *not* sufficient: without
Owner, GitLab ignores `iid` and `created_at` and the migration loses IID
alignment and original timestamps.

- [ ] `jherrera15` — Owner
- [ ] `jbanfelder` — Owner
- [ ] `dest2` — Owner
- [ ] `dest4` — Owner
- [ ] `dest5` — Owner

Set via **Project → Manage → Members → Role**, or:

```bash
curl --request PUT \
  --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
  --data "access_level=50" \
  "https://gitlab.com/api/v4/projects/83486174/members/<user_id>"
```

Access levels: `20` Reporter, `30` Developer, `40` Maintainer, `50` Owner.

> This is a deliberate, temporary elevation for the migration window only.
> Step 9 downgrades every user back to their intended role.

### 4. Fill in `mappings.yml`
- [ ] Source and destination `project_id` values are correct
- [ ] All 5 users listed under `users:` with `dest_username` and `pat_env`

| Source username | Destination username | PAT env var |
|-----------------|----------------------|-------------|
| `jherrera` | `jherrera15` | `PAT_JHERRERA15` |
| `jbanfelder` | `jbanfelder` | `PAT_JBANFELDER` |
| `source_username_2` | `dest2` | `PAT_DEST2` |
| `source_username_4` | `dest4` | `PAT_DEST4` |
| `source_username_5` | `dest5` | `PAT_DEST5` |

> Replace the `source_username_*` placeholders with the real rcode usernames
> before running. An author absent from this map falls back to admin attribution;
> an author present in it without a working Owner PAT fails `--sanity-check`.

### 5. Fill in `.env`
- [ ] `cp .env.example .env`
- [ ] `SOURCE_GITLAB_TOKEN` set to a real token (no `<token>` placeholders)
- [ ] `DEST_GITLAB_TOKEN` set to a real token
- [ ] All five `PAT_*` values set to real tokens
- [ ] `.env` is covered by `.gitignore` and is **not** tracked by git

`.gitignore` lives at the **repository root** (`gitlab-migration/.gitignore`),
one level above this directory. Verify from the repo root:

```bash
cd ..                                   # gitlab-migration/
git check-ignore -v v1/.env             # should print: .gitignore:4:.env  v1/.env
git ls-files --error-unmatch v1/.env 2>/dev/null && echo "TRACKED — fix this now"
cd v1
```

The script rejects an unsubstituted `<token>` placeholder for either admin token
and exits `1` if a required token is missing, so a half-filled `.env` fails
immediately rather than mid-run.

A `PAT_*` left as `<token>` for a user listed in `mappings.yml` is **also a
failure**, reported by `--sanity-check` in Step 1 rather than at startup. It is
not treated as "unset with a graceful fallback": that would silently downgrade a
user you explicitly collected a PAT for, and authorship cannot be fixed after
creation. Read the Step 1 output line by line.

---

## Environment Setup

```bash
# On your migration machine (Linux/macOS recommended)
cd "gitlab-migration/v1"

# Create and activate a Python virtualenv
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Confirm the install and core logic without needing any tokens
python verify_offline.py

# Create your token file from the template and fill it in
cp .env.example .env
$EDITOR .env
```

---

## Runtime Modes

Exactly one mode flag is **required**. A bare `python gitlab_migration.py` is
rejected with a usage error — it will not silently start a real migration.

| Flag | What it does | Writes to destination? |
|------|--------------|------------------------|
| `--sanity-check` | Validates tokens, project access, user mappings, and Owner roles | No |
| `--dry-run` | Simulates the migration and reports what would happen | No |
| `--test-run` | Pilot: migrates the first 50 issues by ascending IID through the full pipeline | Yes |
| `--validation-only` | Compares source vs destination counts | No |
| `--full-run` | Migrates everything, resuming from the checkpoint | Yes |

Supporting flags:

| Flag | Default | Purpose |
|------|---------|---------|
| `--env-source FILE` | — | Load tokens from a `.env` file |
| `--source-project N` | from `mappings.yml` | Override source project ID |
| `--dest-project N` | from `mappings.yml` | Override destination project ID |
| `--limit N` | none (`--test-run`: 50) | Cap the issues processed. Mode-dependent — see Step 7 |
| `--resume` / `--no-resume` | `--resume` | Honor or ignore the checkpoint |
| `--no-fidelity-guard` | guard **on** | Disable the read-back fidelity guard. Not recommended — see Fidelity Model above |
| `--mappings FILE` | `mappings.yml` | Path to the mappings file |
| `-v`, `--verbose` | off | Emit per-item debug logging |
| `--ca-bundle PATH` | — | Custom CA bundle for TLS verification |
| `--insecure` | off | Disable TLS verification (last resort) |

`--ca-bundle` and `--insecure` are **mutually exclusive** — passing both exits
`1` with an explicit error rather than silently preferring one.

`--limit` behaves differently per mode: under `--test-run`/`--dry-run` it is the
first N issues by IID (stable across resumes), and under `--full-run` it is the
next N *unmigrated* issues (a forward-moving window). Step 7 covers this.

Every example below assumes `--env-source .env`. Drop it if you exported the
variables into your shell instead.

---

## Step 1 — Sanity Check (5 minutes)

Run this first, every time, before any migration work.

```bash
python gitlab_migration.py --env-source .env --sanity-check
```

**Expected output** (this is the real log format — four numbered checks, not
three):

```
============================================================
  SANITY CHECK MODE
============================================================
[1/4] Checking source project (ID: 641)...
  ✓  Source: jbanfelder/scicomp-kanban
[2/4] Checking destination project (ID: 83486174)...
  ✓  Destination: rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration
  ✓  DEST_GITLAB_TOKEN authenticates as jherrera15 with Owner
[3/4] Checking user mappings, PATs, and Owner roles...
  ✓  jherrera → jherrera15 (ID: 1234567)
  ✓  PAT for jherrera15 valid (authenticated as: jherrera15)
  ✓  jherrera15 holds Owner on destination
  ✓  jbanfelder → jbanfelder (ID: 2345678)
  ✓  PAT for jbanfelder valid (authenticated as: jbanfelder)
  ✓  jbanfelder holds Owner on destination
  ... (same three lines per remaining mapped user)
[4/4] Checking destination issue state...
  ✓  Destination has no issues — IID range is clear
============================================================
  SANITY CHECK: PASSED ✓
============================================================
```

Each line is also written to `logs/migration.log`, prefixed with a timestamp and
level by the logger.

Check `[2/4]` covers both destination project access and the `DEST_GITLAB_TOKEN`
identity's own Owner role. Check `[3/4]` emits three lines per mapped user: the
username resolution, the PAT identity, and the Owner probe. Check `[4/4]` warns
with `⚠` instead of `✓` if the destination already holds issues — that is a
warning, not a failure, because adopting existing issues is supported.

The command exits `0` when every check passed and `1` if any check failed, so it
is safe to gate a shell script on it.

**If you see any ✗:** fix it before proceeding. A `⚠` on check `[4/4]` is
informational.

| Symptom | Cause | Fix |
|---------|-------|-----|
| `Source project check FAILED` | Bad or wrong-scope source token | Regenerate with `api`, `read_api`, `read_repository` |
| `DEST_GITLAB_TOKEN (...) holds <role>, not Owner` | Your own admin token is not an Owner | Raise to Owner (Pre-Flight §3) |
| `Destination user NOT found` | Wrong `dest_username` in `mappings.yml` | Correct the mapping |
| `PAT_X is not set for <user>` | A user in `mappings.yml` has no PAT, or it is still `<token>` | Supply the PAT, or remove that user from `mappings.yml` |
| `PAT ... authenticates as <other>` | PAT pasted into the wrong `PAT_*` variable | Swap the values |
| `<user> is NOT a member of the destination project` | User was never added | Add them, then set Owner (Pre-Flight §3) |
| `<user> holds <role>, not Owner` | Role too low on destination project | Raise to Owner (Pre-Flight §3) |
| `Required token(s) not set` (exit `1`, before any check runs) | `SOURCE_GITLAB_TOKEN`/`DEST_GITLAB_TOKEN` missing or still `<token>` | Fill in `.env` |
| SSL / certificate error | rcode uses an internal CA | See Troubleshooting → SSL |

---

## Step 2 — Dry Run (10 minutes)

Preview every action without touching the destination.

```bash
python gitlab_migration.py --env-source .env --dry-run --limit 50 -v 2>&1 | tee dry_run.log
```

Then re-run without `--limit` for the complete picture:

```bash
python gitlab_migration.py --env-source .env --dry-run 2>&1 | tee dry_run_full.log
```

Review `dry_run.log` and `reports/migration_report.csv`. You should see:

- Reads against the source succeed (these are real API calls — the source is
  read-only, so this is safe)
- Zero writes to the destination
- The IIDs the script intends to create, in ascending order
- The token each issue and note would be authored with
- A `fidelity` column showing `full` for mapped authors and
  `admin_attributed` for unmapped ones

If the `fidelity` column shows `admin_attributed` for an author you expected to
map, that author is missing from `mappings.yml` — or is in it but their `PAT_*`
is unset, which `--sanity-check` in Step 1 should already have failed on.

---

## Step 3 — Pilot: First 50 Issues (20–30 minutes)

Run the full pipeline against a scratch project first, not the real
destination. Create a throwaway project on gitlab.com, add yourself as Owner,
and point the run at it with `--dest-project`:

```bash
python gitlab_migration.py --env-source .env --test-run --dest-project <scratch_id>
```

### After the pilot — verify manually

Open the scratch project on gitlab.com and check:

- [ ] Issue `#1` exists with the correct title, description, and **author**
- [ ] Issue `#50` exists — and its number is `50`, not `50`-shifted
- [ ] Spot check 3–5 issues: notes have the correct authors and the original
      timestamps ("created 3 years ago", not "created just now")
- [ ] An issue that references an attachment renders the image or file inline
- [ ] Any issue links inside the 1–50 range are present
- [ ] The board columns match the source board
- [ ] `reports/migration_report.csv` shows `status=migrated` for all 50 rows
      (`already_present` means the issue was adopted rather than created — see
      Troubleshooting if you did not expect that)

If anything looks wrong, investigate before proceeding. Do not carry a bad
pilot into the real destination.

### Cleaning up the pilot

Because the pilot ran against a scratch project, cleanup is simply deleting
that project. That is the recommended path — it also resets the IID counter for
free.

If you instead piloted against the real destination `83486174`, you must clear
it completely before the full run, since IIDs cannot be reused:

```bash
# Delete the 50 pilot issues
for i in $(seq 1 50); do
  curl --silent --request DELETE \
    --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
    "https://gitlab.com/api/v4/projects/83486174/issues/$i"
done

# Clear BOTH checkpoint files so the full run re-creates them.
# The .jsonl journal is the write path; deleting only the .json snapshot
# leaves the journal to repopulate the checkpoint on the next start.
rm -f checkpoints/migration_state.json checkpoints/migration_state.jsonl
```

Deleting an issue does **not** roll the project's internal IID counter back.
The script's IID-bump step (Step 4) handles the counter. Note also that if you
leave pilot issues in place, the full run will **adopt** them rather than fail —
see Troubleshooting → "An issue already exists at the target IID" for why that is
only safe when the issue at that IID really is the corresponding source issue.

---

## Step 4 — Full Migration (2–3 hours, unattended)

Run the complete migration. The script is resumable: every phase checks whether
an object already exists before creating it, so re-running the same command is
safe and picks up where it left off.

```bash
# Run in a screen/tmux session so it survives SSH disconnects
screen -S gl-migration
python gitlab_migration.py --env-source .env --full-run 2>&1 | tee -a full_run.log
# Detach: Ctrl+A, D  |  Reattach: screen -r gl-migration
```

### What happens, in order

| Phase | Behavior |
|-------|----------|
| Labels | Creates any label missing on the destination |
| Milestones | Creates missing milestones; maps source IID → destination ID |
| Issues | Sequential, ascending IID; authored by the mapped user's PAT; every create read back and verified |
| Notes | Per-issue, concurrent across issues; original author and timestamp |
| Attachments | Downloaded from source, re-uploaded, markdown links rewritten |
| Issue links | Recreated after all issues exist; same-project only |
| Boards | Boards and lists recreated in `position` order |
| IID bump | Creates and deletes a throwaway issue at `max_iid + 1` |
| Reports | JSON, CSV, and failures written to `reports/` |
| Verification | Source vs destination counts compared |

### Progress monitoring

In a second terminal:

```bash
tail -f logs/migration.log
```

Live progress counters:

```bash
# Issues recorded as done so far — the .jsonl journal is the durable write path
# and is updated per issue; the .json snapshot only lands at phase boundaries.
wc -l < checkpoints/migration_state.jsonl

# Issues actually present on the destination
curl -sI --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/83486174/issues?scope=all&per_page=1" | grep -i "x-total:"
```

The run exits `0` only if it completed with zero recorded failures. Exit `1`
means `reports/failures.json` has content worth reading. Ctrl-C exits `130` and
leaves the journal intact for a resume.

---

## Step 5 — Resume After Interruption

If the migration stops for any reason — network drop, error, machine sleep —
re-run the exact same command:

```bash
python gitlab_migration.py --env-source .env --full-run
```

`--resume` is the default. The script will:

1. Skip every issue already recorded as complete in the checkpoint
   (`checkpoints/migration_state.jsonl` plus its `.json` snapshot)
2. Re-attempt issues that were created but whose note thread never finished,
   appending only the notes that are missing
3. Skip labels, milestones, links, and board lists that already exist on the
   destination, because each phase checks before it creates
4. Adopt, rather than duplicate, any issue that already sits at its target IID
   because the previous run died before journaling it

No data is duplicated. There is no phase-level CLI and nothing to reset — the
existence checks make the whole pipeline idempotent.

> **Never** pass `--no-resume` against a populated destination. It ignores the
> checkpoint and re-attempts every issue. Those issues are adopted rather than
> duplicated, so it is not destructive, but it wastes a round-trip per issue and
> fills the report with `already_present` rows. `--no-resume` is only for
> starting fresh against a genuinely empty project.

---

## Step 6 — Re-running After a Fix

There is no `--phase` or `--reset-phase`. To re-do work after fixing a
configuration problem, correct the input and re-run the same `--full-run`
command:

| Problem | Fix | Then |
|---------|-----|------|
| A user's PAT was wrong, so their notes came in as admin | Correct the `PAT_*` value in `.env` | Delete the affected destination issues, remove their entries from `checkpoints/migration_state.json`, re-run `--full-run` |
| A user was missing from `mappings.yml` | Add the mapping | Same as above for the affected issues |
| Labels or milestones were incomplete | Nothing — they are created on demand | Re-run `--full-run`; existing ones are skipped |
| Links or board columns are missing | Nothing | Re-run `--full-run`; the link and board phases re-check and fill gaps |

Authorship is fixed at creation time and cannot be edited afterward via the
API. Correcting an author therefore always means deleting and re-creating that
issue, which also means its IID becomes unavailable until the counter is dealt
with. Get authorship right in the pilot, not in the full run.

---

## Step 7 — Migrating in Windows

The script has no IID-range flags. To split the work across scheduled windows,
use `--limit` with `--full-run`. Under `--full-run` the limit is applied **after**
checkpoint filtering, so it selects the next N issues that are not yet fully
migrated — each run therefore advances through the backlog:

```bash
# Tonight: the first 2000 outstanding issues
python gitlab_migration.py --env-source .env --full-run --limit 2000

# Tomorrow: the next 2000 outstanding issues
python gitlab_migration.py --env-source .env --full-run --limit 2000
```

Each run logs which window it selected:

```
Limit applied after checkpoint filtering: 2000 of 4137 outstanding issue(s) this window (IIDs 2001–4000).
```

When nothing is left it logs `nothing outstanding — every source issue is
already migrated` and exits `0` without writes, so an over-scheduled window is
harmless.

"Outstanding" means no checkpoint record, or a record whose notes and final state
never completed. An issue interrupted mid-notes is therefore picked up by the
next window rather than stranded.

> **`--limit` means something different under `--test-run` and `--dry-run`.**
> There it is applied *before* checkpoint filtering — the first N issues by
> ascending IID, the same set every run. That is deliberate: a pilot has to be a
> stable, comparable sample, so a resumed `--test-run --limit 50` re-examines
> issues 1–50 rather than sliding forward to 31–80. Do not use `--test-run` to
> walk a backlog; use `--full-run --limit N`.

---

## Step 8 — Post-Migration Verification

Run the built-in comparison first:

```bash
python gitlab_migration.py --env-source .env --validation-only
```

This compares source and destination counts for total issues, open issues,
closed issues, labels, and milestones, writes
`reports/verification_report.json`, and exits non-zero on any mismatch.

### Independent count check

```bash
# Count on source
curl -sI --header "PRIVATE-TOKEN: $SOURCE_GITLAB_TOKEN" \
  "https://rcode.rockefeller.edu/api/v4/projects/641/issues?scope=all&per_page=1" \
  | grep -i "x-total:"

# Count on destination
curl -sI --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/83486174/issues?scope=all&per_page=1" \
  | grep -i "x-total:"
```

Both counts should match.

### Fidelity check

`--validation-only` compares counts, not authorship. Confirm fidelity from the
report and from the UI:

```bash
# How many items were created by the real author vs. attributed to admin
python -c "
import json,collections
r=json.load(open('reports/migration_report.json'))
print(collections.Counter(i.get('fidelity') for i in r['issues']))
"
```

A nonzero `admin_attributed` count is expected only for source authors who are
intentionally absent from `mappings.yml`. If a user *listed* in `mappings.yml`
shows up as `admin_attributed`, their PAT was wrong and `--sanity-check` was
skipped.

### Progress counters

```bash
# Issues recorded as done so far (journal is the durable write path)
wc -l < checkpoints/migration_state.jsonl
```

### Spot checks (manual, in the GitLab UI)

- [ ] Issue `#1` — correct title, description, **author**, open/closed state
- [ ] Issue `#100` — correct assignees and labels
- [ ] Issue `#500` — notes have the correct authors and original timestamps
- [ ] Issue `#1000` — issue links intact (if it had any)
- [ ] A closed issue shows as closed. Its close *date* will read as the migration
      time — the API has no settable `closed_at`. Confirm the original against the
      `closed_at` column of `reports/migration_report.csv` instead
- [ ] An issue with multiple labels — all labels present
- [ ] An issue with a milestone — milestone assigned correctly
- [ ] An issue with an attachment — the file downloads from the destination,
      not from rcode (check the link target contains `gitlab.com`)
- [ ] Kanban board — columns match the source board, in the same order
- [ ] The highest migrated IID matches the source's highest IID

### Verify the IID counter is clean

Create a test issue through the GitLab UI. It should receive
`max_migrated_iid + 1`, not a 409 conflict. If you get a 409, the IID bump did
not complete — re-run `--full-run`, which re-attempts the bump, then delete the
test issue.

---

## Step 9 — Post-Migration Cleanup

Do all of this the same day the migration completes. The Owner grants from
Pre-Flight §3 are a standing risk until they are walked back.

### 9a. Downgrade roles

Every mapped user was elevated to Owner purely so GitLab would honor `iid` and
`created_at`. Return each one to their intended long-term role.

- [ ] `jherrera15` → intended role
- [ ] `jbanfelder` → intended role
- [ ] `dest2` → intended role
- [ ] `dest4` → intended role
- [ ] `dest5` → intended role

```bash
# Example: downgrade to Developer (access_level=30)
curl --request PUT \
  --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
  --data "access_level=30" \
  "https://gitlab.com/api/v4/projects/83486174/members/<user_id>"
```

Confirm the result:

```bash
curl -s --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
  "https://gitlab.com/api/v4/projects/83486174/members/all?per_page=100" \
  | python -c "import json,sys;[print(m['username'], m['access_level']) for m in json.load(sys.stdin)]"
```

Keep at least one Owner on the project — normally the CCE service account or
your own admin identity.

### 9b. Revoke tokens

- [ ] Ask all 5 users to revoke their migration PAT on gitlab.com
      (User Settings → Access Tokens → `scicomp-migration` → Revoke)
- [ ] Revoke your `SOURCE_GITLAB_TOKEN` if it was created solely for this migration
- [ ] Revoke your `DEST_GITLAB_TOKEN` if it was created solely for this migration

### 9c. Clean the migration machine

- [ ] Delete `.env` (`shred -u .env` where available, otherwise `rm .env`)
- [ ] Archive `reports/` and `logs/` somewhere durable — they are the migration
      record of what was created and what failed
- [ ] Delete `cache/` and `checkpoints/` once the reports are archived
- [ ] Confirm nothing sensitive was committed: `git log --stat -- v1/.env`

### 9d. Notify stakeholders

```
Subject: scicomp-kanban Issue Board Migration — Complete

Hi team,

The scicomp-kanban issue board has been migrated from rcode to GitLab.com.

New location:
https://gitlab.com/rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration

All issue numbers (#1, #2, ... #N) are preserved exactly.
Original authors and comment timestamps have been retained.
Attachments, issue links, and board columns were carried over.

One known limitation: for issues that were already closed, the
"closed" date on GitLab.com shows the migration date rather than
the original one. GitLab's API provides no way to set it. The
original close dates are preserved in our migration report.

Action required:
- Please revoke your migration PAT on gitlab.com:
  User Settings -> Access Tokens -> "scicomp-migration" -> Revoke
- Your project role has been returned to its normal level. If you
  cannot do something you expect to, contact us.

If you notice any missing issues or attribution errors, contact
cce@rockefeller.edu.

CCE Team
```

---

## Troubleshooting Reference

### Usage error on a bare invocation

**Symptom:** `error: one of the arguments --sanity-check --dry-run --test-run
--validation-only --full-run is required`, exit code `2`.
**Cause:** Working as designed. A mode flag is mandatory so that a mistyped
command can never start a real migration.
**Fix:** Pick a mode. Passing two mode flags is also exit `2`
(`argument --full-run: not allowed with argument --dry-run`).

### Exit codes

| Code | Meaning | Typical cause |
|------|---------|---------------|
| `0` | Success, zero recorded failures | Normal completion |
| `1` | Failure needing a human | A sanity/verification check failed, at least one issue or note thread failed, the read-back fidelity guard aborted the run, a required token is missing or still `<token>`, `mappings.yml` is missing/unparseable/schema-invalid, `--ca-bundle` was combined with `--insecure`, or the `--ca-bundle` file does not exist |
| `2` | Usage error from argparse | No mode flag, two mode flags, or an unknown flag |
| `130` | Interrupted with Ctrl-C | Progress is journaled; re-run the same command to resume |

Only `0` and `1` come from the migration itself. `2` is emitted by argparse
before any configuration is read, and `130` on `KeyboardInterrupt`.

### `--ca-bundle` and `--insecure` together

**Symptom:** `[ERROR] --ca-bundle and --insecure are mutually exclusive.`,
exit `1`.
**Cause:** The two flags contradict each other — one supplies a trust anchor, the
other discards verification. The script refuses rather than silently choosing.
**Fix:** Pass exactly one. Prefer `--ca-bundle`.

### An issue already exists at the target IID

**Symptom:** `↺  #1234 already exists on the destination as #1234; adopting it
instead of creating a duplicate.` and a `status=already_present` row in the CSV.

**Cause:** A previous run created that issue but died before its checkpoint line
was durable — a hard kill, a machine sleep, or an OOM between the `POST` and the
journal write. It also happens if you pass `--no-resume` against a populated
destination.

**Behavior:** This is handled, not an error. Before creating any issue the script
does a `GET /projects/:id/issues/:iid`. If something is already there it
**adopts** the existing issue: records it in the checkpoint, counts it as
`skipped`, and reports `status=already_present`. The note pass then measures how
many notes are already on the destination issue and appends only the missing
tail, so the thread is completed rather than duplicated. There is no 409 and no
duplicate issue.

The read-back fidelity guard does **not** fire on an adopted issue. Adoption
never calls `POST .../issues`, and the guard only runs on the create path, so an
adopted issue whose `created_at` or author differs from the source is a resume
condition rather than a fidelity failure. A *newly created* issue landing on an
unexpected IID is the opposite — that is a hard abort.

**When you still need to act:** adoption is blind to *content*. If the issue at
that IID is not the corresponding source issue — a leftover from an aborted pilot
against the real destination, or a manually created issue — the script will
attach the source issue's notes to the wrong issue. So:

1. List what already exists:
   ```bash
   curl -s --header "PRIVATE-TOKEN: $DEST_GITLAB_TOKEN" \
     "https://gitlab.com/api/v4/projects/83486174/issues?scope=all&per_page=100" \
     | python -c "import json,sys;print(sorted(i['iid'] for i in json.load(sys.stdin)))"
   ```
2. If those IIDs are correct migrations, do nothing — adoption or the checkpoint
   already covers them.
3. If they are unrelated leftovers, delete them and clear
   `checkpoints/migration_state.json` **and** `checkpoints/migration_state.jsonl`
   before re-running. Deleting only the `.json` snapshot leaves the journal to
   repopulate the checkpoint on the next start.

Deleting an issue does not lower the project's IID counter, so an IID consumed by
a deleted issue cannot be reclaimed. This is exactly why the pilot belongs on a
scratch project.

A genuine 409 from the create call is still possible — for instance if the
project's internal IID counter has been pushed past the IID being requested — and
is recorded in `reports/failures.json` for that issue while the run continues.

### Issues or notes created with the wrong author (all show as admin)

**Cause:** The `PAT_*` variable for that user is missing, revoked, or mistyped,
so the script fell back to `DEST_GITLAB_TOKEN`. This should be impossible if you
ran `--sanity-check` first — it fails on exactly this condition. Reaching it
means the pre-flight was skipped; the run warns loudly per affected user
(`Authorship: <user> is mapped but PAT_X is unset`) but does not abort mid-flight.
**Fix:** Correct the value in `.env`, confirm with `--sanity-check`, then delete
and re-create the affected issues (authorship is immutable after creation).

### Timestamps show "just now" instead of the original date

**Cause:** The writing token is not an Owner on the destination project. GitLab
accepts the request but silently ignores `created_at` and `iid` for
non-owner/non-admin callers.
**Fix:** Raise that user to Owner (Pre-Flight §3), re-run `--sanity-check` to
confirm, then delete and re-create the affected issues.

**You should not reach this symptom by inspection.** The read-back fidelity
guard aborts the run at the *first* affected issue — see the next entry. Seeing
it across many issues means the guard was disabled with
`--no-fidelity-guard`, or the issues predate it.

### `MIGRATION ABORTED — READ-BACK FIDELITY FAILURE`

**Symptom:** The run stops at one issue, exit code `1`, with a block naming a
field, the value requested, and the value returned:

```
============================================================
  MIGRATION ABORTED — READ-BACK FIDELITY FAILURE
============================================================
Read-back fidelity check FAILED on source issue #1 (observed in the create response).

    iid:
      requested: 1
      returned:  152

  Most likely cause: the PAT that wrote this issue (jherrera's own PAT) does not hold
  Owner on destination project 83486174. ...
```

**Cause:** The destination accepted the write (HTTP 201) but discarded what it
was sent. Almost always a missing Owner grant on the writing PAT — GitLab
honors `iid` and `created_at` only for an administrator or a project Owner and
returns no error otherwise.

**Why it aborts rather than recording a failure:** retrying cannot fix it, every
subsequent issue would be wrong in exactly the same way, and neither IIDs nor
authorship can be corrected after creation. Stopping at issue one costs
seconds; discovering it after 3 hours costs the whole destination project.

**Fix:**

1. Read which field mismatched. `iid` or `created_at` means the writing PAT
   lacks Owner. `author.username` means a mapped user's `PAT_*` holds the wrong
   person's token.
2. Grant Owner to every mapped user (Pre-Flight §3), or correct the `PAT_*`
   value in `.env`.
3. Re-run `--sanity-check` until it passes.
4. Delete the issues this run already wrote on the destination.
5. Clear **both** checkpoint files:
   ```bash
   rm -f checkpoints/migration_state.json checkpoints/migration_state.jsonl
   ```
6. Re-run.

Deleting an issue does not roll the IID counter back, which is the reason the
pilot belongs on a scratch project.

**Observed in an explicit re-fetch:** the first successful create of each run is
re-fetched to confirm the create response was not echoing optimistically. That
wording means the response body looked right and the stored object did not —
same cause, same fix.

**Do not work around this with `--no-fidelity-guard`.** The flag exists so the
check can be switched off deliberately, not so an abort can be silenced. Off,
the run completes with every IID scrambled and reports success.

### Assignees are missing

**Cause:** `assignee_ids` (multiple assignees) is a GitLab Premium feature. On
Free the API rejects it, and the script falls back to the single `assignee_id`.
**Effect:** Issues with several assignees keep only the first one. This is a
tier limitation, not a bug. Record it in the migration report and move on.

### Only one board came across

**Cause:** gitlab.com Free allows a single board per project.
**Effect:** The first source board is recreated with all of its lists;
additional boards are reported as **skipped**, not failed.

### SSL certificate errors against rcode

**Cause:** rcode presents a self-signed or internal-CA certificate.
**Fix:** Pass the CA bundle explicitly:

```bash
python gitlab_migration.py --env-source .env --sanity-check \
  --ca-bundle /path/to/rockefeller-ca.pem
```

> Do **not** rely on `REQUESTS_CA_BUNDLE`. That variable is honored by the
> `requests` library; this script uses `aiohttp`, which ignores it entirely.
> Setting it looks like it worked and changes nothing. `--ca-bundle` wires a
> real `ssl.SSLContext` into the aiohttp connector.

As an absolute last resort on a trusted network:

```bash
python gitlab_migration.py --env-source .env --sanity-check --insecure
```

`--insecure` disables certificate verification for the whole run. Use it to
confirm a diagnosis, then fix the trust store and drop the flag.

### Very slow migration

**Cause:** Usually expected. Issue creation is deliberately sequential
(concurrency 1) because IIDs must be assigned in ascending order. Notes,
labels, and milestones are concurrent.
**Check:** `grep -i "rate" logs/migration.log` for 429 handling. The script
honors the `Retry-After` header.
**Tuning:** Raise `MAX_CONCURRENT_REQUESTS` in `.env` to speed up the note and
attachment phases. It will not speed up issue creation. Lower it if you see
sustained 429s.

### Timeouts or connection resets mid-run

**Cause:** Transient network or GitLab-side issues.
**Fix:** Nothing to do — the retry layer covers timeouts, connector errors, and
disconnects. If the process itself dies, re-run the same `--full-run` command;
it resumes from the checkpoint.

### Reading progress from the log

```bash
grep -c "✓" logs/migration.log             # successful operations
grep -i "✗\|ERROR" logs/migration.log      # failures worth reading
python -m json.tool reports/failures.json  # structured failure detail
```

Add `-v` to any run for per-item debug output.

---

## Output Files

| Path | Contents |
|------|----------|
| `logs/migration.log` | Full timestamped log of every operation |
| `checkpoints/migration_state.jsonl` | Append-only journal — one line per issue, the durable write path |
| `checkpoints/migration_state.json` | Compacted snapshot of the journal, written at phase boundaries |
| `cache/user_id_cache.json` | Destination username → user ID cache |
| `cache/upload_map.json` | Source attachment path → destination markdown |
| `reports/migration_report.json` | `stats` block plus an `issues` array of per-issue records |
| `reports/migration_report.csv` | Same per-issue view, for spreadsheet review |
| `reports/failures.json` | Every failure with its error message |
| `reports/verification_report.json` | Source vs destination count comparison |

The CSV and the JSON `issues` records share the same 13 columns, in this order:

```
source_iid, dest_iid, title, state, author, fidelity,
created_at, closed_at, labels, notes, milestone, status, error
```

`status` is one of `migrated`, `already_present` (adopted at its target IID),
`would_migrate` (dry-run), `failed`, or `notes_failed`. `closed_at` is the
**source** close timestamp — the only place it survives.

---

## Key API References

| What | Endpoint | Note |
|------|----------|------|
| Create issue with IID and timestamp | `POST /projects/:id/issues` (`iid`, `created_at`) | Requires admin or **project owner** |
| Create note with timestamp | `POST /projects/:id/issues/:iid/notes` (`created_at`) | Requires admin or **project owner** |
| Upload attachment | `POST /projects/:id/uploads` | Returns the markdown to substitute |
| List / create issue links | `GET`/`POST /projects/:id/issues/:iid/links` | Same-project links only |
| List boards | `GET /projects/:id/boards` | One board per project on Free |
| Create board list | `POST /projects/:id/boards/:board_id/lists` | Needs a destination `label_id` |
| Check a member's role | `GET /projects/:id/members/all/:user_id`, falling back to `GET /projects/:id/members/:user_id` | `access_level` 50 = Owner. The `all` variant counts a role inherited from the parent group |
| Set a member's role | `PUT /projects/:id/members/:user_id` | `access_level=50` to grant Owner |
| Auth check | `GET /user` | Confirms which identity a PAT maps to |

> `order_by=iid` is **not** a valid value on the issues endpoint. GitLab accepts
> only `created_at`, `updated_at`, `priority`, `due_date`, `relative_position`,
> `label_priority`, `milestone_due`, `popularity`, `title`, and `weight`. The
> script fetches and then sorts by `iid` client-side.

> There is **no `closed_at` parameter** on `POST` or `PUT .../issues`. Closing a
> migrated issue stamps it with the current time. `created_at` and `updated_at`
> *are* settable (with Owner rights); `closed_at` is not settable at all.

---

*Runbook version: 2.0 — full-fidelity migration*
*CCE Team — cce@rockefeller.edu*
