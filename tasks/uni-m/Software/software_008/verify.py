"""
Verifier for Software-008-I2: Engineering OKR Tracker across Baserow, Metabase, OpenProject

Checks: 15 weighted checks (25 total points) across baserow, metabase, openproject.
Strategy: Baserow API + Baserow Postgres, Metabase API (incl. card execution),
OpenProject embedded DB.

Required env vars:
  SERVER_HOSTNAME, BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import re
import sys
import json
import time
import subprocess
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
METABASE_PORT = os.environ.get("METABASE_PORT")
METABASE_CONTAINER = os.environ.get("METABASE_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_missing = []
for var in [
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
    "METABASE_PORT", "METABASE_CONTAINER",
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
]:
    if not os.environ.get(var):
        _missing.append(var)
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
METABASE_URL = f"http://{HOST}:{METABASE_PORT}"

# ── Expected data ─────────────────────────────────────────────────────────────
OBJECTIVES = [
    ("OBJ-1", "Scale observability coverage", "Priya Patel", "Q3-2026"),
    ("OBJ-2", "Improve checkout conversion", "Marco Rossi", "Q3-2026"),
    ("OBJ-3", "Harden data pipeline quality", "Elena Volkov", "Q3-2026"),
    ("OBJ-4", "Modernize mobile experience", "Jamal Harris", "Q3-2026"),
    ("OBJ-5", "Grow API gateway adoption", "Sophie Laurent", "Q3-2026"),
]

KEY_RESULTS = [
    ("KR-1", "OBJ-1", "Instrument 100% of production services with tracing", 100, 95),
    ("KR-2", "OBJ-1", "Reduce alert noise by 40%", 40, 10),
    ("KR-3", "OBJ-2", "Lift checkout conversion to 4.5%", 450, 420),
    ("KR-4", "OBJ-2", "Reduce cart abandonment to 55%", 55, 68),
    ("KR-5", "OBJ-3", "Achieve 99% data freshness SLA", 99, 97),
    ("KR-6", "OBJ-3", "Resolve 30 data quality incidents", 30, 12),
    ("KR-7", "OBJ-4", "Ship 15 redesigned mobile screens", 15, 14),
    ("KR-8", "OBJ-4", "Improve mobile crash-free rate to 99.8%", 998, 994),
    ("KR-9", "OBJ-5", "Migrate 25 services behind the API gateway", 25, 8),
    ("KR-10", "OBJ-5", "Onboard 10 external API consumers", 10, 2),
]

# Precompute expected Progress Pct and Status
EXPECTED_KR = {}
for kr_id, obj_id, desc, target, current in KEY_RESULTS:
    pct = round(current / target * 100, 1)
    if pct >= 75:
        status = "OnTrack"
    elif pct >= 40:
        status = "AtRisk"
    else:
        status = "OffTrack"
    EXPECTED_KR[kr_id] = {
        "obj_id": obj_id, "desc": desc, "target": target,
        "current": current, "pct": pct, "status": status,
    }

OFFTRACK_KRS = {k: v for k, v in EXPECTED_KR.items() if v["status"] == "OffTrack"}

# Metabase question name -> required display type (from the task statement)
MB_EXPECTED_CARDS = {
    "KR Status Breakdown": "pie",
    "Average Progress by Objective": "bar",
    "Off-Track KRs": "table",
}

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


def baserow_sql(query: str) -> str:
    """Run a SQL query against the Baserow Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc",
        "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-c", query,
        timeout=30,
    )
    if rc != 0:
        raise RuntimeError(f"baserow psql error: {err.strip()}")
    return out.strip()


def baserow_auth() -> dict:
    """Authenticate to Baserow API and return headers with token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["token"]
    return {"Authorization": f"JWT {token}"}


def metabase_auth() -> str:
    """Authenticate to Metabase and return session token."""
    resp = requests.post(
        f"{METABASE_URL}/api/session",
        json={"username": "admin@metabase.local", "password": "mw-admin-123"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def metabase_get(path: str, token: str, timeout: int = 30):
    r = requests.get(
        f"{METABASE_URL}/api/{path}",
        headers={"X-Metabase-Session": token},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def metabase_post(path: str, token: str, payload=None, timeout: int = 60):
    r = requests.post(
        f"{METABASE_URL}/api/{path}",
        headers={"X-Metabase-Session": token},
        json=payload if payload is not None else {},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


# ── Metabase MBQL/pMBQL normalization helpers (Metabase v0.58.x verified) ─────
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


def mb_run_card(card_id, token):
    """Execute a saved card server-side. Returns (col_names, rows).
    Raises RuntimeError on failure (one retry for sync lag). Works for native SQL cards too."""
    last_err = None
    for attempt in range(2):
        try:
            res = metabase_post(f"card/{card_id}/query", token, timeout=60)
            if res.get("status") == "completed":
                data = res.get("data") or {}
                cols = [c.get("name") for c in data.get("cols", [])]
                return cols, data.get("rows", [])
            last_err = f"status={res.get('status')} error={str(res.get('error'))[:200]}"
        except Exception as e:
            last_err = str(e)
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError(f"card {card_id} execution failed: {last_err}")


def mb_find_pg_db(token, name=None):
    """Locate the Baserow Postgres datasource. Never hardcode db ids.
    name: pass "Baserow Postgres" only where the task text mandates the name."""
    resp = metabase_get("database", token)
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


def mb_resolve_table(token, mb_db_id, physical_table_name):
    """(mb_table_id, {physical_field_name: mb_field_id}) via GET /api/database/<id>/metadata.
    physical_table_name is e.g. f"database_table_{tid}"; field names are f"field_{baserow_fid}"."""
    meta = metabase_get(f"database/{mb_db_id}/metadata", token)
    for tbl in meta.get("tables", []) or []:
        if tbl.get("name") == physical_table_name:
            fmap = {f.get("name"): f.get("id") for f in tbl.get("fields", []) or []}
            return tbl.get("id"), fmap
    return None, {}


# ── Individual checks ─────────────────────────────────────────────────────────

# -- Baserow checks (API) --

_baserow_headers = None
_baserow_db_id = None
_obj_table_id = None
_kr_table_id = None
_obj_rows = None
_kr_rows = None
_obj_fields = None
_kr_fields = None


def _init_baserow():
    """Fetch Baserow state once: database, tables, fields, rows."""
    global _baserow_headers, _baserow_db_id, _obj_table_id, _kr_table_id
    global _obj_rows, _kr_rows, _obj_fields, _kr_fields
    if _baserow_headers is not None:
        return
    _baserow_headers = baserow_auth()

    # Find database
    resp = requests.get(
        f"{BASEROW_URL}/api/applications/",
        headers=_baserow_headers, timeout=15,
    )
    resp.raise_for_status()
    apps = resp.json()
    for app in apps:
        if app.get("name") == "Engineering OKRs Q3 2026":
            _baserow_db_id = app["id"]
            break

    if _baserow_db_id is None:
        return

    # Find tables
    resp = requests.get(
        f"{BASEROW_URL}/api/database/tables/database/{_baserow_db_id}/",
        headers=_baserow_headers, timeout=15,
    )
    resp.raise_for_status()
    tables = resp.json()
    for t in tables:
        if t["name"] == "Objectives":
            _obj_table_id = t["id"]
        elif t["name"] == "Key Results":
            _kr_table_id = t["id"]

    # Fetch fields and rows
    if _obj_table_id:
        resp = requests.get(
            f"{BASEROW_URL}/api/database/fields/table/{_obj_table_id}/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        _obj_fields = resp.json()
        resp = requests.get(
            f"{BASEROW_URL}/api/database/rows/table/{_obj_table_id}/"
            f"?user_field_names=true&size=100",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        _obj_rows = resp.json().get("results", [])

    if _kr_table_id:
        resp = requests.get(
            f"{BASEROW_URL}/api/database/fields/table/{_kr_table_id}/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        _kr_fields = resp.json()
        resp = requests.get(
            f"{BASEROW_URL}/api/database/rows/table/{_kr_table_id}/"
            f"?user_field_names=true&size=100",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        _kr_rows = resp.json().get("results", [])


def _field_by_name(fields, name):
    for f in fields or []:
        if f.get("name") == name:
            return f
    return None


def check_1_baserow_db_and_tables() -> None:
    """Database 'Engineering OKRs Q3 2026' exists with Objectives and Key Results tables."""
    try:
        _init_baserow()
        has_db = _baserow_db_id is not None
        has_obj = _obj_table_id is not None
        has_kr = _kr_table_id is not None
        ok = has_db and has_obj and has_kr
        detail = f"db={has_db}, objectives_tbl={has_obj}, key_results_tbl={has_kr}"
        check("1. Baserow DB and tables exist", 1, ok, detail)
    except Exception as e:
        check("1. Baserow DB and tables exist", 1, False, f"exception: {e}")


def check_2_objectives_data() -> None:
    """Objectives table has 5 rows with correct OBJ IDs, Titles, Owners, Quarter."""
    try:
        _init_baserow()
        if not _obj_rows:
            check("2. Objectives data (5 rows)", 2, False, "no rows found")
            return

        if len(_obj_rows) != 5:
            check("2. Objectives data (5 rows)", 2, False,
                  f"expected 5 rows, got {len(_obj_rows)}")
            return

        # Build lookup by Objective ID
        found = {}
        for row in _obj_rows:
            obj_id = str(row.get("Objective ID", "")).strip()
            found[obj_id] = row

        mismatches = []
        for obj_id, title, owner, quarter in OBJECTIVES:
            if obj_id not in found:
                mismatches.append(f"{obj_id} missing")
                continue
            r = found[obj_id]
            if str(r.get("Title", "")).strip() != title:
                mismatches.append(f"{obj_id} title mismatch")
            if str(r.get("Owner", "")).strip() != owner:
                mismatches.append(f"{obj_id} owner mismatch")
            if str(r.get("Quarter", "")).strip() != quarter:
                mismatches.append(f"{obj_id} quarter mismatch")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all 5 match"
        check("2. Objectives data (5 rows)", 2, ok, detail)
    except Exception as e:
        check("2. Objectives data (5 rows)", 2, False, f"exception: {e}")


def check_3_kr_rows_and_ids() -> None:
    """Key Results table has 10 rows with correct KR IDs, descriptions, objective links."""
    try:
        _init_baserow()
        if not _kr_rows:
            check("3. Key Results rows, IDs, links", 2, False, "no rows found")
            return

        if len(_kr_rows) != 10:
            check("3. Key Results rows, IDs, links", 2, False,
                  f"expected 10 rows, got {len(_kr_rows)}")
            return

        found = {}
        for row in _kr_rows:
            kr_id = str(row.get("KR ID", "")).strip()
            found[kr_id] = row

        mismatches = []
        for kr_id, obj_id, desc, _, _ in KEY_RESULTS:
            if kr_id not in found:
                mismatches.append(f"{kr_id} missing")
                continue
            actual_desc = str(found[kr_id].get("Description", "")).strip()
            if actual_desc != desc:
                mismatches.append(f"{kr_id} desc mismatch")
            # Link field returns [{"id": ..., "value": ...}] with user_field_names=true
            links = found[kr_id].get("Objective ID")
            if not isinstance(links, list) or len(links) != 1:
                mismatches.append(
                    f"{kr_id} objective link: expected exactly 1 link, got {links!r}")
            elif str(links[0].get("value", "")).strip() != obj_id:
                mismatches.append(
                    f"{kr_id} objective link: expected {obj_id}, "
                    f"got {links[0].get('value')!r}")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all 10 match incl. links"
        check("3. Key Results rows, IDs, links", 2, ok, detail)
    except Exception as e:
        check("3. Key Results rows, IDs, links", 2, False, f"exception: {e}")


def check_4_kr_target_current() -> None:
    """Key Results Target and Current values match expected."""
    try:
        _init_baserow()
        if not _kr_rows:
            check("4. Key Results Target/Current values", 2, False, "no rows")
            return

        found = {}
        for row in _kr_rows:
            kr_id = str(row.get("KR ID", "")).strip()
            found[kr_id] = row

        mismatches = []
        for kr_id in EXPECTED_KR:
            exp = EXPECTED_KR[kr_id]
            if kr_id not in found:
                mismatches.append(f"{kr_id} missing")
                continue
            r = found[kr_id]
            # Target and Current may be int or string
            try:
                actual_target = float(r.get("Target", 0))
            except (ValueError, TypeError):
                actual_target = None
            try:
                actual_current = float(r.get("Current", 0))
            except (ValueError, TypeError):
                actual_current = None

            if actual_target != float(exp["target"]):
                mismatches.append(
                    f"{kr_id} target: expected {exp['target']}, got {actual_target}")
            if actual_current != float(exp["current"]):
                mismatches.append(
                    f"{kr_id} current: expected {exp['current']}, got {actual_current}")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all match"
        check("4. Key Results Target/Current values", 2, ok, detail)
    except Exception as e:
        check("4. Key Results Target/Current values", 2, False, f"exception: {e}")


def check_5_kr_progress_pct() -> None:
    """Key Results Progress Pct equals round(Current/Target*100, 1) exactly
    (float-representation tolerance 0.05)."""
    try:
        _init_baserow()
        if not _kr_rows:
            check("5. Key Results Progress Pct", 2, False, "no rows")
            return

        found = {}
        for row in _kr_rows:
            kr_id = str(row.get("KR ID", "")).strip()
            found[kr_id] = row

        mismatches = []
        for kr_id, exp in EXPECTED_KR.items():
            if kr_id not in found:
                mismatches.append(f"{kr_id} missing")
                continue
            try:
                actual_pct = float(found[kr_id].get("Progress Pct", -1))
            except (ValueError, TypeError):
                actual_pct = None
            if actual_pct is None or abs(actual_pct - exp["pct"]) > 0.05:
                mismatches.append(
                    f"{kr_id}: expected {exp['pct']}, got {actual_pct}")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all exact"
        check("5. Key Results Progress Pct", 2, ok, detail)
    except Exception as e:
        check("5. Key Results Progress Pct", 2, False, f"exception: {e}")


def check_6_kr_status() -> None:
    """Key Results Status assigned correctly (OnTrack/AtRisk/OffTrack)."""
    try:
        _init_baserow()
        if not _kr_rows:
            check("6. Key Results Status values", 1, False, "no rows")
            return

        found = {}
        for row in _kr_rows:
            kr_id = str(row.get("KR ID", "")).strip()
            found[kr_id] = row

        mismatches = []
        for kr_id, exp in EXPECTED_KR.items():
            if kr_id not in found:
                mismatches.append(f"{kr_id} missing")
                continue
            # Status may be a dict (single-select) or string
            raw_status = found[kr_id].get("Status", "")
            if isinstance(raw_status, dict):
                actual_status = raw_status.get("value", "")
            else:
                actual_status = str(raw_status).strip()
            if actual_status != exp["status"]:
                mismatches.append(
                    f"{kr_id}: expected {exp['status']}, got {actual_status}")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all correct"
        check("6. Key Results Status values", 1, ok, detail)
    except Exception as e:
        check("6. Key Results Status values", 1, False, f"exception: {e}")


# -- Metabase checks (API) --

_mb_state_cache = None


def _metabase_state() -> dict:
    """Fetch Metabase state once: session, collection, the 3 expected cards, pg datasource."""
    global _mb_state_cache
    if _mb_state_cache is not None:
        return _mb_state_cache
    st = {"error": None, "token": None, "coll_id": None, "cards": {}, "pg_db": None}
    try:
        token = metabase_auth()
        st["token"] = token
        collections = metabase_get("collection", token)
        for c in collections:
            if c.get("name") == "Engineering OKRs Q3 2026":
                st["coll_id"] = c["id"]
                break
        st["pg_db"] = mb_find_pg_db(token, name="Baserow Postgres")
        if st["coll_id"] is not None:
            items = metabase_get(f"collection/{st['coll_id']}/items?models=card", token)
            data = items.get("data", items) if isinstance(items, dict) else items
            for item in data:
                name = item.get("name", "")
                if item.get("model", "card") == "card" and name in MB_EXPECTED_CARDS:
                    st["cards"][name] = metabase_get(f"card/{item['id']}", token)
    except Exception as e:
        st["error"] = str(e)
    _mb_state_cache = st
    return st


_status_opts = None


def _status_option_map() -> dict:
    """Status single-select option id -> value map from Baserow Postgres (computed once)."""
    global _status_opts
    if _status_opts is not None:
        return _status_opts
    status_field = _field_by_name(_kr_fields, "Status")
    if not status_field:
        raise RuntimeError("Status field not found in Baserow Key Results table")
    out = baserow_sql(
        f"SELECT id, value FROM database_selectoption "
        f"WHERE field_id = {status_field['id']}"
    )
    opts = {}
    for line in out.splitlines():
        if "|" in line:
            oid, val = line.split("|", 1)
            opts[oid.strip()] = val.strip()
    _status_opts = opts
    return opts


def _as_int_str(cell):
    """'6', 6, 6.0 -> '6'; anything non-integral -> None."""
    try:
        f = float(cell)
    except (TypeError, ValueError):
        return None
    if f == int(f):
        return str(int(f))
    return None


def _float_eq(cell, expected, tol: float = 0.05) -> bool:
    try:
        return abs(float(cell) - float(expected)) <= tol
    except (TypeError, ValueError):
        return False


def check_7_metabase_collection() -> None:
    """Metabase collection 'Engineering OKRs Q3 2026' exists."""
    try:
        session_id = metabase_auth()
        headers = {"X-Metabase-Session": session_id}
        resp = requests.get(
            f"{METABASE_URL}/api/collection", headers=headers, timeout=15)
        resp.raise_for_status()
        collections = resp.json()
        found = any(
            c.get("name") == "Engineering OKRs Q3 2026" for c in collections
        )
        check("7. Metabase collection exists", 1, found,
              "found" if found else "not found")
    except Exception as e:
        check("7. Metabase collection exists", 1, False, f"exception: {e}")


def check_8a_metabase_questions() -> None:
    """Three questions exist in the collection with exact display types and the
    Baserow Postgres datasource."""
    label = "8a. Metabase questions (display+datasource)"
    try:
        st = _metabase_state()
        if st["error"]:
            check(label, 2, False, f"metabase error: {st['error']}")
            return
        if st["coll_id"] is None:
            check(label, 2, False, "collection not found")
            return

        problems = []
        db = st["pg_db"]
        if db is None:
            problems.append(
                "datasource 'Baserow Postgres' (engine=postgres) not found")
        for name, display in MB_EXPECTED_CARDS.items():
            card = st["cards"].get(name)
            if card is None:
                problems.append(f"card missing in collection: {name}")
                continue
            if card.get("display") != display:
                problems.append(
                    f"{name}: display={card.get('display')!r}, expected {display!r}")
            if db is not None:
                card_db = (card.get("dataset_query") or {}).get("database")
                if card_db != db.get("id"):
                    problems.append(
                        f"{name}: dataset_query.database={card_db} != "
                        f"Baserow Postgres id {db.get('id')}")
        ok = len(problems) == 0
        detail = "; ".join(problems) if problems else \
            f"3 cards with correct display, datasource id={db.get('id')}"
        check(label, 2, ok, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8b_metabase_card_results() -> None:
    """Execute all three cards and compare row sets against verifier-computed
    truth from the static task data; MBQL shape asserted for query-type cards."""
    label = "8b. Metabase card results (executed)"
    try:
        st = _metabase_state()
        if st["error"]:
            check(label, 3, False, f"metabase error: {st['error']}")
            return
        missing = [n for n in MB_EXPECTED_CARDS if n not in st["cards"]]
        if missing:
            check(label, 3, False, f"cards missing: {missing}")
            return

        _init_baserow()
        if _kr_table_id is None or not _kr_fields:
            check(label, 3, False,
                  "Baserow Key Results table/fields unavailable (truth unresolvable)")
            return

        token = st["token"]

        # Truth computed ONCE from the static task data above.
        truth_status = {}
        by_obj = {}
        for exp in EXPECTED_KR.values():
            truth_status[exp["status"]] = truth_status.get(exp["status"], 0) + 1
            by_obj.setdefault(exp["obj_id"], []).append(exp["pct"])
        truth_avg = {obj: sum(v) / len(v) for obj, v in by_obj.items()}
        truth_offtrack_order = sorted(OFFTRACK_KRS, key=lambda k: OFFTRACK_KRS[k]["pct"])
        truth_summary = (
            f"truth: status={truth_status}, "
            f"avg={{{', '.join(f'{k}:{round(v, 2)}' for k, v in sorted(truth_avg.items()))}}}, "
            f"offtrack={truth_offtrack_order}"
        )

        # Option id -> text map (Status stores option ids in the physical column).
        opts = _status_option_map()
        objid_by_rowid = {
            str(r.get("id")): str(r.get("Objective ID", "")).strip()
            for r in (_obj_rows or [])
        }

        problems = []

        # Card 1: KR Status Breakdown -> {OnTrack:6, AtRisk:1, OffTrack:3}
        card1 = st["cards"]["KR Status Breakdown"]
        try:
            _, rows1 = mb_run_card(card1["id"], token)
            got1 = {}
            for row in rows1:
                if len(row) < 2:
                    problems.append(f"card1: unexpected row shape {row!r}")
                    continue
                key = cnt = None
                for a, b in ((row[0], row[1]), (row[1], row[0])):
                    i = _as_int_str(a)
                    norm = opts.get(i) if i is not None else None
                    if norm is None:
                        norm = str(a).strip()
                    if norm in truth_status:
                        key = norm
                        try:
                            cnt = int(float(b))
                        except (TypeError, ValueError):
                            cnt = None
                        break
                if key is None or cnt is None:
                    problems.append(f"card1: unrecognized row {row!r}")
                    continue
                got1[key] = cnt
            if got1 != truth_status:
                problems.append(f"card1: expected {truth_status}, got {got1}")
        except Exception as e:
            problems.append(f"card1 execution failed: {e}")

        # Card 2: Average Progress by Objective (tolerance 0.05)
        card2 = st["cards"]["Average Progress by Objective"]
        try:
            _, rows2 = mb_run_card(card2["id"], token)
            got2 = {}
            for row in rows2:
                key = None
                key_idx = None
                for i, cell in enumerate(row):
                    if re.fullmatch(r"OBJ-\d+", str(cell).strip()):
                        key = str(cell).strip()
                        key_idx = i
                        break
                if key is None:
                    for i, cell in enumerate(row):
                        rid = _as_int_str(cell)
                        if rid is not None and rid in objid_by_rowid:
                            key = objid_by_rowid[rid]
                            key_idx = i
                            break
                val = None
                for i, cell in enumerate(row):
                    if i == key_idx:
                        continue
                    try:
                        val = float(cell)
                        break
                    except (TypeError, ValueError):
                        continue
                if key is None or val is None:
                    problems.append(f"card2: unrecognized row {row!r}")
                    continue
                got2[key] = val
            if set(got2) != set(truth_avg):
                problems.append(
                    f"card2: objective set {sorted(got2)} != {sorted(truth_avg)}")
            else:
                for obj, exp_avg in truth_avg.items():
                    if abs(got2[obj] - exp_avg) > 0.05:
                        problems.append(
                            f"card2 {obj}: expected {round(exp_avg, 2)}, got {got2[obj]}")
        except Exception as e:
            problems.append(f"card2 execution failed: {e}")

        # Card 3: Off-Track KRs -> exactly 3 rows, 5 columns, Progress Pct ascending
        card3 = st["cards"]["Off-Track KRs"]
        try:
            cols3, rows3 = mb_run_card(card3["id"], token)
            if len(cols3) < 5:
                problems.append(
                    f"card3: expected >= 5 columns (KR ID/Description/Current/"
                    f"Target/Progress Pct), got {len(cols3)}")
            if len(rows3) != 3:
                problems.append(f"card3: expected exactly 3 rows, got {len(rows3)}")
            else:
                order = []
                for row in rows3:
                    kr = None
                    for cell in row:
                        if re.fullmatch(r"KR-\d+", str(cell).strip()):
                            kr = str(cell).strip()
                            break
                    if kr is None or kr not in EXPECTED_KR:
                        problems.append(f"card3: row without known KR ID: {row!r}")
                        continue
                    order.append(kr)
                    exp = EXPECTED_KR[kr]
                    if not any(str(c).strip() == exp["desc"] for c in row):
                        problems.append(f"card3 {kr}: Description value missing")
                    for col, expv in (("Current", exp["current"]),
                                      ("Target", exp["target"]),
                                      ("Progress Pct", exp["pct"])):
                        if not any(_float_eq(c, expv) for c in row):
                            problems.append(f"card3 {kr}: {col}={expv} missing")
                if order != truth_offtrack_order:
                    problems.append(
                        f"card3: expected Progress Pct-ascending order "
                        f"{truth_offtrack_order}, got {order}")
        except Exception as e:
            problems.append(f"card3 execution failed: {e}")

        # MBQL shape assertions (query-type cards only; native cards are judged
        # purely by execution above).
        shape1 = mb_shape(card1)
        shape3 = mb_shape(card3)
        if not shape1["is_native"] or not shape3["is_native"]:
            db = st["pg_db"]
            status_fid = (_field_by_name(_kr_fields, "Status") or {}).get("id")
            pct_fid = (_field_by_name(_kr_fields, "Progress Pct") or {}).get("id")
            status_mb_fid = pct_mb_fid = None
            if db is None:
                problems.append(
                    "MBQL shape unverifiable: 'Baserow Postgres' datasource missing")
            else:
                _, fmap = mb_resolve_table(
                    token, db["id"], f"database_table_{_kr_table_id}")
                status_mb_fid = fmap.get(f"field_{status_fid}")
                pct_mb_fid = fmap.get(f"field_{pct_fid}")
                if status_mb_fid is None or pct_mb_fid is None:
                    problems.append(
                        f"MBQL shape unverifiable: database_table_{_kr_table_id} "
                        f"fields not in Metabase metadata (sync incomplete)")
            if not shape1["is_native"] and status_mb_fid is not None:
                if shape1["agg_ops"] != {"count"}:
                    problems.append(
                        f"card1 MBQL: aggregation {shape1['agg_ops']} != count")
                if shape1["breakout_fids"] != [status_mb_fid]:
                    problems.append(
                        f"card1 MBQL: breakout {shape1['breakout_fids']} != "
                        f"[Status field {status_mb_fid}]")
            if not shape3["is_native"] and status_mb_fid is not None:
                offtrack_ids = {oid for oid, v in opts.items() if v == "OffTrack"}
                fsum = mb_filter_summary(shape3["filters"])
                ok_filter = any(
                    op == "=" and fid == status_mb_fid
                    and any(str(v) in offtrack_ids or str(v) == "OffTrack"
                            for v in vals)
                    for op, fid, vals in fsum
                )
                if not ok_filter:
                    problems.append(
                        f"card3 MBQL: no Status=OffTrack filter (got {fsum})")
                if shape3["order_by"] != [("asc", pct_mb_fid)]:
                    problems.append(
                        f"card3 MBQL: order_by {shape3['order_by']} != "
                        f"[(asc, Progress Pct field {pct_mb_fid})]")

        ok = len(problems) == 0
        detail = "; ".join(problems) if problems else f"3 cards match ({truth_summary})"
        check(label, 3, ok, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_9_metabase_dashboard() -> None:
    """Dashboard is in the collection with exactly the 3 verified question cards."""
    label = "9. Metabase dashboard (collection + cards)"
    try:
        st = _metabase_state()
        if st["error"]:
            check(label, 2, False, f"metabase error: {st['error']}")
            return
        token = st["token"]
        headers = {"X-Metabase-Session": token}

        # Search for dashboard
        resp = requests.get(
            f"{METABASE_URL}/api/search",
            params={"q": "Engineering OKRs Q3 2026 Dashboard", "models": "dashboard"},
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])

        dash_id = None
        for r in results:
            if r.get("name") == "Engineering OKRs Q3 2026 Dashboard":
                dash_id = r["id"]
                break

        if dash_id is None:
            check(label, 2, False, "dashboard not found")
            return

        dash = metabase_get(f"dashboard/{dash_id}", token)

        problems = []
        if st["coll_id"] is None or dash.get("collection_id") != st["coll_id"]:
            problems.append(
                f"collection_id={dash.get('collection_id')} != "
                f"'Engineering OKRs Q3 2026' id {st['coll_id']}")

        expected_ids = {c.get("id") for c in st["cards"].values()} - {None}
        if len(expected_ids) != 3:
            problems.append(
                f"only {len(expected_ids)}/3 expected question cards resolved")

        dashcards = dash.get("dashcards", dash.get("ordered_cards", []))
        found_ids = set()
        n_question = 0
        for c in dashcards:
            cid = c.get("card_id") or (c.get("card") or {}).get("id")
            if cid:
                n_question += 1
                found_ids.add(cid)
        if n_question != 3:
            problems.append(f"expected exactly 3 question cards, got {n_question}")
        if found_ids != expected_ids:
            problems.append(
                f"dashcard card_ids {sorted(found_ids)} != "
                f"question card ids {sorted(expected_ids)}")

        ok = len(problems) == 0
        detail = "; ".join(problems) if problems else \
            f"in collection {st['coll_id']}, card ids {sorted(found_ids)}"
        check(label, 2, ok, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_metabase_datasource() -> None:
    """'Baserow Postgres' datasource exists and its metadata shows the synced
    Key Results physical table."""
    label = "14. Metabase Baserow Postgres datasource"
    try:
        st = _metabase_state()
        if st["error"]:
            check(label, 1, False, f"metabase error: {st['error']}")
            return
        db = st["pg_db"]
        if db is None:
            check(label, 1, False,
                  "no postgres datasource named 'Baserow Postgres' found")
            return
        _init_baserow()
        if _kr_table_id is None:
            check(label, 1, False, "Baserow Key Results table id unknown")
            return
        tid, _ = mb_resolve_table(
            st["token"], db["id"], f"database_table_{_kr_table_id}")
        ok = tid is not None
        dbname = (db.get("details") or {}).get("dbname")
        detail = (
            f"db id={db['id']}, dbname={dbname}, "
            f"database_table_{_kr_table_id} {'synced' if ok else 'MISSING (sync incomplete)'}"
        )
        check(label, 1, ok, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# -- Baserow schema check (REST fields API) --

def check_13_baserow_schema() -> None:
    """Both tables have exactly the specified fields with correct types, primary
    flags, link target, decimal places and select options."""
    label = "13. Baserow field schema"
    try:
        _init_baserow()
        if not _obj_fields or not _kr_fields:
            check(label, 2, False, "field metadata unavailable (tables missing?)")
            return

        problems = []

        obj_expected_names = {"Objective ID", "Title", "Owner", "Quarter"}
        obj_names = {f.get("name") for f in _obj_fields}
        if obj_names != obj_expected_names:
            problems.append(
                f"Objectives field set {sorted(obj_names)} != "
                f"{sorted(obj_expected_names)}")

        kr_expected_names = {"KR ID", "Objective ID", "Description", "Target",
                             "Current", "Progress Pct", "Status"}
        kr_names = {f.get("name") for f in _kr_fields}
        if kr_names != kr_expected_names:
            problems.append(
                f"Key Results field set {sorted(kr_names)} != "
                f"{sorted(kr_expected_names)}")

        def expect(fields, table, name, ftype, primary=None, decimal_places=None,
                   options=None, link_table=None):
            f = _field_by_name(fields, name)
            if not f:
                problems.append(f"{table}.{name}: field missing")
                return
            if f.get("type") != ftype:
                problems.append(
                    f"{table}.{name}: type={f.get('type')!r}, expected {ftype!r}")
            if primary is not None and bool(f.get("primary")) != primary:
                problems.append(f"{table}.{name}: primary={f.get('primary')}")
            if decimal_places is not None and \
                    f.get("number_decimal_places") != decimal_places:
                problems.append(
                    f"{table}.{name}: number_decimal_places="
                    f"{f.get('number_decimal_places')}, expected {decimal_places}")
            if options is not None:
                got = {o.get("value") for o in f.get("select_options") or []}
                if got != options:
                    problems.append(
                        f"{table}.{name}: options {sorted(got)} != {sorted(options)}")
            if link_table is not None and f.get("link_row_table_id") != link_table:
                problems.append(
                    f"{table}.{name}: link_row_table_id="
                    f"{f.get('link_row_table_id')}, expected {link_table}")

        expect(_obj_fields, "Objectives", "Objective ID", "text", primary=True)
        expect(_obj_fields, "Objectives", "Title", "text")
        expect(_obj_fields, "Objectives", "Owner", "text")
        expect(_obj_fields, "Objectives", "Quarter", "text")

        expect(_kr_fields, "Key Results", "KR ID", "text", primary=True)
        expect(_kr_fields, "Key Results", "Objective ID", "link_row",
               link_table=_obj_table_id)
        expect(_kr_fields, "Key Results", "Description", "text")
        expect(_kr_fields, "Key Results", "Target", "number")
        expect(_kr_fields, "Key Results", "Current", "number")
        expect(_kr_fields, "Key Results", "Progress Pct", "number",
               decimal_places=1)
        expect(_kr_fields, "Key Results", "Status", "single_select",
               options={"OnTrack", "AtRisk", "OffTrack"})

        ok = len(problems) == 0
        detail = "; ".join(problems) if problems else "both table schemas exact"
        check(label, 2, ok, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# -- OpenProject checks (embedded DB) --

def _op_query(sql: str) -> str:
    """Run a SQL query against OpenProject's embedded Postgres."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed (rc={r.returncode}): {r.stderr.strip()}")
    return r.stdout.strip()


def check_10_op_recover_kr_tasks() -> None:
    """OpenProject 'API Gateway' has exactly one Task WP per OffTrack KR (set equality)."""
    try:
        # Get project ID
        project_id = _op_query(
            "SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1"
        )
        if not project_id:
            check("10. OpenProject Recover KR tasks (3)", 2, False,
                  "project 'API Gateway' not found")
            return

        # Get type ID for 'Task' -- lookup failure is a FAIL, never an
        # unfiltered fallback.
        task_type_id = _op_query(
            "SELECT id FROM types WHERE name = 'Task' LIMIT 1"
        )
        if not task_type_id:
            check("10. OpenProject Recover KR tasks (3)", 2, False,
                  "type 'Task' not found in types table")
            return

        # Find Task work packages with subject matching 'Recover KR: KR-*'
        rows = _op_query(
            f"SELECT subject FROM work_packages "
            f"WHERE project_id = {project_id} AND type_id = {task_type_id} "
            f"AND subject LIKE 'Recover KR:%'"
        )
        found_subjects = {line.strip() for line in rows.splitlines() if line.strip()}

        expected_subjects = {f"Recover KR: {kr_id}" for kr_id in OFFTRACK_KRS}
        missing = expected_subjects - found_subjects
        extra = found_subjects - expected_subjects

        ok = found_subjects == expected_subjects
        detail_parts = []
        if missing:
            detail_parts.append(f"missing: {missing}")
        if extra:
            detail_parts.append(f"extra: {extra}")
        if not detail_parts:
            detail_parts.append(f"exactly {len(expected_subjects)} subjects match")
        check("10. OpenProject Recover KR tasks (3)", 2, ok,
              "; ".join(detail_parts))
    except Exception as e:
        check("10. OpenProject Recover KR tasks (3)", 2, False,
              f"exception: {e}")


def check_11_op_priority_high() -> None:
    """Recover KR work packages have High priority."""
    try:
        project_id = _op_query(
            "SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1"
        )
        if not project_id:
            check("11. OpenProject WP priority High", 1, False,
                  "project not found")
            return

        rows = _op_query(
            f"SELECT wp.subject, p.name AS priority "
            f"FROM work_packages wp "
            f"JOIN enumerations p ON wp.priority_id = p.id "
            f"WHERE wp.project_id = {project_id} "
            f"AND wp.subject LIKE 'Recover KR:%'"
        )
        if not rows:
            check("11. OpenProject WP priority High", 1, False, "no WPs found")
            return

        non_high = []
        for line in rows.splitlines():
            parts = line.split("|")
            if len(parts) == 2:
                subj, pri = parts[0].strip(), parts[1].strip()
                if pri != "High":
                    non_high.append(f"{subj} has priority {pri}")

        ok = len(non_high) == 0
        detail = "; ".join(non_high) if non_high else "all High"
        check("11. OpenProject WP priority High", 1, ok, detail)
    except Exception as e:
        check("11. OpenProject WP priority High", 1, False, f"exception: {e}")


def check_12_op_description() -> None:
    """Recover KR work package descriptions contain the exact substring
    'Target=<T>, Current=<C>, Progress=<P>%'."""
    try:
        project_id = _op_query(
            "SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1"
        )
        if not project_id:
            check("12. OpenProject WP descriptions", 1, False,
                  "project not found")
            return

        subjects = _op_query(
            f"SELECT subject FROM work_packages "
            f"WHERE project_id = {project_id} "
            f"AND subject LIKE 'Recover KR:%'"
        )
        subject_list = [s.strip() for s in subjects.splitlines() if s.strip()]
        if not subject_list:
            check("12. OpenProject WP descriptions", 1, False, "no WPs found")
            return

        mismatches = []
        for subj in subject_list:
            kr_id = subj.replace("Recover KR:", "").strip()
            if kr_id not in EXPECTED_KR:
                mismatches.append(f"{subj!r}: unknown KR")
                continue
            exp = EXPECTED_KR[kr_id]
            subj_sql = subj.replace("'", "''")
            desc = _op_query(
                f"SELECT COALESCE(description, '') FROM work_packages "
                f"WHERE project_id = {project_id} "
                f"AND subject = '{subj_sql}' LIMIT 1"
            )
            needle = (
                f"Target={exp['target']}, Current={exp['current']}, "
                f"Progress={exp['pct']}%"
            )
            if needle not in desc:
                mismatches.append(f"{kr_id}: missing exact substring {needle!r}")

        ok = len(mismatches) == 0
        detail = "; ".join(mismatches) if mismatches else "all exact"
        check("12. OpenProject WP descriptions", 1, ok, detail)
    except Exception as e:
        check("12. OpenProject WP descriptions", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_db_and_tables()
    check_2_objectives_data()
    check_3_kr_rows_and_ids()
    check_4_kr_target_current()
    check_5_kr_progress_pct()
    check_6_kr_status()
    check_7_metabase_collection()
    check_8a_metabase_questions()
    check_8b_metabase_card_results()
    check_9_metabase_dashboard()
    check_10_op_recover_kr_tasks()
    check_11_op_priority_high()
    check_12_op_description()
    check_13_baserow_schema()
    check_14_metabase_datasource()

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
