"""
Verifier for Software-038-I1: Build Code-to-Test Ratio Engineering Metrics Dashboard

Checks: 17 weighted checks across code-server, baserow, metabase, openproject.
Strategy: docker exec (code-server filesystem, openproject DB), REST API (baserow, metabase)

Ground truth is recomputed by the verifier itself (find commands re-run inside the
code-server container -> (S, T) per project -> ratio/tier derived at runtime).
Agent-filled data is never trusted; if truth recompute fails, truth-dependent
checks (ck5/6/14/15/16) FAIL with no fallback.

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import subprocess
import json
import re
from collections import Counter

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
METABASE_PORT = os.environ.get("METABASE_PORT")
METABASE_CONTAINER = os.environ.get("METABASE_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

for _var in [
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
    "METABASE_PORT", "METABASE_CONTAINER",
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
]:
    if not os.environ.get(_var):
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
METABASE_URL = f"http://{HOST}:{METABASE_PORT}"

PROJECTS = ["blog-engine", "data-analyzer", "json", "todo-api"]  # alphabetical

COMMANDS = {
    "todo-api": "echo \"$(find app -type f -name '*.py' | wc -l) $(find tests -type f -name 'test_*.py' | wc -l)\"",
    "blog-engine": "echo \"$(find src -type f -name '*.js' | wc -l) $(find tests -type f -name '*.test.js' 2>/dev/null | wc -l)\"",
    "data-analyzer": "echo \"$(find src -type f -name '*.py' | wc -l) $(find tests -type f -name 'test_*.py' | wc -l)\"",
    "json": "echo \"$(find include -type f -name '*.hpp' | wc -l) $(find tests/src -type f -name 'unit-*.cpp' | wc -l)\"",
}

EXPECTED_FIELDS = {"Project", "Source Files", "Test Files", "Test Coverage Ratio",
                   "Quality Tier", "Measured At"}
EXPECTED_TIERS = {"Gold", "Silver", "Bronze", "AtRisk"}
EXPECTED_QUESTIONS = ("Source vs Test File Counts", "Tier Distribution", "Ratio Ranking")

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15,
                env_vars: dict[str, str] | None = None) -> tuple[int, str, str]:
    cmd = ["docker", "exec"]
    for k, v in (env_vars or {}).items():
        cmd += ["-e", f"{k}={v}"]
    cmd += [container, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


_baserow_token: str | None = None


def baserow_headers() -> dict:
    global _baserow_token
    if _baserow_token is None:
        resp = requests.post(
            f"{BASEROW_URL}/api/user/token-auth/",
            json={"email": "admin@example.com", "password": "Admin1234"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        _baserow_token = data.get("access_token") or data.get("token")
    return {"Authorization": f"JWT {_baserow_token}"}


_metabase_session: str | None = None


def metabase_headers() -> dict:
    global _metabase_session
    if _metabase_session is None:
        resp = requests.post(
            f"{METABASE_URL}/api/session",
            json={"username": "admin@metabase.local", "password": "mw-admin-123"},
            timeout=15,
        )
        resp.raise_for_status()
        _metabase_session = resp.json()["id"]
    return {"X-Metabase-Session": _metabase_session}


def compute_tier(ratio: float) -> str:
    if ratio >= 0.8:
        return "Gold"
    if ratio >= 0.5:
        return "Silver"
    if ratio >= 0.2:
        return "Bronze"
    return "AtRisk"


def _truth_ratio(S: int, T: int) -> float:
    return round(T / S, 3) if S > 0 else 0.0


# ── Shared state across checks ───────────────────────────────────────────────
ref_data: dict[str, tuple[int, int]] = {}  # project -> (S, T)  VERIFIER-computed truth
_baserow_table_id: int | None = None
_baserow_fields: dict = {}   # field_name -> field dict
_baserow_rows: list = []
_baserow_views: list = []
_mb_collection_id: int | None = None
_mb_dashboard_id: int | None = None
_mb_db_id: int | None = None            # Metabase id of "Baserow Postgres" datasource
_mb_table_id: int | None = None         # Metabase table id of database_table_{baserow id}
_mb_field_ids: dict = {}                # physical column name "field_<bid>" -> metabase field id
_mb_field_display: dict = {}            # metabase field id -> display name
_mb_card_ids: dict = {}                 # question name -> metabase card id


def _ref_complete() -> bool:
    return len(ref_data) == 4


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_code_server_projects() -> None:
    """Gather reference S, T from code-server project directories (ground truth).

    Hard precondition: ck5/6/14/15/16 FAIL when this yields fewer than 4 projects.
    """
    try:
        # Locate workspace root by finding one of the known project dirs
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER, "bash", "-c",
            "find / -maxdepth 4 -type d -name 'todo-api' 2>/dev/null | head -1",
            timeout=30,
        )
        root = ""
        if rc == 0 and out.strip():
            # root is parent of todo-api
            todo_path = out.strip().rstrip("/")
            root = todo_path.rsplit("/", 1)[0] if "/" in todo_path else ""

        if not root:
            root = "/home/coder"

        for proj in PROJECTS:
            cmd = f"cd {root}/{proj} && {COMMANDS[proj]}"
            rc2, out2, _ = docker_exec(
                CODE_SERVER_CONTAINER, "bash", "-c", cmd, timeout=30
            )
            if rc2 == 0 and out2.strip():
                parts = out2.strip().split()
                if len(parts) >= 2:
                    try:
                        ref_data[proj] = (int(parts[0]), int(parts[1]))
                    except ValueError:
                        pass

        check("1. Code-server project dirs & file counts", 0,
            _ref_complete(),
            f"found {len(ref_data)}/4: {ref_data}",
        )
    except Exception as e:
        check("1. Code-server project dirs & file counts", 0, False, f"exception: {e}")


def check_2_baserow_database() -> None:
    """Verify Baserow database 'Engineering Quality Metrics' exists."""
    try:
        headers = baserow_headers()
        resp = requests.get(f"{BASEROW_URL}/api/applications/", headers=headers, timeout=15)
        resp.raise_for_status()
        apps = resp.json()
        if isinstance(apps, dict):
            apps = apps.get("results", apps.get("applications", []))

        found = any(a.get("name") == "Engineering Quality Metrics" for a in apps)
        check(
            "2. Baserow DB 'Engineering Quality Metrics'", 1, found,
            f"databases: {[a.get('name') for a in apps]}",
        )
    except Exception as e:
        check("2. Baserow DB 'Engineering Quality Metrics'", 1, False, f"exception: {e}")


def _opt_values(field: dict) -> set:
    return {o.get("value") for o in (field.get("select_options") or [])}


def check_3_baserow_table_fields() -> None:
    """Verify table 'Project Metrics' exists with the exact 6-field schema."""
    global _baserow_table_id, _baserow_fields
    try:
        headers = baserow_headers()
        resp = requests.get(f"{BASEROW_URL}/api/applications/", headers=headers, timeout=15)
        resp.raise_for_status()
        apps = resp.json()
        if isinstance(apps, dict):
            apps = apps.get("results", apps.get("applications", []))

        db_id = None
        for a in apps:
            if a.get("name") == "Engineering Quality Metrics":
                db_id = a["id"]
                break
        if not db_id:
            check("3. Table 'Project Metrics' schema", 2, False, "database not found")
            return

        resp = requests.get(
            f"{BASEROW_URL}/api/database/tables/database/{db_id}/",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        tables = resp.json()

        for t in tables:
            if t.get("name") == "Project Metrics":
                _baserow_table_id = t["id"]
                break
        if not _baserow_table_id:
            check(
                "3. Table 'Project Metrics' schema", 2, False,
                f"table not found; tables={[t.get('name') for t in tables]}",
            )
            return

        resp = requests.get(
            f"{BASEROW_URL}/api/database/fields/table/{_baserow_table_id}/",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        fields = resp.json()
        _baserow_fields = {f["name"]: f for f in fields}

        problems = []
        names = set(_baserow_fields.keys())
        if names != EXPECTED_FIELDS:
            extra = sorted(names - EXPECTED_FIELDS)
            missing = sorted(EXPECTED_FIELDS - names)
            problems.append(f"field set mismatch (missing={missing}, extra={extra})")

        fp = _baserow_fields.get("Project")
        if fp:
            if not fp.get("primary"):
                problems.append("Project not primary")
            if fp.get("type") != "single_select":
                problems.append(f"Project type={fp.get('type')} != single_select")
            elif _opt_values(fp) != set(PROJECTS):
                problems.append(f"Project options {sorted(_opt_values(fp))} != {PROJECTS}")

        for n in ("Source Files", "Test Files"):
            f = _baserow_fields.get(n)
            if f and f.get("type") != "number":
                problems.append(f"{n} type={f.get('type')} != number")

        fr = _baserow_fields.get("Test Coverage Ratio")
        if fr:
            if fr.get("type") != "number":
                problems.append(f"Test Coverage Ratio type={fr.get('type')} != number")
            else:
                dp = fr.get("number_decimal_places")
                if dp is None or int(dp) != 3:
                    problems.append(f"Test Coverage Ratio decimal_places={dp} != 3")

        ft = _baserow_fields.get("Quality Tier")
        if ft:
            if ft.get("type") != "single_select":
                problems.append(f"Quality Tier type={ft.get('type')} != single_select")
            elif _opt_values(ft) != EXPECTED_TIERS:
                problems.append(f"Quality Tier options {sorted(_opt_values(ft))} != {sorted(EXPECTED_TIERS)}")

        fm = _baserow_fields.get("Measured At")
        if fm and fm.get("type") != "date":
            problems.append(f"Measured At type={fm.get('type')} != date")

        check(
            "3. Table 'Project Metrics' schema", 2,
            len(problems) == 0,
            "; ".join(problems) if problems else "exact 6-field schema (types/options/decimals) verified",
        )
    except Exception as e:
        check("3. Table 'Project Metrics' schema", 2, False, f"exception: {e}")


def _field_key(name: str) -> str | None:
    f = _baserow_fields.get(name)
    return f"field_{f['id']}" if f else None


def _row_value(row: dict, field_name: str):
    """Extract value from a Baserow row for the given field name."""
    key = _field_key(field_name)
    if not key:
        return None
    val = row.get(key)
    if isinstance(val, dict):
        return val.get("value", "")
    return val


def check_4_baserow_rows() -> None:
    """Verify exactly 4 rows whose PHYSICAL order equals alphabetical PROJECTS."""
    global _baserow_rows
    try:
        if not _baserow_table_id:
            check("4. Four project rows in alphabetical order", 2, False, "table not found")
            return
        headers = baserow_headers()
        resp = requests.get(
            f"{BASEROW_URL}/api/database/rows/table/{_baserow_table_id}/",
            headers=headers, params={"size": 100}, timeout=15,
        )
        resp.raise_for_status()
        _baserow_rows = resp.json().get("results", [])

        # no sorted(): physical row order must already be alphabetical
        row_projects = [str(_row_value(r, "Project") or "") for r in _baserow_rows]
        check(
            "4. Four project rows in alphabetical order", 2,
            len(_baserow_rows) == 4 and row_projects == PROJECTS,
            f"expected {PROJECTS}, got (physical order) {row_projects}",
        )
    except Exception as e:
        check("4. Four project rows in alphabetical order", 2, False, f"exception: {e}")


def check_5_source_test_counts() -> None:
    """Verify Source Files and Test Files match verifier-recomputed truth."""
    try:
        if not _ref_complete():
            check("5. Source/Test file counts match", 2, False,
                  f"ref data incomplete ({len(ref_data)}/4); truth recompute failed")
            return
        if not _baserow_rows:
            check("5. Source/Test file counts match", 2, False, "no rows")
            return

        mismatches = []
        for r in _baserow_rows:
            proj = str(_row_value(r, "Project") or "")
            if proj not in ref_data:
                mismatches.append(f"{proj}: not a known project")
                continue
            exp_s, exp_t = ref_data[proj]
            try:
                got_s = int(float(str(_row_value(r, "Source Files") or 0)))
                got_t = int(float(str(_row_value(r, "Test Files") or 0)))
            except (ValueError, TypeError):
                mismatches.append(f"{proj}: non-numeric values")
                continue
            if got_s != exp_s or got_t != exp_t:
                mismatches.append(f"{proj}: S={got_s}(exp {exp_s}), T={got_t}(exp {exp_t})")

        check(
            "5. Source/Test file counts match", 2,
            len(mismatches) == 0,
            "; ".join(mismatches) if mismatches else "all counts match",
        )
    except Exception as e:
        check("5. Source/Test file counts match", 2, False, f"exception: {e}")


def check_6_ratios_tiers() -> None:
    """Verify Test Coverage Ratio and Quality Tier against recomputed truth."""
    try:
        if not _ref_complete():
            check("6. Ratios & tiers correct", 4, False,
                  f"ref data incomplete ({len(ref_data)}/4); truth recompute failed")
            return
        if not _baserow_rows:
            check("6. Ratios & tiers correct", 4, False, "no rows")
            return

        mismatches = []
        for r in _baserow_rows:
            proj = str(_row_value(r, "Project") or "")
            if proj not in ref_data:
                mismatches.append(f"{proj}: not a known project")
                continue
            S, T = ref_data[proj]
            exp_ratio = _truth_ratio(S, T)
            exp_tier = compute_tier(exp_ratio)

            try:
                got_ratio = float(str(_row_value(r, "Test Coverage Ratio") or 0))
            except (ValueError, TypeError):
                got_ratio = None
            got_tier = str(_row_value(r, "Quality Tier") or "")

            ratio_ok = got_ratio is not None and abs(got_ratio - exp_ratio) < 0.0005
            tier_ok = got_tier == exp_tier
            if not ratio_ok or not tier_ok:
                mismatches.append(
                    f"{proj}: ratio={got_ratio}(exp {exp_ratio}), tier={got_tier}(exp {exp_tier})"
                )

        check(
            "6. Ratios & tiers correct", 4,
            len(mismatches) == 0,
            "; ".join(mismatches) if mismatches else "all correct",
        )
    except Exception as e:
        check("6. Ratios & tiers correct", 4, False, f"exception: {e}")


def check_7_measured_at() -> None:
    """Verify Measured At = 2026-04-01 for all rows."""
    try:
        if not _baserow_rows:
            check("7. Measured At dates", 1, False, "no rows")
            return
        dates = [str(_row_value(r, "Measured At") or "") for r in _baserow_rows]
        ok = all("2026-04-01" in d for d in dates)
        check("7. Measured At dates", 1, ok, f"dates={dates}")
    except Exception as e:
        check("7. Measured At dates", 1, False, f"exception: {e}")


def check_8_grid_view() -> None:
    """Verify Grid view 'Quality Ranking' with exactly one sort: ratio DESC."""
    global _baserow_views
    try:
        if not _baserow_table_id:
            check("8. Grid view 'Quality Ranking' sorted by ratio DESC", 1, False, "table not found")
            return
        headers = baserow_headers()
        resp = requests.get(
            f"{BASEROW_URL}/api/database/views/table/{_baserow_table_id}/",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        _baserow_views = resp.json()
        view = next(
            (v for v in _baserow_views
             if v.get("name") == "Quality Ranking" and v.get("type") == "grid"),
            None,
        )
        if view is None:
            check(
                "8. Grid view 'Quality Ranking' sorted by ratio DESC", 1, False,
                f"grid view not found; views={[(v.get('name'), v.get('type')) for v in _baserow_views]}",
            )
            return

        resp = requests.get(
            f"{BASEROW_URL}/api/database/views/{view['id']}/sortings/",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        sorts = resp.json()
        if isinstance(sorts, dict):
            sorts = sorts.get("sortings", sorts.get("results", []))

        ratio_fid = (_baserow_fields.get("Test Coverage Ratio") or {}).get("id")
        ok = (
            ratio_fid is not None
            and len(sorts) == 1
            and sorts[0].get("field") == ratio_fid
            and str(sorts[0].get("order", "")).upper() == "DESC"
        )
        got = [(s.get("field"), s.get("order")) for s in sorts]
        check(
            "8. Grid view 'Quality Ranking' sorted by ratio DESC", 1, ok,
            f"expected exactly one sort (field={ratio_fid}, DESC), got {got}",
        )
    except Exception as e:
        check("8. Grid view 'Quality Ranking' sorted by ratio DESC", 1, False, f"exception: {e}")


def check_9_gallery_view() -> None:
    """Verify Gallery view 'By Tier' exists."""
    try:
        views = _baserow_views
        if not views and _baserow_table_id:
            headers = baserow_headers()
            resp = requests.get(
                f"{BASEROW_URL}/api/database/views/table/{_baserow_table_id}/",
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            views = resp.json()
        found = any(
            v.get("name") == "By Tier" and v.get("type") == "gallery"
            for v in views
        )
        check(
            "9. Gallery view 'By Tier'", 1, found,
            f"views={[(v.get('name'), v.get('type')) for v in views]}",
        )
    except Exception as e:
        check("9. Gallery view 'By Tier'", 1, False, f"exception: {e}")


def check_M0_metabase_datasource() -> None:
    """Verify Metabase datasource 'Baserow Postgres' config + synced physical table."""
    global _mb_db_id, _mb_table_id, _mb_field_ids, _mb_field_display
    try:
        headers = metabase_headers()
        resp = requests.get(f"{METABASE_URL}/api/database", headers=headers, timeout=15)
        resp.raise_for_status()
        dbs = resp.json()
        if isinstance(dbs, dict):
            dbs = dbs.get("data", [])

        db = next((d for d in dbs if d.get("name") == "Baserow Postgres"), None)
        if db is None:
            check(
                "M0. Metabase datasource 'Baserow Postgres'", 2, False,
                f"not found; databases={[d.get('name') for d in dbs]}",
            )
            return

        problems = []
        if db.get("engine") != "postgres":
            problems.append(f"engine={db.get('engine')} != postgres")
        det = db.get("details") or {}
        if det.get("dbname") != "baserow":
            problems.append(f"dbname={det.get('dbname')} != baserow")
        if det.get("user") != "baserow":
            problems.append(f"user={det.get('user')} != baserow")
        if det.get("host") != "host.docker.internal":
            problems.append(f"host={det.get('host')} != host.docker.internal")
        exp_port = int(BASEROW_PORT) + 27
        try:
            got_port = int(det.get("port"))
        except (TypeError, ValueError):
            got_port = None
        if got_port != exp_port:
            problems.append(f"port={det.get('port')} != {exp_port}")

        if _baserow_table_id is None:
            problems.append("Baserow table id unknown (ck3 failed); cannot verify schema sync")
        else:
            resp = requests.get(
                f"{METABASE_URL}/api/database/{db['id']}/metadata",
                headers=headers, timeout=30,
            )
            resp.raise_for_status()
            meta = resp.json()
            tname = f"database_table_{_baserow_table_id}"
            tbl = next((t for t in (meta.get("tables") or []) if t.get("name") == tname), None)
            if tbl is None:
                problems.append(f"physical table {tname} not in metadata (schema sync missing)")
            else:
                _mb_table_id = tbl.get("id")
                for f in tbl.get("fields") or []:
                    _mb_field_ids[f.get("name")] = f.get("id")
                    _mb_field_display[f.get("id")] = f.get("display_name") or f.get("name") or ""

        _mb_db_id = db.get("id")
        check(
            "M0. Metabase datasource 'Baserow Postgres'", 2,
            len(problems) == 0,
            "; ".join(problems) if problems else
            f"postgres datasource verified (db id={_mb_db_id}, table {_mb_table_id} synced)",
        )
    except Exception as e:
        check("M0. Metabase datasource 'Baserow Postgres'", 2, False, f"exception: {e}")


def check_10_metabase_collection() -> None:
    """Verify Metabase collection 'Code Quality Audit Q2 2026' exists (0pt precondition)."""
    global _mb_collection_id
    try:
        headers = metabase_headers()
        resp = requests.get(f"{METABASE_URL}/api/collection", headers=headers, timeout=15)
        resp.raise_for_status()
        collections = resp.json()
        for c in collections:
            if c.get("name") == "Code Quality Audit Q2 2026":
                _mb_collection_id = c["id"]
                break
        check(
            "10. Metabase collection", 0,
            _mb_collection_id is not None,
            f"id={_mb_collection_id}" if _mb_collection_id
            else f"not found in {[c.get('name') for c in collections]}",
        )
    except Exception as e:
        check("10. Metabase collection", 0, False, f"exception: {e}")


# ── MBQL / column-name matching helpers ───────────────────────────────────────
def _norm_col(s) -> str:
    return re.sub(r"[\s_]+", "", str(s).strip().lower())


def _col_matches(val, baserow_name: str) -> bool:
    """Match a result-column reference (string) against a Baserow field.

    Accepts the physical column name 'field_<id>', the Metabase display name,
    or the Baserow field display name (case/space/underscore-insensitive).
    """
    f = _baserow_fields.get(baserow_name)
    if not f:
        return False
    col = f"field_{f['id']}"
    cand = {_norm_col(col), _norm_col(baserow_name)}
    mbid = _mb_field_ids.get(col)
    if mbid is not None:
        disp = _mb_field_display.get(mbid)
        if disp:
            cand.add(_norm_col(disp))
    return _norm_col(val) in cand


def _ref_matches(ref, baserow_name: str) -> bool:
    """Match an MBQL field ref against a Baserow field, comparing field id only
    (shape-normalized: ignores base-type/options third element)."""
    f = _baserow_fields.get(baserow_name)
    if not f:
        return False
    col = f"field_{f['id']}"
    mbid = _mb_field_ids.get(col)
    if isinstance(ref, (list, tuple)) and len(ref) >= 2 and str(ref[0]).lower() == "field":
        v = ref[1]
        if isinstance(v, bool):
            return False
        if isinstance(v, int):
            return mbid is not None and v == mbid
        return _col_matches(v, baserow_name)
    if isinstance(ref, str):
        return _col_matches(ref, baserow_name)
    return False


def _option_map(field_name: str) -> dict:
    """select option id -> option value, from the Baserow field definition."""
    return {
        o.get("id"): o.get("value")
        for o in (_baserow_fields.get(field_name, {}).get("select_options") or [])
    }


def _cell_is(cell, text: str, opts: dict) -> bool:
    """A result cell matches `text` either literally or via single-select option id."""
    if cell is None:
        return False
    s = str(cell).strip()
    if s == text:
        return True
    try:
        return opts.get(int(float(s))) == text
    except (ValueError, TypeError):
        return False


def _row_numbers(row) -> list:
    out = []
    for c in row:
        try:
            out.append(float(str(c)))
        except (ValueError, TypeError):
            pass
    return out


def _has_num(nums: list, x: float, tol: float = 1e-6) -> bool:
    return any(abs(n - x) <= tol for n in nums)


def _run_card_query(cid: int, headers: dict) -> list:
    resp = requests.post(f"{METABASE_URL}/api/card/{cid}/query", headers=headers, timeout=30)
    resp.raise_for_status()
    j = resp.json()
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(str(j["error"])[:120])
    rows = (j.get("data") or {}).get("rows")
    if rows is None:
        raise RuntimeError("no rows in query response")
    return rows


def check_11_metabase_questions() -> None:
    """Verify the 3 questions: names, datasource, MBQL structure, and (best-effort)
    execution reconciliation against verifier-recomputed truth."""
    label = "11. Three Metabase questions (dataset_query audit)"
    try:
        if _mb_collection_id is None:
            check(label, 3, False, "collection not found")
            return
        headers = metabase_headers()
        resp = requests.get(
            f"{METABASE_URL}/api/collection/{_mb_collection_id}/items",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()
        item_list = items.get("data", []) if isinstance(items, dict) else items
        for i in item_list:
            if i.get("model") == "card" and i.get("name") in EXPECTED_QUESTIONS:
                _mb_card_ids[i["name"]] = i["id"]

        problems = []
        missing = set(EXPECTED_QUESTIONS) - set(_mb_card_ids)
        if missing:
            problems.append(f"missing questions: {sorted(missing)}")

        if _mb_db_id is None or _mb_table_id is None:
            problems.append("Baserow Postgres datasource/table not resolved (M0 failed)")
            check(label, 3, False, "; ".join(problems))
            return

        cards = {}
        for name, cid in _mb_card_ids.items():
            r = requests.get(f"{METABASE_URL}/api/card/{cid}", headers=headers, timeout=15)
            r.raise_for_status()
            cards[name] = r.json()

        def base_query(card: dict, tag: str) -> dict:
            errs = []
            dsq = card.get("dataset_query") or {}
            db_ref = card.get("database_id") or dsq.get("database")
            if db_ref != _mb_db_id:
                errs.append(f"database_id={db_ref} != Baserow Postgres ({_mb_db_id})")
            if dsq.get("type") != "query":
                errs.append(f"query type={dsq.get('type')} != MBQL 'query'")
            q = dsq.get("query") or {}
            if q.get("source-table") != _mb_table_id:
                errs.append(f"source-table={q.get('source-table')} != {_mb_table_id}")
            if errs:
                problems.append(f"{tag}: " + ", ".join(errs))
            return q

        # Q1: bar chart, dual-branch acceptance (aggregated breakout vs raw columns)
        c1 = cards.get("Source vs Test File Counts")
        if c1:
            errs = []
            q = base_query(c1, "Q1")
            if c1.get("display") != "bar":
                errs.append(f"display={c1.get('display')} != bar")
            breakouts = q.get("breakout") or []
            breakout_ok = any(_ref_matches(b, "Project") for b in breakouts)
            vs = c1.get("visualization_settings") or {}
            dims = vs.get("graph.dimensions") or []
            mets = vs.get("graph.metrics") or []
            viz_ok = (
                any(_col_matches(d, "Project") for d in dims)
                and any(_col_matches(m, "Source Files") for m in mets)
                and any(_col_matches(m, "Test Files") for m in mets)
            )
            if not (breakout_ok or viz_ok):
                errs.append("neither Project breakout nor graph.dimensions=Project + graph.metrics={Source Files, Test Files}")
            if errs:
                problems.append("Q1: " + ", ".join(errs))

        # Q2: pie chart, count grouped by Quality Tier
        c2 = cards.get("Tier Distribution")
        if c2:
            errs = []
            q = base_query(c2, "Q2")
            if c2.get("display") != "pie":
                errs.append(f"display={c2.get('display')} != pie")
            aggs = q.get("aggregation") or []
            agg_ok = (
                len(aggs) == 1
                and isinstance(aggs[0], (list, tuple))
                and len(aggs[0]) >= 1
                and str(aggs[0][0]).lower() == "count"
            )
            if not agg_ok:
                errs.append(f"aggregation {aggs} != [['count']]")
            breakouts = q.get("breakout") or []
            if not (len(breakouts) == 1 and _ref_matches(breakouts[0], "Quality Tier")):
                errs.append("breakout is not exactly Quality Tier")
            if errs:
                problems.append("Q2: " + ", ".join(errs))

        # Q3: table, sorted by Test Coverage Ratio descending
        c3 = cards.get("Ratio Ranking")
        if c3:
            errs = []
            q = base_query(c3, "Q3")
            if c3.get("display") != "table":
                errs.append(f"display={c3.get('display')} != table")
            ob = q.get("order-by") or []
            ob_ok = (
                len(ob) == 1
                and isinstance(ob[0], (list, tuple))
                and len(ob[0]) >= 2
                and str(ob[0][0]).lower() == "desc"
                and _ref_matches(ob[0][1], "Test Coverage Ratio")
            )
            if not ob_ok:
                errs.append(f"order-by {ob} != [desc, Test Coverage Ratio]")
            if errs:
                problems.append("Q3: " + ", ".join(errs))

        # Execution reconciliation against verifier truth (best-effort: degrades
        # to structure-only on connectivity/execution errors, per plan risk 4).
        recon_notes = []
        if _ref_complete():
            truth = {}
            for p, (S, T) in ref_data.items():
                ratio = _truth_ratio(S, T)
                truth[p] = (S, T, ratio, compute_tier(ratio))
            proj_opts = _option_map("Project")
            tier_opts = _option_map("Quality Tier")

            name = "Source vs Test File Counts"
            if name in _mb_card_ids:
                try:
                    rows = _run_card_query(_mb_card_ids[name], headers)
                    for p, (S, T, _, _) in truth.items():
                        prows = [r for r in rows if any(_cell_is(c, p, proj_opts) for c in r)]
                        if not prows:
                            problems.append(f"Q1 exec: no row for {p}")
                        elif not any(
                            _has_num(_row_numbers(r), S) and _has_num(_row_numbers(r), T)
                            for r in prows
                        ):
                            problems.append(f"Q1 exec: {p} row lacks S={S}/T={T}")
                except Exception as e:
                    recon_notes.append(f"Q1 exec skipped ({type(e).__name__})")

            name = "Tier Distribution"
            if name in _mb_card_ids:
                try:
                    rows = _run_card_query(_mb_card_ids[name], headers)
                    exp_counts = Counter(t[3] for t in truth.values())
                    if len(rows) != len(exp_counts):
                        problems.append(f"Q2 exec: {len(rows)} rows != {len(exp_counts)} tiers")
                    for tier, cnt in exp_counts.items():
                        if not any(
                            any(_cell_is(c, tier, tier_opts) for c in r)
                            and _has_num(_row_numbers(r), cnt)
                            for r in rows
                        ):
                            problems.append(f"Q2 exec: missing ({tier}, {cnt})")
                except Exception as e:
                    recon_notes.append(f"Q2 exec skipped ({type(e).__name__})")

            name = "Ratio Ranking"
            if name in _mb_card_ids:
                try:
                    rows = _run_card_query(_mb_card_ids[name], headers)
                    if len(rows) != 4:
                        problems.append(f"Q3 exec: {len(rows)} rows != 4")
                    order = []
                    for p, (_, _, ratio, _) in truth.items():
                        idxs = [i for i, r in enumerate(rows)
                                if any(_cell_is(c, p, proj_opts) for c in r)]
                        if not idxs:
                            problems.append(f"Q3 exec: no row for {p}")
                            continue
                        if not _has_num(_row_numbers(rows[idxs[0]]), ratio, tol=0.0005):
                            problems.append(f"Q3 exec: {p} row lacks ratio {ratio}")
                        order.append((idxs[0], ratio))
                    order.sort()
                    if any(order[k][1] < order[k + 1][1] - 1e-9 for k in range(len(order) - 1)):
                        problems.append("Q3 exec: rows not sorted by ratio desc")
                except Exception as e:
                    recon_notes.append(f"Q3 exec skipped ({type(e).__name__})")
        else:
            recon_notes.append("exec recon skipped: ref data incomplete")

        detail = "; ".join(problems) if problems else "3 questions structurally valid"
        if recon_notes:
            detail += " [" + "; ".join(recon_notes) + "]"
        check(label, 3, len(problems) == 0, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_12_metabase_dashboard() -> None:
    """Verify dashboard name and description."""
    global _mb_dashboard_id
    try:
        if _mb_collection_id is None:
            check("12. Metabase dashboard", 1, False, "collection not found")
            return
        headers = metabase_headers()
        resp = requests.get(
            f"{METABASE_URL}/api/collection/{_mb_collection_id}/items",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        items = resp.json()
        item_list = items.get("data", []) if isinstance(items, dict) else items

        for d in item_list:
            if d.get("model") == "dashboard" and d.get("name") == "Code-to-Test Coverage Audit":
                _mb_dashboard_id = d["id"]
                break

        if _mb_dashboard_id is None:
            dash_names = [d.get("name") for d in item_list if d.get("model") == "dashboard"]
            check("12. Metabase dashboard", 1, False, f"not found; dashboards={dash_names}")
            return

        resp = requests.get(
            f"{METABASE_URL}/api/dashboard/{_mb_dashboard_id}",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        dash = resp.json()
        desc = (dash.get("description") or "").strip()
        expected_desc = "Code-to-test ratio audit 2026-04-01 across 4 projects"
        check(
            "12. Metabase dashboard", 1,
            desc == expected_desc,
            f"desc='{desc}'" if desc != expected_desc else "name & description correct",
        )
    except Exception as e:
        check("12. Metabase dashboard", 1, False, f"exception: {e}")


def check_13_dashboard_cards() -> None:
    """Verify dashcard card_id multiset == exactly the three question ids."""
    try:
        if _mb_dashboard_id is None:
            check("13. Dashboard cards are the 3 questions", 1, False, "dashboard not found")
            return
        expected_ids = [_mb_card_ids.get(n) for n in EXPECTED_QUESTIONS]
        if any(i is None for i in expected_ids):
            unresolved = [n for n in EXPECTED_QUESTIONS if _mb_card_ids.get(n) is None]
            check("13. Dashboard cards are the 3 questions", 1, False,
                  f"question ids unresolved: {unresolved}")
            return
        headers = metabase_headers()
        resp = requests.get(
            f"{METABASE_URL}/api/dashboard/{_mb_dashboard_id}",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        dash = resp.json()
        cards = dash.get("ordered_cards", dash.get("dashcards", [])) or []
        got_ids = sorted(c.get("card_id") for c in cards if c.get("card_id"))
        ok = got_ids == sorted(expected_ids)
        check(
            "13. Dashboard cards are the 3 questions", 1, ok,
            f"expected card ids {sorted(expected_ids)}, got {got_ids}",
        )
    except Exception as e:
        check("13. Dashboard cards are the 3 questions", 1, False, f"exception: {e}")


def _bronze_atrisk() -> dict[str, tuple[int, int, float, str]]:
    """Return {project: (S, T, ratio, tier)} for Bronze/AtRisk projects.

    Derived purely from verifier-recomputed ref_data; only meaningful when
    _ref_complete() is true (callers must guard).
    """
    result = {}
    for proj in PROJECTS:
        if proj in ref_data:
            S, T = ref_data[proj]
            ratio = _truth_ratio(S, T)
            tier = compute_tier(ratio)
            if tier in ("Bronze", "AtRisk"):
                result[proj] = (S, T, ratio, tier)
    return result


def check_14_op_work_packages() -> None:
    """Verify OpenProject work package subject set for Bronze/AtRisk projects."""
    try:
        if not _ref_complete():
            check("14. OP work packages exist", 1, False,
                  f"ref data incomplete ({len(ref_data)}/4); truth recompute failed")
            return
        ba = _bronze_atrisk()

        rc, out, err = docker_exec(
            OPENPROJECT_CONTAINER,
            "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
            "-t", "-A", "-c",
            "SELECT wp.subject, t.name "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Customer Portal Redesign' "
            "AND t.name = 'Task' "
            "AND wp.subject LIKE 'Raise test coverage:%'",
            env_vars={"PGPASSWORD": "openproject"},
        )
        if rc != 0:
            check("14. OP work packages exist", 1, False, f"psql error: {err.strip()}")
            return

        found_subjects = [
            line.split("|")[0]
            for line in out.strip().split("\n")
            if line.strip()
        ]
        expected_subjects = [
            f"Raise test coverage: {proj} (ratio {ratio:.3f})"
            for proj, (_, _, ratio, _) in sorted(ba.items())
        ]

        missing = [s for s in expected_subjects if s not in found_subjects]
        extra = [s for s in found_subjects if s not in expected_subjects]
        ok = len(missing) == 0 and len(extra) == 0 and len(expected_subjects) == len(found_subjects)

        details = []
        if missing:
            details.append(f"missing: {missing}")
        if extra:
            details.append(f"extra: {extra}")
        if not details:
            details.append(f"{len(found_subjects)} WPs match expected set of {len(expected_subjects)}")
        check("14. OP work packages exist", 1, ok, "; ".join(details))
    except Exception as e:
        check("14. OP work packages exist", 1, False, f"exception: {e}")


def check_15_op_assignee_priority() -> None:
    """Verify WP set == verifier-computed Bronze/AtRisk set with assignee/priority.

    Zero-case fix: empty psql result passes ONLY if the truth set is empty;
    rows outside the expected set FAIL; every expected subject must be present.
    """
    try:
        if not _ref_complete():
            check("15. WP assignee & priority", 2, False,
                  f"ref data incomplete ({len(ref_data)}/4); truth recompute failed")
            return
        ba = _bronze_atrisk()
        expected = {
            f"Raise test coverage: {proj} (ratio {ratio:.3f})": tier
            for proj, (_, _, ratio, tier) in ba.items()
        }

        rc, out, err = docker_exec(
            OPENPROJECT_CONTAINER,
            "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
            "-t", "-A", "-c",
            "SELECT wp.subject, u.login, e.name "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "LEFT JOIN enumerations e ON wp.priority_id = e.id "
            "WHERE p.name = 'Customer Portal Redesign' "
            "AND t.name = 'Task' "
            "AND wp.subject LIKE 'Raise test coverage:%'",
            env_vars={"PGPASSWORD": "openproject"},
        )
        if rc != 0:
            check("15. WP assignee & priority", 2, False, f"psql error: {err.strip()}")
            return

        rows = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            rows.append((parts[0], parts[1], parts[2]))

        issues = []
        if len(rows) != len(expected):
            issues.append(f"row count {len(rows)} != expected {len(expected)}")

        seen = set()
        for subject, assignee, priority in rows:
            if subject not in expected:
                issues.append(f"unexpected WP '{subject}'")
                continue
            seen.add(subject)
            if assignee != "qa_lead":
                issues.append(f"assignee={assignee}(exp qa_lead) for '{subject}'")
            exp_prio = "High" if expected[subject] == "AtRisk" else "Normal"
            if priority != exp_prio:
                issues.append(f"priority={priority}(exp {exp_prio}) for '{subject}'")

        for subj in sorted(set(expected) - seen):
            issues.append(f"missing WP '{subj}'")

        check(
            "15. WP assignee & priority", 2,
            len(issues) == 0,
            "; ".join(issues) if issues else
            f"all {len(expected)} Bronze/AtRisk WPs correct (assignee/priority)",
        )
    except Exception as e:
        check("15. WP assignee & priority", 2, False, f"exception: {e}")


def check_16_op_description() -> None:
    """Verify WP descriptions exactly against verifier-computed truth.

    Same zero-case gating as ck15: row count must equal the truth set size,
    every expected subject must be present, rows outside the set FAIL.
    """
    try:
        if not _ref_complete():
            check("16. WP descriptions", 2, False,
                  f"ref data incomplete ({len(ref_data)}/4); truth recompute failed")
            return
        ba = _bronze_atrisk()
        expected = {
            f"Raise test coverage: {proj} (ratio {ratio:.3f})":
                f"Source: {S}; Tests: {T}; Tier: {tier}; Measured: 2026-04-01"
            for proj, (S, T, ratio, tier) in ba.items()
        }

        rc, out, err = docker_exec(
            OPENPROJECT_CONTAINER,
            "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
            "-t", "-A", "-c",
            "SELECT wp.subject, "
            "regexp_replace(coalesce(wp.description, ''), E'[\\n\\r]+', ' ', 'g') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Customer Portal Redesign' "
            "AND t.name = 'Task' "
            "AND wp.subject LIKE 'Raise test coverage:%'",
            env_vars={"PGPASSWORD": "openproject"},
        )
        if rc != 0:
            check("16. WP descriptions", 2, False, f"psql error: {err.strip()}")
            return

        rows = []
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            rows.append((parts[0], parts[1]))

        issues = []
        if len(rows) != len(expected):
            issues.append(f"row count {len(rows)} != expected {len(expected)}")

        seen = set()
        for subject, desc_raw in rows:
            if subject not in expected:
                issues.append(f"unexpected WP '{subject}'")
                continue
            seen.add(subject)
            # Strip CKEditor backslash escapes, HTML tags, normalize whitespace
            desc_text = desc_raw.replace("\\", "")
            desc_text = re.sub(r"<[^>]+>", " ", desc_text)
            desc_text = re.sub(r"\s+", " ", desc_text).strip()
            if desc_text != expected[subject]:
                issues.append(f"'{subject}': got '{desc_text}', exp '{expected[subject]}'")

        for subj in sorted(set(expected) - seen):
            issues.append(f"missing WP '{subj}'")

        check(
            "16. WP descriptions", 2,
            len(issues) == 0,
            "; ".join(issues) if issues else
            f"all {len(expected)} descriptions exactly correct",
        )
    except Exception as e:
        check("16. WP descriptions", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_code_server_projects()
    check_2_baserow_database()
    check_3_baserow_table_fields()
    check_4_baserow_rows()
    check_5_source_test_counts()
    check_6_ratios_tiers()
    check_7_measured_at()
    check_8_grid_view()
    check_9_gallery_view()
    check_M0_metabase_datasource()
    check_10_metabase_collection()
    check_11_metabase_questions()
    check_12_metabase_dashboard()
    check_13_dashboard_cards()
    check_14_op_work_packages()
    check_15_op_assignee_priority()
    check_16_op_description()

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
