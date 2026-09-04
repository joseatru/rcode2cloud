# AGENTS.md

Operating guidance for AI agents working in this directory. Read this before
changing code or running anything.

---

## What this project is

A single-purpose, one-shot migration tool. It copies the issue board of one
GitLab project from an on-prem instance to gitlab.com with **full fidelity** —
original issue numbers, original authors, original timestamps.

| | Instance | Project | ID |
|---|---|---|---|
| **Source** | `rcode.rockefeller.edu` (on-prem) | `jbanfelder/scicomp-kanban` | `641` |
| **Destination** | `gitlab.com` (SaaS, Free tier) | `rockefelleruniversity/it/infra/cce/rcode_forks/scicomp-kanban_cce_migration` | `83486174` |

Owner: CCE — Rockefeller University IT (`cce@rockefeller.edu`).

This is not a long-lived service. Optimize for correctness and auditability over
elegance or reuse. A wrong write to the destination is expensive because issue
numbers cannot be reclaimed once consumed.

### Scope

| Migrate | Preserve | Do not cover |
|---------|----------|--------------|
| Issues, descriptions, assignees, labels, due dates, milestones, notes, open/closed state, attachments, same-project issue links, boards and board lists | Original `iid`, original author, original `created_at` and `updated_at` (issues and notes), source project links | Merge requests, wikis, repository content, CI configuration, cross-project issue links, epics, iterations and other Premium/Ultimate-only objects |

Two things are migrated but **not** fully preserved:

- **`closed_at`** — the issues API has no settable parameter for it, so a closed
  issue records the migration time. The source value survives only in
  `reports/migration_report.{json,csv}`. Closed *state* is preserved; the close
  *date* is not.
- **Multiple assignees** — `assignee_ids` is Premium-only, so on Free only the
  first assignee is set.

The authoritative in-repo statement of this split is the `Not covered` block in
the `gitlab_migration.py` module docstring. Keep this table, the README features
table, and that docstring in agreement; if they drift, the docstring wins because
it sits next to the code.

`GitLab Project Migration Scrip 1.docx.pdf` in this directory is the
authoritative source for the project IDs/URLs and the original scope request. If
a doc and the PDF disagree on scope, the PDF wins — flag the discrepancy rather
than silently picking one.

---

## Files

| File | Role |
|------|------|
| `gitlab_migration.py` | Entry point. A thin shim over `migration.cli.main()` that re-exports the package's public names. Underscore, not hyphen. |
| `migration/` | The pipeline, one module per phase. See [Code layout](#code-layout--where-to-change-what) below. |
| `config.py` | CLI parser, `.env` loader, runtime config dict |
| `mappings.yml` | Project IDs and the source→destination user map. Committed. |
| `.env.example` | Token template. Committed. |
| `.env` | Real tokens. **Never** committed. |
| `requirements.txt` | `aiohttp`, `PyYAML` |
| `README.md` | Reference: features, flags, exit codes, env vars |
| `RUNBOOK.md` | The operational procedure an operator follows |
| `verify_offline.py` | Offline assertions over `_apply_limit()`, `_is_pending()`, `token_for_author()`, `validate_mappings()`, the report columns, and the read-back fidelity guard (driven against an in-memory fake HTTP layer). No network, no credentials. Run it after touching any of those. |
| `gitlab_migration_script_skeleton_template.py` | Abandoned early draft. Ignore it; do not extend it. |

### Code layout — where to change what

`gitlab_migration.py` was a single ~3,150-line module and got split along the
phase seams that already existed in it. It is still the entry point at the same
path with the same CLI; the phases moved into `migration/`.

Each phase is a **mixin** composed into `GitLabMigration` in
`migration/orchestrator.py`. That keeps one shared session, one checkpoint, and
one stats dict while letting a phase be read in isolation. `self.<anything>`
inside a phase module resolves against the composed object.

| Change this | Edit |
|-------------|------|
| Retry policy, backoff, `Retry-After`, TLS context, a new HTTP verb | `migration/http_client.py` |
| Checkpoint journal/snapshot format, atomic writes, cache files | `migration/state.py` |
| File locations | `migration/paths.py` |
| Log format or the logger name | `migration/logging_setup.py` |
| Timestamp parsing or comparison | `migration/timeutils.py` |
| `mappings.yml` schema rules | `migration/mappings.py` |
| Which PAT authors what, `fidelity=` resolution, dest user lookup | `migration/users.py` |
| The read-back fidelity guard | `migration/fidelity.py` |
| `--sanity-check` checks or their `[n/4]` output | `migration/sanity.py` |
| Label phase, label name→ID map | `migration/labels.py` |
| Milestone phase, milestone map | `migration/milestones.py` |
| `--limit` semantics, `_is_pending()`, issue create, IID adoption, `updated_at` restore | `migration/issues.py` |
| Attachment download/re-upload, markdown rewriting | `migration/attachments.py` |
| Note phase, `skip_existing` resume offset | `migration/notes.py` |
| Issue link phase, dedupe, cross-project skip | `migration/links.py` |
| Board / board-list phase | `migration/boards.py` |
| IID counter bump | `migration/bump.py` |
| `--validation-only`, IID set comparison | `migration/verification.py` |
| `REPORT_FIELDNAMES`, report rows, JSON/CSV/failures output | `migration/reporting.py` |
| Phase order, `GitLabMigration.__init__`, stats keys | `migration/orchestrator.py` |
| `main()`, exit codes, `--env-source` handling | `migration/cli.py` |
| Flags, help text, config dict keys | `config.py` (unchanged by the split) |

Two constraints on this layout:

- **`gitlab_migration.py` stays the entry point at its current path with an
  identical CLI.** Every command in `README.md` and `RUNBOOK.md` must keep
  working verbatim. Do not move it, rename it, or turn it into a console script.
- **Imports at the top of each module, no inline imports.** The dependency
  graph is acyclic — `paths` → `logging_setup` → everything else, phases →
  `orchestrator` → `cli` — so there is no circular-import excuse for one.

`.gitignore` lives at the **repository root** (`gitlab-migration/.gitignore`),
one level above this directory, not in `v1/`. Its `.env` rule still covers
`v1/.env` — confirm with `git check-ignore -v v1/.env` from the repo root.

Generated at runtime and gitignored: `checkpoints/`, `cache/`, `logs/`,
`reports/`.

---

## The five modes

Exactly one mode flag is **required**. A bare invocation is a usage error by
design, so that a mistyped command cannot start a real migration. Do not
"helpfully" restore a default mode.

| Flag | Writes? | Purpose |
|------|---------|---------|
| `--sanity-check` | No | Tokens, project access, user mappings, Owner roles |
| `--dry-run` | No | Full simulation with reports |
| `--test-run` | Yes | Pilot, first 50 issues by ascending IID |
| `--validation-only` | No | Source vs destination count comparison |
| `--full-run` | Yes | Everything, resuming from the checkpoint |

Supporting flags: `--env-source FILE`, `--source-project N`, `--dest-project N`,
`--limit N`, `--resume` (default) / `--no-resume`, `--no-fidelity-guard`,
`--mappings FILE`, `-v/--verbose`, `--ca-bundle PATH`, `--insecure`.

`--ca-bundle` and `--insecure` are **mutually exclusive** — `get_config()` exits
`1` if both are given rather than silently preferring one. Do not "fix" that into
a precedence rule.

### `--limit` is mode-dependent — do not unify it

| Mode | Applied | Selects |
|------|---------|---------|
| `--test-run`, `--dry-run` | **Before** checkpoint filtering | First N by ascending IID — the same set on every run |
| `--full-run` | **After** checkpoint filtering | Next N issues that are not yet fully migrated |

This asymmetry is deliberate and load-bearing. A pilot must be a stable,
comparable sample, so a resumed `--test-run --limit 50` has to re-examine issues
1–50 rather than sliding to 31–80. A scheduling window must make forward
progress, so `--full-run --limit 2000` has to skip what is done first. Both live
in `_apply_limit()`; read its docstring before touching it.

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success — the selected mode passed with zero recorded failures |
| `1` | Check or issue failure, missing/placeholder token, invalid `mappings.yml`, a read-back fidelity abort, or contradictory TLS flags |
| `2` | argparse usage error (no mode flag, or two mode flags) |
| `130` | `KeyboardInterrupt`; progress is journaled, re-run to resume |

`2` comes from argparse before any config is read. `1` is raised from
`get_config()`, `load_mappings()`, or `main()` when `run()` returns False.

### Mandatory progression

```
--sanity-check  →  --dry-run  →  --test-run (scratch project)  →  --full-run  →  --validation-only
```

Never skip forward. `--sanity-check` and `--dry-run` cost seconds; a bad
`--full-run` costs hours and may make IIDs unrecoverable. If asked to "just run
the migration," run the sanity check and dry run first and report their output.

Configuration always comes from `mappings.yml` plus a `.env` loaded via
`--env-source`. There is no `config.yml` and no `secrets.yml`; if you see those
names referenced anywhere, it is stale documentation from the abandoned draft.

---

## Fidelity and authorship model

The binding constraint, and the reason for most of the design:

> GitLab honors `iid` and `created_at` on `POST .../issues` and
> `POST .../notes` **only** for an administrator or a project **owner**.
> gitlab.com SaaS has no instance administrator and no `sudo` impersonation.

Therefore each item is written by a token that is simultaneously the intended
author *and* an Owner on the destination project:

| Author mapped with a usable PAT? | Written with | Result |
|---|---|---|
| Not in `mappings.yml` at all | `DEST_GITLAB_TOKEN` | `fidelity=admin_attributed` — original IID and timestamp, plus a markdown attribution header naming the real author. **Supported fallback.** |
| In `mappings.yml`, PAT present and Owner | That user's `PAT_*` | `fidelity=full` — real author, original IID, original timestamp |
| In `mappings.yml`, PAT missing / `<token>` / not Owner | — | **Hard `--sanity-check` failure.** Not a downgrade. |

Do not "improve" the third row into a graceful degradation. Silently writing a
mapped user's content as admin defeats the reason their PAT was collected, and
authorship is immutable after creation, so the damage is unrecoverable without
burning IIDs. If a PAT is genuinely unavailable, the correct action is to remove
that user from `mappings.yml`, which moves them into row one deliberately.

`token_for_author()` does still fall back at runtime rather than crashing
mid-migration, but it logs a `WARNING` per affected user. That is a safety net for
an operator who skipped the pre-flight, not a supported mode.

Two operational consequences:

1. All five mapped users hold **Owner** on project `83486174` for the migration
   window, then get downgraded (RUNBOOK Step 9). Reporter/Developer/Maintainer
   are insufficient — GitLab accepts the request and silently drops `iid` and
   `created_at`, which is the worst failure mode because it looks like success.
2. Issue creation is **sequential in ascending IID order** (concurrency 1),
   because IIDs are assigned monotonically. Notes are concurrent across issues,
   serial within one issue. Do not "optimize" issue creation into a
   `gather()` — that breaks IID alignment.

`--sanity-check` probes `GET /projects/:id/members/all/:user_id` per mapped user
(falling back to the direct-membership endpoint on a 404, so a group-inherited
role counts) and fails if `access_level != 50`.

### The read-back fidelity guard — do not weaken it

`--sanity-check` is a pre-flight and can be skipped or go stale. The guard is
the write-time backstop for the same failure, and it exists because **GitLab
does not error when it ignores `iid` and `created_at`**: a rejected `iid`
returns HTTP 201 with a *different* `iid` in the body. Silent rejection is
indistinguishable from success at the HTTP layer, so without a read-back check a
`--full-run` can exit `0` having renumbered every issue and re-dated every one.

Every created issue is compared against its request:

| Field | Comparison |
|-------|------------|
| `iid` | Exact |
| `created_at` | Parsed instants within `CREATED_AT_TOLERANCE_SECONDS` (2s). **Not** a string comparison — GitLab may echo a different precision or offset |
| `author.username` | The mapped `dest_username`, for `fidelity=full` only |

Response body first (zero extra API calls), then one confirming
`GET /projects/:id/issues/:iid` for the **first** successful create of the run,
after which response-body checking suffices. Any mismatch raises
`FidelityGuardError`, which propagates past `migrate_issue()`'s per-issue
handler and exits `1`.

Four properties are load-bearing. Do not "improve" any of them:

1. **It aborts; it does not record a failure.** Retrying cannot fix a permission
   problem, every later write would be wrong identically, and IIDs and
   authorship are unrecoverable once consumed. `migrate_issue()` re-raises
   `FidelityGuardError` explicitly before its generic `except Exception`.
2. **`admin_attributed` authorship never aborts.** That fallback is legitimate
   for a source author absent from `mappings.yml`. `_expected_dest_author()`
   returns `None` for it, so only a *mapped* user resolving to the wrong author
   fails. `iid` and `created_at` are still enforced for those writes.
3. **Adoption never aborts.** `_adopt_existing_issue()` is on the pre-create
   path and `migrate_issue()` returns straight after it, so an adopted issue
   never reaches the guard. That is the distinction: an issue already at the
   target IID is a resume, whereas a *newly created* issue landing on an
   unexpected IID is a hard failure. Keeping the guard on the create path only
   is what separates them — do not move it into a post-hoc sweep over all
   issues, which would conflate the two.
4. **It is inert in `--dry-run`.** Nothing is written, so there is nothing to
   read back.

`--no-fidelity-guard` exists as a documented escape hatch and logs a loud
`WARNING` when active. Never make it the default, and never reach for it to
silence an abort.

### User map

| Source username | Destination username | PAT env var |
|-----------------|----------------------|-------------|
| `jherrera` | `jherrera15` | `PAT_JHERRERA15` |
| `jbanfelder` | `jbanfelder` | `PAT_JBANFELDER` |
| `source_username_2` | `dest2` | `PAT_DEST2` |
| `source_username_4` | `dest4` | `PAT_DEST4` |
| `source_username_5` | `dest5` | `PAT_DEST5` |

The `source_username_*` entries are placeholders awaiting the real rcode
usernames. Do not invent values for them.

---

## Hard rules

1. **The source project is read-only.** Never issue a POST, PUT, PATCH, or
   DELETE against `rcode.rockefeller.edu`. Reads only, always.
2. **Never commit `.env`** or any real token. If you find `v1/.env` staged, run
   `git rm --cached v1/.env` from the repo root and confirm the root
   `.gitignore` covers it. Never paste a token value into a commit message, log,
   comment, or chat response.
3. **Never pass `--no-resume` against a populated destination.** It ignores the
   checkpoint and re-attempts every issue. Existing issues get adopted rather
   than duplicated, so it is not destructive, but it burns an API round-trip per
   issue and floods the report with `already_present` rows. It is only valid
   against a genuinely empty project.
4. **Never run `--full-run` without a passing `--sanity-check` and a reviewed
   `--dry-run` first.** Both in the same session, against the same config.
5. **Pilot on a scratch project**, via `--dest-project <scratch_id>`. Never
   pilot against `83486174`. Deleting an issue does not lower the project's IID
   counter, so a consumed IID is gone.
6. **Authorship is immutable after creation.** The API cannot change an issue's
   or note's author. Fixing attribution means delete-and-recreate, which burns
   the IID. Get the mapping right before writing.
7. **Do not weaken TLS by default.** `--insecure` exists for diagnosis only.
   Never make it the default or hardcode `verify=False` equivalents.
8. **Preserve exit-code semantics.** `0` success with zero recorded failures,
   `1` something needs a human, `2` argparse usage error, `130` Ctrl-C.
   Operators gate scripts on this.
9. **Do not claim a capability that is not implemented.** The old script
   docstring asserted "all items are fully implemented" while four scope items
   were missing. Documentation must describe the code that exists. The module
   docstring's `Not covered` block, the README features table, and the Scope
   table above must stay in agreement.

---

## API gotchas — do not rediscover these

| Gotcha | Detail |
|--------|--------|
| `order_by=iid` is invalid | The issues endpoint accepts only `created_at`, `updated_at`, `priority`, `due_date`, `relative_position`, `label_priority`, `milestone_due`, `popularity`, `title`, `weight`. Fetch, then sort by `iid` client-side. |
| `iid` / `created_at` need Owner | Silently ignored for non-owner, non-admin callers. No error is returned. Verify by reading back the created object, not by trusting a 201 — the read-back fidelity guard does exactly this and aborts the run on a mismatch. |
| `closed_at` cannot be set at all | Neither `POST` nor `PUT .../issues` accepts a `closed_at` parameter, and Owner rights do not change that. A closed issue records the *migration* time. Do not add a `closed_at` key to the payload expecting it to work — the source value belongs in the report only (it is the `closed_at` CSV/JSON column). `created_at` and `updated_at` *are* settable. |
| Restore `updated_at` last | Every note write bumps `updated_at`, so `_finalize_issue()` runs after the note thread. Moving it earlier silently loses the original value. |
| An occupied target IID is adopted, not a 409 | Before creating, the script does `GET .../issues/:iid`. If something is there it adopts it (`status=already_present`), then appends only the notes the destination is missing. This is the normal recovery path when a run dies between the `POST` and its checkpoint journal line. Adoption is content-blind, so it is only correct when the issue at that IID really is the corresponding source issue. The fidelity guard does not apply to adopted issues — see above. |
| `assignee_ids` is Premium-only | On Free the API rejects it; fall back to the single `assignee_id`. Issues with multiple assignees keep only the first. Read `assignees` (plural) from the source; `assignee` is deprecated. |
| One board per project on Free | `POST /projects/:id/boards` fails for a second board. Report extra boards as **skipped**, not failed. Board lists need a destination `label_id`, resolved through a label name→id map. |
| aiohttp ignores `REQUESTS_CA_BUNDLE` | That variable belongs to `requests`. Setting it looks like it worked and does nothing. Use `--ca-bundle`, which builds an `ssl.SSLContext` for the aiohttp connector. |
| Pagination is header-driven | Follow `X-Next-Page`. A single `per_page=100` request silently truncates at 100 items. |
| Notes need explicit ordering | Request `sort=asc` rather than relying on `reversed()` over the default order. |
| System notes are noise | Skip system-generated notes (state changes, label updates); migrate only user-authored ones. |
| Attachment URLs are project-scoped | `/uploads/<secret>/<file>` links resolve only against the source project. Download with the source token, re-upload via `POST /projects/:id/uploads`, and rewrite the markdown. Cache in `cache/upload_map.json` so a duplicate uploads once. |
| Issue links are bidirectional | `GET .../links` reports both directions. Dedupe pairs before re-creating, and skip cross-project links. |
| Deleting an issue does not reset the IID counter | Hence the post-migration bump: create then delete a throwaway issue at `max_iid + 1` to initialize `internal_ids`, preventing a 409 "Duplicated issue" on the operator's first manually created issue. |
| 429 carries `Retry-After` | Honor the header instead of pure exponential doubling. |
| Retryable failures are broader than HTTP status | Also retry `asyncio.TimeoutError`, `aiohttp.ClientConnectorError`, `ClientOSError`, and `ServerDisconnectedError`. |

---

## Idempotency

There is no phase CLI — no `--phase`, no `--reset-phase`. Instead, every phase
checks whether an object already exists before creating it, and the checkpoint
records completed issues by source IID. Re-running `--full-run` is therefore
safe and resumes automatically.

Preserve this property. Any new phase you add must check-then-create, and must
tolerate being run against a partially populated destination.

The checkpoint is append-only JSONL (`checkpoints/migration_state.jsonl`, the
durable per-issue write path) plus atomic temp-and-rename snapshots
(`migration_state.json`, written at phase boundaries). Do not replace it with a
full-file rewrite per issue — that was an O(n²) hot spot on a multi-thousand-issue
project. Both files are read at startup, with later journal lines winning, so
clearing the checkpoint means deleting **both**.

A checkpoint record is only "done" once `notes_done` is true. Issue creation sets
it False; the note pass sets it True. `_is_pending()` relies on that, so
`--full-run --limit N` windows finish an interrupted note thread instead of
stranding it.

---

## Working conventions

- **Verify before asserting.** Run the command and quote its real output. Do not
  report a migration as complete without a `--validation-only` run and a
  fidelity check against `reports/migration_report.json`. With no credentials
  available, `python verify_offline.py`, `python3 -m py_compile`, and
  `--help` are the checks you *can* run — use them rather than reasoning about
  what the code probably does.
- **Read `RUNBOOK.md` before touching operator-facing behavior.** If you change
  a flag, a phase, or a permission requirement, update `RUNBOOK.md` and
  `README.md` in the same change. Documentation drift is how this project
  accumulated a runbook describing a tool that never existed.
- **Prefer `--dry-run -v` for investigation.** It exercises the real read path
  and populates reports without writing anything.
- **Keep secrets out of logs.** Log which `PAT_*` variable was used, never its
  value.
- **Status glyphs:** `✓` pass, `✗` fail, `⚠` warning. Match the existing docs
  and log output. No emoji beyond these.
