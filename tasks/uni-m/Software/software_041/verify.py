"""
Verifier for Software-041-I4: QA Regression Suite Registry for Sprint-2026-06

Checks: 18 weighted checks (ck0 is a 0pt truth-recompute precondition) across
code-server, baserow, openproject. Total weight: 25.

Strategy: the verifier RECOMPUTES ground truth itself by actually re-running
both test suites at verify time in throwaway containers from the live
code-server container's own PRISTINE image (immune to agent edits):
  - tabler: `npm run coverage:audit -- --reporter verbose` (vitest, ~12s)
  - json:   `ctest --test-dir build -N` (test-name list, seconds) +
            `ctest --test-dir build -j 16` (statuses, measured ~2m37s),
            chained in ONE docker run so both see the same build tree
Agent-filled Baserow data is NEVER trusted as a truth source. If truth
recompute fails, all truth-dependent checks FAIL (no fallback).

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import re
import sys
import subprocess

try:
    import requests
except ImportError:
    print("FATAL: requests library not available", file=sys.stderr)
    sys.exit(1)

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

# ── Task constants (from the task description, NOT from agent data) ──────────
KNOWN_FLAKY = {
    "tabler::test_modal_focus_trap",
    "json::test_bson_roundtrip_large",
    "tabler::test_tooltip_positioning",
}
RUN_DATE = "2026-06-10"
QA_REPORT_PATH = "/home/coder/workspace/devops-configs/docs/qa-report-2026-06.md"
PREFIX_MAP = [
    ("tests/src/integration-", "Integration"),
    ("tests/src/regression-", "Regression"),
    ("tests/src/unit-", "Unit"),
    ("preview/", "Smoke"),
    ("core/", "Unit"),
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


def op_db_query(sql: str, timeout: int = 15) -> str:
    """Query OpenProject's embedded PostgreSQL."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject", "-t", "-A", "-c", sql,
        timeout=timeout,
    )
    return out.strip()


def baserow_auth() -> dict:
    """Authenticate to Baserow API and return auth headers."""
    resp = requests.post(f"{BASEROW_URL}/api/user/token-auth/", json={
        "email": "admin@example.com",
        "password": "Admin1234",
    }, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    token = data.get("token") or data.get("access_token", "")
    headers = {"Authorization": f"JWT {token}"}
    return headers


def baserow_get(path: str, headers: dict, params: dict = None) -> dict:
    resp = requests.get(f"{BASEROW_URL}{path}", headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


# ── Ground truth (recomputed at verify time; never taken from agent data) ────
TRUTH: dict | None = None      # {project: {test_name: "Pass"|"Fail"|"Skipped"}}
TRUTH_ERR: str = "truth not computed"
TRUTH_SUMMARY: dict = {}       # {project: {total, passed, failed, skipped, flaky, rate, verdict}}


def _compute_tabler_truth() -> dict:
    """Re-run the tabler vitest suite; parse verbose `✓/×/↓ <file> > <name>` lines.

    Runs in a throwaway container from the PRISTINE image (not the live,
    agent-touched container)."""
    cmd = "cd /home/coder/workspace/tabler && npm run coverage:audit -- --reporter verbose 2>&1"
    # measured ~12s; give generous headroom (plus docker-run container-start overhead)
    rc, out, err = image_exec("bash", "-c", cmd, timeout=220)
    text = _strip_ansi(out + "\n" + err)
    tests: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(✓|×|✗|↓)\s+(\S+)\s+>\s+(.+?)\s*$", line)
        if not m:
            continue
        marker, _file, rest = m.group(1), m.group(2), m.group(3)
        name = re.sub(r"\s+\d+(?:\.\d+)?\s*m?s$", "", rest).strip()
        name = re.sub(r"\s*\(retry x\d+\)\s*$", "", name).strip()
        if not name:
            continue
        status = "Pass" if marker == "✓" else ("Skipped" if marker == "↓" else "Fail")
        tests[name] = status
    if not tests:
        raise RuntimeError(f"tabler: no vitest test lines parsed (rc={rc})")
    return tests


def _compute_json_truth() -> dict:
    """Re-run the json ctest suite: names via `ctest -N` (fast), statuses via full run.

    Both ctest invocations run in ONE throwaway container from the PRISTINE
    image (a single docker run, sections separated by printed markers), so the
    -N listing and the full run are guaranteed to see the same build tree."""
    # 1) authoritative test-name list (seconds) +
    # 2) full run for statuses. Measured wall time with -j16 is ~2m37s.
    # NOTE: the eval runner enforces a global 300s verify budget, so 240s is the
    # practical ceiling for this single subprocess call — do NOT raise past it.
    # --output-on-failure is reserved for failure-name extraction; the tail keeps
    # the summary + "The following tests FAILED:" block.
    cmd = (
        "echo '@@CTEST_N@@'; "
        "ctest --test-dir /home/coder/workspace/json/build -N 2>&1; "
        "echo '@@CTEST_RUN@@'; "
        "ctest --test-dir /home/coder/workspace/json/build -j 16 2>&1 | tail -40"
    )
    rc, out, err = image_exec("bash", "-c", cmd, timeout=240)
    combined = _strip_ansi(out + "\n" + err)
    if "@@CTEST_N@@" not in combined or "@@CTEST_RUN@@" not in combined:
        raise RuntimeError("json: ctest section markers missing from combined output")
    listing, _, tail = combined.partition("@@CTEST_N@@")[2].partition("@@CTEST_RUN@@")

    names = re.findall(r"Test\s+#\d+:\s+(.+?)\s*$", listing, flags=re.M)
    m_total = re.search(r"Total Tests:\s*(\d+)", listing)
    if not names or not m_total or int(m_total.group(1)) != len(names):
        raise RuntimeError(f"json: ctest -N listing unparseable ({len(names)} names)")
    m_sum = re.search(r"(\d+)%\s+tests passed,\s*(\d+)\s+tests failed out of\s+(\d+)", tail)
    if not m_sum:
        raise RuntimeError("json: ctest run summary line not found in tail output")
    failed_count, total = int(m_sum.group(2)), int(m_sum.group(3))
    if total != len(names):
        raise RuntimeError(f"json: ctest ran {total} tests but -N listed {len(names)}")

    statuses = {name: "Pass" for name in names}
    if failed_count > 0:
        failed_names = []
        in_block = False
        for line in tail.splitlines():
            if "The following tests FAILED:" in line:
                in_block = True
                continue
            if in_block:
                fm = re.match(r"^\s*\d+\s*-\s*(\S+)\s*\((.+)\)\s*$", line)
                if fm:
                    failed_names.append(fm.group(1))
        if len(failed_names) != failed_count:
            raise RuntimeError(
                f"json: summary says {failed_count} failed but extracted "
                f"{len(failed_names)} failed names")
        for name in failed_names:
            if name not in statuses:
                raise RuntimeError(f"json: failed test '{name}' not in -N listing")
            statuses[name] = "Fail"
    return statuses


def _derive_summary(truth: dict) -> dict:
    summary = {}
    for proj, tests in truth.items():
        total = len(tests)
        passed = sum(1 for s in tests.values() if s == "Pass")
        failed = sum(1 for s in tests.values() if s == "Fail")
        skipped = sum(1 for s in tests.values() if s == "Skipped")
        flaky = sum(1 for name in tests if f"{proj}::{name}" in KNOWN_FLAKY)
        rate = round(passed / total * 100, 2) if total else 0.0
        if rate >= 96.50 and flaky <= 3:
            verdict = "Green"
        elif rate < 85.00:
            verdict = "Red"
        else:
            verdict = "Yellow"
        summary[proj] = {"total": total, "passed": passed, "failed": failed,
                         "skipped": skipped, "flaky": flaky, "rate": rate,
                         "verdict": verdict}
    return summary


def _truth_failed_tests() -> list[tuple[str, str]]:
    """[(project, test_name)] of failed tests in the recomputed truth."""
    out = []
    for proj in sorted(TRUTH.keys()):
        for name in sorted(TRUTH[proj].keys()):
            if TRUTH[proj][name] == "Fail":
                out.append((proj, name))
    return out


def check_0_truth_recompute():
    """ck0 (0pt precondition): actually re-run both test suites and parse results."""
    global TRUTH, TRUTH_ERR, TRUTH_SUMMARY
    label = "0. Truth recompute: re-ran tabler vitest + json ctest"
    try:
        tabler = _compute_tabler_truth()
        jsonp = _compute_json_truth()
        TRUTH = {"tabler": tabler, "json": jsonp}
        TRUTH_SUMMARY = _derive_summary(TRUTH)
        TRUTH_ERR = ""
        detail = "; ".join(
            f"{p}: {s['total']} tests, {s['passed']} passed, {s['failed']} failed, "
            f"{s['skipped']} skipped" for p, s in sorted(TRUTH_SUMMARY.items()))
        check(label, 0, True, detail)
    except subprocess.TimeoutExpired as e:
        TRUTH = None
        TRUTH_ERR = f"timeout while re-running tests: {e}"
        check(label, 0, False, TRUTH_ERR)
    except Exception as e:
        TRUTH = None
        TRUTH_ERR = f"{e}"
        check(label, 0, False, f"truth recompute failed: {e}")


def _truth_gate(label: str, weight: int) -> bool:
    """Truth-dependent checks FAIL when truth recompute failed (no fallback)."""
    if TRUTH is None:
        check(label, weight, False, f"truth recompute failed: {TRUTH_ERR}")
        return False
    return True


# ── Shared Baserow state ─────────────────────────────────────────────────────
_br_headers = None
_test_cases_table_id = None
_summary_table_id = None
_db_id = None
_test_cases_fields: dict = {}
_summary_fields: dict = {}
_test_cases_rows: list = []
_summary_rows: list = []


def _init_baserow():
    """Load Baserow database, tables, fields, and rows into module state."""
    global _br_headers, _test_cases_table_id, _summary_table_id, _db_id
    global _test_cases_fields, _summary_fields, _test_cases_rows, _summary_rows

    _br_headers = baserow_auth()

    # Find database
    apps = baserow_get("/api/applications/", _br_headers)
    for app in apps:
        if app.get("name") == "QA Regression Registry 2026-06":
            _db_id = app["id"]
            break

    if not _db_id:
        return

    # Find tables
    tables = baserow_get(f"/api/database/tables/database/{_db_id}/", _br_headers)
    for t in tables:
        if t["name"] == "Test Cases":
            _test_cases_table_id = t["id"]
        elif t["name"] == "Project Run Summary":
            _summary_table_id = t["id"]

    # Load fields and rows for Test Cases
    if _test_cases_table_id:
        fields = baserow_get(f"/api/database/fields/table/{_test_cases_table_id}/", _br_headers)
        _test_cases_fields = {f["name"]: f for f in fields}
        page = 1
        while True:
            data = baserow_get(
                f"/api/database/rows/table/{_test_cases_table_id}/",
                _br_headers, params={"size": 200, "page": page},
            )
            _test_cases_rows.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1

    # Load fields and rows for Project Run Summary
    if _summary_table_id:
        fields = baserow_get(f"/api/database/fields/table/{_summary_table_id}/", _br_headers)
        _summary_fields = {f["name"]: f for f in fields}
        data = baserow_get(
            f"/api/database/rows/table/{_summary_table_id}/",
            _br_headers, params={"size": 200},
        )
        _summary_rows.extend(data.get("results", []))


def _get_field_value(row: dict, field_name: str, fields_map: dict):
    """Get the value of a named field from a Baserow row."""
    field = fields_map.get(field_name)
    if not field:
        return None
    field_key = f"field_{field['id']}"
    val = row.get(field_key)
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


def _select_option_ids(field: dict, value: str) -> set:
    return {o["id"] for o in field.get("select_options", []) if o.get("value") == value}


def _select_option_values(field: dict) -> set:
    return {o.get("value") for o in field.get("select_options", [])}


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_baserow_db_exists():
    """Baserow database 'QA Regression Registry 2026-06' exists."""
    try:
        _init_baserow()
        check("1. Baserow DB 'QA Regression Registry 2026-06' exists", 1,
              _db_id is not None,
              "" if _db_id else "database not found")
    except Exception as e:
        check("1. Baserow DB 'QA Regression Registry 2026-06' exists", 1, False, f"exception: {e}")


def check_2_test_cases_table():
    """Test Cases table exists in the database."""
    try:
        check("2. 'Test Cases' table exists", 1,
              _test_cases_table_id is not None,
              "" if _test_cases_table_id else "table not found")
    except Exception as e:
        check("2. 'Test Cases' table exists", 1, False, f"exception: {e}")


def check_3_summary_table():
    """Project Run Summary table exists."""
    try:
        check("3. 'Project Run Summary' table exists", 1,
              _summary_table_id is not None,
              "" if _summary_table_id else "table not found")
    except Exception as e:
        check("3. 'Project Run Summary' table exists", 1, False, f"exception: {e}")


def check_4_test_cases_rows_vs_truth():
    """Test Cases rows match recomputed truth: per-project row counts, exact
    Test Name sets, continuous TC-0001.. sequence consistent with row order
    (Project alpha -> Test File -> Test Name).

    Note: json ctest tests carry no file path, so Test File values are NOT
    strongly asserted (empty / test-name / path are all acceptable); the strong
    assertion is on (Project, Test Name). Ordering is checked against the
    recorded Test File values for monotonic consistency only.
    """
    label = "4. Test Cases rows match recomputed truth (counts, names, TC-ID sequence, order)"
    try:
        if not _truth_gate(label, 2):
            return
        if not _test_cases_rows:
            check(label, 2, False, "no rows found")
            return

        errors = []
        per_project_names: dict[str, list] = {}
        ordered = []  # (project, test_file, test_name) in physical row order
        tc_ids = []
        for row in _test_cases_rows:
            proj = str(_get_field_value(row, "Project", _test_cases_fields) or "")
            name = str(_get_field_value(row, "Test Name", _test_cases_fields) or "")
            tfile = str(_get_field_value(row, "Test File", _test_cases_fields) or "")
            tc_id = str(_get_field_value(row, "Test ID", _test_cases_fields) or "")
            per_project_names.setdefault(proj, []).append(name)
            ordered.append((proj, tfile, name))
            tc_ids.append(tc_id)

        extra_projects = set(per_project_names) - set(TRUTH)
        if extra_projects:
            errors.append(f"unexpected Project values: {sorted(extra_projects)}")

        for proj in sorted(TRUTH):
            truth_names = set(TRUTH[proj].keys())
            actual = per_project_names.get(proj, [])
            if len(actual) != len(truth_names):
                errors.append(f"{proj}: {len(actual)} rows, expected exactly {len(truth_names)}")
            actual_set = set(actual)
            missing = truth_names - actual_set
            extra = actual_set - truth_names
            if missing:
                errors.append(f"{proj}: {len(missing)} truth tests missing "
                              f"(e.g. '{sorted(missing)[0][:60]}')")
            if extra:
                errors.append(f"{proj}: {len(extra)} rows not in truth "
                              f"(e.g. '{sorted(extra)[0][:60]}')")
            if len(actual) != len(actual_set):
                errors.append(f"{proj}: duplicate Test Name rows")

        # TC-0001.. continuous sequence in physical row order
        expected_ids = [f"TC-{i:04d}" for i in range(1, len(_test_cases_rows) + 1)]
        if tc_ids != expected_ids:
            first_bad = next((i for i, (a, b) in enumerate(zip(tc_ids, expected_ids)) if a != b),
                             min(len(tc_ids), len(expected_ids)))
            errors.append(f"Test ID sequence not TC-0001..TC-{len(_test_cases_rows):04d} "
                          f"(first mismatch at row {first_bad + 1}: got '{tc_ids[first_bad] if first_bad < len(tc_ids) else '?'}')")

        # Row order: Project alpha -> Test File -> Test Name (against recorded values;
        # accept case-sensitive or casefolded collation)
        keys_cs = ordered
        keys_ci = [(p.casefold(), f.casefold(), n.casefold()) for p, f, n in ordered]
        if not (keys_cs == sorted(keys_cs) or keys_ci == sorted(keys_ci)):
            bad = next((i for i in range(1, len(keys_cs)) if keys_cs[i] < keys_cs[i - 1]),
                       next((i for i in range(1, len(keys_ci)) if keys_ci[i] < keys_ci[i - 1]), 0))
            errors.append(f"rows not ordered by (Project, Test File, Test Name); "
                          f"first violation at row {bad + 1}")

        passed = not errors
        detail = "; ".join(errors[:4]) if errors else f"{len(_test_cases_rows)} rows match truth"
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_suite_category():
    """Suite Category correctly assigned based on prefix mapping (recomputed per
    row from Test File; json ctest tests have no path -> default 'Unit')."""
    try:
        if not _test_cases_rows:
            check("5. Suite Category assignment correct", 2, False, "no rows")
            return

        errors = 0
        sample_error = ""
        for row in _test_cases_rows:
            test_file = str(_get_field_value(row, "Test File", _test_cases_fields) or "")
            category = str(_get_field_value(row, "Suite Category", _test_cases_fields) or "")

            expected_cat = "Unit"  # default (covers json's path-less ctest tests)
            for prefix, cat in PREFIX_MAP:
                if test_file.startswith(prefix):
                    expected_cat = cat
                    break

            if category != expected_cat:
                errors += 1
                if not sample_error:
                    sample_error = f"file={test_file}, expected={expected_cat}, got={category}"

        passed = errors == 0
        detail = f"{errors} mismatches" + (f"; first: {sample_error}" if sample_error else "")
        check("5. Suite Category assignment correct", 2, passed, detail)
    except Exception as e:
        check("5. Suite Category assignment correct", 2, False, f"exception: {e}")


def check_6_flaky_flags():
    """Flaky flags: true iff project::name is in the known flaky list.
    (Weight rebalanced 2->1 to fund ck17; row completeness is already gated
    by ck4 against recomputed truth.)"""
    try:
        if not _test_cases_rows:
            check("6. Flaky flags correct", 1, False, "no rows")
            return

        errors = 0
        sample_error = ""
        for row in _test_cases_rows:
            proj = str(_get_field_value(row, "Project", _test_cases_fields) or "")
            test_name = str(_get_field_value(row, "Test Name", _test_cases_fields) or "")
            flaky = _get_field_value(row, "Flaky", _test_cases_fields)

            key = f"{proj}::{test_name}"
            expected_flaky = key in KNOWN_FLAKY
            actual_flaky = bool(flaky)

            if actual_flaky != expected_flaky:
                errors += 1
                if not sample_error:
                    sample_error = f"{key}: expected={expected_flaky}, got={actual_flaky}"

        passed = errors == 0
        detail = f"{errors} mismatches" + (f"; first: {sample_error}" if sample_error else "")
        check("6. Flaky flags correct", 1, passed, detail)
    except Exception as e:
        check("6. Flaky flags correct", 1, False, f"exception: {e}")


def check_7_last_run_date():
    """Last Run = 2026-06-10 for all test case rows."""
    try:
        if not _test_cases_rows:
            check("7. Last Run date correct", 1, False, "no rows")
            return

        errors = 0
        for row in _test_cases_rows:
            last_run = _get_field_value(row, "Last Run", _test_cases_fields)
            if last_run and isinstance(last_run, str):
                if not last_run.startswith(RUN_DATE):
                    errors += 1
            else:
                errors += 1

        passed = errors == 0
        detail = f"{errors}/{len(_test_cases_rows)} rows with wrong date" if errors else ""
        check("7. Last Run date correct", 1, passed, detail)
    except Exception as e:
        check("7. Last Run date correct", 1, False, f"exception: {e}")


def check_8_summary_values():
    """Project Run Summary matches recomputed truth: exactly one row per project;
    Total/Passed/Failed/Skipped/Flaky Count/Pass Rate Pct (±0.01)/Run Verdict all
    computed from TRUTH, not from agent Test Cases rows."""
    label = "8. Project Run Summary matches recomputed truth"
    try:
        if not _truth_gate(label, 2):
            return
        if not _summary_rows:
            check(label, 2, False, "no summary rows")
            return

        errors = []
        seen_projects = []
        for srow in _summary_rows:
            proj = str(_get_field_value(srow, "Project", _summary_fields) or "")
            seen_projects.append(proj)
            if proj not in TRUTH_SUMMARY:
                errors.append(f"unexpected project row '{proj}'")
                continue
            exp = TRUTH_SUMMARY[proj]

            def _num(field_name):
                v = _get_field_value(srow, field_name, _summary_fields)
                try:
                    return float(str(v))
                except (ValueError, TypeError):
                    return None

            for field_name, exp_key in (("Total Tests", "total"), ("Passed", "passed"),
                                        ("Failed", "failed"), ("Skipped", "skipped"),
                                        ("Flaky Count", "flaky")):
                actual = _num(field_name)
                if actual is None or int(actual) != exp[exp_key] or actual != int(actual):
                    errors.append(f"{proj}: {field_name} expected {exp[exp_key]}, "
                                  f"got {_get_field_value(srow, field_name, _summary_fields)}")

            rate = _num("Pass Rate Pct")
            if rate is None or abs(rate - exp["rate"]) > 0.01:
                errors.append(f"{proj}: Pass Rate Pct expected {exp['rate']}, "
                              f"got {_get_field_value(srow, 'Pass Rate Pct', _summary_fields)}")

            verdict = str(_get_field_value(srow, "Run Verdict", _summary_fields) or "")
            if verdict != exp["verdict"]:
                errors.append(f"{proj}: Run Verdict expected {exp['verdict']}, got '{verdict}'")

        if sorted(seen_projects) != sorted(TRUTH_SUMMARY.keys()):
            errors.append(f"expected exactly one row per project {sorted(TRUTH_SUMMARY)}, "
                          f"found {seen_projects}")

        passed = not errors
        check(label, 2, passed, "; ".join(errors[:4]) if errors else "")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_failures_only_view():
    """'Failures Only' grid view: filter Status=Fail + sort Test Name ASC."""
    label = "9. 'Failures Only' grid view with Status=Fail filter and Test Name ASC sort"
    try:
        if not _test_cases_table_id or not _br_headers:
            check(label, 1, False, "no Test Cases table or auth")
            return

        views = baserow_get(f"/api/database/views/table/{_test_cases_table_id}/", _br_headers)
        view = next((v for v in views if v["name"] == "Failures Only"), None)
        if not view:
            check(label, 1, False, f"view not found; views: {[v['name'] for v in views]}")
            return

        errors = []
        if view.get("type") != "grid":
            errors.append(f"view type '{view.get('type')}', expected 'grid'")

        status_field = _test_cases_fields.get("Status") or {}
        name_field = _test_cases_fields.get("Test Name") or {}
        fail_ids = {str(i) for i in _select_option_ids(status_field, "Fail")}

        filters = baserow_get(f"/api/database/views/{view['id']}/filters/", _br_headers)
        def _is_fail_filter(f):
            if f.get("field") != status_field.get("id"):
                return False
            val = str(f.get("value", ""))
            if f.get("type") in ("single_select_equal", "equal"):
                return val in fail_ids or val == "Fail"
            if f.get("type") == "single_select_is_any_of":
                return set(v.strip() for v in val.split(",") if v.strip()) == fail_ids
            return False
        if not any(_is_fail_filter(f) for f in filters):
            errors.append(f"no Status=Fail filter (filters: {[(f.get('type'), f.get('value')) for f in filters]})")

        sortings = baserow_get(f"/api/database/views/{view['id']}/sortings/", _br_headers)
        if not any(s.get("field") == name_field.get("id") and s.get("order") == "ASC"
                   for s in sortings):
            errors.append(f"no Test Name ASC sort (sortings: {len(sortings)})")

        check(label, 1, not errors, "; ".join(errors))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_10_flaky_tests_view():
    """'Flaky Tests' grid view: filter Flaky boolean=true."""
    label = "10. 'Flaky Tests' grid view with Flaky=true boolean filter"
    try:
        if not _test_cases_table_id or not _br_headers:
            check(label, 1, False, "no Test Cases table or auth")
            return

        views = baserow_get(f"/api/database/views/table/{_test_cases_table_id}/", _br_headers)
        view = next((v for v in views if v["name"] == "Flaky Tests"), None)
        if not view:
            check(label, 1, False, f"view not found; views: {[v['name'] for v in views]}")
            return

        errors = []
        if view.get("type") != "grid":
            errors.append(f"view type '{view.get('type')}', expected 'grid'")

        flaky_field = _test_cases_fields.get("Flaky") or {}
        filters = baserow_get(f"/api/database/views/{view['id']}/filters/", _br_headers)
        truthy = {"1", "true", "True", "t", "yes", "on"}
        has_filter = any(
            f.get("field") == flaky_field.get("id") and f.get("type") == "boolean"
            and str(f.get("value", "")) in truthy
            for f in filters
        )
        if not has_filter:
            errors.append(f"no Flaky=true boolean filter (filters: {[(f.get('type'), f.get('value')) for f in filters]})")

        check(label, 1, not errors, "; ".join(errors))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_11_qa_report_exists():
    """File exists at the pinned path devops-configs/docs/qa-report-2026-06.md."""
    label = "11. qa-report-2026-06.md exists at devops-configs/docs/"
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER, "test", "-f", QA_REPORT_PATH, timeout=10,
        )
        check(label, 1, rc == 0,
              "" if rc == 0 else f"not a file: {QA_REPORT_PATH}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_12_qa_report_content():
    """qa-report content: exact lines with numbers recomputed from TRUTH; exactly
    two project lines '- <proj>: <rate>% (<verdict>)' in alphabetical order."""
    label = "12. qa-report content matches recomputed truth"
    try:
        if not _truth_gate(label, 2):
            return
        rc, content, err = docker_exec(CODE_SERVER_CONTAINER, "cat", QA_REPORT_PATH, timeout=10)
        if rc != 0:
            check(label, 2, False, f"cannot read {QA_REPORT_PATH}")
            return

        lines = [l.rstrip() for l in content.rstrip("\n").split("\n")]
        while lines and not lines[-1].strip():
            lines.pop()

        t_total = sum(s["total"] for s in TRUTH_SUMMARY.values())
        t_passed = sum(s["passed"] for s in TRUTH_SUMMARY.values())
        t_failed = sum(s["failed"] for s in TRUTH_SUMMARY.values())
        t_skipped = sum(s["skipped"] for s in TRUTH_SUMMARY.values())
        t_flaky = sum(s["flaky"] for s in TRUTH_SUMMARY.values())
        expected_projects = sorted(TRUTH_SUMMARY.keys())  # alphabetical

        errors = []
        expected_line_count = 4 + len(expected_projects)
        if len(lines) != expected_line_count:
            errors.append(f"{len(lines)} non-trailing-blank lines, expected exactly {expected_line_count}")
        if any(not l.strip() for l in lines):
            errors.append("blank line inside report body")

        def _line(i):
            return lines[i].strip() if i < len(lines) else ""

        if _line(0) != "# QA Regression Report: Sprint-2026-06":
            errors.append(f"line 1 mismatch: '{_line(0)[:60]}'")
        if _line(1) != f"Run Date: {RUN_DATE}":
            errors.append(f"line 2 mismatch: '{_line(1)[:60]}'")
        exp_line3 = f"Total tests: {t_total}; Passed: {t_passed}; Failed: {t_failed}; Skipped: {t_skipped}"
        if _line(2) != exp_line3:
            errors.append(f"line 3 mismatch: got '{_line(2)[:70]}', expected '{exp_line3}'")
        if _line(3) != f"Flaky count: {t_flaky}":
            errors.append(f"line 4 mismatch: got '{_line(3)[:60]}', expected 'Flaky count: {t_flaky}'")

        for i, proj in enumerate(expected_projects):
            actual = _line(4 + i)
            m = re.fullmatch(r"- (\S+): ([0-9]+(?:\.[0-9]+)?)% \((Green|Yellow|Red)\)", actual)
            exp = TRUTH_SUMMARY[proj]
            if not m:
                errors.append(f"project line {i + 1} format wrong: '{actual[:60]}'")
                continue
            if m.group(1) != proj:
                errors.append(f"project line {i + 1}: expected project '{proj}', got '{m.group(1)}'")
            # accept numerically-equivalent representations (100.0 vs 100.00)
            if abs(float(m.group(2)) - exp["rate"]) > 0.005:
                errors.append(f"{proj}: rate {m.group(2)} != truth {exp['rate']}")
            if m.group(3) != exp["verdict"]:
                errors.append(f"{proj}: verdict '{m.group(3)}' != truth '{exp['verdict']}'")

        check(label, 2, not errors, "; ".join(errors[:4]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_13_op_epic_exists():
    """Exactly one Epic 'QA Regression: Sprint-2026-06' in demo-project with
    assignee admin and priority High (priority via enumerations join)."""
    label = "13. Epic exists (count==1, assignee admin, priority High)"
    try:
        sql = """
        SELECT wp.id, COALESCE(u.login, ''), e.name
        FROM work_packages wp
        JOIN projects p ON wp.project_id = p.id
        JOIN types t ON wp.type_id = t.id
        JOIN enumerations e ON wp.priority_id = e.id
        LEFT JOIN users u ON wp.assigned_to_id = u.id
        WHERE p.identifier = 'demo-project'
          AND t.name = 'Epic'
          AND wp.subject = 'QA Regression: Sprint-2026-06'
        """
        result = op_db_query(sql)
        rows = [r for r in result.split("\n") if r.strip()]

        if not rows:
            check(label, 2, False, "epic not found")
            return

        errors = []
        if len(rows) != 1:
            errors.append(f"expected exactly 1 Epic, found {len(rows)}")
        parts = rows[0].split("|")
        assignee = parts[1].strip() if len(parts) > 1 else ""
        priority = parts[2].strip() if len(parts) > 2 else ""
        if assignee != "admin":  # demo-project's admin; UI name "OpenProject Admin"
            errors.append(f"assignee='{assignee}', expected 'admin' (OpenProject Admin)")
        if priority != "High":
            errors.append(f"priority='{priority}', expected 'High'")

        check(label, 2, not errors, "; ".join(errors))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_op_epic_description():
    """Epic description: whole-template comparison (after backslash strip) with
    Total and Pass Rate recomputed from TRUTH (both round representations OK)."""
    label = "14. Epic description matches recomputed truth (whole template)"
    try:
        if not _truth_gate(label, 2):
            return
        sql = """
        SELECT wp.description
        FROM work_packages wp
        JOIN projects p ON wp.project_id = p.id
        JOIN types t ON wp.type_id = t.id
        WHERE p.identifier = 'demo-project'
          AND t.name = 'Epic'
          AND wp.subject = 'QA Regression: Sprint-2026-06'
        """
        result = op_db_query(sql)
        if not result:
            check(label, 2, False, "epic not found")
            return

        t_total = sum(s["total"] for s in TRUTH_SUMMARY.values())
        t_passed = sum(s["passed"] for s in TRUTH_SUMMARY.values())
        exp_rate = round(t_passed / t_total * 100, 2) if t_total else 0.0

        desc = result.replace("\\", "").strip()
        m = re.fullmatch(
            r"Run Date: 2026-06-10; Report: devops-configs/docs/qa-report-2026-06\.md; "
            r"Total: (\d+); Pass Rate: ([0-9]+(?:\.[0-9]+)?)%",
            desc,
        )
        errors = []
        if not m:
            errors.append(f"description does not match template: '{desc[:100]}'")
        else:
            if int(m.group(1)) != t_total:
                errors.append(f"Total {m.group(1)} != truth {t_total}")
            # accept 100.0 / 100.00 style numeric equivalence
            if abs(float(m.group(2)) - exp_rate) > 0.005:
                errors.append(f"Pass Rate {m.group(2)} != truth {exp_rate}")

        check(label, 2, not errors, "; ".join(errors))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def _get_epic_id() -> str:
    return op_db_query("""
    SELECT wp.id
    FROM work_packages wp
    JOIN projects p ON wp.project_id = p.id
    JOIN types t ON wp.type_id = t.id
    WHERE p.identifier = 'demo-project'
      AND t.name = 'Epic'
      AND wp.subject = 'QA Regression: Sprint-2026-06'
    """).strip().split("\n")[0].strip()


def check_15_op_child_bugs():
    """Child Bug count == VERIFIER-measured failed test count (from TRUTH; the
    zero-failure branch is gated on the recomputed truth, never on agent rows)."""
    label = "15. Child Bug count matches recomputed failed-test count"
    try:
        if not _truth_gate(label, 2):
            return
        epic_id = _get_epic_id()
        if not epic_id:
            check(label, 2, False, "epic not found")
            return

        expected_failed = len(_truth_failed_tests())

        bug_count_str = op_db_query(f"""
        SELECT COUNT(*)
        FROM work_packages wp
        JOIN types t ON wp.type_id = t.id
        WHERE wp.parent_id = {epic_id}
          AND t.name = 'Bug'
        """).strip()
        try:
            bug_count = int(bug_count_str)
        except ValueError:
            bug_count = -1

        passed = bug_count == expected_failed
        check(label, 2, passed,
              f"truth failed tests={expected_failed}, child Bugs found={bug_count}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_16_op_bug_details():
    """When truth has failures: exact Bug subject set {'[<Project>] Test fail:
    <Test Name>'}, priority High/Normal by flaky list, and description
    'File:/Category:/Last Run:' exact per Bug. Zero-failure branch gated on
    VERIFIER-measured failed count."""
    label = "16. Bug subjects/priorities/descriptions correct"
    try:
        if not _truth_gate(label, 2):
            return
        epic_id = _get_epic_id()
        if not epic_id:
            check(label, 2, False, "epic not found")
            return

        failed_tests = _truth_failed_tests()
        expected_subjects = {f"[{proj}] Test fail: {name}": (proj, name)
                             for proj, name in failed_tests}

        result = op_db_query(f"""
        SELECT wp.id, e.name
        FROM work_packages wp
        JOIN types t ON wp.type_id = t.id
        JOIN enumerations e ON wp.priority_id = e.id
        WHERE wp.parent_id = {epic_id}
          AND t.name = 'Bug'
        """)
        bug_rows = [r for r in result.split("\n") if r.strip()]

        if not expected_subjects:
            # Zero failed tests (verifier-measured) -> zero child Bugs is correct.
            check(label, 2, len(bug_rows) == 0,
                  f"truth failed=0, child Bugs found={len(bug_rows)}"
                  + ("" if not bug_rows else " (expected 0)"))
            return

        errors = []
        actual = {}  # subject -> (priority, description)
        duplicate = False
        for r in bug_rows:
            parts = r.split("|")
            if len(parts) < 2:
                continue
            wp_id, priority = parts[0].strip(), parts[1].strip()
            subj = op_db_query(f"SELECT wp.subject FROM work_packages wp WHERE wp.id = {wp_id}").strip()
            desc = op_db_query(f"SELECT wp.description FROM work_packages wp WHERE wp.id = {wp_id}")
            if subj in actual:
                duplicate = True
            actual[subj] = (priority, desc)
        if duplicate:
            errors.append("duplicate Bug subjects")

        if set(actual.keys()) != set(expected_subjects.keys()):
            missing = sorted(set(expected_subjects) - set(actual))
            extra = sorted(set(actual) - set(expected_subjects))
            if missing:
                errors.append(f"{len(missing)} expected Bug subjects missing (e.g. '{missing[0][:70]}')")
            if extra:
                errors.append(f"{len(extra)} unexpected Bug subjects (e.g. '{extra[0][:70]}')")

        # per-Bug priority + description for the matched subjects
        for subj, (proj, name) in expected_subjects.items():
            if subj not in actual:
                continue
            priority, desc = actual[subj]
            is_flaky = f"{proj}::{name}" in KNOWN_FLAKY
            exp_priority = "Normal" if is_flaky else "High"
            if priority != exp_priority:
                errors.append(f"'{subj[:50]}': priority expected {exp_priority}, got {priority}")

            # description exactly "File: <Test File>; Category: <Suite Category>; Last Run: <Last Run>"
            # File/Category taken from the matching Baserow row (row correctness is
            # gated by ck4/ck5 against truth); Last Run must be the 2026-06-10 date.
            row = next((r for r in _test_cases_rows
                        if str(_get_field_value(r, "Project", _test_cases_fields) or "") == proj
                        and str(_get_field_value(r, "Test Name", _test_cases_fields) or "") == name),
                       None)
            desc_clean = desc.replace("\\", "").strip()
            m = re.fullmatch(r"File: (.*?); Category: (.*?); Last Run: (.*)", desc_clean)
            if not m:
                errors.append(f"'{subj[:50]}': description template mismatch: '{desc_clean[:80]}'")
                continue
            d_file, d_cat, d_lastrun = m.group(1), m.group(2), m.group(3).strip()
            if row is not None:
                row_file = str(_get_field_value(row, "Test File", _test_cases_fields) or "")
                row_cat = str(_get_field_value(row, "Suite Category", _test_cases_fields) or "")
                if d_file != row_file:
                    errors.append(f"'{subj[:50]}': File '{d_file[:40]}' != row '{row_file[:40]}'")
                if d_cat != row_cat:
                    errors.append(f"'{subj[:50]}': Category '{d_cat}' != row '{row_cat}'")
            else:
                errors.append(f"'{subj[:50]}': no matching Baserow Test Cases row")
            if not d_lastrun.startswith(RUN_DATE):
                errors.append(f"'{subj[:50]}': Last Run '{d_lastrun[:20]}' != {RUN_DATE}")

        check(label, 2, not errors, "; ".join(errors[:4]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_17_field_schema():
    """Both tables' field schema: exact field-name sets, primary fields, types,
    exact single-select option sets, Flaky boolean, Last Run date, Pass Rate Pct
    number with 2 decimal places."""
    label = "17. Field schema of both tables correct"
    try:
        if not _test_cases_fields or not _summary_fields:
            check(label, 1, False, "tables/fields not loaded")
            return

        errors = []

        # ── Table 1: Test Cases ──
        exp_tc_names = {"Test ID", "Project", "Test Name", "Test File",
                        "Suite Category", "Status", "Flaky", "Last Run"}
        if set(_test_cases_fields.keys()) != exp_tc_names:
            errors.append(f"Test Cases fields {sorted(_test_cases_fields.keys())} "
                          f"!= expected {sorted(exp_tc_names)}")

        def _f(fields, name):
            return fields.get(name) or {}

        tc_specs = [
            ("Test ID", "text", None, True),
            ("Project", "single_select", {"tabler", "json"}, None),
            ("Test Name", "text", None, None),
            ("Test File", "text", None, None),
            ("Suite Category", "single_select", {"Unit", "Integration", "Regression", "Smoke"}, None),
            ("Status", "single_select", {"Pass", "Fail", "Skipped"}, None),
            ("Flaky", "boolean", None, None),
            ("Last Run", "date", None, None),
        ]
        for name, exp_type, exp_opts, exp_primary in tc_specs:
            f = _f(_test_cases_fields, name)
            if not f:
                continue  # already reported by the name-set assertion
            if f.get("type") != exp_type:
                errors.append(f"Test Cases '{name}' type '{f.get('type')}' != '{exp_type}'")
            if exp_opts is not None and _select_option_values(f) != exp_opts:
                errors.append(f"Test Cases '{name}' options {sorted(_select_option_values(f))} "
                              f"!= {sorted(exp_opts)}")
            if exp_primary is not None and bool(f.get("primary")) != exp_primary:
                errors.append(f"Test Cases '{name}' primary={f.get('primary')}, expected {exp_primary}")

        # ── Table 2: Project Run Summary ──
        exp_sum_names = {"Project", "Total Tests", "Passed", "Failed", "Skipped",
                         "Flaky Count", "Pass Rate Pct", "Run Verdict"}
        if set(_summary_fields.keys()) != exp_sum_names:
            errors.append(f"Project Run Summary fields {sorted(_summary_fields.keys())} "
                          f"!= expected {sorted(exp_sum_names)}")

        sum_specs = [
            ("Project", "single_select", {"tabler", "json"}, True),
            ("Total Tests", "number", None, None),
            ("Passed", "number", None, None),
            ("Failed", "number", None, None),
            ("Skipped", "number", None, None),
            ("Flaky Count", "number", None, None),
            ("Pass Rate Pct", "number", None, None),
            ("Run Verdict", "single_select", {"Green", "Yellow", "Red"}, None),
        ]
        for name, exp_type, exp_opts, exp_primary in sum_specs:
            f = _f(_summary_fields, name)
            if not f:
                continue
            if f.get("type") != exp_type:
                errors.append(f"Summary '{name}' type '{f.get('type')}' != '{exp_type}'")
            if exp_opts is not None and _select_option_values(f) != exp_opts:
                errors.append(f"Summary '{name}' options {sorted(_select_option_values(f))} "
                              f"!= {sorted(exp_opts)}")
            if exp_primary is not None and bool(f.get("primary")) != exp_primary:
                errors.append(f"Summary '{name}' primary={f.get('primary')}, expected {exp_primary}")

        prp = _f(_summary_fields, "Pass Rate Pct")
        if prp and prp.get("type") == "number" and prp.get("number_decimal_places") != 2:
            errors.append(f"Pass Rate Pct number_decimal_places="
                          f"{prp.get('number_decimal_places')}, expected 2")

        check(label, 1, not errors, "; ".join(errors[:4]))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_truth_recompute()
    check_1_baserow_db_exists()
    check_2_test_cases_table()
    check_3_summary_table()
    check_4_test_cases_rows_vs_truth()
    check_5_suite_category()
    check_6_flaky_flags()
    check_7_last_run_date()
    check_8_summary_values()
    check_9_failures_only_view()
    check_10_flaky_tests_view()
    check_11_qa_report_exists()
    check_12_qa_report_content()
    check_13_op_epic_exists()
    check_14_op_epic_description()
    check_15_op_child_bugs()
    check_16_op_bug_details()
    check_17_field_schema()

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
