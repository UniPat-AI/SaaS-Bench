"""
Verifier for Software-014-I1: Inventory and Govern Feature Flags Across todo-api and blog-engine

Checks: 18 weighted checks (total weight 27) across code-server, baserow, metabase, openproject.
Strategy: flag-location ground truth is recomputed at verify time by grepping the
          workspace in a throwaway container from the code-server container's own
          pristine image — immune to agent edits (computed once, cached; recompute
          failure fails every dependent check). docker exec DB for Baserow schema/order + OpenProject,
          Baserow API for row data, Metabase API for collection/questions/dashboard,
          with card execution (POST /api/card/<id>/query) as the primary semantic check.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import re
import sys
import subprocess
import json  # noqa: F401
import time
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

_required = {
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "METABASE_PORT": METABASE_PORT,
    "METABASE_CONTAINER": METABASE_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
METABASE_URL = f"http://{HOST}:{METABASE_PORT}"
OPENPROJECT_URL = f"http://{HOST}:{OPENPROJECT_PORT}"

# ── Expected data ─────────────────────────────────────────────────────────────
FLAG_METADATA = {
    "NEW_CHECKOUT": {"default_state": "Enabled", "owner": "alice@example.com", "target_removal_date": "2026-05-15"},
    "DARK_MODE": {"default_state": "Enabled", "owner": "bob@example.com", "target_removal_date": "2026-08-30"},
    "LEGACY_AUTH": {"default_state": "Disabled", "owner": "carol@example.com", "target_removal_date": "2026-04-10"},
    "BETA_COMMENTS": {"default_state": "Disabled", "owner": "dave@example.com", "target_removal_date": "2026-12-01"},
    "EXPERIMENTAL_SEARCH": {"default_state": "Enabled", "owner": "eve@example.com", "target_removal_date": "2026-06-20"},
}
SUNSET_CUTOFF = "2026-07-01"
SUNSET_FLAGS = {k for k, v in FLAG_METADATA.items() if v["target_removal_date"] <= SUNSET_CUTOFF}
# Expected: NEW_CHECKOUT, LEGACY_AUTH, EXPERIMENTAL_SEARCH

EXPECTED_FIELDS = [
    "Flag Name", "Project", "File Path", "Line Number",
    "Default State", "Owner", "Target Removal Date", "Status",
]

# field name -> expected django content-type model in Baserow's DB
EXPECTED_FIELD_MODELS = {
    "Flag Name": "textfield",
    "Project": "singleselectfield",
    "File Path": "textfield",
    "Line Number": "numberfield",
    "Default State": "singleselectfield",
    "Owner": "textfield",
    "Target Removal Date": "datefield",
    "Status": "singleselectfield",
}
EXPECTED_SELECT_OPTIONS = {
    "Project": {"todo-api", "blog-engine"},
    "Default State": {"Enabled", "Disabled"},
    "Status": {"Active", "Sunset", "Removed"},
}

FLAG_PROJECTS = ("todo-api", "blog-engine")
WORKSPACE = "/home/coder/workspace"

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
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


def baserow_db_query(sql: str) -> str:
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", sql,
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def baserow_db_query_sep(sql: str) -> str:
    """Baserow psql with a \\x1f field separator (safe for text containing '|')."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-F", "\x1f", "-c", sql,
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def openproject_db_query(sql: str, sep: str | None = None) -> str:
    args = [
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1",
        "-U", "openproject", "-d", "openproject", "-t", "-A",
    ]
    if sep is not None:
        args += ["-F", sep]
    args += ["-c", sql]
    rc, out, err = docker_exec(OPENPROJECT_CONTAINER, *args)
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def baserow_api_auth() -> tuple[str, dict]:
    """Get Baserow JWT token and return (token, headers)."""
    resp = requests.post(f"{BASEROW_URL}/api/user/token-auth/", json={
        "email": "admin@example.com", "password": "Admin1234",
    }, timeout=10)
    resp.raise_for_status()
    token = resp.json()["token"]
    return token, {"Authorization": f"JWT {token}"}


def metabase_api_auth() -> tuple[str, dict]:
    """Get Metabase session token and return (session_id, headers)."""
    resp = requests.post(f"{METABASE_URL}/api/session", json={
        "username": "admin@metabase.local", "password": "mw-admin-123",
    }, timeout=10)
    resp.raise_for_status()
    sid = resp.json()["id"]
    return sid, {"X-Metabase-Session": sid}


def metabase_get(path: str, headers: dict, timeout: int = 15):
    resp = requests.get(f"{METABASE_URL}/api/{path}", headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def metabase_post(path: str, headers: dict, payload=None, timeout: int = 60):
    resp = requests.post(
        f"{METABASE_URL}/api/{path}",
        headers=headers,
        json=payload if payload is not None else {},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _get_baserow_table_id() -> str:
    """Return the Baserow table ID for 'Feature Flags' or empty string."""
    return baserow_db_query(
        "SELECT dt.id FROM database_table dt "
        "JOIN core_application ca ON dt.database_id = ca.id "
        "WHERE ca.name = 'Feature Flag Governance' AND dt.name = 'Feature Flags' LIMIT 1;"
    ).strip()


def _get_baserow_rows_and_field_map() -> tuple[list, dict]:
    """Fetch all rows and build a field_name -> field_key map via Baserow API."""
    token, headers = baserow_api_auth()
    table_id = _get_baserow_table_id()
    if not table_id:
        return [], {}
    resp = requests.get(
        f"{BASEROW_URL}/api/database/rows/table/{table_id}/?size=200",
        headers=headers, timeout=15,
    )
    resp.raise_for_status()
    rows = resp.json().get("results", [])
    fields_resp = requests.get(
        f"{BASEROW_URL}/api/database/fields/table/{table_id}/",
        headers=headers, timeout=15,
    )
    fields_resp.raise_for_status()
    field_map = {f["name"]: f"field_{f['id']}" for f in fields_resp.json()}
    return rows, field_map


def _select_value(val) -> str:
    """Extract the display value from a Baserow single-select cell."""
    if isinstance(val, dict):
        return val.get("value", "")
    if val is None:
        return ""
    return str(val)


def _get_metabase_collection_id(headers: dict) -> int | None:
    resp = requests.get(f"{METABASE_URL}/api/collection", headers=headers, timeout=15)
    resp.raise_for_status()
    for c in resp.json():
        if c.get("name") == "Feature Flag Governance Q2 2026":
            return c["id"]
    return None


# ── Baserow schema helpers (psql) ─────────────────────────────────────────────
_field_info_cache: dict | None = None


def _get_field_info() -> dict:
    """{field name: {'id': int, 'model': str, 'primary': bool}} for the Feature
    Flags table via psql (cached)."""
    global _field_info_cache
    if _field_info_cache is not None:
        return _field_info_cache
    info: dict = {}
    table_id = _get_baserow_table_id()
    if table_id:
        out = baserow_db_query_sep(
            f"SELECT df.id, df.name, ct.model, df.\"primary\" FROM database_field df "
            f"JOIN django_content_type ct ON ct.id = df.content_type_id "
            f"WHERE df.table_id = {table_id} AND df.trashed = false;"
        )
        for line in out.split("\n"):
            parts = line.split("\x1f")
            if len(parts) != 4 or not parts[0].strip().isdigit():
                continue
            info[parts[1]] = {
                "id": int(parts[0]),
                "model": parts[2].strip(),
                "primary": parts[3].strip() == "t",
            }
    _field_info_cache = info
    return info


def _select_options(field_id: int) -> dict[int, str]:
    """{option id: option text} for a single-select field."""
    out = baserow_db_query_sep(
        f"SELECT id, value FROM database_selectoption WHERE field_id = {field_id};"
    )
    opts: dict[int, str] = {}
    for line in out.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 2 and parts[0].strip().isdigit():
            opts[int(parts[0])] = parts[1]
    return opts


def _norm_select_cell(val, opt_text: dict[int, str]) -> str:
    """Normalize an executed-card cell: raw single-select columns hold option ids —
    map them back to their text via database_selectoption."""
    if isinstance(val, bool):
        return str(val)
    if isinstance(val, (int, float)) and float(val).is_integer() and int(val) in opt_text:
        return opt_text[int(val)]
    s = str(val).strip()
    if s.isdigit() and int(s) in opt_text:
        return opt_text[int(s)]
    return s


# ── Ground truth: flag locations recomputed by grep (once, cached) ────────────
_flag_truth: dict | None = None
_flag_truth_done = False
_flag_truth_error = ""


def get_flag_truth() -> dict | None:
    """{flag: (project, relpath, line)} recomputed by grepping the workspace in a
    throwaway container from the code-server container's pristine image (never the
    live, agent-touched container). Computed once and cached; returns None on
    failure — every dependent check must then FAIL (never fall back to trusting
    agent-entered data)."""
    global _flag_truth, _flag_truth_done, _flag_truth_error
    if _flag_truth_done:
        return _flag_truth
    _flag_truth_done = True
    try:
        rc, out, err = image_exec(
            "bash", "-c",
            f"grep -rnE 'FEATURE_FLAG_([A-Z0-9_]+)\\s*=' "
            f"{WORKSPACE}/todo-api {WORKSPACE}/blog-engine 2>/dev/null || true",
            timeout=180,
        )
        truth: dict = {}
        for line in out.strip().split("\n"):
            m = re.match(
                rf"^{re.escape(WORKSPACE)}/(todo-api|blog-engine)/(.+?):(\d+):.*?"
                r"FEATURE_FLAG_([A-Z0-9_]+)\s*=", line)
            if not m:
                continue
            project, relpath, lineno, flag = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            if flag not in truth:
                truth[flag] = (project, relpath, lineno)
        if not truth:
            _flag_truth_error = "grep recompute returned no FEATURE_FLAG_* definitions"
            return None
        _flag_truth = truth
        return _flag_truth
    except Exception as e:
        _flag_truth_error = f"grep recompute failed: {e}"
        return None


# ── Metabase card helpers (probe-verified against Metabase v0.58.5.2 pMBQL) ──
def _mb_field_id(ref):
    """Field id from a field ref in pMBQL (["field",{opts},id]) or legacy
    (["field",id,opts]) form. None if not a field ref."""
    if not isinstance(ref, (list, tuple)) or not ref or ref[0] != "field":
        return None
    for item in ref[1:]:
        if isinstance(item, int):
            return item
    return None


def mb_shape(card):
    """Normalize a Metabase card (GET /api/card/<id> JSON) to a comparable shape.
    Handles pMBQL stages and legacy MBQL. Keys:
      display, visualization_settings, database, is_native, native_sql,
      source_table, agg_ops (set of "count" | (op, fid)), breakout_fids,
      filters (raw clause list), order_by ([(dir, fid)]), fields_fids.
    """
    dq = card.get("dataset_query") or {}
    shape = {
        "display": card.get("display"),
        "visualization_settings": card.get("visualization_settings") or {},
        "database": dq.get("database"),
        "is_native": False,
        "native_sql": None,
        "source_table": None,
        "agg_ops": set(),
        "breakout_fids": [],
        "filters": [],
        "order_by": [],
        "fields_fids": [],
    }
    if dq.get("lib/type") == "mbql/query" and dq.get("stages"):
        stage = dq["stages"][0]
        if stage.get("lib/type") == "mbql.stage/native":
            shape["is_native"] = True
            nat = stage.get("native")
            shape["native_sql"] = nat if isinstance(nat, str) else (nat or {}).get("query", "")
            return shape
        shape["source_table"] = stage.get("source-table")
        aggs = stage.get("aggregation") or []
        filters = stage.get("filters") or []
        breakouts = stage.get("breakout") or []
        order_by = stage.get("order-by") or []
        fields = stage.get("fields") or []
    elif dq.get("type") == "native":
        shape["is_native"] = True
        shape["native_sql"] = (dq.get("native") or {}).get("query", "")
        return shape
    else:
        q = dq.get("query") or {}
        shape["source_table"] = q.get("source-table")
        aggs = q.get("aggregation") or []
        filters = [q["filter"]] if q.get("filter") else []
        breakouts = q.get("breakout") or []
        order_by = q.get("order-by") or []
        fields = q.get("fields") or []
    for agg in aggs:
        if not isinstance(agg, (list, tuple)) or not agg:
            continue
        op = agg[0]
        fid = None
        for item in agg[1:]:
            fid = _mb_field_id(item)
            if fid is not None:
                break
        shape["agg_ops"].add(op if fid is None else (op, fid))
    shape["breakout_fids"] = [_mb_field_id(b) for b in breakouts]
    shape["filters"] = filters
    for o in order_by:
        if isinstance(o, (list, tuple)) and o:
            fid = None
            for item in o[1:]:
                fid = _mb_field_id(item)
                if fid is not None:
                    break
            shape["order_by"].append((str(o[0]).lower(), fid))
    shape["fields_fids"] = [_mb_field_id(f) for f in fields]
    return shape


def mb_filter_summary(filters):
    """Normalize filter clauses to [(op, field_id, values_tuple)]; flattens and/or.
    Handles pMBQL ["=",{opts},["field",{opts},id],v...] and legacy ["=",["field",id,opts],v...]."""
    out = []

    def walk(cl):
        if not isinstance(cl, (list, tuple)) or not cl:
            return
        op = cl[0]
        if op in ("and", "or"):
            for sub in cl[1:]:
                walk(sub)
            return
        fid = None
        vals = []
        for item in cl[1:]:
            if isinstance(item, dict):
                continue  # pMBQL opts map
            got = _mb_field_id(item)
            if got is not None and fid is None:
                fid = got
            elif not isinstance(item, (list, tuple)):
                vals.append(item)
        out.append((op, fid, tuple(vals)))

    for cl in filters or []:
        walk(cl)
    return out


def mb_run_card(card_id: int, headers: dict) -> tuple[list, list]:
    """Execute a saved card server-side. Returns (col_names, rows).
    Raises RuntimeError on failure (one retry for sync lag). Works for native SQL cards too."""
    last_err = None
    for attempt in range(2):
        try:
            res = metabase_post(f"card/{card_id}/query", headers, timeout=60)
            if res.get("status") == "completed":
                data = res.get("data") or {}
                cols = [c.get("name") for c in data.get("cols", [])]
                return cols, data.get("rows", [])
            last_err = f"status={res.get('status')} error={str(res.get('error'))[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError(f"card {card_id} execution failed: {last_err}")


def mb_find_pg_db(headers: dict, name: str | None = None):
    """Locate the Baserow Postgres datasource. Never hardcode db ids.
    name: pass "Baserow Postgres" only where the task text mandates the name."""
    resp = metabase_get("database", headers)
    dbs = resp.get("data", resp) if isinstance(resp, dict) else resp
    for db in dbs:
        if db.get("engine") != "postgres":
            continue
        if name is not None and db.get("name") != name:
            continue
        details = db.get("details") or {}
        if "dbname" in details and details.get("dbname") != "baserow":
            continue
        return db
    return None


def mb_resolve_table(headers: dict, mb_db_id: int, physical_table_name: str):
    """(mb_table_id, {physical_field_name: mb_field_id}) via GET /api/database/<id>/metadata.
    physical_table_name is e.g. f"database_table_{tid}"; field names are f"field_{baserow_fid}"."""
    meta = metabase_get(f"database/{mb_db_id}/metadata", headers)
    for tbl in meta.get("tables", []) or []:
        if tbl.get("name") == physical_table_name:
            fmap = {f.get("name"): f.get("id") for f in tbl.get("fields", []) or []}
            return tbl.get("id"), fmap
    return None, {}


def _find_card(headers: dict, coll_id: int, name: str) -> dict | None:
    """Exact-name card lookup inside the collection."""
    resp = metabase_get("card", headers)
    for c in resp:
        if c.get("name") == name and c.get("collection_id") == coll_id:
            return c
    return None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_code_server_flag_files():
    """Ground-truth recompute (0pt diagnostic): grep FEATURE_FLAG_* definitions.
    The grep result IS the ground-truth input for checks A/B/10/11."""
    try:
        truth = get_flag_truth()
        if truth is None:
            check("1. Flag ground truth (grep recompute)", 0, False,
                  _flag_truth_error or "recompute failed")
            return
        summary = ", ".join(f"{k}={v[0]}/{v[1]}:{v[2]}" for k, v in sorted(truth.items()))
        passed = set(truth) == set(FLAG_METADATA)
        check("1. Flag ground truth (grep recompute)", 0, passed,
              f"{len(truth)} flags: {summary}")
    except Exception as e:
        check("1. Flag ground truth (grep recompute)", 0, False, f"exception: {e}")


def check_2_baserow_database_exists():
    """Baserow database 'Feature Flag Governance' exists."""
    try:
        result = baserow_db_query(
            "SELECT ca.id FROM core_application ca "
            "JOIN django_content_type ct ON ca.content_type_id = ct.id "
            "WHERE ca.name = 'Feature Flag Governance' "
            "AND ct.app_label = 'database' AND ct.model = 'database' LIMIT 1;"
        )
        passed = bool(result.strip())
        check("2. Baserow DB 'Feature Flag Governance' exists", 1, passed,
              f"id={result}" if passed else "database not found")
    except Exception as e:
        check("2. Baserow DB 'Feature Flag Governance' exists", 1, False, f"exception: {e}")


def check_3_baserow_table_exists():
    """Baserow table 'Feature Flags' exists."""
    try:
        table_id = _get_baserow_table_id()
        passed = bool(table_id)
        check("3. Table 'Feature Flags' exists", 1, passed,
              f"table_id={table_id}" if passed else "table not found")
    except Exception as e:
        check("3. Table 'Feature Flags' exists", 1, False, f"exception: {e}")


def check_4_baserow_fields():
    """Full field schema: types, primary flag, select-option sets, decimal places."""
    try:
        info = _get_field_info()
        errors = []
        for name in EXPECTED_FIELDS:
            if name not in info:
                errors.append(f"missing field '{name}'")
        for name, model in EXPECTED_FIELD_MODELS.items():
            fi = info.get(name)
            if fi and fi["model"] != model:
                errors.append(f"'{name}' type={fi['model']} expected {model}")
        fi = info.get("Flag Name")
        if fi and not fi["primary"]:
            errors.append("'Flag Name' is not the primary field")
        for name, exp_opts in EXPECTED_SELECT_OPTIONS.items():
            fi = info.get(name)
            if fi and fi["model"] == "singleselectfield":
                opts = set(_select_options(fi["id"]).values())
                if opts != exp_opts:
                    errors.append(f"'{name}' options {sorted(opts)} != {sorted(exp_opts)}")
        fi = info.get("Line Number")
        if fi and fi["model"] == "numberfield":
            dec = baserow_db_query(
                f"SELECT number_decimal_places FROM database_numberfield "
                f"WHERE field_ptr_id = {fi['id']};"
            ).strip()
            if dec != "0":
                errors.append(f"'Line Number' decimal_places={dec} expected 0")
        passed = len(errors) == 0
        check("4. Field schema (types/primary/options/decimals)", 2, passed,
              "; ".join(errors[:6]) if errors else
              f"all {len(EXPECTED_FIELDS)} fields with correct types/options")
    except Exception as e:
        check("4. Field schema (types/primary/options/decimals)", 2, False, f"exception: {e}")


def check_5_baserow_flags_present():
    """Exactly 5 rows and Flag Name set equality vs FLAG_METADATA keys."""
    try:
        rows, field_map = _get_baserow_rows_and_field_map()
        fn_key = field_map.get("Flag Name")
        if not fn_key:
            check("5. Exactly the 5 expected flag rows", 2, False, "Flag Name field not found")
            return
        found_flags = [str(row.get(fn_key) or "").strip() for row in rows]
        expected = set(FLAG_METADATA.keys())
        missing = expected - set(found_flags)
        extra = set(found_flags) - expected
        passed = len(rows) == 5 and set(found_flags) == expected
        detail = f"rows={len(rows)}"
        if missing:
            detail += f", missing: {sorted(missing)}"
        if extra:
            detail += f", extra: {sorted(extra)}"
        if passed:
            detail += ", flag set matches"
        check("5. Exactly the 5 expected flag rows", 2, passed, detail)
    except Exception as e:
        check("5. Exactly the 5 expected flag rows", 2, False, f"exception: {e}")


def check_6_flag_metadata_correct():
    """Default State, Owner, Target Removal Date match expected values (unknown-flag rows fail)."""
    try:
        rows, field_map = _get_baserow_rows_and_field_map()
        fn_key = field_map.get("Flag Name")
        ds_key = field_map.get("Default State")
        owner_key = field_map.get("Owner")
        trd_key = field_map.get("Target Removal Date")
        if not all([fn_key, ds_key, owner_key, trd_key]):
            check("6. Flag metadata correct", 2, False,
                  f"missing field keys: fn={fn_key}, ds={ds_key}, owner={owner_key}, trd={trd_key}")
            return
        errors = []
        for row in rows:
            flag_name = str(row.get(fn_key, "")).strip()
            if flag_name not in FLAG_METADATA:
                errors.append(f"unexpected row with flag '{flag_name}'")
                continue
            exp = FLAG_METADATA[flag_name]
            ds_val = _select_value(row.get(ds_key))
            if ds_val != exp["default_state"]:
                errors.append(f"{flag_name}: Default State='{ds_val}' expected '{exp['default_state']}'")
            owner_val = str(row.get(owner_key, "")).strip()
            if owner_val != exp["owner"]:
                errors.append(f"{flag_name}: Owner='{owner_val}' expected '{exp['owner']}'")
            trd_val = str(row.get(trd_key, "")).strip()[:10]
            if trd_val != exp["target_removal_date"]:
                errors.append(f"{flag_name}: TRD='{trd_val}' expected '{exp['target_removal_date']}'")
        if not rows:
            errors.append("no rows found")
        passed = len(errors) == 0
        check("6. Flag metadata correct", 2, passed,
              "; ".join(errors[:6]) if errors else "all metadata matches")
    except Exception as e:
        check("6. Flag metadata correct", 2, False, f"exception: {e}")


def check_A_flag_locations():
    """Project / File Path / Line Number per row vs grep ground truth."""
    label = "A. Flag locations match grep ground truth"
    try:
        truth = get_flag_truth()
        if truth is None:
            check(label, 2, False, f"ground truth unavailable: {_flag_truth_error}")
            return
        rows, field_map = _get_baserow_rows_and_field_map()
        fn_key = field_map.get("Flag Name")
        proj_key = field_map.get("Project")
        fp_key = field_map.get("File Path")
        ln_key = field_map.get("Line Number")
        if not all([fn_key, proj_key, fp_key, ln_key]):
            check(label, 2, False,
                  f"missing field keys: fn={fn_key}, proj={proj_key}, fp={fp_key}, ln={ln_key}")
            return
        errors = []
        seen = set()
        for row in rows:
            flag = str(row.get(fn_key) or "").strip()
            if flag not in truth:
                errors.append(f"row with unknown flag '{flag}'")
                continue
            seen.add(flag)
            gt_project, gt_relpath, gt_line = truth[flag]
            proj = _select_value(row.get(proj_key))
            if proj != gt_project:
                errors.append(f"{flag}: Project='{proj}' expected '{gt_project}'")
            raw_path = str(row.get(fp_key) or "").strip()
            norm = raw_path[2:] if raw_path.startswith("./") else raw_path
            if norm.startswith(gt_project + "/"):
                norm = norm[len(gt_project) + 1:]
            if norm != gt_relpath:
                errors.append(f"{flag}: File Path='{raw_path}' expected '{gt_relpath}'")
            raw_line = row.get(ln_key)
            try:
                line_val = float(str(raw_line))
                line_ok = line_val.is_integer() and int(line_val) == gt_line
            except (TypeError, ValueError):
                line_ok = False
            if not line_ok:
                errors.append(f"{flag}: Line Number='{raw_line}' expected {gt_line}")
        missing = set(truth) - seen
        if missing:
            errors.append(f"missing rows for {sorted(missing)}")
        passed = len(errors) == 0
        check(label, 2, passed,
              "; ".join(errors[:6]) if errors else f"all {len(truth)} locations match grep truth")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_B_row_order():
    """Rows ordered by (Project, File Path, Line Number) — checked via ("order", id)."""
    label = "B. Rows sorted by (Project, File Path, Line Number)"
    try:
        truth = get_flag_truth()
        if truth is None:
            check(label, 1, False, f"ground truth unavailable: {_flag_truth_error}")
            return
        expected = [f for f, _ in sorted(truth.items(),
                                         key=lambda kv: (kv[1][0], kv[1][1], kv[1][2]))]
        table_id = _get_baserow_table_id()
        fi = _get_field_info().get("Flag Name")
        if not table_id or not fi:
            check(label, 1, False, "table or 'Flag Name' field not found")
            return
        out = baserow_db_query(
            f"SELECT field_{fi['id']} FROM database_table_{table_id} "
            f"WHERE trashed = false ORDER BY \"order\", id;"
        )
        got = [l.strip() for l in out.split("\n") if l.strip()]
        passed = got == expected
        check(label, 1, passed,
              f"order matches: {expected}" if passed else f"got {got}, expected {expected}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_7_status_correct():
    """Status field: Sunset if date <= 2026-07-01, else Active."""
    try:
        rows, field_map = _get_baserow_rows_and_field_map()
        fn_key = field_map.get("Flag Name")
        status_key = field_map.get("Status")
        if not all([fn_key, status_key]):
            check("7. Status correctly computed", 2, False, "missing fields")
            return
        errors = []
        for row in rows:
            flag_name = str(row.get(fn_key, "")).strip()
            if flag_name not in FLAG_METADATA:
                continue
            expected_status = "Sunset" if flag_name in SUNSET_FLAGS else "Active"
            status_val = _select_value(row.get(status_key))
            if status_val != expected_status:
                errors.append(f"{flag_name}: status='{status_val}' expected '{expected_status}'")
        passed = len(errors) == 0
        check("7. Status correctly computed", 2, passed,
              "; ".join(errors) if errors else "all statuses correct")
    except Exception as e:
        check("7. Status correctly computed", 2, False, f"exception: {e}")


def check_8_baserow_gallery_view():
    """Gallery view exists on Feature Flags table."""
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check("8. Gallery view on Feature Flags table", 1, False, "table not found")
            return
        _, headers = baserow_api_auth()
        resp = requests.get(
            f"{BASEROW_URL}/api/database/views/table/{table_id}/",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        views = resp.json()
        gallery = next((v for v in views if v.get("type") == "gallery"), None)
        passed = gallery is not None
        check("8. Gallery view on Feature Flags table", 1, passed,
              f"view_id={gallery.get('id')}" if passed else "no gallery view found")
    except Exception as e:
        check("8. Gallery view on Feature Flags table", 1, False, f"exception: {e}")


def check_9_metabase_collection():
    """Metabase collection 'Feature Flag Governance Q2 2026' exists."""
    try:
        _, headers = metabase_api_auth()
        coll_id = _get_metabase_collection_id(headers)
        passed = coll_id is not None
        check("9. Metabase collection exists", 1, passed,
              f"id={coll_id}" if passed else "collection not found")
    except Exception as e:
        check("9. Metabase collection exists", 1, False, f"exception: {e}")


def check_10_metabase_question_bar_chart():
    """'Flags by Project and State': bar display gated, correct datasource,
    count-by-(Project, Default State) semantics, execution matches recomputed truth."""
    label = "10. Question 'Flags by Project and State'"
    try:
        _, headers = metabase_api_auth()
        coll_id = _get_metabase_collection_id(headers)
        if coll_id is None:
            check(label, 2, False, "collection not found")
            return
        found = _find_card(headers, coll_id, "Flags by Project and State")
        if not found:
            check(label, 2, False, "question not found in collection")
            return
        card = metabase_get(f"card/{found['id']}", headers)
        shape = mb_shape(card)
        errors = []
        if shape["display"] != "bar":
            errors.append(f"display={shape['display']} expected bar")
        pg_db = mb_find_pg_db(headers)
        if pg_db is None:
            errors.append("no Baserow Postgres datasource in Metabase")
        elif shape["database"] != pg_db["id"]:
            errors.append(f"card database={shape['database']} expected {pg_db['id']}")

        truth = get_flag_truth()
        if truth is None:
            errors.append(f"ground truth unavailable: {_flag_truth_error}")
        missing_meta = set(FLAG_METADATA) - set(truth or {})
        if truth is not None and missing_meta:
            errors.append(f"ground truth missing flags {sorted(missing_meta)}")

        info = _get_field_info()
        table_id = _get_baserow_table_id()
        proj_fi = info.get("Project")
        ds_fi = info.get("Default State")
        if not proj_fi or not ds_fi:
            errors.append("Baserow Project/Default State fields not found")
        mb_fields: dict = {}
        if pg_db is not None and table_id:
            _, mb_fields = mb_resolve_table(headers, pg_db["id"], f"database_table_{table_id}")

        if not shape["is_native"]:
            if shape["agg_ops"] != {"count"}:
                errors.append(f"aggregation={shape['agg_ops'] or 'none'} expected count")
            mb_proj_fid = mb_fields.get(f"field_{proj_fi['id']}") if proj_fi else None
            mb_ds_fid = mb_fields.get(f"field_{ds_fi['id']}") if ds_fi else None
            if mb_proj_fid is None or mb_ds_fid is None:
                errors.append("cannot resolve Project/Default State fields in Metabase metadata")
            elif set(shape["breakout_fids"]) != {mb_proj_fid, mb_ds_fid}:
                errors.append(
                    f"breakout={shape['breakout_fids']} expected Project+Default State "
                    f"fields {sorted({mb_proj_fid, mb_ds_fid})}")

        # Execution-level comparison (native or MBQL alike) vs recomputed truth.
        if truth is not None and not missing_meta and proj_fi and ds_fi:
            expected_counts: Counter = Counter()
            for flag, (project, _, _) in truth.items():
                meta = FLAG_METADATA.get(flag)
                if meta:
                    expected_counts[(project, meta["default_state"])] += 1
            opt_text: dict[int, str] = {}
            opt_text.update(_select_options(proj_fi["id"]))
            opt_text.update(_select_options(ds_fi["id"]))
            proj_vals = set(FLAG_PROJECTS)
            state_vals = {"Enabled", "Disabled"}
            try:
                cols, rows = mb_run_card(found["id"], headers)
                got_counts: Counter = Counter()
                row_errors = []
                for r in rows:
                    if len(r) != 3:
                        row_errors.append(f"unexpected row shape {r}")
                        continue
                    cells = [_norm_select_cell(v, opt_text) for v in r[:2]]
                    proj = next((c for c in cells if c in proj_vals), None)
                    state = next((c for c in cells if c in state_vals), None)
                    try:
                        cnt = int(float(r[2]))
                    except (TypeError, ValueError):
                        cnt = None
                    if proj is None or state is None or cnt is None:
                        row_errors.append(f"unrecognized row {r}")
                        continue
                    got_counts[(proj, state)] += cnt
                if row_errors:
                    errors.extend(row_errors[:3])
                elif got_counts != expected_counts:
                    errors.append(
                        f"executed rows {dict(got_counts)} != expected {dict(expected_counts)}")
            except Exception as e:
                errors.append(f"card execution failed: {e}")
        passed = len(errors) == 0
        check(label, 2, passed,
              "; ".join(str(e) for e in errors[:5]) if errors else
              "bar chart on Baserow Postgres; executed counts match recomputed truth")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_metabase_question_sunset():
    """'Sunset Flag Schedule': table display gated, correct datasource,
    Sunset filter + TRD ascending semantics, execution matches recomputed truth."""
    label = "11. Question 'Sunset Flag Schedule'"
    try:
        _, headers = metabase_api_auth()
        coll_id = _get_metabase_collection_id(headers)
        if coll_id is None:
            check(label, 2, False, "collection not found")
            return
        found = _find_card(headers, coll_id, "Sunset Flag Schedule")
        if not found:
            check(label, 2, False, "question not found in collection")
            return
        card = metabase_get(f"card/{found['id']}", headers)
        shape = mb_shape(card)
        errors = []
        if shape["display"] != "table":
            errors.append(f"display={shape['display']} expected table")
        pg_db = mb_find_pg_db(headers)
        if pg_db is None:
            errors.append("no Baserow Postgres datasource in Metabase")
        elif shape["database"] != pg_db["id"]:
            errors.append(f"card database={shape['database']} expected {pg_db['id']}")

        truth = get_flag_truth()
        if truth is None:
            errors.append(f"ground truth unavailable: {_flag_truth_error}")
        missing_sunset = SUNSET_FLAGS - set(truth or {})
        if truth is not None and missing_sunset:
            errors.append(f"ground truth missing sunset flags {sorted(missing_sunset)}")

        info = _get_field_info()
        table_id = _get_baserow_table_id()
        fn_fi = info.get("Flag Name")
        proj_fi = info.get("Project")
        trd_fi = info.get("Target Removal Date")
        status_fi = info.get("Status")
        if not all([fn_fi, proj_fi, trd_fi, status_fi]):
            errors.append("Baserow Flag Name/Project/Target Removal Date/Status fields not found")
        mb_fields: dict = {}
        if pg_db is not None and table_id:
            _, mb_fields = mb_resolve_table(headers, pg_db["id"], f"database_table_{table_id}")

        if not shape["is_native"] and status_fi and trd_fi:
            status_mb_fid = mb_fields.get(f"field_{status_fi['id']}")
            trd_mb_fid = mb_fields.get(f"field_{trd_fi['id']}")
            if status_mb_fid is None or trd_mb_fid is None:
                errors.append("cannot resolve Status/Target Removal Date fields in Metabase metadata")
            else:
                status_opts = _select_options(status_fi["id"])
                sunset_ids = {oid for oid, v in status_opts.items() if v == "Sunset"}
                sunset_strs = {str(oid) for oid in sunset_ids} | {"Sunset"}
                fsum = mb_filter_summary(shape["filters"])
                filter_ok = any(
                    op == "=" and fid == status_mb_fid and len(vals) == 1
                    and (vals[0] in sunset_ids or str(vals[0]) in sunset_strs)
                    for op, fid, vals in fsum)
                if not filter_ok:
                    errors.append(f"no Status=Sunset filter (filters={fsum})")
                if ("asc", trd_mb_fid) not in shape["order_by"]:
                    errors.append(f"no order-by Target Removal Date asc (order_by={shape['order_by']})")

        # Execution-level comparison vs recomputed truth.
        if truth is not None and not missing_sunset and fn_fi and proj_fi and trd_fi:
            expected_rows = sorted(
                ((f, truth[f][0], FLAG_METADATA[f]["target_removal_date"]) for f in SUNSET_FLAGS),
                key=lambda t: t[2])
            opt_text: dict[int, str] = {}
            if proj_fi:
                opt_text.update(_select_options(proj_fi["id"]))
            if status_fi:
                opt_text.update(_select_options(status_fi["id"]))
            try:
                cols, rows = mb_run_card(found["id"], headers)
                phys_needed = {f"field_{fn_fi['id']}", f"field_{proj_fi['id']}", f"field_{trd_fi['id']}"}
                norm_cols = {re.sub(r"[\s_]+", "", str(c).lower()) for c in cols}
                cols_ok = phys_needed <= set(cols) or \
                    {"flagname", "project", "targetremovaldate"} <= norm_cols
                if not cols_ok:
                    errors.append(f"columns {cols} missing Flag Name/Project/Target Removal Date")
                if len(rows) != 3:
                    errors.append(f"executed {len(rows)} rows, expected 3")
                else:
                    for i, (flag, project, date) in enumerate(expected_rows):
                        vals = set()
                        for v in rows[i]:
                            s = _norm_select_cell(v, opt_text)
                            vals.add(s)
                            vals.add(s[:10])
                        if not {flag, project, date} <= vals:
                            errors.append(
                                f"row {i} {rows[i]} != expected ({flag}, {project}, {date})")
            except Exception as e:
                errors.append(f"card execution failed: {e}")
        passed = len(errors) == 0
        check(label, 2, passed,
              "; ".join(str(e) for e in errors[:5]) if errors else
              "table card on Baserow Postgres; 3 sunset rows date-ascending match truth")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_metabase_dashboard_exists():
    """Dashboard 'Feature Flag Sunset Dashboard' exists in collection."""
    try:
        _, headers = metabase_api_auth()
        coll_id = _get_metabase_collection_id(headers)
        if coll_id is None:
            check("12. Dashboard 'Feature Flag Sunset Dashboard'", 1, False, "collection not found")
            return
        resp = requests.get(f"{METABASE_URL}/api/dashboard", headers=headers, timeout=15)
        resp.raise_for_status()
        dashboards = resp.json()
        found = [d for d in dashboards
                 if d.get("name") == "Feature Flag Sunset Dashboard"
                 and d.get("collection_id") == coll_id]
        passed = len(found) > 0
        check("12. Dashboard 'Feature Flag Sunset Dashboard'", 1, passed,
              f"id={found[0]['id']}" if passed else "dashboard not found in collection")
    except Exception as e:
        check("12. Dashboard 'Feature Flag Sunset Dashboard'", 1, False, f"exception: {e}")


def check_13_metabase_dashboard_cards():
    """Dashboard contains exactly the two question cards (exact names, card-id equality)."""
    try:
        _, headers = metabase_api_auth()
        coll_id = _get_metabase_collection_id(headers)
        if coll_id is None:
            check("13. Dashboard has exactly the 2 question cards", 2, False, "collection not found")
            return
        resp = requests.get(f"{METABASE_URL}/api/dashboard", headers=headers, timeout=15)
        resp.raise_for_status()
        dashboards = resp.json()
        found = [d for d in dashboards
                 if d.get("name") == "Feature Flag Sunset Dashboard"
                 and d.get("collection_id") == coll_id]
        if not found:
            check("13. Dashboard has exactly the 2 question cards", 2, False, "dashboard not found")
            return
        dash_id = found[0]["id"]
        dash_data = metabase_get(f"dashboard/{dash_id}", headers)
        cards_list = dash_data.get("ordered_cards") or dash_data.get("dashcards") or []
        question_cards = [c for c in cards_list if c.get("card_id")]
        card_ids = {c.get("card_id") for c in question_cards}
        card_names = sorted((c.get("card") or {}).get("name", "") for c in question_cards)

        bar_card = _find_card(headers, coll_id, "Flags by Project and State")
        sunset_card = _find_card(headers, coll_id, "Sunset Flag Schedule")
        errors = []
        if bar_card is None:
            errors.append("card 'Flags by Project and State' not found in collection")
        if sunset_card is None:
            errors.append("card 'Sunset Flag Schedule' not found in collection")
        if len(question_cards) != 2:
            errors.append(f"dashboard has {len(question_cards)} question cards, expected 2")
        if sorted(["Flags by Project and State", "Sunset Flag Schedule"]) != card_names:
            errors.append(f"dashboard card names {card_names}")
        if bar_card and sunset_card and card_ids != {bar_card["id"], sunset_card["id"]}:
            errors.append(
                f"dashboard card ids {sorted(card_ids)} != {sorted({bar_card['id'], sunset_card['id']})}")
        passed = len(errors) == 0
        check("13. Dashboard has exactly the 2 question cards", 2, passed,
              "; ".join(errors) if errors else f"cards: {card_names}")
    except Exception as e:
        check("13. Dashboard has exactly the 2 question cards", 2, False, f"exception: {e}")


def check_C_metabase_datasource():
    """Metabase has a postgres datasource named 'Baserow Postgres' with the table synced."""
    label = "C. 'Baserow Postgres' datasource synced"
    try:
        _, headers = metabase_api_auth()
        db = mb_find_pg_db(headers, name="Baserow Postgres")
        if db is None:
            check(label, 1, False, "no postgres datasource named 'Baserow Postgres'")
            return
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 1, False, "Baserow 'Feature Flags' table not found")
            return
        mb_tid, _ = mb_resolve_table(headers, db["id"], f"database_table_{table_id}")
        passed = mb_tid is not None
        check(label, 1, passed,
              f"db_id={db['id']}, table database_table_{table_id} "
              + ("synced" if passed else "NOT visible in metadata (sync missing?)"))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_14_openproject_sunset_work_packages():
    """OpenProject sunset work packages: subject set equality (extras/duplicates fail)."""
    try:
        expected_subjects = {f"Remove feature flag: {flag}" for flag in SUNSET_FLAGS}
        result = openproject_db_query(
            "SELECT wp.subject FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "WHERE p.identifier = 'demo-project' "
            "AND wp.subject LIKE 'Remove feature flag:%';"
        )
        found_list = [s.strip() for s in result.split("\n") if s.strip()]
        found_subjects = set(found_list)
        missing = expected_subjects - found_subjects
        extra = found_subjects - expected_subjects
        passed = (found_subjects == expected_subjects
                  and len(found_list) == len(expected_subjects))
        detail = f"found {len(found_list)} WPs"
        if missing:
            detail += f", missing: {sorted(missing)}"
        if extra:
            detail += f", extra: {sorted(extra)}"
        if len(found_list) != len(found_subjects):
            detail += ", duplicate subjects"
        check("14. OpenProject sunset WP subjects (set equality)", 2, passed, detail)
    except Exception as e:
        check("14. OpenProject sunset WP subjects (set equality)", 2, False, f"exception: {e}")


def check_15_openproject_wp_details():
    """Work packages have correct assignee email, non-empty due date, and priority Normal."""
    try:
        result = openproject_db_query(
            "SELECT wp.subject, u.mail, wp.due_date::text, "
            "  (SELECT e.name FROM enumerations e WHERE e.id = wp.priority_id), "
            "  (SELECT t.name FROM types t WHERE t.id = wp.type_id) "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "WHERE p.identifier = 'demo-project' "
            "AND wp.subject LIKE 'Remove feature flag:%';",
            sep="\x1f",
        )
        errors = []
        found_count = 0
        for line in result.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split("\x1f")]
            if len(parts) < 5:
                errors.append(f"unparseable WP row: {line[:60]}")
                continue
            subject, assignee_email, due_date, priority, wp_type = parts[:5]
            flag_name = subject.replace("Remove feature flag:", "").strip()
            found_count += 1
            if flag_name not in FLAG_METADATA:
                errors.append(f"WP for unknown flag '{flag_name}'")
                continue
            exp = FLAG_METADATA[flag_name]
            if assignee_email != exp["owner"]:
                errors.append(f"{flag_name}: assignee='{assignee_email}' expected '{exp['owner']}'")
            if not due_date:
                errors.append(f"{flag_name}: due date empty, expected '{exp['target_removal_date']}'")
            elif due_date[:10] != exp["target_removal_date"]:
                errors.append(f"{flag_name}: due={due_date[:10]} expected '{exp['target_removal_date']}'")
            if priority.lower() != "normal":
                errors.append(f"{flag_name}: priority='{priority}' expected 'Normal'")
            if wp_type.lower() != "task":
                errors.append(f"{flag_name}: type='{wp_type}' expected 'Task'")
        if found_count == 0:
            errors.append("no matching work packages found")
        if found_count != len(SUNSET_FLAGS) and found_count > 0:
            errors.append(f"expected {len(SUNSET_FLAGS)} WPs, found {found_count}")
        passed = len(errors) == 0 and found_count == len(SUNSET_FLAGS)
        check("15. WP details (assignee, due date, priority, type)", 2, passed,
              "; ".join(errors[:6]) if errors else "all details match")
    except Exception as e:
        check("15. WP details (assignee, due date, priority, type)", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_code_server_flag_files()
    check_2_baserow_database_exists()
    check_3_baserow_table_exists()
    check_4_baserow_fields()
    check_5_baserow_flags_present()
    check_6_flag_metadata_correct()
    check_A_flag_locations()
    check_B_row_order()
    check_7_status_correct()
    check_8_baserow_gallery_view()
    check_9_metabase_collection()
    check_10_metabase_question_bar_chart()
    check_11_metabase_question_sunset()
    check_12_metabase_dashboard_exists()
    check_13_metabase_dashboard_cards()
    check_C_metabase_datasource()
    check_14_openproject_sunset_work_packages()
    check_15_openproject_wp_details()

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
