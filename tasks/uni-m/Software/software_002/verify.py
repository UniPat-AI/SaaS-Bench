"""
Verifier for Software-002-I3: Test Execution Audit for data-analyzer and todo-api

Checks: 13 weighted checks (21 pts; check 1 is a 0pt diagnostic) across
code-server, baserow, openproject. Ground truth for test counts is recomputed
by re-running both test commands in a throwaway container from the code-server
container's own pristine image (once, cached) — agent-entered numbers are never
trusted and agent edits to the live workspace cannot skew the truth.
Strategy: code-server=docker exec (deliverables) + pristine-image docker run
(ground truth), baserow=REST API, openproject=docker exec psql

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import json
import re
import subprocess
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_missing = []
if not CODE_SERVER_CONTAINER:
    _missing.append("CODE_SERVER_CONTAINER")
if not BASEROW_PORT:
    _missing.append("BASEROW_PORT")
if not BASEROW_CONTAINER:
    _missing.append("BASEROW_CONTAINER")
if not OPENPROJECT_CONTAINER:
    _missing.append("OPENPROJECT_CONTAINER")
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15,
                exec_args: tuple[str, ...] = ()) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", *exec_args, container, *args],
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


def baserow_auth() -> str:
    """Authenticate to Baserow and return JWT access token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    # token-auth returns access_token or token depending on version
    return data.get("access_token", data.get("token", ""))


def baserow_get(path: str, token: str) -> dict:
    resp = requests.get(
        f"{BASEROW_URL}{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def op_sql(query: str) -> str:
    """Run a SQL query against OpenProject's embedded postgres."""
    # Use env var for password and pass query via -c to avoid shell escaping issues
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "localhost", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql error: {r.stderr.strip()}")
    return r.stdout.strip()


def _oneline(text: str, limit: int = 4000) -> str:
    """Flatten text to a single line so check details stay protocol-safe."""
    flat = text.replace("\r", "").replace("\n", "\\n")
    return flat if len(flat) <= limit else flat[:limit] + "..."


# ── Shared state for Baserow checks ──────────────────────────────────────────
_br_token = None
_br_db_id = None
_br_table_id = None
_br_fields = {}   # field name -> field object
_br_rows = []     # list of row dicts


def _init_baserow():
    """Fetch Baserow DB, table, fields, and rows. Called once."""
    global _br_token, _br_db_id, _br_table_id, _br_fields, _br_rows
    _br_token = baserow_auth()

    # Find the database
    apps = baserow_get("/api/applications/", _br_token)
    for app in apps:
        if app.get("name") == "Regression Test Audit March 2026" and app.get("type") == "database":
            _br_db_id = app["id"]
            break

    if not _br_db_id:
        return

    # Find the table
    tables = baserow_get(f"/api/database/tables/database/{_br_db_id}/", _br_token)
    for t in tables:
        if t.get("name") == "Test Execution Audit":
            _br_table_id = t["id"]
            break

    if not _br_table_id:
        return

    # Get fields
    fields_list = baserow_get(f"/api/database/fields/table/{_br_table_id}/", _br_token)
    for f in fields_list:
        _br_fields[f["name"]] = f

    # Get rows
    rows_resp = baserow_get(f"/api/database/rows/table/{_br_table_id}/?user_field_names=true", _br_token)
    _br_rows = rows_resp.get("results", [])


# ── Ground-truth recompute (each command run ONCE, result cached) ─────────────
_TRUTH_CMDS = {
    "data-analyzer": ("/home/coder/workspace/data-analyzer", "pytest tests/test_analyzer.py -v"),
    "todo-api": ("/home/coder/workspace/todo-api", "make test"),
}

_truth: dict | None = None


def _parse_test_summary(out: str) -> tuple[dict | None, str]:
    """Extract passed/failed counts from the pytest summary line ONLY.

    The summary line is the last line matching ^=+ .* =+$ or containing
    ' passed' (make echoes other lines; numbers are never taken from them).
    Returns ({'passed': n, 'failed': n}, summary) or (None, reason).
    """
    summary = ""
    for line in out.splitlines():
        s = line.strip()
        if re.match(r"^=+ .* =+$", s) or " passed" in s:
            summary = s
    if not summary:
        return None, "no pytest summary line found"
    if re.search(r"(\d+) error", summary):
        return None, f"errors in summary: {summary}"
    m_passed = re.search(r"(\d+) passed", summary)
    if not m_passed:
        return None, f"no passed count in summary: {summary}"
    m_failed = re.search(r"(\d+) failed", summary)
    failed = int(m_failed.group(1)) if m_failed else 0
    return {"passed": int(m_passed.group(1)), "failed": failed}, summary


def _get_truth() -> dict:
    """Re-run both test commands once each (in a throwaway container from the
    code-server container's pristine image, never the agent-touched live one)
    and cache the parsed ground truth.

    Per project: {'ok': bool, 'passed', 'failed', 'rate', 'verdict', 'detail'}.
    If truth acquisition fails for a project, ok=False and every check that
    depends on it must FAIL (never fall back to agent-entered data).
    """
    global _truth
    if _truth is not None:
        return _truth
    truth = {}
    for proj, (workdir, cmd) in _TRUTH_CMDS.items():
        entry = {"ok": False, "detail": ""}
        try:
            rc, out, err = image_exec(
                "bash", "-lc", f"cd {workdir} && {cmd}",
                timeout=240,
            )
            counts, summary = _parse_test_summary(out)
            tail = _oneline("\n".join((out + "\n" + err).splitlines()[-30:]), 1500)
            if counts is None:
                entry["detail"] = f"{summary}; output tail: {tail}"
            elif rc != 0 and counts["failed"] == 0:
                entry["detail"] = (f"command failed (rc={rc}) without failed tests; "
                                   f"output tail: {tail}")
            elif counts["passed"] + counts["failed"] == 0:
                entry["detail"] = f"no tests ran; summary: {summary}"
            else:
                total = counts["passed"] + counts["failed"]
                rate = round(counts["passed"] / total * 100, 2)
                entry.update({
                    "ok": True,
                    "passed": counts["passed"],
                    "failed": counts["failed"],
                    "rate": rate,
                    "verdict": "Pass" if rate >= 85.00 else "Fail",
                    "detail": summary,
                })
        except Exception as e:
            entry["detail"] = f"exception: {e}"
        truth[proj] = entry
    _truth = truth
    return _truth


def _truth_str(t: dict) -> str:
    """Short measured-truth summary for check details."""
    if t.get("ok"):
        return (f"measured: {t['passed']} passed/{t['failed']} failed, "
                f"rate={t['rate']}, verdict={t['verdict']} [{t['detail']}]")
    return f"truth unavailable: {t.get('detail', '')}"


def _rate_regex(rate: float) -> str:
    """Regex matching the truth pass rate (e.g. 100 -> 100, 100.0, 100.00)."""
    if float(rate).is_integer():
        return rf"\b{int(rate)}(\.0{{1,2}})?\b"
    return rf"\b{re.escape(f'{rate:.2f}')}\b"


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_projects_exist():
    """Diagnostic (0pt): data-analyzer and todo-api project dirs exist (seeded)."""
    try:
        rc1, out1, _ = docker_exec(CODE_SERVER_CONTAINER, "test", "-d", "/home/coder/workspace/data-analyzer")
        rc2, out2, _ = docker_exec(CODE_SERVER_CONTAINER, "test", "-d", "/home/coder/workspace/todo-api")
        both = (rc1 == 0 and rc2 == 0)
        detail = ""
        if rc1 != 0:
            detail += "data-analyzer not found; "
        if rc2 != 0:
            detail += "todo-api not found; "
        check("1. Project dirs exist (diagnostic)", 0, both, detail.rstrip("; "))
    except Exception as e:
        check("1. Project dirs exist (diagnostic)", 0, False, f"exception: {e}")


def check_2_baserow_db_exists():
    """Verify Baserow database 'Regression Test Audit March 2026' exists."""
    try:
        _init_baserow()
        check("2. Baserow DB 'Regression Test Audit March 2026' exists", 1,
              _br_db_id is not None,
              "" if _br_db_id else "database not found")
    except Exception as e:
        check("2. Baserow DB 'Regression Test Audit March 2026' exists", 1, False, f"exception: {e}")


def check_3_baserow_table_exists():
    """Verify table 'Test Execution Audit' exists in the DB."""
    check("3. Table 'Test Execution Audit' exists", 1,
          _br_table_id is not None,
          "" if _br_table_id else "table not found")


def _fmt_field(f: dict | None) -> str:
    if f is None:
        return "missing"
    return (f"type={f.get('type')}, primary={f.get('primary')}, "
            f"decimals={f.get('number_decimal_places')}")


def check_s_field_schema():
    """Verify the field schema of 'Test Execution Audit' (types/primary/options)."""
    try:
        if not _br_fields:
            check("S. Baserow field schema", 2, False, "fields not loaded (table missing?)")
            return
        issues = []

        f = _br_fields.get("Project")
        if not f or f.get("type") != "text" or not f.get("primary"):
            issues.append(f"Project: expected primary text, got {_fmt_field(f)}")

        for name in ("Tests Passed", "Tests Failed"):
            f = _br_fields.get(name)
            if not f or f.get("type") != "number" or f.get("number_decimal_places") != 0:
                issues.append(f"{name}: expected number with 0 decimals, got {_fmt_field(f)}")

        f = _br_fields.get("Pass Rate")
        if not f or f.get("type") != "number" or f.get("number_decimal_places") != 2:
            issues.append(f"Pass Rate: expected number with 2 decimals, got {_fmt_field(f)}")

        f = _br_fields.get("Pass/Fail")
        opts = {o.get("value") for o in (f.get("select_options") or [])} if f else set()
        if not f or f.get("type") != "single_select" or opts != {"Pass", "Fail"}:
            issues.append(f"Pass/Fail: expected single_select with options {{Pass, Fail}}, "
                          f"got {_fmt_field(f)}, options={sorted(str(o) for o in opts)}")

        f = _br_fields.get("Captured At")
        if not f or f.get("type") != "date":
            issues.append(f"Captured At: expected date, got {_fmt_field(f)}")

        check("S. Baserow field schema", 2, not issues,
              "; ".join(issues) if issues else "all field types/options correct")
    except Exception as e:
        check("S. Baserow field schema", 2, False, f"exception: {e}")


def check_4_exactly_two_rows():
    """Verify the table has exactly 2 rows."""
    n = len(_br_rows)
    check("4. Table has exactly 2 rows", 2,
          n == 2,
          f"found {n} rows")


def _find_row(project_name: str) -> dict | None:
    """Find a row by Project field value (case-insensitive)."""
    for row in _br_rows:
        val = row.get("Project", "")
        if isinstance(val, str) and val.strip().lower() == project_name.lower():
            return row
    return None


def _check_counts_vs_truth(num: int, proj: str) -> None:
    """Row (Tests Passed, Tests Failed) must equal the recomputed truth (tolerance 0)."""
    label = f"{num}. {proj} counts match recomputed truth"
    try:
        t = _get_truth()[proj]
        if not t["ok"]:
            check(label, 2, False, _truth_str(t))
            return
        row = _find_row(proj)
        if not row:
            check(label, 2, False, f"row not found; {_truth_str(t)}")
            return
        tp = row.get("Tests Passed")
        tf = row.get("Tests Failed")
        try:
            tp_f = float(str(tp))
            tf_f = float(str(tf))
        except (ValueError, TypeError):
            check(label, 2, False, f"non-numeric values (tp={tp}, tf={tf}); {_truth_str(t)}")
            return
        passed = tp_f == t["passed"] and tf_f == t["failed"]
        detail = f"Tests Passed={tp}, Tests Failed={tf}; {_truth_str(t)}"
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_data_analyzer_counts():
    """Verify data-analyzer row counts equal the verifier-recomputed truth."""
    _check_counts_vs_truth(5, "data-analyzer")


def check_6_todo_api_counts():
    """Verify todo-api row counts equal the verifier-recomputed truth."""
    _check_counts_vs_truth(6, "todo-api")


def check_7_pass_rate_correct():
    """Verify Pass Rate == round(truth_tp/(truth_tp+truth_tf)*100, 2) from recomputed truth."""
    try:
        all_ok = True
        details = []
        for proj in ["data-analyzer", "todo-api"]:
            t = _get_truth()[proj]
            if not t["ok"]:
                all_ok = False
                details.append(f"{proj}: {_truth_str(t)}")
                continue
            row = _find_row(proj)
            if not row:
                all_ok = False
                details.append(f"{proj}: row not found")
                continue
            pr = row.get("Pass Rate")
            if pr is None:
                all_ok = False
                details.append(f"{proj}: Pass Rate missing")
                continue
            try:
                pr_f = float(str(pr))
            except (ValueError, TypeError):
                all_ok = False
                details.append(f"{proj}: non-numeric Pass Rate {pr}")
                continue
            expected_rate = round(t["passed"] / (t["passed"] + t["failed"]) * 100, 2)
            if abs(pr_f - expected_rate) < 0.005:
                details.append(f"{proj}: rate={pr_f} == truth {expected_rate}")
            else:
                all_ok = False
                details.append(f"{proj}: expected truth rate {expected_rate}, got {pr_f}")
        check("7. Pass Rate matches recomputed truth", 2, all_ok, "; ".join(details))
    except Exception as e:
        check("7. Pass Rate matches recomputed truth", 2, False, f"exception: {e}")


def check_8_pass_fail_threshold():
    """Verify Pass/Fail equals the truth-derived verdict vs the 85.00 threshold."""
    try:
        all_ok = True
        details = []
        for proj in ["data-analyzer", "todo-api"]:
            t = _get_truth()[proj]
            if not t["ok"]:
                all_ok = False
                details.append(f"{proj}: {_truth_str(t)}")
                continue
            row = _find_row(proj)
            if not row:
                all_ok = False
                details.append(f"{proj}: row not found")
                continue
            pf = row.get("Pass/Fail")
            if pf is None:
                all_ok = False
                details.append(f"{proj}: Pass/Fail missing")
                continue
            # Pass/Fail may be a dict (single_select) or string
            pf_val = pf
            if isinstance(pf, dict):
                pf_val = pf.get("value", "")
            pf_str = str(pf_val).strip()
            expected_pf = t["verdict"]
            if pf_str.lower() != expected_pf.lower():
                all_ok = False
                details.append(f"{proj}: truth rate={t['rate']}, expected {expected_pf}, got {pf_str}")
            else:
                details.append(f"{proj}: {pf_str} correct for truth rate {t['rate']}")
        check("8. Pass/Fail matches truth verdict (85.00 threshold)", 2, all_ok, "; ".join(details))
    except Exception as e:
        check("8. Pass/Fail matches truth verdict (85.00 threshold)", 2, False, f"exception: {e}")


def check_9_captured_at():
    """Verify Captured At is populated and parses as a YYYY-MM-DD date."""
    try:
        all_ok = True
        details = []
        for proj in ["data-analyzer", "todo-api"]:
            row = _find_row(proj)
            if not row:
                all_ok = False
                details.append(f"{proj}: row not found")
                continue
            ca = row.get("Captured At")
            ca_str = str(ca).strip() if ca is not None else ""
            if not ca_str:
                all_ok = False
                details.append(f"{proj}: Captured At empty")
            elif not re.match(r"^\d{4}-\d{2}-\d{2}", ca_str):
                all_ok = False
                details.append(f"{proj}: Captured At '{ca_str}' not YYYY-MM-DD")
            else:
                details.append(f"{proj}: {ca_str}")
        check("9. Captured At is a valid date", 1, all_ok, "; ".join(details))
    except Exception as e:
        check("9. Captured At is a valid date", 1, False, f"exception: {e}")


def check_10_op_work_package_exists():
    """Verify OpenProject has exactly one Task WP 'Test Execution Audit Report' in 'product-catalog'."""
    try:
        result = op_sql(
            "SELECT count(*), COALESCE(string_agg(wp.id::text, ','), '') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.identifier = 'product-catalog' "
            "AND wp.subject = 'Test Execution Audit Report' "
            "AND t.name = 'Task'"
        )
        parts = result.split("|")
        count = int(parts[0]) if parts and parts[0].strip() else 0
        ids = parts[1] if len(parts) > 1 else ""
        check("10. Exactly one audit Task WP in product-catalog", 2,
              count == 1,
              f"count={count}, ids=[{ids}]")
    except Exception as e:
        check("10. Exactly one audit Task WP in product-catalog", 2, False, f"exception: {e}")


def _project_block(desc: str, proj: str) -> str:
    """Line block for a project: from the line containing the project name to
    the next line containing another project name (or end of description).
    Falls back to the whole description when no per-project block is found."""
    names = ["data-analyzer", "todo-api"]
    lines = desc.splitlines()
    start = None
    for i, line in enumerate(lines):
        if proj in line.lower():
            start = i
            break
    if start is None:
        return desc
    end = len(lines)
    others = [n for n in names if n != proj]
    for j in range(start + 1, len(lines)):
        if any(n in lines[j].lower() for n in others):
            end = j
            break
    return "\n".join(lines[start:end])


def _check_desc_metrics(num: int, proj: str, require_threshold: bool) -> None:
    """WP description block for the project must contain the truth-derived
    passed/failed counts, pass rate, and verdict word (regexes built from truth)."""
    label = f"{num}. WP description has {proj} truth metrics"
    try:
        result = op_sql(
            "SELECT wp.description FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "WHERE p.identifier = 'product-catalog' "
            "AND wp.subject = 'Test Execution Audit Report'"
        )
        if not result:
            check(label, 2, False, "WP not found")
            return
        t = _get_truth()[proj]
        if not t["ok"]:
            check(label, 2, False, _truth_str(t))
            return
        if proj not in result.lower():
            check(label, 2, False,
                  f"'{proj}' not mentioned; description: {_oneline(result)}")
            return
        block = _project_block(result, proj)
        verdict_rx = r"\bpass(ed)?\b" if t["verdict"] == "Pass" else r"\bfail(ed)?\b"
        required = {
            f"passed count {t['passed']}": rf"\b{t['passed']}\b",
            f"failed count {t['failed']}": rf"\b{t['failed']}\b",
            f"rate {t['rate']}": _rate_regex(t["rate"]),
            f"verdict word ({t['verdict']})": verdict_rx,
        }
        missing = [name for name, rx in required.items()
                   if not re.search(rx, block, re.IGNORECASE)]
        if require_threshold and not re.search(r"85(\.00?)?", result):
            missing.append("threshold 85.00")
        ok = not missing
        if ok:
            detail = f"all truth metrics present; {_truth_str(t)}"
        else:
            detail = (f"missing: {', '.join(missing)}; {_truth_str(t)}; "
                      f"description: {_oneline(result)}")
        check(label, 2, ok, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_op_desc_data_analyzer():
    """Verify WP description block for data-analyzer carries truth metrics."""
    _check_desc_metrics(11, "data-analyzer", require_threshold=False)


def check_12_op_desc_todo_api():
    """Verify WP description block for todo-api carries truth metrics + threshold."""
    _check_desc_metrics(12, "todo-api", require_threshold=True)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_projects_exist()
    check_2_baserow_db_exists()
    check_3_baserow_table_exists()
    check_s_field_schema()
    check_4_exactly_two_rows()
    check_5_data_analyzer_counts()
    check_6_todo_api_counts()
    check_7_pass_rate_correct()
    check_8_pass_fail_threshold()
    check_9_captured_at()
    check_10_op_work_package_exists()
    check_11_op_desc_data_analyzer()
    check_12_op_desc_todo_api()

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
