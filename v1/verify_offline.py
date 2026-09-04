"""
Offline verification harness for gitlab_migration.py.

Makes NO network calls. Exercises the pure-logic surfaces that a live run would
otherwise be the first thing to test:

  - _apply_limit() under --test-run / --dry-run / --full-run / --no-resume
  - _is_pending() checkpoint semantics
  - token_for_author() fidelity resolution
  - _report_row() field set vs REPORT_FIELDNAMES
  - generate_reports() CSV header and row content
  - mappings.yml schema validation, including rejection cases
  - the read-back fidelity guard, driven against an in-memory fake HTTP layer:
    timestamp normalisation, iid / created_at / author mismatch aborts,
    admin_attributed and adoption both staying quiet, and
    --no-fidelity-guard suppressing the whole check

Run from the v1/ directory with a Python that has aiohttp and PyYAML:

    python verify_offline.py

Exits 0 if every assertion passes, 1 otherwise.
"""

import asyncio
import csv
import io
import os
import sys
import tempfile
from pathlib import Path

import aiohttp
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gitlab_migration as gm

# Report output paths are read from the module that owns them, so the temporary
# redirect below has to patch migration.reporting rather than the entry-point
# shim's re-exported aliases.
from migration import reporting as gm_reporting

PASSED: list = []
FAILED: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(label)
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def base_cfg(**overrides) -> dict:
    cfg = {
        "SOURCE_GITLAB_URL": "https://src.example",
        "DEST_GITLAB_URL": "https://dst.example",
        "SOURCE_TOKEN": "src-token",
        "DEST_TOKEN": "dst-token",
        "MAX_CONCURRENT_REQUESTS": 4,
        "REQUEST_TIMEOUT": 10,
        "RETRY_COUNT": 2,
        "RETRY_BACKOFF": 0.1,
        "DRY_RUN": False,
        "TEST_RUN": False,
        "SANITY_CHECK": False,
        "VALIDATION_ONLY": False,
        "FULL_RUN": False,
        "RESUME": True,
        "FIDELITY_GUARD": True,
        "SOURCE_PROJECT_OVERRIDE": None,
        "DEST_PROJECT_OVERRIDE": None,
        "LIMIT": None,
        "CA_BUNDLE": None,
        "INSECURE": False,
        "VERBOSE": False,
        "MAPPINGS_FILE": "mappings.yml",
    }
    cfg.update(overrides)
    return cfg


MAPPINGS = yaml.safe_load(Path("mappings.yml").read_text())


def build(cfg: dict, checkpoint: dict) -> gm.GitLabMigration:
    """Instantiate the migration object with an injected checkpoint."""
    migration = gm.GitLabMigration(mappings=MAPPINGS, cfg=cfg)
    migration.checkpoint = checkpoint
    return migration


def issues(*iids) -> list:
    return [{"iid": i, "title": f"issue {i}", "state": "opened",
             "created_at": "2020-01-01T00:00:00Z", "labels": [],
             "author": {"username": "jherrera"}} for i in iids]


def done(*iids) -> dict:
    return {str(i): {"source_iid": str(i), "dest_iid": i, "notes_done": True}
            for i in iids}


def iids_of(selected) -> list:
    return [i["iid"] for i in selected]


# ---------------------------------------------------------------- #
print("\n=== _apply_limit(): --test-run is stable across resumes ===")

all_issues = issues(*range(1, 121))

m = build(base_cfg(TEST_RUN=True, LIMIT=50), {})
first = iids_of(m._apply_limit(all_issues))
check("--test-run, empty checkpoint -> IIDs 1..50",
      first == list(range(1, 51)), f"got {first[0]}..{first[-1]}")

m = build(base_cfg(TEST_RUN=True, LIMIT=50), done(*range(1, 31)))
resumed = iids_of(m._apply_limit(all_issues))
check("--test-run, 30 already done -> STILL IIDs 1..50 (no drift to 31..80)",
      resumed == list(range(1, 51)), f"got {resumed[0]}..{resumed[-1]}")

m = build(base_cfg(DRY_RUN=True, LIMIT=50), done(*range(1, 31)))
dry = iids_of(m._apply_limit(all_issues))
check("--dry-run, 30 already done -> IIDs 1..50 (same pre-checkpoint rule)",
      dry == list(range(1, 51)), f"got {dry[0]}..{dry[-1]}")

# ---------------------------------------------------------------- #
print("\n=== _apply_limit(): --full-run windows walk forward ===")

m = build(base_cfg(FULL_RUN=True, LIMIT=50), {})
w1 = iids_of(m._apply_limit(all_issues))
check("--full-run window 1, empty checkpoint -> IIDs 1..50",
      w1 == list(range(1, 51)), f"got {w1[0]}..{w1[-1]}")

m = build(base_cfg(FULL_RUN=True, LIMIT=50), done(*range(1, 51)))
w2 = iids_of(m._apply_limit(all_issues))
check("--full-run window 2, 1..50 done -> IIDs 51..100 (forward progress)",
      w2 == list(range(51, 101)), f"got {w2[0]}..{w2[-1]}")

m = build(base_cfg(FULL_RUN=True, LIMIT=50), done(*range(1, 101)))
w3 = iids_of(m._apply_limit(all_issues))
check("--full-run window 3, 1..100 done -> IIDs 101..120 (short tail)",
      w3 == list(range(101, 121)), f"got {w3}")

m = build(base_cfg(FULL_RUN=True, LIMIT=50), done(*range(1, 121)))
w4 = iids_of(m._apply_limit(all_issues))
check("--full-run window 4, everything done -> empty selection",
      w4 == [], f"got {w4}")

# ---------------------------------------------------------------- #
print("\n=== _apply_limit(): edge cases ===")

partial = done(*range(1, 51))
partial["25"] = {"source_iid": "25", "dest_iid": 25, "notes_done": False}
m = build(base_cfg(FULL_RUN=True, LIMIT=10), partial)
w = iids_of(m._apply_limit(all_issues))
check("--full-run picks up an issue created but with notes unfinished",
      w[0] == 25, f"first selected = {w[0]}, expected 25")

m = build(base_cfg(FULL_RUN=True, LIMIT=50, RESUME=False), done(*range(1, 51)))
nores = iids_of(m._apply_limit(all_issues))
check("--full-run --no-resume ignores checkpoint -> IIDs 1..50",
      nores == list(range(1, 51)), f"got {nores[0]}..{nores[-1]}")

m = build(base_cfg(FULL_RUN=True, LIMIT=None), done(*range(1, 51)))
nolimit = iids_of(m._apply_limit(all_issues))
check("--full-run with no --limit processes everything (checkpoint skips later)",
      nolimit == list(range(1, 121)), f"{len(nolimit)} issues")

m = build(base_cfg(FULL_RUN=True, LIMIT=500), done(*range(1, 51)))
over = iids_of(m._apply_limit(all_issues))
check("--full-run --limit larger than the backlog -> the whole backlog",
      over == list(range(51, 121)), f"{len(over)} issues")

# ---------------------------------------------------------------- #
print("\n=== _is_pending() ===")

m = build(base_cfg(FULL_RUN=True), {
    "1": {"source_iid": "1", "notes_done": True},
    "2": {"source_iid": "2", "notes_done": False},
    "3": {"source_iid": "3"},
})
check("_is_pending: no record -> pending", m._is_pending({"iid": 99}) is True)
check("_is_pending: notes_done=True -> not pending",
      m._is_pending({"iid": 1}) is False)
check("_is_pending: notes_done=False -> pending",
      m._is_pending({"iid": 2}) is True)
check("_is_pending: notes_done absent -> pending",
      m._is_pending({"iid": 3}) is True)

# ---------------------------------------------------------------- #
print("\n=== token_for_author() ===")

m = build(base_cfg(FULL_RUN=True), {})
os.environ["PAT_JHERRERA15"] = "real-user-token"
os.environ["PAT_JBANFELDER"] = "<token>"
os.environ.pop("PAT_DEST2", None)
m.author_tokens = {}

tok, fid = m.token_for_author("jherrera")
check("mapped user with a real PAT -> own token, fidelity=full",
      (tok, fid) == ("real-user-token", "full"), f"fidelity={fid}")

tok, fid = m.token_for_author("jbanfelder")
check("mapped user whose PAT is a <token> placeholder -> DEST_TOKEN, attributed",
      (tok, fid) == ("dst-token", "admin_attributed"), f"fidelity={fid}")

tok, fid = m.token_for_author("source_username_2")
check("mapped user with no PAT env var at all -> DEST_TOKEN, attributed",
      (tok, fid) == ("dst-token", "admin_attributed"), f"fidelity={fid}")

tok, fid = m.token_for_author("someone_unmapped")
check("author absent from mappings.yml -> DEST_TOKEN, attributed",
      (tok, fid) == ("dst-token", "admin_attributed"), f"fidelity={fid}")

tok, fid = m.token_for_author(None)
check("author None -> DEST_TOKEN, attributed",
      (tok, fid) == ("dst-token", "admin_attributed"), f"fidelity={fid}")

# ---------------------------------------------------------------- #
print("\n=== report rows vs REPORT_FIELDNAMES ===")

m = build(base_cfg(FULL_RUN=True), {})
row = m._report_row(
    {"iid": 7, "title": "t", "state": "closed",
     "created_at": "2020-01-01T00:00:00Z",
     "closed_at": "2021-06-01T00:00:00Z",
     "labels": ["a", "b"], "milestone": {"title": "M1"}},
    dest_iid=7, notes=3, author="jherrera", fidelity="full", status="migrated"
)
check("'fidelity' is in REPORT_FIELDNAMES (CSV, not just JSON)",
      "fidelity" in m.REPORT_FIELDNAMES,
      f"fieldnames={m.REPORT_FIELDNAMES}")
check("row keys exactly match REPORT_FIELDNAMES",
      set(row) == set(m.REPORT_FIELDNAMES),
      f"row-only={set(row) - set(m.REPORT_FIELDNAMES)} "
      f"fieldnames-only={set(m.REPORT_FIELDNAMES) - set(row)}")
check("source closed_at is retained in the row",
      row["closed_at"] == "2021-06-01T00:00:00Z", row["closed_at"])

# ---------------------------------------------------------------- #
print("\n=== generate_reports(): real CSV header ===")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    orig_json, orig_csv, orig_fail = (gm_reporting.REPORT_JSON,
                                      gm_reporting.REPORT_CSV,
                                      gm_reporting.FAILURES_JSON)
    gm_reporting.REPORT_JSON = tmp_path / "migration_report.json"
    gm_reporting.REPORT_CSV = tmp_path / "migration_report.csv"
    gm_reporting.FAILURES_JSON = tmp_path / "failures.json"
    try:
        m = build(base_cfg(FULL_RUN=True), {})
        m.report_rows = [row]
        m.generate_reports()
        text = gm_reporting.REPORT_CSV.read_text()
        header = next(csv.reader(io.StringIO(text)))
        expected = ["source_iid", "dest_iid", "title", "state", "author",
                    "fidelity", "created_at", "closed_at", "labels", "notes",
                    "milestone", "status", "error"]
        check("CSV header is the documented 13 columns in order",
              header == expected, f"got {header}")
        written = list(csv.DictReader(io.StringIO(text)))[0]
        check("CSV row carries fidelity=full", written["fidelity"] == "full",
              written["fidelity"])
        check("CSV row carries the source closed_at",
              written["closed_at"] == "2021-06-01T00:00:00Z",
              written["closed_at"])
    finally:
        (gm_reporting.REPORT_JSON, gm_reporting.REPORT_CSV,
         gm_reporting.FAILURES_JSON) = (orig_json, orig_csv, orig_fail)

# ---------------------------------------------------------------- #
print("\n=== validate_mappings() ===")

check("the real mappings.yml validates clean",
      gm.validate_mappings(MAPPINGS) == [],
      str(gm.validate_mappings(MAPPINGS)))
check("mappings.yml has exactly 5 users", len(MAPPINGS["users"]) == 5,
      str(sorted(MAPPINGS["users"])))
check("None is rejected", gm.validate_mappings(None) != [])
check("a missing 'users' block is rejected",
      gm.validate_mappings({"source": {"project_id": 1},
                            "destination": {"project_id": 2}}) != [])
check("a user missing pat_env is rejected",
      gm.validate_mappings({
          "source": {"project_id": 1}, "destination": {"project_id": 2},
          "users": {"u": {"dest_username": "d"}}}) != [])

# ---------------------------------------------------------------- #
print("\n=== parse_gitlab_timestamp() normalisation ===")

check("Z suffix and explicit +00:00 offset parse to the same instant",
      gm.parse_gitlab_timestamp("2020-01-01T00:00:00Z")
      == gm.parse_gitlab_timestamp("2020-01-01T00:00:00+00:00"))
check("fractional seconds do not change the compared instant",
      abs((gm.parse_gitlab_timestamp("2020-01-01T00:00:00.000Z")
           - gm.parse_gitlab_timestamp("2020-01-01T00:00:00Z"))
          .total_seconds()) < 0.001)
check("a non-UTC offset is normalised, not compared as a string",
      gm.parse_gitlab_timestamp("2020-01-01T00:00:00Z")
      == gm.parse_gitlab_timestamp("2019-12-31T19:00:00-05:00"))
check("an unparseable timestamp returns None",
      gm.parse_gitlab_timestamp("not a timestamp") is None)
check("an empty timestamp returns None",
      gm.parse_gitlab_timestamp("") is None)


# ---------------------------------------------------------------- #
# Read-back fidelity guard — driven against an in-memory fake HTTP layer.
#
# The guard's whole job is to notice that GitLab answered 201 while ignoring
# what it was sent, which cannot be reproduced without controlling the
# response body. Every _get/_post on the migration object is therefore
# replaced with a local callable; nothing touches the network.
# ---------------------------------------------------------------- #
print("\n=== read-back fidelity guard ===")

SOURCE_ISSUE = {
    "iid": 7,
    "title": "guarded issue",
    "state": "opened",
    "created_at": "2020-01-01T00:00:00Z",
    "updated_at": "2020-02-01T00:00:00Z",
    "labels": [],
    "description": "body with no attachments",
    "author": {"username": "jherrera"},
}


def not_found() -> Exception:
    """Build the 404 that _existing_dest_issue() treats as 'IID is free'."""
    return aiohttp.ClientResponseError(
        None, None, status=404, message="Not Found"
    )


def install_fake_http(migration, create_response, refetch_response=None,
                      existing=None):
    """
    Replace the migration's HTTP surface with in-memory responses.

    Args:
        migration:        A GitLabMigration instance.
        create_response:  Body POST .../issues will return.
        refetch_response:  Body GET .../issues/:iid will return *after* the
                          create; defaults to create_response, which is the
                          honest-server case.
        existing:         Body the pre-create IID probe returns. None means
                          the IID is free (404), which is the create path.

    Returns:
        dict of call counters, for asserting the re-fetch happened once.
    """
    calls = {"post": 0, "get": 0, "refetch": 0}
    created_holder: dict = {}

    async def fake_post(session, url, token, payload):
        calls["post"] += 1
        created_holder["payload"] = payload
        return create_response

    async def fake_get(session, url, token):
        calls["get"] += 1
        if created_holder:
            calls["refetch"] += 1
            return (refetch_response if refetch_response is not None
                    else create_response)
        if existing is None:
            raise not_found()
        return existing

    migration._post = fake_post
    migration._get = fake_get
    migration._journal_checkpoint = lambda record: None
    return calls


def run_guarded(cfg_overrides=None, **fake_kwargs):
    """
    Drive migrate_issue() once and report what the guard did.

    Returns:
        (raised, message, calls) where raised is the FidelityGuardError or None.
    """
    cfg = base_cfg(FULL_RUN=True, **(cfg_overrides or {}))
    migration = build(cfg, {})
    migration.author_tokens = {}
    calls = install_fake_http(migration, **fake_kwargs)
    try:
        asyncio.run(migration.migrate_issue(None, dict(SOURCE_ISSUE)))
    except gm.FidelityGuardError as exc:
        return exc, str(exc), calls
    return None, "", calls


os.environ["PAT_JHERRERA15"] = "real-user-token"

GOOD = {"iid": 7, "created_at": "2020-01-01T00:00:00Z",
        "author": {"username": "jherrera15"}}

raised, msg, calls = run_guarded(create_response=GOOD)
check("honoured iid/created_at/author -> no abort",
      raised is None, msg.splitlines()[0] if msg else "")
check("the first create of the run is confirmed by one explicit re-fetch",
      calls["refetch"] == 1, f"refetch calls={calls['refetch']}")

# ---- iid mismatch ---- #
raised, msg, _ = run_guarded(
    create_response={**GOOD, "iid": 152})
check("a wrong returned iid aborts the run",
      raised is not None)
check("the iid abort names the field, both values, and the likely cause",
      all(token in msg for token in ("iid:", "requested: 7", "returned:  152",
                                     "Owner")),
      msg.replace("\n", " ")[:160])

# ---- created_at mismatch ---- #
raised, msg, _ = run_guarded(
    create_response={**GOOD, "created_at": "2026-08-26T12:00:00Z"})
check("a wrong created_at aborts the run",
      raised is not None)
check("the created_at abort names the field and both values",
      "created_at:" in msg and "2026-08-26T12:00:00Z" in msg,
      msg.replace("\n", " ")[:160])

raised, msg, _ = run_guarded(
    create_response={**GOOD, "created_at": "2020-01-01T00:00:01.500+00:00"})
check("created_at within tolerance and at another precision -> no abort",
      raised is None, msg.replace("\n", " ")[:160])

raised, msg, _ = run_guarded(
    create_response={**GOOD, "created_at": None})
check("an absent returned created_at aborts (cannot confirm != confirmed)",
      raised is not None)

# ---- author mismatch for a MAPPED user ---- #
raised, msg, _ = run_guarded(
    create_response={**GOOD, "author": {"username": "some-admin"}})
check("a wrong author for a mapped (fidelity=full) user aborts",
      raised is not None)
check("the author abort names the expected and returned usernames",
      "author.username:" in msg and "jherrera15" in msg
      and "some-admin" in msg,
      msg.replace("\n", " ")[:160])

# ---- admin_attributed must NOT abort ---- #
UNMAPPED_ISSUE = {**SOURCE_ISSUE, "author": {"username": "nobody_in_mappings"}}


def run_unmapped(create_response):
    cfg = base_cfg(FULL_RUN=True)
    migration = build(cfg, {})
    migration.author_tokens = {}
    install_fake_http(migration, create_response=create_response)
    try:
        asyncio.run(migration.migrate_issue(None, dict(UNMAPPED_ISSUE)))
    except gm.FidelityGuardError as exc:
        return exc, str(exc)
    return None, ""


raised, msg = run_unmapped({**GOOD, "author": {"username": "some-admin"}})
check("admin_attributed authorship does NOT abort (supported fallback)",
      raised is None, msg.replace("\n", " ")[:160])

raised, msg = run_unmapped(
    {"iid": 152, "created_at": "2020-01-01T00:00:00Z",
     "author": {"username": "some-admin"}})
check("admin_attributed still aborts on a wrong iid (only author is exempt)",
      raised is not None)

# ---- adopted issues must NOT abort ---- #
ADOPTED = {"iid": 7, "created_at": "2026-08-26T12:00:00Z",
           "author": {"username": "some-admin"}, "title": "pre-existing"}

cfg = base_cfg(FULL_RUN=True, RESUME=False)
m = build(cfg, {})
m.author_tokens = {}
adopt_calls = install_fake_http(m, create_response=GOOD, existing=ADOPTED)
adopt_raised = None
try:
    asyncio.run(m.migrate_issue(None, dict(SOURCE_ISSUE)))
except gm.FidelityGuardError as exc:
    adopt_raised = exc
check("an adopted issue does NOT abort, even with a mismatched created_at",
      adopt_raised is None, str(adopt_raised).replace("\n", " ")[:160])
check("adoption creates nothing, so the guard never runs",
      adopt_calls["post"] == 0 and adopt_calls["refetch"] == 0,
      f"post={adopt_calls['post']} refetch={adopt_calls['refetch']}")
check("the adopted issue is recorded as already_present",
      m.report_rows and m.report_rows[0]["status"] == "already_present",
      str(m.report_rows[:1]))

# ---- the create response can echo optimistically; the re-fetch catches it ---- #
raised, msg, calls = run_guarded(
    create_response=GOOD,
    refetch_response={**GOOD, "iid": 152})
check("an optimistic create response is caught by the confirming re-fetch",
      raised is not None)
check("the re-fetch abort says where the mismatch was observed",
      "an explicit re-fetch" in msg, msg.replace("\n", " ")[:160])

# ---- --no-fidelity-guard suppresses everything ---- #
raised, msg, calls = run_guarded(
    cfg_overrides={"FIDELITY_GUARD": False},
    create_response={"iid": 152, "created_at": "2026-08-26T12:00:00Z",
                     "author": {"username": "some-admin"}})
check("--no-fidelity-guard suppresses iid, created_at, and author checks",
      raised is None, msg.replace("\n", " ")[:160])
check("--no-fidelity-guard also skips the confirming re-fetch",
      calls["refetch"] == 0, f"refetch calls={calls['refetch']}")

# ---- dry-run is inert ---- #
cfg = base_cfg(DRY_RUN=True)
m = build(cfg, {})
m.author_tokens = {}
dry_calls = install_fake_http(
    m, create_response={"iid": 152, "created_at": "2026-08-26T12:00:00Z",
                        "author": {"username": "some-admin"}})
dry_raised = None
try:
    asyncio.run(m.migrate_issue(None, dict(SOURCE_ISSUE)))
except gm.FidelityGuardError as exc:
    dry_raised = exc
check("--dry-run never reaches the guard (no writes occur)",
      dry_raised is None and dry_calls["post"] == 0,
      f"raised={dry_raised} post={dry_calls['post']}")

# ---------------------------------------------------------------- #
print("\n=== fidelity guard config plumbing ===")

parser = gm.cfg_module.build_arg_parser()
args = parser.parse_args(["--full-run"])
check("the fidelity guard defaults to enabled", args.fidelity_guard is True)
args = parser.parse_args(["--full-run", "--no-fidelity-guard"])
check("--no-fidelity-guard clears it", args.fidelity_guard is False)

# ---------------------------------------------------------------- #
print("\n" + "=" * 60)
print(f"  {len(PASSED)} passed, {len(FAILED)} failed")
print("=" * 60)
if FAILED:
    for label in FAILED:
        print(f"  FAILED: {label}")
    sys.exit(1)
print("  All offline checks passed. No network calls were made.")
