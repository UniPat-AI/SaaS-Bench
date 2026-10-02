#!/usr/bin/env python3
"""
Verifier for Software-031-I4: Sprint retrospective data-gathering for Pentest Round 1
in Security Audit project.

Checks: 14 weighted checks across openproject, code-server, baserow.
Strategy: verifier recomputes ground truth itself (never trusts agent-filled data):
  - OpenProject psql (docker exec) for the 'Pentest Round 1' work-package row set
  - test-suite truth runs in a throwaway container from the live code-server
    container's own PRISTINE image (immune to agent edits): full
    `python3 -m pytest tests/ -v` rerun for data-analyzer and the `ctest -N`
    total-tests anchor for json (the full json ctest suite is STRICTLY NOT run:
    it exceeds the 300s verify budget); `find tests/ -type f | wc -l` test-file
    counts still read the live container (they validate delivered state)
  - Baserow REST (JWT) for field schema / row values, psql for tables & views
If a truth recompute fails, every truth-dependent check FAILs (no fallback to
agent data).

Required env vars:
  SERVER_HOSTNAME, OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENPROJECT_PORT = os.getenv("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.getenv("OPENPROJECT_CONTAINER")
CODE_SERVER_PORT = os.getenv("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.getenv("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.getenv("BASEROW_PORT")
BASEROW_CONTAINER = os.getenv("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.getenv("BASEROW_DB_CONTAINER")

_missing = []
for var in [
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
]:
    if not os.getenv(var):
        _missing.append(var)
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"

SPRINT_FIELD_NAMES = {"WP ID", "Subject", "Type", "Status", "Estimated Hours", "Closed"}
HEALTH_FIELD_NAMES = {"Project", "Tests Passed", "Tests Failed", "Test Files Count",
                      "Pass Rate", "Health Badge"}

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


def _d(s, limit: int = 260) -> str:
    """Sanitize a detail string: single line, bounded length."""
    s = str(s).replace("\r", " ").replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "..."


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


def baserow_sql(query: str) -> str:
    """Run a SQL query against the Baserow postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"baserow psql failed (rc={rc}): {err.strip()[:300]}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def openproject_sql(query: str, field_sep: str = "|") -> str:
    """Run a SQL query against the OpenProject postgres DB (embedded in app container).

    Raises on failure so truth-dependent checks FAIL instead of silently passing."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject",
        "-d", "openproject", "-t", "-A", "-F", field_sep, "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"openproject psql failed (rc={rc}): {err.strip()[:300]}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def find_table_ids(table_name: str) -> list[str]:
    """Return ids of non-trashed tables named `table_name` in the target DB.

    Returns a list so callers can iterate candidates instead of interpolating
    a possibly-multiline psql result into follow-up SQL."""
    out = baserow_sql(
        "SELECT dt.id FROM database_table dt "
        "JOIN database_database da ON dt.database_id = da.application_ptr_id "
        "JOIN core_application ca ON da.application_ptr_id = ca.id "
        "WHERE ca.name = 'Retro Pentest Round 1' "
        f"AND dt.name = '{table_name}' AND dt.trashed = false "
        "ORDER BY dt.id;"
    )
    return [line.strip() for line in out.split("\n") if line.strip()] if out else []


# ── Baserow REST helpers ──────────────────────────────────────────────────────
def _http_json(method: str, url: str, payload=None, token: str | None = None,
               timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"JWT {token}")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


_jwt_token: str | None = None


def baserow_token() -> str:
    global _jwt_token
    if _jwt_token is None:
        resp = _http_json(
            "POST", f"{BASEROW_BASE}/api/user/token-auth/",
            payload={"email": "admin@example.com", "password": "Admin1234"},
        )
        _jwt_token = resp.get("access_token") or resp.get("token")
        if not _jwt_token:
            raise RuntimeError("baserow token-auth returned no token")
    return _jwt_token


_fields_cache: dict[str, list] = {}


def baserow_fields(table_id: str) -> list:
    if table_id not in _fields_cache:
        _fields_cache[table_id] = _http_json(
            "GET", f"{BASEROW_BASE}/api/database/fields/table/{table_id}/",
            token=baserow_token(),
        )
    return _fields_cache[table_id]


def baserow_rows(table_id: str) -> list:
    rows: list = []
    url = f"{BASEROW_BASE}/api/database/rows/table/{table_id}/?user_field_names=true&size=200"
    while url:
        data = _http_json("GET", url, token=baserow_token())
        rows.extend(data.get("results", []))
        url = data.get("next")
    return rows


def pick_table(table_name: str, expected_field_names: set[str]) -> str | None:
    """Pick the table used for row-level checks: first candidate whose field-name
    set matches the spec, else the first candidate."""
    ids = find_table_ids(table_name)
    if not ids:
        return None
    for tid in ids:
        try:
            names = {f.get("name") for f in baserow_fields(tid)}
        except Exception:
            continue
        if names == expected_field_names:
            return tid
    return ids[0]


# ── Value coercions ───────────────────────────────────────────────────────────
def _num(v) -> float | None:
    """Coerce a Baserow number value (str/int/float/None/'') to float or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _sel(v) -> str | None:
    """Extract the value of a Baserow single-select cell."""
    if isinstance(v, dict):
        return v.get("value")
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _eq(a, b, tol: float = 0.005) -> bool:
    return a is not None and b is not None and abs(a - b) <= tol


def badge_for(rate: float) -> str:
    return "Green" if rate >= 95 else ("Yellow" if rate >= 80 else "Red")


# ── Ground truth (memoized; failure => dependent checks FAIL) ────────────────
_memo: dict = {}


def _once(key: str, fn):
    if key not in _memo:
        try:
            _memo[key] = (fn(), None)
        except Exception as e:
            _memo[key] = (None, _d(f"{type(e).__name__}: {e}"))
    return _memo[key]


def _op_truth_impl() -> list[dict]:
    """Recompute the 'Pentest Round 1' work-package row set from OpenProject psql."""
    sep = "\x1f"
    out = openproject_sql(
        "SELECT wp.id, wp.subject, t.name, s.name, COALESCE(wp.estimated_hours::text, '') "
        "FROM work_packages wp "
        "JOIN versions v ON wp.version_id = v.id "
        "JOIN projects p ON wp.project_id = p.id "
        "JOIN types t ON wp.type_id = t.id "
        "JOIN statuses s ON wp.status_id = s.id "
        "WHERE p.name = 'Security Audit' AND v.name = 'Pentest Round 1' "
        "ORDER BY wp.id;",
        field_sep=sep,
    )
    rows = []
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split(sep)
        if len(parts) != 5:
            raise RuntimeError(f"unparseable psql row: {line[:120]!r}")
        wp_id, subject, wp_type, status, est_raw = parts
        rows.append({
            "id": int(wp_id),
            "subject": subject,
            "type": wp_type,
            "status": status,
            "est": float(est_raw) if est_raw.strip() else None,
            "closed": status == "Closed",
        })
    if not rows:
        raise RuntimeError("OP truth query returned no work packages for version 'Pentest Round 1'")
    return rows


def get_op_truth():
    return _once("op_truth", _op_truth_impl)


def _pytest_truth_impl() -> tuple[int, int]:
    """Actually rerun the data-analyzer test suite (fast, ~1.5s) and parse the tail.

    Truth runs in a throwaway container from the PRISTINE image, not the live
    (agent-touched) container."""
    rc, out, err = image_exec(
        "bash", "-c",
        "cd /home/coder/workspace/data-analyzer && python3 -m pytest tests/ -v 2>&1 | tail -15",
        timeout=220,
    )
    text = out + "\n" + err
    pm = re.findall(r"(\d+) passed", text)
    fm = re.findall(r"(\d+) failed", text)
    em = re.findall(r"(\d+) error", text)
    if not pm and not fm and not em:
        raise RuntimeError(f"could not parse pytest summary (rc={rc}): {text.strip()[-160:]!r}")
    passed = int(pm[-1]) if pm else 0
    failed = (int(fm[-1]) if fm else 0) + (int(em[-1]) if em else 0)
    if passed + failed == 0:
        raise RuntimeError("pytest summary parsed to 0 total tests")
    return passed, failed


def get_pytest_truth():
    return _once("pytest", _pytest_truth_impl)


def _ctest_total_impl() -> int:
    """Total-tests anchor for json via `ctest -N` (<2s), run in a throwaway
    container from the PRISTINE image (agent edits to the build tree cannot
    move the anchor).

    NOTE: the full json ctest suite is deliberately NOT run — it takes several
    minutes and would blow the 300s verify budget (see docs/tightening plan)."""
    rc, out, err = image_exec(
        "bash", "-c",
        "ctest --test-dir /home/coder/workspace/json/build -N 2>/dev/null | tail -1",
        timeout=120,
    )
    m = re.search(r"Total Tests:\s*(\d+)", out)
    if not m:
        raise RuntimeError(f"could not parse 'ctest -N' output (rc={rc}): {out.strip()[-120:]!r}")
    total = int(m.group(1))
    if total <= 0:
        raise RuntimeError("ctest -N reported 0 total tests")
    return total


def get_ctest_total():
    return _once("ctest_total", _ctest_total_impl)


def _files_counts_impl(project: str) -> set[int]:
    """Test Files Count truth: both readings of the ambiguous spec ('count of files
    under the project's tests/ directory') — direct children and recursive."""
    rc, out, err = docker_exec(
        CODE_SERVER_CONTAINER, "bash", "-c",
        f"find /home/coder/workspace/{project}/tests -maxdepth 1 -type f | wc -l; "
        f"find /home/coder/workspace/{project}/tests -type f | wc -l",
        timeout=60,
    )
    nums = [int(x) for x in out.split() if x.strip().isdigit()]
    if rc != 0 or len(nums) != 2:
        raise RuntimeError(f"find/wc failed for {project} (rc={rc}): {out.strip()[:120]!r}")
    return {nums[0], nums[1]}


def get_files_counts(project: str):
    return _once(f"files_{project}", lambda: _files_counts_impl(project))


def _health_data_impl():
    """Parsed non-placeholder Test Health rows, grouped by Project select value."""
    tid = pick_table("Test Health", HEALTH_FIELD_NAMES)
    if tid is None:
        raise RuntimeError("Test Health table not found")
    by_proj: dict[str, list] = {}
    count = 0
    for raw in baserow_rows(tid):
        row = {
            "proj": _sel(raw.get("Project")),
            "passed": _num(raw.get("Tests Passed")),
            "failed": _num(raw.get("Tests Failed")),
            "files": _num(raw.get("Test Files Count")),
            "rate": _num(raw.get("Pass Rate")),
            "badge": _sel(raw.get("Health Badge")),
        }
        # Skip Baserow's auto-created blank placeholder rows (every field empty).
        if row["proj"] is None and row["badge"] is None and all(
            row[k] is None for k in ("passed", "failed", "files", "rate")
        ):
            continue
        count += 1
        by_proj.setdefault(row["proj"] or "", []).append(row)
    return by_proj, count


def get_health_data():
    return _once("health_data", _health_data_impl)


def get_da_health():
    """(rate, badge) truth for data-analyzer from the verifier's own pytest rerun."""
    def impl():
        (pf, perr) = get_pytest_truth()
        if perr:
            raise RuntimeError(f"pytest rerun failed: {perr}")
        p, f = pf
        rate = round(p / (p + f) * 100, 2)
        return rate, badge_for(rate)
    return _once("da_health", impl)


def get_json_eff():
    """Effective (rate, badge) for json: the Baserow row's numbers, admitted only
    after the three anchor gates (Passed+Failed == ctest total; Pass Rate ==
    round(P/(P+F)*100,2) recomputed; badge recomputed from Pass Rate)."""
    def impl():
        by_proj, count = None, None
        (hd, herr) = get_health_data()
        if herr:
            raise RuntimeError(f"Test Health rows unavailable: {herr}")
        by_proj, count = hd
        (total, cerr) = get_ctest_total()
        if cerr:
            raise RuntimeError(f"ctest -N anchor failed: {cerr}")
        rows = by_proj.get("json", [])
        if len(rows) != 1:
            raise RuntimeError(f"expected exactly 1 json row, found {len(rows)}")
        j = rows[0]
        if j["passed"] is None or j["failed"] is None:
            raise RuntimeError("json row missing Tests Passed/Tests Failed")
        s = j["passed"] + j["failed"]
        if int(round(s)) != total:
            raise RuntimeError(f"json Passed+Failed={s:g} != ctest total {total}")
        rate_e = round(j["passed"] / s * 100, 2)
        if not _eq(j["rate"], rate_e):
            raise RuntimeError(f"json Pass Rate={j['rate']} != recomputed {rate_e}")
        return rate_e, badge_for(rate_e)
    return _once("json_eff", impl)


def get_red_count():
    """R truth: count of Red badges across the recomputed test-health truth."""
    def impl():
        (da, derr) = get_da_health()
        if derr:
            raise RuntimeError(f"data-analyzer truth failed: {derr}")
        (js, jerr) = get_json_eff()
        if jerr:
            raise RuntimeError(f"json truth failed: {jerr}")
        return sum(1 for _, badge in (da, js) if badge == "Red")
    return _once("red_count", impl)


# ── Field schema assertion helper ─────────────────────────────────────────────
def _field_errors(fields: list, expected_names: set[str], spec: dict) -> list[str]:
    errs: list[str] = []
    names = {f.get("name") for f in fields}
    if names != expected_names:
        extra = sorted(names - expected_names)
        missing = sorted(expected_names - names)
        errs.append(f"field name set mismatch (missing={missing}, extra={extra})")
    by_name = {f.get("name"): f for f in fields}
    for name, req in spec.items():
        f = by_name.get(name)
        if f is None:
            continue  # covered by the set mismatch above
        if f.get("type") != req["type"]:
            errs.append(f"{name}: type={f.get('type')!r} expected {req['type']!r}")
        if req.get("primary") and not f.get("primary"):
            errs.append(f"{name}: not primary")
        if "decimal_places" in req and f.get("number_decimal_places") != req["decimal_places"]:
            errs.append(f"{name}: decimal_places={f.get('number_decimal_places')} "
                        f"expected {req['decimal_places']}")
        if "options" in req:
            opts = {o.get("value") for o in (f.get("select_options") or [])}
            if opts != req["options"]:
                errs.append(f"{name}: options={sorted(opts)} expected {sorted(req['options'])}")
    return errs


# ── Baserow checks ────────────────────────────────────────────────────────────
def check_1_baserow_db_exists() -> None:
    """Database 'Retro Pentest Round 1' exists in Baserow."""
    try:
        result = baserow_sql(
            "SELECT d.application_ptr_id FROM database_database d "
            "JOIN core_application a ON d.application_ptr_id = a.id "
            "WHERE a.name = 'Retro Pentest Round 1';"
        )
        found = bool(result.strip())
        check("1. Baserow DB 'Retro Pentest Round 1' exists", 1, found,
              f"db_id={_d(result)}" if found else "not found")
    except Exception as e:
        check("1. Baserow DB 'Retro Pentest Round 1' exists", 1, False, _d(f"exception: {e}"))


def check_2_sprint_wp_table() -> None:
    """Table 'Sprint Work Packages' exists with the exact field schema."""
    label = "2. Table 'Sprint Work Packages' field schema"
    spec = {
        "WP ID": {"type": "number", "primary": True},
        "Subject": {"type": "text"},
        "Type": {"type": "single_select",
                 "options": {"Task", "Bug", "Feature", "Epic", "Milestone"}},
        "Status": {"type": "text"},
        "Estimated Hours": {"type": "number", "decimal_places": 1},
        "Closed": {"type": "boolean"},
    }
    try:
        table_ids = find_table_ids("Sprint Work Packages")
        if not table_ids:
            check(label, 2, False, "table not found")
            return
        best_errs: list[str] | None = None
        for table_id in table_ids:
            errs = _field_errors(baserow_fields(table_id), SPRINT_FIELD_NAMES, spec)
            if not errs:
                check(label, 2, True, "all 6 fields match types/primary/options/decimals")
                return
            if best_errs is None or len(errs) < len(best_errs):
                best_errs = errs
        check(label, 2, False, _d("; ".join(best_errs or ["no candidate table validated"])))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_3_test_health_table() -> None:
    """Table 'Test Health' exists with the exact field schema."""
    label = "3. Table 'Test Health' field schema"
    spec = {
        "Project": {"type": "single_select", "primary": True,
                    "options": {"json", "data-analyzer"}},
        "Tests Passed": {"type": "number"},
        "Tests Failed": {"type": "number"},
        "Test Files Count": {"type": "number"},
        "Pass Rate": {"type": "number", "decimal_places": 2},
        "Health Badge": {"type": "single_select", "options": {"Green", "Yellow", "Red"}},
    }
    try:
        table_ids = find_table_ids("Test Health")
        if not table_ids:
            check(label, 2, False, "table not found")
            return
        best_errs: list[str] | None = None
        for table_id in table_ids:
            errs = _field_errors(baserow_fields(table_id), HEALTH_FIELD_NAMES, spec)
            if not errs:
                check(label, 2, True, "all 6 fields match types/primary/options/decimals")
                return
            if best_errs is None or len(errs) < len(best_errs):
                best_errs = errs
        check(label, 2, False, _d("; ".join(best_errs or ["no candidate table validated"])))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_4_sprint_wp_rows() -> None:
    """'Sprint Work Packages' row set reconciles exactly with OpenProject truth."""
    label = "4. Sprint Work Packages rows match OpenProject truth"
    try:
        (truth, terr) = get_op_truth()
        if terr:
            check(label, 2, False, _d(f"OP truth recompute failed: {terr}"))
            return
        tid = pick_table("Sprint Work Packages", SPRINT_FIELD_NAMES)
        if tid is None:
            check(label, 2, False, "table not found")
            return
        rows = []
        for raw in baserow_rows(tid):
            row = {
                "wp_id": _num(raw.get("WP ID")),
                "subject": (raw.get("Subject") or "").strip(),
                "type": _sel(raw.get("Type")),
                "status": (raw.get("Status") or "").strip(),
                "est": _num(raw.get("Estimated Hours")),
                "closed": bool(raw.get("Closed")),
            }
            # Skip blank placeholder rows (all agent-fillable cells empty).
            if (row["wp_id"] is None and not row["subject"] and row["type"] is None
                    and not row["status"] and row["est"] is None and not row["closed"]):
                continue
            rows.append(row)

        errs: list[str] = []
        if len(rows) != len(truth):
            errs.append(f"row count {len(rows)} != truth {len(truth)}")
        truth_by_id = {t["id"]: t for t in truth}
        seen_ids: set[int] = set()
        for row in rows:
            if row["wp_id"] is None:
                errs.append("row with empty WP ID")
                continue
            wid = int(round(row["wp_id"]))
            if wid in seen_ids:
                errs.append(f"duplicate WP ID {wid}")
                continue
            seen_ids.add(wid)
            t = truth_by_id.get(wid)
            if t is None:
                errs.append(f"WP ID {wid} not in OpenProject version")
                continue
            if row["subject"] != t["subject"]:
                errs.append(f"WP {wid}: Subject={row['subject']!r} != {t['subject']!r}")
            if row["type"] != t["type"]:
                errs.append(f"WP {wid}: Type={row['type']!r} != {t['type']!r}")
            if row["status"] != t["status"]:
                errs.append(f"WP {wid}: Status={row['status']!r} != {t['status']!r}")
            if t["est"] is None:
                # OP NULL estimated_hours tolerates Baserow empty or 0.
                if row["est"] is not None and abs(row["est"]) > 0.005:
                    errs.append(f"WP {wid}: Estimated Hours={row['est']} != NULL truth")
            elif not _eq(row["est"], t["est"]):
                errs.append(f"WP {wid}: Estimated Hours={row['est']} != {t['est']}")
            if row["closed"] != t["closed"]:
                errs.append(f"WP {wid}: Closed={row['closed']} != {t['closed']}")
        missing = sorted(set(truth_by_id) - seen_ids)
        if missing:
            errs.append(f"missing WP IDs {missing[:8]}")
        ok = not errs
        check(label, 2, ok,
              _d(f"{len(rows)} rows reconciled against {len(truth)} OP work packages"
                 if ok else "; ".join(errs[:4]) + (f" (+{len(errs) - 4} more)" if len(errs) > 4 else "")))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_5_test_health_rows() -> None:
    """'Test Health' rows reconcile with rerun test truth (pytest) / ctest anchors."""
    label = "5. Test Health rows match rerun/anchored test truth"
    try:
        (hd, herr) = get_health_data()
        if herr:
            check(label, 2, False, _d(f"Test Health rows unavailable: {herr}"))
            return
        by_proj, count = hd
        (pf, perr) = get_pytest_truth()
        (total, cerr) = get_ctest_total()
        (fset_da, fe1) = get_files_counts("data-analyzer")
        (fset_js, fe2) = get_files_counts("json")
        truth_errs = [f"{msg}: {e}" for e, msg in (
            (perr, "pytest rerun failed"),
            (cerr, "ctest -N anchor failed"),
            (fe1, "data-analyzer tests/ file count failed"),
            (fe2, "json tests/ file count failed"),
        ) if e]
        if truth_errs:
            check(label, 2, False, _d("truth recompute failed - " + "; ".join(truth_errs)))
            return

        errs: list[str] = []
        if count != 2:
            errs.append(f"non-placeholder row count {count} != 2")
        for proj in ("data-analyzer", "json"):
            if len(by_proj.get(proj, [])) != 1:
                errs.append(f"{proj}: {len(by_proj.get(proj, []))} rows (expected exactly 1)")

        p_t, f_t = pf
        rate_t = round(p_t / (p_t + f_t) * 100, 2)
        if len(by_proj.get("data-analyzer", [])) == 1:
            r = by_proj["data-analyzer"][0]
            if not _eq(r["passed"], p_t, 0.5):
                errs.append(f"da Tests Passed={r['passed']} != truth {p_t}")
            if not _eq(r["failed"], f_t, 0.5):
                errs.append(f"da Tests Failed={r['failed']} != truth {f_t}")
            if not _eq(r["rate"], rate_t):
                errs.append(f"da Pass Rate={r['rate']} != truth {rate_t}")
            if r["badge"] != badge_for(rate_t):
                errs.append(f"da Health Badge={r['badge']!r} != truth {badge_for(rate_t)!r}")
            if r["files"] is None or int(round(r["files"])) not in fset_da:
                errs.append(f"da Test Files Count={r['files']} not in {sorted(fset_da)}")

        # json: the full ctest suite is NOT run (exceeds the 300s verify budget);
        # gate via the `ctest -N` total anchor + recomputed rate/badge arithmetic.
        if len(by_proj.get("json", [])) == 1:
            j = by_proj["json"][0]
            if j["passed"] is None or j["failed"] is None:
                errs.append("json row missing Tests Passed/Tests Failed")
            else:
                s = j["passed"] + j["failed"]
                if int(round(s)) != total:
                    errs.append(f"json Passed+Failed={s:g} != ctest total {total}")
                if s > 0:
                    rate_e = round(j["passed"] / s * 100, 2)
                    if not _eq(j["rate"], rate_e):
                        errs.append(f"json Pass Rate={j['rate']} != recomputed {rate_e}")
                    if j["badge"] != badge_for(rate_e):
                        errs.append(f"json Health Badge={j['badge']!r} != recomputed {badge_for(rate_e)!r}")
            if j["files"] is None or int(round(j["files"])) not in fset_js:
                errs.append(f"json Test Files Count={j['files']} not in {sorted(fset_js)}")

        ok = not errs
        check(label, 2, ok,
              _d(f"da truth {p_t}p/{f_t}f rate={rate_t}, json ctest total={total}"
                 if ok else "; ".join(errs[:4]) + (f" (+{len(errs) - 4} more)" if len(errs) > 4 else "")))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_6_completion_summary_view() -> None:
    """Grid view 'Completion Summary' on 'Sprint Work Packages', grouped by Closed."""
    label = "6. View 'Completion Summary' is grid grouped by Closed"
    try:
        result = baserow_sql(
            "SELECT v.id FROM database_view v "
            "JOIN database_table dt ON v.table_id = dt.id "
            "JOIN database_database da ON dt.database_id = da.application_ptr_id "
            "JOIN core_application ca ON da.application_ptr_id = ca.id "
            "WHERE ca.name = 'Retro Pentest Round 1' "
            "AND dt.name = 'Sprint Work Packages' "
            "AND dt.trashed = false "
            "AND v.name = 'Completion Summary';"
        )
        view_ids = [line.strip() for line in result.split("\n") if line.strip()]
        if not view_ids:
            check(label, 1, False, "view not found")
            return
        best_detail = ""
        for vid in view_ids:
            is_grid = bool(baserow_sql(
                f"SELECT 1 FROM database_gridview WHERE view_ptr_id = {vid};"
            ).strip())
            gb_out = baserow_sql(
                "SELECT f.name FROM database_viewgroupby g "
                "JOIN database_field f ON g.field_id = f.id "
                f"WHERE g.view_id = {vid};"
            )
            gb_fields = [line.strip() for line in gb_out.split("\n") if line.strip()]
            grouped = "Closed" in gb_fields
            if is_grid and grouped:
                check(label, 1, True, f"view_id={vid}, type=grid, group_bys={gb_fields}")
                return
            best_detail = f"view_id={vid}, grid={is_grid}, group_by_fields={gb_fields}"
        check(label, 1, False, _d(best_detail))
    except Exception as e:
        check(label, 1, False, _d(f"exception: {e}"))


# ── code-server checks ───────────────────────────────────────────────────────
def _read_retro_file() -> str | None:
    """Read the retro markdown file from code-server container. Returns content or None."""
    rc, out, err = docker_exec(
        CODE_SERVER_CONTAINER,
        "cat", "/home/coder/workspace/devops-configs/docs/retro-Pentest Round 1.md",
    )
    if rc != 0:
        return None
    return out


def check_7_retro_file_exists() -> None:
    """Retro markdown file exists in code-server."""
    try:
        content = _read_retro_file()
        check("7. Retro file exists", 1, content is not None,
              "found" if content is not None else "file not found")
    except Exception as e:
        check("7. Retro file exists", 1, False, _d(f"exception: {e}"))


def check_8_retro_header_date() -> None:
    """Lines 1-2: header and date correct."""
    try:
        content = _read_retro_file()
        if content is None:
            check("8. Retro header & date", 1, False, "file not found")
            return
        lines = content.strip().split("\n")
        if len(lines) < 2:
            check("8. Retro header & date", 1, False, f"only {len(lines)} lines")
            return
        header_ok = lines[0].strip() == "# Retrospective: Pentest Round 1"
        date_ok = lines[1].strip() == "Date: 2024-12-03"
        ok = header_ok and date_ok
        check("8. Retro header & date", 1, ok,
              _d(f"line1={'ok' if header_ok else repr(lines[0])}, "
                 f"line2={'ok' if date_ok else repr(lines[1])}"))
    except Exception as e:
        check("8. Retro header & date", 1, False, _d(f"exception: {e}"))


def check_9_retro_closed_hours() -> None:
    """Lines 3-4: X/T/P compared against OpenProject-recomputed truth."""
    label = "9. Retro closed count & hours match OP truth"
    try:
        (truth, terr) = get_op_truth()
        if terr:
            check(label, 2, False, _d(f"OP truth recompute failed: {terr}"))
            return
        content = _read_retro_file()
        if content is None:
            check(label, 2, False, "file not found")
            return
        lines = content.strip().split("\n")
        if len(lines) < 4:
            check(label, 2, False, f"only {len(lines)} lines")
            return
        x_t = sum(1 for t in truth if t["closed"])
        t_t = len(truth)
        p_t = round(sum((t["est"] or 0.0) for t in truth if t["closed"]), 1)

        errs: list[str] = []
        m3 = re.fullmatch(r"Work packages closed: (\d+) of (\d+)", lines[2].strip())
        if not m3:
            errs.append(f"line3={lines[2].strip()!r}")
        else:
            if int(m3.group(1)) != x_t:
                errs.append(f"X={m3.group(1)} != truth {x_t}")
            if int(m3.group(2)) != t_t:
                errs.append(f"T={m3.group(2)} != truth {t_t}")
        m4 = re.fullmatch(r"Planned hours closed: (\d+(?:\.\d+)?)", lines[3].strip())
        if not m4:
            errs.append(f"line4={lines[3].strip()!r}")
        # Numeric equivalence: '0' and '0.0' both accepted for P.
        elif not _eq(float(m4.group(1)), p_t):
            errs.append(f"P={m4.group(1)} != truth {p_t}")
        ok = not errs
        check(label, 2, ok,
              _d(f"closed={x_t}/{t_t}, hours={p_t}" if ok else "; ".join(errs)))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_10_retro_test_health() -> None:
    """Line 5: verifier-generated expectation from recomputed test-health truth."""
    label = "10. Retro test health line matches recomputed truth"
    try:
        (da, derr) = get_da_health()
        (js, jerr) = get_json_eff()
        if derr or jerr:
            check(label, 2, False,
                  _d("test-health truth unavailable - " +
                     "; ".join(e for e in (derr, jerr) if e)))
            return
        content = _read_retro_file()
        if content is None:
            check(label, 2, False, "file not found")
            return
        lines = content.strip().split("\n")
        if len(lines) < 5:
            check(label, 2, False, f"only {len(lines)} lines")
            return
        line5 = lines[4].strip()
        m = re.fullmatch(r"Test health — (.+)", line5)
        if not m:
            check(label, 2, False, _d(f"line5 must start with 'Test health — ': {line5!r}"))
            return
        segs = [s.strip() for s in m.group(1).split(";")]
        if len(segs) != 2:
            check(label, 2, False, _d(f"expected 2 ';'-separated segments, got {len(segs)}: {line5!r}"))
            return
        # Alphabetical project order: data-analyzer before json.
        expected = [("data-analyzer",) + da, ("json",) + js]
        seg_re = re.compile(r"([A-Za-z0-9_.-]+):(\d+(?:\.\d+)?)% \((Green|Yellow|Red)\)")
        errs: list[str] = []
        for seg, (proj, rate_t, badge_t) in zip(segs, expected):
            sm = seg_re.fullmatch(seg)
            if not sm:
                errs.append(f"unparseable segment {seg!r}")
                continue
            if sm.group(1) != proj:
                errs.append(f"segment project {sm.group(1)!r} != {proj!r} (alphabetical order)")
                continue
            # Numeric equivalence (100.00 vs 100.0 tolerated); badge exact.
            if not _eq(float(sm.group(2)), rate_t):
                errs.append(f"{proj} rate {sm.group(2)} != truth {rate_t}")
            if sm.group(3) != badge_t:
                errs.append(f"{proj} badge {sm.group(3)} != truth {badge_t}")
        ok = not errs
        check(label, 2, ok,
              _d(f"da={da[0]}% ({da[1]}), json={js[0]}% ({js[1]})" if ok else "; ".join(errs)))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_11_retro_red_badges() -> None:
    """Line 6: Red badges count equals recomputed truth."""
    label = "11. Retro red badges line matches recomputed truth"
    try:
        (red, rerr) = get_red_count()
        if rerr:
            check(label, 1, False, _d(f"red-badge truth unavailable: {rerr}"))
            return
        content = _read_retro_file()
        if content is None:
            check(label, 1, False, "file not found")
            return
        lines = content.strip().split("\n")
        if len(lines) < 6:
            check(label, 1, False, f"only {len(lines)} lines")
            return
        line6 = lines[5].strip()
        m = re.fullmatch(r"Red badges: (\d+)", line6)
        ok = m is not None and int(m.group(1)) == red
        check(label, 1, ok,
              _d(f"R={red}" if ok else f"line={line6!r}, expected 'Red badges: {red}'"))
    except Exception as e:
        check(label, 1, False, _d(f"exception: {e}"))


# ── OpenProject checks ────────────────────────────────────────────────────────
def check_12_retro_wp_exists() -> None:
    """Exactly one Task WP 'Retro action items: Pentest Round 1' in 'Security Audit'."""
    label = "12. OpenProject retro WP exists exactly once"
    try:
        result = openproject_sql(
            "SELECT count(*) FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Security Audit' "
            "AND t.name = 'Task' "
            "AND wp.subject = 'Retro action items: Pentest Round 1';"
        )
        count = int(result.strip()) if result.strip() else 0
        ok = count == 1
        check(label, 2, ok, f"count={count} (expected exactly 1)")
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


def check_13_retro_wp_assignee_priority() -> None:
    """Retro WP has assignee admin (OpenProject Admin) and priority Normal."""
    try:
        result = openproject_sql(
            "SELECT u.login, ip.name FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "LEFT JOIN enumerations ip ON wp.priority_id = ip.id "
            "WHERE p.name = 'Security Audit' "
            "AND t.name = 'Task' "
            "AND wp.subject = 'Retro action items: Pentest Round 1';"
        )
        rows = [line for line in result.split("\n") if line.strip()]
        if not rows:
            check("13. Retro WP assignee & priority", 2, False, "WP not found")
            return
        if len(rows) > 1:
            check("13. Retro WP assignee & priority", 2, False,
                  f"{len(rows)} matching WPs (expected exactly 1)")
            return
        parts = rows[0].split("|")
        assignee = parts[0].strip() if len(parts) > 0 else ""
        priority = parts[1].strip() if len(parts) > 1 else ""
        assignee_ok = assignee == "admin"
        priority_ok = priority.lower() == "normal"
        ok = assignee_ok and priority_ok
        check("13. Retro WP assignee & priority", 2, ok,
              _d(f"assignee={assignee!r}, priority={priority!r}"))
    except Exception as e:
        check("13. Retro WP assignee & priority", 2, False, _d(f"exception: {e}"))


def check_14_retro_wp_description() -> None:
    """Retro WP description exactly equals the truth-assembled expected string."""
    label = "14. Retro WP description matches truth-assembled string"
    try:
        (truth, terr) = get_op_truth()
        (red, rerr) = get_red_count()
        if terr or rerr:
            check(label, 2, False,
                  _d("truth recompute failed - " + "; ".join(e for e in (terr, rerr) if e)))
            return
        x_t = sum(1 for t in truth if t["closed"])
        t_t = len(truth)
        expected = (
            "Retro doc: devops-configs/docs/retro-Pentest Round 1.md; "
            f"Closed rate: {x_t}/{t_t}; Red projects: {red}"
        )
        # Read the description straight off work_packages (journals.data is not
        # a queryable jsonb column in OpenProject 17).
        result = openproject_sql(
            "SELECT COALESCE(wp.description, '') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Security Audit' "
            "AND t.name = 'Task' "
            "AND wp.subject = 'Retro action items: Pentest Round 1' "
            "LIMIT 1;"
        )
        # CKEditor may escape markdown punctuation (e.g. '\_') when storing text;
        # strip backslashes, then require whole-string equality.
        desc = result.strip().replace("\\", "").strip()
        ok = desc == expected
        check(label, 2, ok,
              _d(f"desc={desc!r}" + ("" if ok else f", expected={expected!r}")))
    except Exception as e:
        check(label, 2, False, _d(f"exception: {e}"))


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_db_exists()
    check_2_sprint_wp_table()
    check_3_test_health_table()
    check_4_sprint_wp_rows()
    check_5_test_health_rows()
    check_6_completion_summary_view()
    check_7_retro_file_exists()
    check_8_retro_header_date()
    check_9_retro_closed_hours()
    check_10_retro_test_health()
    check_11_retro_red_badges()
    check_12_retro_wp_exists()
    check_13_retro_wp_assignee_priority()
    check_14_retro_wp_description()

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
