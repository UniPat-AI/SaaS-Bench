"""
Verifier for Software-033-I3: Audit devops-configs CI workflow and track missing stages

Checks: 12 weighted checks across code-server, baserow, openproject.
Strategy: verifier recomputes ground truth itself by parsing deploy.yml —
          the STRUCTURE truth (job names / needs / stage categories / missing
          stages) is read from the code-server container's own PRISTINE image
          via a throwaway `docker run` (immune to agent edits of the live
          workspace); per-job runs-on VALUES are read from the live file
          because upgrading the runner is an agent deliverable. Agent-filled
          data is never trusted as a truth source. Git state is verified with a
          three-stage check (exact subject -> file list -> commit blob
          content). Baserow via REST API; OpenProject via embedded psql.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import json
import os
import re
import subprocess
import sys

import requests
import yaml

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_required = {
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
OPENPROJECT_URL = f"http://{HOST}:{OPENPROJECT_PORT}"

# Slot values
CI_WORKFLOW_PATH = "devops-configs/.github/workflows/deploy.yml"
REPO_PATH = "/home/coder/workspace/devops-configs"
DEPLOY_YML_ABS = f"/home/coder/workspace/{CI_WORKFLOW_PATH}"
DEPLOY_YML_REL = ".github/workflows/deploy.yml"
OLD_RUNNER = "ubuntu-18.04"
NEW_RUNNER = "ubuntu-22.04"
BASEROW_DB_NAME = "CI Workflow Remediation Tracker"
COMMIT_MSG = "ci: upgrade runner to ubuntu-22.04"
STAGE_MAP = {
    "docker-build": "Build", "npm-build": "Build",
    "jest": "Test", "e2e": "Test",
    "prettier": "Lint", "tflint": "Lint",
    "deploy-staging": "Deploy", "deploy-prod": "Deploy",
    "notify": "Other",
}
REQUIRED_STAGES = ["Build", "Test", "Lint", "Deploy"]
STAGE_OPTIONS = {"Build", "Test", "Lint", "Deploy", "Other"}
OP_PROJECT = "devops-automation"
CI_OWNER = "Paul Harris"

# BAKED expected structure of the *committed* deploy.yml (check 3c).
# Why a baked constant is REQUIRED here: in the pristine code-server image the
# whole `.github/` directory is UNTRACKED (`git status` shows `?? .github/`),
# so the agent's commit is an add-file commit and there is no ancestor blob in
# git history to diff against. Without this constant a malicious agent could
# rewrite the jobs (e.g. add a `prettier` job to eliminate MISSING stages)
# before committing, and the "live file parse" truth would be poisoned.
# NOTE: if the image's seeded deploy.yml ever changes, this constant MUST be
# updated in sync with the image.
EXPECTED_COMMIT_JOBS = [
    # (job name, runs-on, needs — None means no `needs:` key)
    ("docker-build", NEW_RUNNER, None),
    ("npm-build", NEW_RUNNER, None),
    ("jest", NEW_RUNNER, ["npm-build"]),
    ("e2e", NEW_RUNNER, ["docker-build", "npm-build"]),
    ("notify", NEW_RUNNER, ["jest", "e2e"]),
]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


_pristine_image_cache: str | None = None


def _pristine_image() -> str:
    """The code-server container's own image ref — truth reads go here, immune to agent edits."""
    global _pristine_image_cache
    if _pristine_image_cache is None:
        r = subprocess.run(["docker", "inspect", CODE_SERVER_CONTAINER, "--format", "{{.Image}}"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f"docker inspect failed: {r.stderr.strip()[:200]}")
        _pristine_image_cache = r.stdout.strip()
    return _pristine_image_cache


def image_exec(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a command in a throwaway container from the PRISTINE image (not the live one)."""
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", args[0], _pristine_image(), *args[1:]],
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def git_devops(*args: str) -> tuple[int, str, str]:
    """Run a git command inside the devops-configs repo (with safe.directory)."""
    return docker_exec(
        CODE_SERVER_CONTAINER,
        "git", "-c", f"safe.directory={REPO_PATH}", "-C", REPO_PATH, *args,
    )


def baserow_auth() -> str:
    """Get Baserow JWT token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def baserow_get(path: str, token: str, params: dict = None) -> dict:
    resp = requests.get(
        f"{BASEROW_URL}/api{path}",
        headers={"Authorization": f"JWT {token}"},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def op_db_query(sql: str) -> str:
    """Query OpenProject embedded postgres."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
        "-t", "-A", "-c", sql,
        timeout=15,
    )
    return out.strip()


# ── Ground truth: parse the live deploy.yml ──────────────────────────────────
_TRUTH_CACHE: list = []  # [truth_or_None] once computed


def parse_deploy_yml():
    """Recompute ground truth for deploy.yml.

    STRUCTURE truth (job names, needs flags, stage categories, missing
    stages) is parsed from the PRISTINE image's deploy.yml via a throwaway
    `docker run` — the agent cannot poison it by rewriting jobs in the live
    workspace. The per-job runs-on VALUES are read from the LIVE file,
    because upgrading the runner to ubuntu-22.04 is an agent deliverable
    (asserted by checks 2 and 7).

    Returns dict {jobs: [(name, live_runs_on, has_needs)], cats: [...],
    missing: [...], raw: str} or None if either read/parse failed. All
    downstream expectations derive from this parse — NEVER from agent-filled
    rows.
    """
    if _TRUTH_CACHE:
        return _TRUTH_CACHE[0]
    truth = None
    try:
        # Pristine-image read: structural truth, immune to agent edits.
        rc_p, out_p, err_p = image_exec("cat", DEPLOY_YML_ABS, timeout=60)
        # Live read: runs-on values only (agent deliverable state).
        rc_l, out_l, err_l = docker_exec(CODE_SERVER_CONTAINER, "cat", DEPLOY_YML_ABS)
        if rc_p == 0 and out_p.strip() and rc_l == 0 and out_l.strip():
            data = yaml.safe_load(out_p)
            live_data = yaml.safe_load(out_l)
            # NOTE: yaml.safe_load parses the top-level `on:` key as boolean
            # True — irrelevant here, we only read `jobs` (dict order == file
            # order on Python 3.7+).
            live_runs: dict = {}
            live_jobs_map = live_data.get("jobs") if isinstance(live_data, dict) else None
            if isinstance(live_jobs_map, dict):
                for name, spec in live_jobs_map.items():
                    spec = spec if isinstance(spec, dict) else {}
                    live_runs[str(name)] = spec.get("runs-on")
            jobs_map = data.get("jobs") if isinstance(data, dict) else None
            if isinstance(jobs_map, dict) and jobs_map:
                jobs = []
                for name, spec in jobs_map.items():
                    spec = spec if isinstance(spec, dict) else {}
                    needs = spec.get("needs")
                    if isinstance(needs, str):
                        needs = [needs]
                    # runs-on comes from the LIVE file, keyed by the pristine
                    # job name (missing/renamed live jobs yield None -> FAIL
                    # downstream, same as any wrong runner value).
                    jobs.append((str(name), live_runs.get(str(name)), bool(needs)))
                cats = [STAGE_MAP.get(n, "Other") for n, _, _ in jobs]
                covered = set(cats)
                missing = [s for s in REQUIRED_STAGES if s not in covered]
                truth = {"jobs": jobs, "cats": cats, "missing": missing, "raw": out_l}
    except Exception:
        truth = None
    _TRUTH_CACHE.append(truth)
    return truth


def _sel_value(v) -> str:
    """Normalize a Baserow single-select cell to its option value string."""
    if isinstance(v, dict):
        return str(v.get("value") or "")
    return "" if v is None else str(v)


def _bool_value(v) -> bool:
    if isinstance(v, dict):
        v = v.get("value")
    return bool(v)


# ── Check 1: deploy.yml has no old runner ─────────────────────────────────────
def check_1_no_old_runner() -> None:
    """Verify deploy.yml contains no 'ubuntu-18.04'."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "grep", "-c", OLD_RUNNER, DEPLOY_YML_ABS,
        )
        # grep -c: rc==0 → matches found; rc==1 with empty stderr → zero matches.
        # Any other rc (or stderr output, e.g. missing file/container) is an
        # infrastructure/truth failure and must FAIL, not pass as "no matches".
        if rc not in (0, 1) or err.strip():
            check("1. deploy.yml no old runner", 1, False,
                  f"grep failed (rc={rc}): {err.strip()[:200]}")
            return
        has_old = (rc == 0 and out.strip() != "0")
        check("1. deploy.yml no old runner", 1, not has_old,
              f"found {out.strip()} occurrences of '{OLD_RUNNER}'" if has_old else "")
    except Exception as e:
        check("1. deploy.yml no old runner", 1, False, f"exception: {e}")


# ── Check 2: deploy.yml has new runner on every job ──────────────────────────
def check_2_has_new_runner() -> None:
    """Verify 'ubuntu-22.04' occurs exactly once per runs-on key (truth-derived count)."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("2. deploy.yml has new runner", 1, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return
        expected_count = sum(1 for _, runs_on, _ in truth["jobs"] if runs_on is not None)
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "grep", "-c", NEW_RUNNER, DEPLOY_YML_ABS,
        )
        count = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
        ok = expected_count > 0 and count == expected_count
        check("2. deploy.yml has new runner", 1, ok,
              f"{count} occurrences of '{NEW_RUNNER}', expected exactly {expected_count} (one per runs-on key)")
    except Exception as e:
        check("2. deploy.yml has new runner", 1, False, f"exception: {e}")


# ── Check 3: Git commit (subject + file list + committed content) ─────────────
def check_3_commit() -> None:
    """Three-stage git check: exact subject -> single-file commit -> blob content
    matches the BAKED expected structure; working tree equals the commit blob."""
    try:
        problems = []

        # (a) exact subject match
        rc, out, err = git_devops("log", "--all", "--format=%H%x00%s")
        if rc != 0:
            check("3. Git commit message exact", 2, False, f"git log failed: {err.strip()[:120]}")
            return
        shas = []
        for line in out.splitlines():
            if "\x00" not in line:
                continue
            sha, subject = line.split("\x00", 1)
            if subject == COMMIT_MSG:
                shas.append(sha)
        if len(shas) != 1:
            check("3. Git commit message exact", 2, False,
                  f"expected exactly 1 commit with subject '{COMMIT_MSG}', found {len(shas)}")
            return
        sha = shas[0]

        # (b) commit touches exactly .github/workflows/deploy.yml
        rc, out, err = git_devops("show", "--name-only", "--format=", sha)
        files = [l.strip() for l in out.splitlines() if l.strip()]
        if rc != 0 or files != [DEPLOY_YML_REL]:
            problems.append(f"commit files == {files}, expected exactly ['{DEPLOY_YML_REL}']")

        # (c) committed blob parses to the BAKED expected structure
        rc, blob, err = git_devops("show", f"{sha}:{DEPLOY_YML_REL}")
        if rc != 0 or not blob.strip():
            problems.append("could not read committed blob")
        else:
            try:
                data = yaml.safe_load(blob)
                jobs_map = data.get("jobs") if isinstance(data, dict) else None
                if not isinstance(jobs_map, dict):
                    problems.append("committed yaml has no jobs mapping")
                else:
                    got_names = [str(n) for n in jobs_map.keys()]
                    exp_names = [n for n, _, _ in EXPECTED_COMMIT_JOBS]
                    if got_names != exp_names:
                        problems.append(f"committed job order {got_names} != expected {exp_names}")
                    else:
                        for name, exp_runs, exp_needs in EXPECTED_COMMIT_JOBS:
                            spec = jobs_map.get(name)
                            spec = spec if isinstance(spec, dict) else {}
                            runs_on = spec.get("runs-on")
                            needs = spec.get("needs")
                            if isinstance(needs, str):
                                needs = [needs]
                            needs = list(needs) if needs else None
                            if runs_on != exp_runs:
                                problems.append(f"committed {name}.runs-on == {runs_on!r}, expected {exp_runs!r}")
                            if needs != exp_needs:
                                problems.append(f"committed {name}.needs == {needs}, expected {exp_needs}")
            except yaml.YAMLError as e:
                problems.append(f"committed blob is not valid yaml: {e}")

            # working tree file must equal the committed blob
            rc2, wt, err2 = docker_exec(CODE_SERVER_CONTAINER, "cat", DEPLOY_YML_ABS)
            if rc2 != 0 or wt.rstrip("\n") != blob.rstrip("\n"):
                problems.append("working-tree deploy.yml differs from committed blob")

        check("3. Git commit message exact", 2, not problems,
              "; ".join(problems[:3]) if problems else f"commit {sha[:10]} verified (subject+files+content)")
    except Exception as e:
        check("3. Git commit message exact", 2, False, f"exception: {e}")


# ── Check 4: Baserow database exists ─────────────────────────────────────────
def check_4_baserow_db() -> None:
    """Verify Baserow database 'CI Workflow Remediation Tracker' exists."""
    try:
        token = baserow_auth()
        apps = baserow_get("/applications/", token)
        # apps is a list of applications
        found = any(
            app.get("name") == BASEROW_DB_NAME
            for app in apps
        )
        check("4. Baserow DB exists", 1, found,
              "" if found else f"'{BASEROW_DB_NAME}' not found among {len(apps)} applications")
    except Exception as e:
        check("4. Baserow DB exists", 1, False, f"exception: {e}")


# ── Check 5: CI Jobs table with correct fields ───────────────────────────────
def _get_ci_jobs_table(token: str):
    """Find the CI Jobs table and return (table_id, fields_dict)."""
    apps = baserow_get("/applications/", token)
    for app in apps:
        if app.get("name") == BASEROW_DB_NAME:
            tables = app.get("tables", [])
            for tbl in tables:
                if tbl.get("name") == "CI Jobs":
                    table_id = tbl["id"]
                    fields_resp = baserow_get(f"/database/fields/table/{table_id}/", token)
                    fields = {f["name"]: f for f in fields_resp}
                    return table_id, fields
    return None, {}


def check_5_table_fields() -> None:
    """Verify CI Jobs table has required fields with correct types, Job ID
    primary, and the exact Stage Category option set."""
    try:
        token = baserow_auth()
        table_id, fields = _get_ci_jobs_table(token)
        if table_id is None:
            check("5. CI Jobs table + fields", 2, False, "table 'CI Jobs' not found")
            return

        expected_fields = {
            "Job ID": "text",
            "Job Name": "text",
            "Runs On": "text",
            "Has Dependencies": "boolean",
            "Stage Category": "single_select",
            "Missing Stage": "boolean",
        }
        problems = []
        for fname, ftype in expected_fields.items():
            if fname not in fields:
                problems.append(f"missing field: {fname}")
            elif fields[fname]["type"] != ftype:
                problems.append(f"{fname}: expected type {ftype}, got {fields[fname]['type']}")

        if "Job ID" in fields and not fields["Job ID"].get("primary"):
            problems.append("Job ID is not the primary field")

        if "Stage Category" in fields and fields["Stage Category"]["type"] == "single_select":
            options = {o.get("value") for o in fields["Stage Category"].get("select_options", [])}
            if options != STAGE_OPTIONS:
                problems.append(f"Stage Category options {sorted(options)} != expected {sorted(STAGE_OPTIONS)}")

        check("5. CI Jobs table + fields", 2, not problems,
              "; ".join(problems[:4]) if problems else "6 fields, Job ID primary, option set exact")
    except Exception as e:
        check("5. CI Jobs table + fields", 2, False, f"exception: {e}")


# ── Row helpers ───────────────────────────────────────────────────────────────
def _get_all_rows(token: str, table_id: int) -> list[dict]:
    """Fetch all rows from a Baserow table."""
    rows = []
    page = 1
    while True:
        resp = baserow_get(
            f"/database/rows/table/{table_id}/",
            token,
            params={"page": page, "size": 200, "user_field_names": "true"},
        )
        rows.extend(resp.get("results", []))
        if resp.get("next") is None:
            break
        page += 1
    return rows


def _primary_field_name(fields: dict) -> str:
    for f in fields.values():
        if f.get("primary"):
            return f["name"]
    return "Job ID"


def _real_rows(rows: list[dict], primary_field: str) -> list[dict]:
    """Drop empty placeholder rows (blank Job ID AND blank Job Name)."""
    real = []
    for row in rows:
        jid = str(row.get(primary_field) or "").strip()
        jname = str(row.get("Job Name") or "").strip()
        if not jid and not jname:
            continue
        real.append(row)
    return real


# ── Check 6: exactly one row per job + MISSING rows, CJ-01.. contiguous ──────
def check_6_row_count_and_ids() -> None:
    """Verify (placeholders excluded) row count == len(JOBS)+len(MISSING) and
    Job ID sequence in row order is exactly CJ-01..CJ-NN."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("6. Job rows with CJ-NN IDs", 2, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return
        expected_n = len(truth["jobs"]) + len(truth["missing"])

        token = baserow_auth()
        table_id, fields = _get_ci_jobs_table(token)
        if table_id is None:
            check("6. Job rows with CJ-NN IDs", 2, False, "table not found")
            return

        primary_field = _primary_field_name(fields)
        rows = _real_rows(_get_all_rows(token, table_id), primary_field)
        if len(rows) != expected_n:
            check("6. Job rows with CJ-NN IDs", 2, False,
                  f"{len(rows)} non-placeholder rows, expected exactly {expected_n}")
            return

        got_ids = [str(r.get(primary_field) or "").strip() for r in rows]
        exp_ids = [f"CJ-{i:02d}" for i in range(1, expected_n + 1)]
        ok = got_ids == exp_ids
        check("6. Job rows with CJ-NN IDs", 2, ok,
              f"row-order IDs {got_ids} != expected {exp_ids}" if not ok
              else f"{expected_n} rows, IDs CJ-01..CJ-{expected_n:02d} contiguous")
    except Exception as e:
        check("6. Job rows with CJ-NN IDs", 2, False, f"exception: {e}")


# ── Check 7: job rows match parsed truth ─────────────────────────────────────
def check_7_job_rows() -> None:
    """Verify non-MISSING rows: Job Name order == parsed job order; per row
    Stage Category == STAGE_MAP, Runs On == ubuntu-22.04, Has Dependencies ==
    truth needs flag, Missing Stage == false."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("7. Job rows match deploy.yml truth", 2, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return

        token = baserow_auth()
        table_id, fields = _get_ci_jobs_table(token)
        if table_id is None:
            check("7. Job rows match deploy.yml truth", 2, False, "table not found")
            return

        primary_field = _primary_field_name(fields)
        rows = _real_rows(_get_all_rows(token, table_id), primary_field)
        non_missing = [r for r in rows
                       if not str(r.get("Job Name") or "").strip().startswith("MISSING:")]

        exp_names = [n for n, _, _ in truth["jobs"]]
        got_names = [str(r.get("Job Name") or "").strip() for r in non_missing]
        if got_names != exp_names:
            check("7. Job rows match deploy.yml truth", 2, False,
                  f"job-row names {got_names} != parsed order {exp_names}")
            return

        problems = []
        for row, (name, runs_on, has_needs), cat in zip(non_missing, truth["jobs"], truth["cats"]):
            got_cat = _sel_value(row.get("Stage Category"))
            got_runs = str(row.get("Runs On") or "").strip()
            got_deps = _bool_value(row.get("Has Dependencies"))
            got_missing = _bool_value(row.get("Missing Stage"))
            if got_cat != cat:
                problems.append(f"{name}: Stage Category '{got_cat}' != '{cat}'")
            if runs_on != NEW_RUNNER or got_runs != NEW_RUNNER:
                problems.append(f"{name}: Runs On '{got_runs}' (file: '{runs_on}') != '{NEW_RUNNER}'")
            if got_deps != has_needs:
                problems.append(f"{name}: Has Dependencies {got_deps} != {has_needs}")
            if got_missing:
                problems.append(f"{name}: Missing Stage must be false")

        check("7. Job rows match deploy.yml truth", 2, not problems,
              "; ".join(problems[:3]) if problems else f"{len(non_missing)} job rows match parsed truth")
    except Exception as e:
        check("7. Job rows match deploy.yml truth", 2, False, f"exception: {e}")


# ── Check 8: MISSING stage rows ──────────────────────────────────────────────
def check_8_missing_rows() -> None:
    """Verify MISSING rows are exactly one per truth-derived missing stage, in
    required-stage order, each Missing Stage=true, Runs On empty, Has
    Dependencies=false, Stage Category=<stage>."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("8. MISSING stage placeholder rows", 2, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return
        exp_missing = truth["missing"]  # REQUIRED_STAGES order

        token = baserow_auth()
        table_id, fields = _get_ci_jobs_table(token)
        if table_id is None:
            check("8. MISSING stage placeholder rows", 2, False, "table not found")
            return

        primary_field = _primary_field_name(fields)
        rows = _real_rows(_get_all_rows(token, table_id), primary_field)
        missing_rows = [r for r in rows
                        if str(r.get("Job Name") or "").strip().startswith("MISSING:")]

        exp_names = [f"MISSING:{s}" for s in exp_missing]
        got_names = [str(r.get("Job Name") or "").strip() for r in missing_rows]
        if got_names != exp_names:
            check("8. MISSING stage placeholder rows", 2, False,
                  f"MISSING rows {got_names} != expected {exp_names} (truth-derived, in required-stage order)")
            return

        problems = []
        for row, stage in zip(missing_rows, exp_missing):
            got_missing = _bool_value(row.get("Missing Stage"))
            got_runs = str(row.get("Runs On") or "").strip()
            got_deps = _bool_value(row.get("Has Dependencies"))
            got_cat = _sel_value(row.get("Stage Category"))
            if not got_missing:
                problems.append(f"MISSING:{stage}: Missing Stage must be true")
            if got_runs != "":
                problems.append(f"MISSING:{stage}: Runs On '{got_runs}' must be empty")
            if got_deps:
                problems.append(f"MISSING:{stage}: Has Dependencies must be false")
            if got_cat != stage:
                problems.append(f"MISSING:{stage}: Stage Category '{got_cat}' != '{stage}'")

        check("8. MISSING stage placeholder rows", 2, not problems,
              "; ".join(problems[:3]) if problems else f"MISSING rows exact: {exp_missing}")
    except Exception as e:
        check("8. MISSING stage placeholder rows", 2, False, f"exception: {e}")


# ── Check 9: Gaps view ───────────────────────────────────────────────────────
def check_9_gaps_view() -> None:
    """Verify 'Gaps' grid view exists on the CI Jobs table, filtered to Missing Stage=true."""
    try:
        token = baserow_auth()
        table_id, fields = _get_ci_jobs_table(token)
        if table_id is None:
            check("9. Gaps view exists", 2, False, "table not found")
            return

        views_resp = baserow_get(f"/database/views/table/{table_id}/", token)
        gaps_view = None
        for v in views_resp:
            if v.get("name") == "Gaps":
                gaps_view = v
                break

        if gaps_view is None:
            check("9. Gaps view exists", 2, False, "'Gaps' view not found")
            return

        # Check that view type is grid
        is_grid = gaps_view.get("type") == "grid"

        # Check filters
        view_id = gaps_view["id"]
        filters_resp = baserow_get(f"/database/views/{view_id}/filters/", token)
        # Look for a filter on the Missing Stage field
        missing_stage_field_id = None
        for f in fields.values():
            if f["name"] == "Missing Stage":
                missing_stage_field_id = f["id"]
                break

        has_filter = False
        if missing_stage_field_id:
            for flt in filters_resp:
                if flt.get("field") == missing_stage_field_id:
                    # Boolean filter: type "boolean" with value "true" or "1"
                    if str(flt.get("value", "")).lower() in ("true", "1"):
                        has_filter = True
                        break

        ok = is_grid and has_filter
        detail_parts = []
        if not is_grid:
            detail_parts.append(f"view type is '{gaps_view.get('type')}', expected 'grid'")
        if not has_filter:
            detail_parts.append("no filter on Missing Stage=true found")
        check("9. Gaps view exists", 2, ok,
              "; ".join(detail_parts) if detail_parts else "grid view with correct filter")
    except Exception as e:
        check("9. Gaps view exists", 2, False, f"exception: {e}")


# ── Check 10: OpenProject work packages exact set ────────────────────────────
def check_10_op_work_packages() -> None:
    """Verify Task WPs 'Add CI stage: <Category>' exist as EXACTLY the
    truth-derived missing-stage set (count anchored, no extras)."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("10. OP work packages exist", 2, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return
        expected_subjects = sorted(f"Add CI stage: {s}" for s in truth["missing"])

        # Find project id
        project_id = op_db_query(
            f"SELECT id FROM projects WHERE identifier='{OP_PROJECT}'"
        )
        if not project_id:
            check("10. OP work packages exist", 2, False,
                  f"project '{OP_PROJECT}' not found")
            return

        # Find Task type id
        task_type_id = op_db_query(
            "SELECT id FROM types WHERE LOWER(name)='task' LIMIT 1"
        )
        if not task_type_id:
            check("10. OP work packages exist", 2, False, "Task type not found in OpenProject")
            return

        # Get Task work packages with subject matching pattern (all of them —
        # extras must FAIL the exact-set comparison)
        wps_raw = op_db_query(
            f"SELECT subject FROM work_packages "
            f"WHERE project_id={project_id} "
            f"AND subject LIKE 'Add CI stage:%' "
            f"AND type_id={task_type_id}"
        )
        found_subjects = sorted(s.strip() for s in wps_raw.split("\n") if s.strip()) if wps_raw else []

        ok = found_subjects == expected_subjects and len(found_subjects) == len(truth["missing"])
        check("10. OP work packages exist", 2, ok,
              f"found {found_subjects}, expected exactly {expected_subjects} (count=={len(expected_subjects)})")
    except Exception as e:
        check("10. OP work packages exist", 2, False, f"exception: {e}")


# ── Check 11: OP assignee and priority ────────────────────────────────────────
def check_11_op_assignee_priority() -> None:
    """Verify work packages are assigned to Paul Harris with High priority.
    (Total is anchored at the truth-derived count by check 10.)"""
    try:
        project_id = op_db_query(
            f"SELECT id FROM projects WHERE identifier='{OP_PROJECT}'"
        )
        if not project_id:
            check("11. OP assignee + priority", 2, False, "project not found")
            return

        # Get user id for Paul Harris
        paul_id = op_db_query(
            f"SELECT id FROM users WHERE CONCAT(firstname, ' ', lastname) = '{CI_OWNER}' LIMIT 1"
        )

        # Get priority id for High
        high_priority_id = op_db_query(
            "SELECT id FROM enumerations WHERE name='High' AND type='IssuePriority' LIMIT 1"
        )

        # Count WPs with correct assignee and priority
        conditions = f"project_id={project_id} AND subject LIKE 'Add CI stage:%'"
        total = int(op_db_query(f"SELECT COUNT(*) FROM work_packages WHERE {conditions}") or "0")

        if total == 0:
            check("11. OP assignee + priority", 2, False, "no work packages found")
            return

        assignee_ok = 0
        priority_ok = 0
        if paul_id:
            assignee_ok = int(op_db_query(
                f"SELECT COUNT(*) FROM work_packages WHERE {conditions} AND assigned_to_id={paul_id}"
            ) or "0")
        if high_priority_id:
            priority_ok = int(op_db_query(
                f"SELECT COUNT(*) FROM work_packages WHERE {conditions} AND priority_id={high_priority_id}"
            ) or "0")

        ok = (assignee_ok == total) and (priority_ok == total)
        detail = f"assignee correct: {assignee_ok}/{total}, priority correct: {priority_ok}/{total}"
        if not paul_id:
            detail += "; Paul Harris user not found"
        if not high_priority_id:
            detail += "; High priority not found"
        check("11. OP assignee + priority", 2, ok, detail)
    except Exception as e:
        check("11. OP assignee + priority", 2, False, f"exception: {e}")


# ── Check 12: OP description exact (truth-assembled) ─────────────────────────
def check_12_op_description() -> None:
    """Verify each missing-stage WP description equals the truth-assembled
    string exactly (backslash-stripped whole-string compare)."""
    try:
        truth = parse_deploy_yml()
        if truth is None:
            check("12. OP description format", 2, False,
                  "truth recompute failed: could not parse deploy.yml (pristine-image structure + live runs-on)")
            return
        current_jobs = ", ".join(sorted(n for n, _, _ in truth["jobs"]))

        project_id = op_db_query(
            f"SELECT id FROM projects WHERE identifier='{OP_PROJECT}'"
        )
        if not project_id:
            check("12. OP description format", 2, False, "project not found")
            return

        problems = []
        for stage in truth["missing"]:
            subject = f"Add CI stage: {stage}"
            expected = (
                f"Add a job of category {stage} to "
                f"devops-configs/.github/workflows/deploy.yml; "
                f"current jobs: {current_jobs}"
            )
            n = int(op_db_query(
                f"SELECT COUNT(*) FROM work_packages "
                f"WHERE project_id={project_id} AND subject='{subject}'"
            ) or "0")
            if n != 1:
                problems.append(f"'{subject}': {n} WPs, expected exactly 1")
                continue
            raw = op_db_query(
                f"SELECT COALESCE(description, '') FROM work_packages "
                f"WHERE project_id={project_id} AND subject='{subject}'"
            )
            # Strip CKEditor backslash escapes, then whole-string compare
            desc = raw.replace("\\", "").replace("\r", "").strip()
            if desc != expected:
                problems.append(f"'{subject}': description '{desc[:120]}' != expected '{expected[:120]}'")

        if not truth["missing"]:
            check("12. OP description format", 2, True, "no missing stages — no WP descriptions required")
            return
        check("12. OP description format", 2, not problems,
              "; ".join(problems[:2]) if problems else
              f"{len(truth['missing'])} descriptions match truth-assembled string exactly")
    except Exception as e:
        check("12. OP description format", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_no_old_runner()
    check_2_has_new_runner()
    check_3_commit()
    check_4_baserow_db()
    check_5_table_fields()
    check_6_row_count_and_ids()
    check_7_job_rows()
    check_8_missing_rows()
    check_9_gaps_view()
    check_10_op_work_packages()
    check_11_op_assignee_priority()
    check_12_op_description()

    total = sum(w for _, w, _, _ in _checks)
    earned = sum(w for _, w, p, _ in _checks if p)
    all_pass = all(p for _, _, p, _ in _checks) and bool(_checks)
    score = (earned / total) if total else 0.0

    print(
        f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})",
        file=sys.stderr,
    )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
