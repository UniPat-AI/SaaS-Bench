"""
Verifier for Software-021-I1: Build Engineering Hiring Pipeline Analytics
across Baserow, Metabase, and OpenProject.

Checks: 14 weighted checks (22 total points).
Strategy: Baserow API, Metabase API (card semantics), OpenProject embedded DB.

Required env vars:
  SERVER_HOSTNAME, BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER.
"""

import os
import re
import sys
import json
import time
import subprocess
import requests
from datetime import date

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

REQUIRED_VARS = {
    "BASEROW_PORT": None,
    "BASEROW_CONTAINER": None,
    "METABASE_PORT": None,
    "METABASE_CONTAINER": None,
    "OPENPROJECT_PORT": None,
    "OPENPROJECT_CONTAINER": None,
}
for var in REQUIRED_VARS:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    REQUIRED_VARS[var] = val

BASEROW_PORT = REQUIRED_VARS["BASEROW_PORT"]
METABASE_PORT = REQUIRED_VARS["METABASE_PORT"]
OPENPROJECT_PORT = REQUIRED_VARS["OPENPROJECT_PORT"]

BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"
METABASE_BASE = f"http://{HOST}:{METABASE_PORT}"
OPENPROJECT_BASE = f"http://{HOST}:{OPENPROJECT_PORT}"

# Login credentials
BASEROW_EMAIL = "admin@example.com"
BASEROW_PASSWORD = "Admin1234"
METABASE_EMAIL = "admin@metabase.local"
METABASE_PASSWORD = "mw-admin-123"
OPENPROJECT_USER = "admin"
OPENPROJECT_PASS = "AdminPass123!"

# Expected data
REPORT_DATE = date(2026, 3, 15)

POSITIONS_DATA = [
    {"pos_id": "POS-001", "role": "Senior Backend Engineer", "level": "Senior", "manager": "Lane Mahon", "team": "Platform", "opened": "2026-01-12", "approved": True},
    {"pos_id": "POS-002", "role": "Staff Frontend Engineer", "level": "Staff", "manager": "Elizabeth Cunningham", "team": "Frontend", "opened": "2026-01-20", "approved": True},
    {"pos_id": "POS-003", "role": "Mid Data Engineer", "level": "Mid", "manager": "Jane Dradder", "team": "Data", "opened": "2026-02-03", "approved": True},
    {"pos_id": "POS-004", "role": "Principal Security Engineer", "level": "Principal", "manager": "Latisha Mazon", "team": "Security", "opened": "2026-02-10", "approved": False},
    {"pos_id": "POS-005", "role": "Junior DevOps Engineer", "level": "Junior", "manager": "John Marshall", "team": "DevOps", "opened": "2026-02-18", "approved": True},
    {"pos_id": "POS-006", "role": "Senior Platform Engineer", "level": "Senior", "manager": "Lane Mahon", "team": "Platform", "opened": "2026-02-25", "approved": False},
]

CANDIDATES_DATA = [
    {"cand_id": "CAND-001", "pos": "POS-001", "stage": "Onsite", "sourced": "2026-02-15", "days": 28, "updated": "2026-03-10", "offer": "None"},
    {"cand_id": "CAND-002", "pos": "POS-001", "stage": "Offer", "sourced": "2026-02-01", "days": 42, "updated": "2026-03-12", "offer": "Pending"},
    {"cand_id": "CAND-003", "pos": "POS-002", "stage": "Technical", "sourced": "2026-02-20", "days": 23, "updated": "2026-03-11", "offer": "None"},
    {"cand_id": "CAND-004", "pos": "POS-002", "stage": "Screen", "sourced": "2026-03-01", "days": 14, "updated": "2026-03-08", "offer": "None"},
    {"cand_id": "CAND-005", "pos": "POS-003", "stage": "Hired", "sourced": "2026-02-05", "days": 38, "updated": "2026-03-14", "offer": "Accepted"},
    {"cand_id": "CAND-006", "pos": "POS-003", "stage": "Rejected", "sourced": "2026-02-10", "days": 33, "updated": "2026-02-25", "offer": "Declined"},
    {"cand_id": "CAND-007", "pos": "POS-004", "stage": "Sourced", "sourced": "2026-03-05", "days": 10, "updated": "2026-03-05", "offer": "None"},
    {"cand_id": "CAND-008", "pos": "POS-005", "stage": "Screen", "sourced": "2026-02-28", "days": 15, "updated": "2026-03-09", "offer": "None"},
    {"cand_id": "CAND-009", "pos": "POS-005", "stage": "Technical", "sourced": "2026-02-22", "days": 21, "updated": "2026-03-13", "offer": "None"},
    {"cand_id": "CAND-010", "pos": "POS-006", "stage": "Sourced", "sourced": "2026-03-08", "days": 7, "updated": "2026-03-08", "offer": "None"},
]

APPROVED_POSITIONS = [p for p in POSITIONS_DATA if p["approved"]]

EXPECTED_QUESTIONS = {
    "Funnel by Stage",
    "Average Days-In-Pipeline by Team",
    "Open Positions by Level",
}


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Auth helpers ──────────────────────────────────────────────────────────────
_baserow_token = None
_metabase_session = None


def baserow_auth() -> str:
    global _baserow_token
    if _baserow_token:
        return _baserow_token
    r = requests.post(
        f"{BASEROW_BASE}/api/user/token-auth/",
        json={"email": BASEROW_EMAIL, "password": BASEROW_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    # Baserow returns both 'token' and 'access_token'; use access_token with JWT auth
    _baserow_token = data.get("access_token", data.get("token", ""))
    return _baserow_token


def baserow_get(path: str) -> dict | list:
    token = baserow_auth()
    r = requests.get(
        f"{BASEROW_BASE}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def metabase_auth() -> str:
    global _metabase_session
    if _metabase_session:
        return _metabase_session
    r = requests.post(
        f"{METABASE_BASE}/api/session",
        json={"username": METABASE_EMAIL, "password": METABASE_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    _metabase_session = r.json()["id"]
    return _metabase_session


def metabase_get(path: str) -> dict | list:
    session = metabase_auth()
    r = requests.get(
        f"{METABASE_BASE}/api/{path}",
        headers={"X-Metabase-Session": session},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def metabase_post(path: str, payload=None, timeout: int = 60):
    session = metabase_auth()
    r = requests.post(
        f"{METABASE_BASE}/api/{path}",
        headers={"X-Metabase-Session": session},
        json=payload if payload is not None else {},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def openproject_get(path: str) -> dict:
    """OpenProject API with Host header and basic auth."""
    host_header = f"{HOST}:{OPENPROJECT_PORT}"
    r = requests.get(
        f"{OPENPROJECT_BASE}/api/v3/{path}",
        auth=("apikey", OPENPROJECT_PASS),
        headers={"Host": host_header},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def op_db_query(query: str) -> str:
    """Run a SQL query against the OpenProject embedded Postgres."""
    container = REQUIRED_VARS["OPENPROJECT_CONTAINER"]
    rc, out, err = docker_exec(
        container, "bash", "-c",
        f"PGPASSWORD=openproject psql -U openproject -d openproject -h 127.0.0.1 -t -A -c \"{query}\"",
    )
    if rc != 0:
        raise RuntimeError(f"op_db_query failed: {err.strip()}")
    return out.strip()


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


def mb_run_card(card_id):
    """Execute a saved card server-side. Returns (col_names, rows).
    Raises RuntimeError on failure (one retry for sync lag). Works for native SQL cards too."""
    last_err = None
    for attempt in range(2):
        try:
            res = metabase_post(f"card/{card_id}/query", timeout=60)
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


def mb_find_pg_db(name=None):
    """Locate the Baserow Postgres datasource. Never hardcode db ids.
    name: pass "Baserow Postgres" only where the task text mandates the name."""
    resp = metabase_get("database")
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


def mb_resolve_table(mb_db_id, physical_table_name):
    """(mb_table_id, {physical_field_name: mb_field_id}) via GET /api/database/<id>/metadata.
    physical_table_name is e.g. f"database_table_{tid}"; field names are f"field_{baserow_fid}"."""
    meta = metabase_get(f"database/{mb_db_id}/metadata")
    for tbl in meta.get("tables", []) or []:
        if tbl.get("name") == physical_table_name:
            fmap = {f.get("name"): f.get("id") for f in tbl.get("fields", []) or []}
            return tbl.get("id"), fmap
    return None, {}


# ── Shared state (populated by early checks, used by later ones) ─────────────
_baserow_db_id = None
_positions_table_id = None
_candidates_table_id = None
_positions_fields = None
_candidates_fields = None
_mb_pg_db = None
_mb_pg_db_resolved = False
_mb_cards_cache = None


def _get_positions_fields() -> list:
    global _positions_fields
    if _positions_fields is None and _positions_table_id:
        _positions_fields = baserow_get(f"database/fields/table/{_positions_table_id}/")
    return _positions_fields or []


def _get_candidates_fields() -> list:
    global _candidates_fields
    if _candidates_fields is None and _candidates_table_id:
        _candidates_fields = baserow_get(f"database/fields/table/{_candidates_table_id}/")
    return _candidates_fields or []


def _field_by_name(fields, name):
    for f in fields or []:
        if f.get("name") == name:
            return f
    return None


def _select_value(val) -> str:
    """Normalize a Baserow single-select cell ({'id':..,'value':..} or str)."""
    if isinstance(val, dict):
        return str(val.get("value", "") or "").strip()
    return str(val or "").strip()


def _find_baserow_pg_db():
    """Resolve the 'Baserow Postgres' Metabase datasource once (never hardcoded)."""
    global _mb_pg_db, _mb_pg_db_resolved
    if not _mb_pg_db_resolved:
        _mb_pg_db = mb_find_pg_db(name="Baserow Postgres")
        _mb_pg_db_resolved = True
    return _mb_pg_db


def _mb_expected_cards() -> dict:
    """Full card JSON for the three expected questions, keyed by name (fetched once)."""
    global _mb_cards_cache
    if _mb_cards_cache is not None:
        return _mb_cards_cache
    cards = {}
    coll_id = _find_collection_id("Hiring Analytics")
    if coll_id:
        items = metabase_get(f"collection/{coll_id}/items?models=card")
        data = items.get("data", items) if isinstance(items, dict) else items
        for item in data:
            name = item.get("name", "")
            if item.get("model", "card") == "card" and name in EXPECTED_QUESTIONS:
                cards[name] = metabase_get(f"card/{item['id']}")
    _mb_cards_cache = cards
    return cards


def _schema_problems(fields, table: str, spec: dict) -> list[str]:
    """Assert per-field type/primary/options/link target against spec."""
    problems = []
    for name, exp in spec.items():
        f = _field_by_name(fields, name)
        if not f:
            problems.append(f"{table}.{name}: field missing")
            continue
        if f.get("type") != exp["type"]:
            problems.append(
                f"{table}.{name}: type={f.get('type')!r}, expected {exp['type']!r}")
        if exp.get("primary") and not f.get("primary"):
            problems.append(f"{table}.{name}: primary={f.get('primary')}")
        if "options" in exp:
            got = {o.get("value") for o in f.get("select_options") or []}
            if got != exp["options"]:
                problems.append(
                    f"{table}.{name}: options {sorted(got)} != {sorted(exp['options'])}")
        if "link_table" in exp:
            if f.get("link_row_table_id") != exp["link_table"]:
                problems.append(
                    f"{table}.{name}: link_row_table_id={f.get('link_row_table_id')}, "
                    f"expected {exp['link_table']}")
    return problems


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_baserow_database() -> None:
    """Database 'Engineering Hiring Pipeline' exists in Baserow."""
    global _baserow_db_id
    try:
        apps = baserow_get("applications/")
        for app in apps:
            if app.get("name") == "Engineering Hiring Pipeline" and app.get("type") == "database":
                _baserow_db_id = app["id"]
                break
        check("1. Baserow database exists", 1, _baserow_db_id is not None,
              f"db_id={_baserow_db_id}" if _baserow_db_id else "not found")
    except Exception as e:
        check("1. Baserow database exists", 1, False, f"exception: {e}")


def check_2_open_positions_table() -> None:
    """'Open Positions' has exactly 6 rows matching POSITIONS_DATA per Position ID."""
    global _positions_table_id
    try:
        if not _baserow_db_id:
            check("2. Open Positions table", 2, False, "no database found")
            return
        tables = baserow_get(f"database/tables/database/{_baserow_db_id}/")
        for t in tables:
            if t["name"] == "Open Positions":
                _positions_table_id = t["id"]
                break
        if not _positions_table_id:
            check("2. Open Positions table", 2, False, "table not found")
            return

        rows_resp = baserow_get(f"database/rows/table/{_positions_table_id}/?user_field_names=true&size=200")
        rows = rows_resp.get("results", [])

        problems = []
        if len(rows) != 6:
            problems.append(f"expected 6 rows, got {len(rows)}")
        by_id = {}
        for row in rows:
            pid = str(row.get("Position ID", "") or "").strip()
            if pid in by_id:
                problems.append(f"duplicate Position ID {pid}")
            by_id[pid] = row
        expected_ids = {p["pos_id"] for p in POSITIONS_DATA}
        if set(by_id) != expected_ids:
            problems.append(
                f"Position ID set mismatch: missing={sorted(expected_ids - set(by_id))}, "
                f"extra={sorted(set(by_id) - expected_ids)}")
        for p in POSITIONS_DATA:
            row = by_id.get(p["pos_id"])
            if row is None:
                continue
            pid = p["pos_id"]
            if str(row.get("Role Title", "") or "").strip() != p["role"]:
                problems.append(f"{pid}: Role Title={row.get('Role Title')!r}")
            if _select_value(row.get("Level")) != p["level"]:
                problems.append(f"{pid}: Level={_select_value(row.get('Level'))!r}")
            if str(row.get("Hiring Manager", "") or "").strip() != p["manager"]:
                problems.append(f"{pid}: Hiring Manager={row.get('Hiring Manager')!r}")
            if _select_value(row.get("Team")) != p["team"]:
                problems.append(f"{pid}: Team={_select_value(row.get('Team'))!r}")
            if not str(row.get("Date Opened") or "").startswith(p["opened"]):
                problems.append(f"{pid}: Date Opened={row.get('Date Opened')!r}")
            if bool(row.get("Headcount Approved")) != p["approved"]:
                problems.append(
                    f"{pid}: Headcount Approved={row.get('Headcount Approved')!r}")

        check("2. Open Positions table", 2, not problems,
              "6 rows, all fields match" if not problems else "; ".join(problems))
    except Exception as e:
        check("2. Open Positions table", 2, False, f"exception: {e}")


def check_2b_open_positions_schema() -> None:
    """'Open Positions' field schema: types, primary, select option sets."""
    try:
        if not _positions_table_id:
            check("2b. Open Positions schema", 1, False, "table not found")
            return
        fields = _get_positions_fields()
        spec = {
            "Position ID": {"type": "text", "primary": True},
            "Role Title": {"type": "text"},
            "Level": {"type": "single_select",
                      "options": {"Junior", "Mid", "Senior", "Staff", "Principal"}},
            "Hiring Manager": {"type": "text"},
            "Team": {"type": "single_select",
                     "options": {"Platform", "Frontend", "Data", "Security", "DevOps"}},
            "Date Opened": {"type": "date"},
            "Headcount Approved": {"type": "boolean"},
        }
        problems = _schema_problems(fields, "Open Positions", spec)
        check("2b. Open Positions schema", 1, not problems,
              "schema matches" if not problems else "; ".join(problems))
    except Exception as e:
        check("2b. Open Positions schema", 1, False, f"exception: {e}")


def check_3_candidate_pipeline_table() -> None:
    """'Candidate Pipeline' has exactly 10 rows; per Candidate ID the Position
    link and Stage match CANDIDATES_DATA."""
    global _candidates_table_id
    try:
        if not _baserow_db_id:
            check("3. Candidate Pipeline table", 2, False, "no database found")
            return
        tables = baserow_get(f"database/tables/database/{_baserow_db_id}/")
        for t in tables:
            if t["name"] == "Candidate Pipeline":
                _candidates_table_id = t["id"]
                break
        if not _candidates_table_id:
            check("3. Candidate Pipeline table", 2, False, "table not found")
            return

        rows_resp = baserow_get(f"database/rows/table/{_candidates_table_id}/?user_field_names=true&size=200")
        rows = rows_resp.get("results", [])

        problems = []
        if len(rows) != 10:
            problems.append(f"expected 10 rows, got {len(rows)}")
        by_id = {}
        for row in rows:
            cid = str(row.get("Candidate ID", "") or "").strip()
            if cid in by_id:
                problems.append(f"duplicate Candidate ID {cid}")
            by_id[cid] = row
        expected_ids = {c["cand_id"] for c in CANDIDATES_DATA}
        if set(by_id) != expected_ids:
            problems.append(
                f"Candidate ID set mismatch: missing={sorted(expected_ids - set(by_id))}, "
                f"extra={sorted(set(by_id) - expected_ids)}")
        for c in CANDIDATES_DATA:
            row = by_id.get(c["cand_id"])
            if row is None:
                continue
            cid = c["cand_id"]
            links = row.get("Position ID")
            if not isinstance(links, list) or len(links) != 1:
                problems.append(f"{cid}: expected exactly 1 Position link, got {links!r}")
            elif str(links[0].get("value", "")).strip() != c["pos"]:
                problems.append(
                    f"{cid}: Position link={links[0].get('value')!r}, expected {c['pos']}")
            if _select_value(row.get("Stage")) != c["stage"]:
                problems.append(f"{cid}: Stage={_select_value(row.get('Stage'))!r}")

        check("3. Candidate Pipeline table", 2, not problems,
              "10 rows, links and stages match" if not problems else "; ".join(problems))
    except Exception as e:
        check("3. Candidate Pipeline table", 2, False, f"exception: {e}")


def check_3b_candidate_pipeline_schema() -> None:
    """'Candidate Pipeline' field schema incl. link_row target table."""
    try:
        if not _candidates_table_id:
            check("3b. Candidate Pipeline schema", 1, False, "table not found")
            return
        fields = _get_candidates_fields()
        spec = {
            "Candidate ID": {"type": "text", "primary": True},
            "Position ID": {"type": "link_row", "link_table": _positions_table_id},
            "Stage": {"type": "single_select",
                      "options": {"Sourced", "Screen", "Technical", "Onsite",
                                  "Offer", "Hired", "Rejected"}},
            "Days In Pipeline": {"type": "number"},
            "Last Updated": {"type": "date"},
            "Offer Status": {"type": "single_select",
                             "options": {"None", "Pending", "Accepted", "Declined"}},
        }
        if not _positions_table_id:
            spec["Position ID"] = {"type": "link_row"}
        problems = _schema_problems(fields, "Candidate Pipeline", spec)
        if not _positions_table_id:
            problems.append("Open Positions table id unknown; link target unverified")
        check("3b. Candidate Pipeline schema", 1, not problems,
              "schema matches" if not problems else "; ".join(problems))
    except Exception as e:
        check("3b. Candidate Pipeline schema", 1, False, f"exception: {e}")


def check_4_days_in_pipeline() -> None:
    """Per Candidate ID: (Days In Pipeline, Last Updated, Offer Status) match."""
    try:
        if not _candidates_table_id:
            check("4. Days/Last Updated/Offer per candidate", 2, False,
                  "no candidates table")
            return

        rows_resp = baserow_get(f"database/rows/table/{_candidates_table_id}/?user_field_names=true&size=200")
        rows = rows_resp.get("results", [])

        by_id = {str(row.get("Candidate ID", "") or "").strip(): row for row in rows}
        problems = []
        for c in CANDIDATES_DATA:
            cid = c["cand_id"]
            row = by_id.get(cid)
            if row is None:
                problems.append(f"{cid}: row missing")
                continue
            try:
                days = int(float(str(row.get("Days In Pipeline"))))
            except (ValueError, TypeError):
                days = None
            if days != c["days"]:
                problems.append(f"{cid}: Days In Pipeline={row.get('Days In Pipeline')!r}, "
                                f"expected {c['days']}")
            if not str(row.get("Last Updated") or "").startswith(c["updated"]):
                problems.append(f"{cid}: Last Updated={row.get('Last Updated')!r}, "
                                f"expected {c['updated']}")
            if _select_value(row.get("Offer Status")) != c["offer"]:
                problems.append(f"{cid}: Offer Status="
                                f"{_select_value(row.get('Offer Status'))!r}, "
                                f"expected {c['offer']}")

        check("4. Days/Last Updated/Offer per candidate", 2, not problems,
              "all 10 triples match" if not problems else "; ".join(problems))
    except Exception as e:
        check("4. Days/Last Updated/Offer per candidate", 2, False, f"exception: {e}")


def check_5_gallery_view() -> None:
    """Gallery view 'Funnel' exists on Candidate Pipeline."""
    try:
        if not _candidates_table_id:
            check("5. Gallery view Funnel", 1, False, "no candidates table")
            return

        views = baserow_get(f"database/views/table/{_candidates_table_id}/")
        funnel = None
        for v in views:
            if v.get("name") == "Funnel" and v.get("type") == "gallery":
                funnel = v
                break

        check("5. Gallery view Funnel", 1, funnel is not None,
              "found gallery view" if funnel else "not found or not gallery type")
    except Exception as e:
        check("5. Gallery view Funnel", 1, False, f"exception: {e}")


def check_6_metabase_collection() -> None:
    """Metabase collection 'Hiring Analytics' exists."""
    try:
        collections = metabase_get("collection")
        found = any(c.get("name") == "Hiring Analytics" for c in collections)
        check("6. Metabase collection", 1, found,
              "found" if found else "collection 'Hiring Analytics' not found")
    except Exception as e:
        check("6. Metabase collection", 1, False, f"exception: {e}")


def check_6b_metabase_datasource() -> None:
    """PostgreSQL datasource 'Baserow Postgres' (dbname baserow) exists."""
    try:
        db = _find_baserow_pg_db()
        if db is None:
            check("6b. Baserow Postgres datasource", 1, False,
                  "no postgres datasource named 'Baserow Postgres' with dbname=baserow")
            return
        dbname = (db.get("details") or {}).get("dbname")
        check("6b. Baserow Postgres datasource", 1, True,
              f"db_id={db['id']}, dbname={dbname}")
    except Exception as e:
        check("6b. Baserow Postgres datasource", 1, False, f"exception: {e}")


def _find_collection_id(name: str) -> int | None:
    collections = metabase_get("collection")
    for c in collections:
        if c.get("name") == name:
            return c["id"]
    return None


def _has_ident(sql: str, ident: str) -> bool:
    return re.search(rf"\b{re.escape(ident)}\b", sql) is not None


def check_7_metabase_questions() -> None:
    """Three saved questions with correct display, datasource, and query
    semantics (MBQL shape or native SQL shape)."""
    label = "7. Metabase questions (semantics)"
    try:
        cards = _mb_expected_cards()
        missing = EXPECTED_QUESTIONS - set(cards)
        if missing:
            check(label, 3, False, f"missing cards: {sorted(missing)}")
            return

        problems = []
        db = _find_baserow_pg_db()
        db_id = db["id"] if db else None
        if db_id is None:
            problems.append("datasource 'Baserow Postgres' not found "
                            "(database binding + MBQL fields unverifiable)")

        if not (_positions_table_id and _candidates_table_id):
            check(label, 3, False, "Baserow table ids unresolved")
            return
        pos_fields = _get_positions_fields()
        cand_fields = _get_candidates_fields()

        def fid(fields, name):
            f = _field_by_name(fields, name)
            return f["id"] if f else None

        stage_fid = fid(cand_fields, "Stage")
        days_fid = fid(cand_fields, "Days In Pipeline")
        team_fid = fid(pos_fields, "Team")
        level_fid = fid(pos_fields, "Level")
        approved_fid = fid(pos_fields, "Headcount Approved")
        if None in (stage_fid, days_fid, team_fid, level_fid, approved_fid):
            check(label, 3, False,
                  "Baserow field ids unresolved (schema incomplete)")
            return

        cand_tbl = f"database_table_{_candidates_table_id}"
        pos_tbl = f"database_table_{_positions_table_id}"

        _fmaps = {}

        def mb_fmap(phys):
            if phys not in _fmaps:
                if db_id is None:
                    return {}
                _, fmap = mb_resolve_table(db_id, phys)
                _fmaps[phys] = fmap
            return _fmaps[phys]

        def norm_sql(s: str) -> str:
            return re.sub(r"\s+", " ", (s or "").lower())

        shapes = {name: mb_shape(card) for name, card in cards.items()}

        for name, sh in shapes.items():
            if db_id is not None and sh["database"] != db_id:
                problems.append(
                    f"{name}: dataset_query.database={sh['database']} != "
                    f"Baserow Postgres id {db_id}")

        # Card 1: Funnel by Stage -- bar, count grouped by Stage.
        sh = shapes["Funnel by Stage"]
        if sh["display"] != "bar":
            problems.append(f"Funnel by Stage: display={sh['display']!r}, expected 'bar'")
        if sh["is_native"]:
            sql = norm_sql(sh["native_sql"])
            if not re.search(r"\bcount\s*\(", sql):
                problems.append("Funnel by Stage: native SQL lacks count()")
            if not _has_ident(sql, cand_tbl):
                problems.append(f"Funnel by Stage: native SQL missing {cand_tbl}")
            if "group by" not in sql or not _has_ident(sql, f"field_{stage_fid}"):
                problems.append(
                    f"Funnel by Stage: native SQL lacks group by on field_{stage_fid}")
        elif db_id is not None:
            stage_mb = mb_fmap(cand_tbl).get(f"field_{stage_fid}")
            if stage_mb is None:
                problems.append(
                    f"Funnel by Stage: {cand_tbl}.field_{stage_fid} not in "
                    f"Metabase metadata (sync incomplete)")
            else:
                if sh["agg_ops"] != {"count"}:
                    problems.append(
                        f"Funnel by Stage: aggregation {sh['agg_ops']} != count")
                if sh["breakout_fids"] != [stage_mb]:
                    problems.append(
                        f"Funnel by Stage: breakout {sh['breakout_fids']} != "
                        f"[Stage field {stage_mb}]")

        # Card 2: Average Days-In-Pipeline by Team -- bar, avg(Days) by Team via join.
        sh = shapes["Average Days-In-Pipeline by Team"]
        if sh["display"] != "bar":
            problems.append(
                f"Average Days-In-Pipeline by Team: display={sh['display']!r}, "
                f"expected 'bar'")
        if sh["is_native"]:
            sql = norm_sql(sh["native_sql"])
            if not re.search(r"\bavg\s*\(", sql) or not _has_ident(sql, f"field_{days_fid}"):
                problems.append(
                    f"Average Days-In-Pipeline by Team: native SQL lacks "
                    f"avg on field_{days_fid}")
            if not _has_ident(sql, cand_tbl) or not _has_ident(sql, pos_tbl):
                problems.append(
                    f"Average Days-In-Pipeline by Team: native SQL must reference "
                    f"both {cand_tbl} and {pos_tbl}")
            if "group by" not in sql or not _has_ident(sql, f"field_{team_fid}"):
                problems.append(
                    f"Average Days-In-Pipeline by Team: native SQL lacks "
                    f"group by on field_{team_fid}")
        elif db_id is not None:
            days_mb = mb_fmap(cand_tbl).get(f"field_{days_fid}")
            team_mb = mb_fmap(pos_tbl).get(f"field_{team_fid}")
            if days_mb is None or team_mb is None:
                problems.append(
                    "Average Days-In-Pipeline by Team: Days/Team fields not in "
                    "Metabase metadata (sync incomplete)")
            else:
                if sh["agg_ops"] != {("avg", days_mb)}:
                    problems.append(
                        f"Average Days-In-Pipeline by Team: aggregation "
                        f"{sh['agg_ops']} != avg(Days field {days_mb})")
                if sh["breakout_fids"] != [team_mb]:
                    problems.append(
                        f"Average Days-In-Pipeline by Team: breakout "
                        f"{sh['breakout_fids']} != [Team field {team_mb}]")

        # Card 3: Open Positions by Level -- pie, count by Level, Headcount Approved = true.
        sh = shapes["Open Positions by Level"]
        if sh["display"] != "pie":
            problems.append(
                f"Open Positions by Level: display={sh['display']!r}, expected 'pie'")
        if sh["is_native"]:
            sql = norm_sql(sh["native_sql"])
            if not re.search(r"\bcount\s*\(", sql):
                problems.append("Open Positions by Level: native SQL lacks count()")
            if not _has_ident(sql, pos_tbl):
                problems.append(f"Open Positions by Level: native SQL missing {pos_tbl}")
            if "group by" not in sql or not _has_ident(sql, f"field_{level_fid}"):
                problems.append(
                    f"Open Positions by Level: native SQL lacks group by on "
                    f"field_{level_fid}")
            approved_filtered = _has_ident(sql, f"field_{approved_fid}") and (
                "is true" in sql
                or re.search(rf"field_{approved_fid}\"?\s*=\s*true", sql) is not None
            )
            if not approved_filtered:
                problems.append(
                    f"Open Positions by Level: native SQL lacks "
                    f"field_{approved_fid} = true filter")
        elif db_id is not None:
            level_mb = mb_fmap(pos_tbl).get(f"field_{level_fid}")
            approved_mb = mb_fmap(pos_tbl).get(f"field_{approved_fid}")
            if level_mb is None or approved_mb is None:
                problems.append(
                    "Open Positions by Level: Level/Headcount Approved fields not "
                    "in Metabase metadata (sync incomplete)")
            else:
                if sh["agg_ops"] != {"count"}:
                    problems.append(
                        f"Open Positions by Level: aggregation {sh['agg_ops']} != count")
                if sh["breakout_fids"] != [level_mb]:
                    problems.append(
                        f"Open Positions by Level: breakout {sh['breakout_fids']} != "
                        f"[Level field {level_mb}]")
                fsum = mb_filter_summary(sh["filters"])
                ok_filter = any(
                    op == "=" and f == approved_mb
                    and any(v is True or str(v).lower() == "true" for v in vals)
                    for op, f, vals in fsum
                )
                if not ok_filter:
                    problems.append(
                        f"Open Positions by Level: no Headcount Approved=true "
                        f"filter (got {fsum})")

        check(label, 3, not problems,
              f"3 cards ok (db_id={db_id}, {cand_tbl}, {pos_tbl})"
              if not problems else "; ".join(problems))
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_8_metabase_dashboard_exists() -> None:
    """Dashboard 'Hiring Funnel Snapshot' exists with correct description."""
    try:
        coll_id = _find_collection_id("Hiring Analytics")
        if not coll_id:
            check("8. Metabase dashboard", 1, False, "collection not found")
            return

        items = metabase_get(f"collection/{coll_id}/items?models=dashboard")
        data = items.get("data", items) if isinstance(items, dict) else items
        dash = None
        for item in data:
            if item.get("name") == "Hiring Funnel Snapshot":
                dash = item
                break

        if not dash:
            check("8. Metabase dashboard", 1, False, "dashboard not found in collection")
            return

        dash_detail = metabase_get(f"dashboard/{dash['id']}")
        desc = dash_detail.get("description", "")
        expected_desc = "Hiring funnel snapshot as of 2026-03-15"
        check("8. Metabase dashboard", 1, desc == expected_desc,
              f"found, desc matches" if desc == expected_desc else f"desc={desc!r}")
    except Exception as e:
        check("8. Metabase dashboard", 1, False, f"exception: {e}")


def check_9_dashboard_cards() -> None:
    """Dashboard dashcards include the 3 verified question cards (bound by card id)."""
    try:
        cards = _mb_expected_cards()
        missing = EXPECTED_QUESTIONS - set(cards)
        if missing:
            check("9. Dashboard cards", 2, False,
                  f"question cards not resolved: {sorted(missing)}")
            return
        expected_ids = {c.get("id") for c in cards.values()} - {None}
        if len(expected_ids) != 3:
            check("9. Dashboard cards", 2, False,
                  f"only {len(expected_ids)}/3 question card ids resolved")
            return

        coll_id = _find_collection_id("Hiring Analytics")
        if not coll_id:
            check("9. Dashboard cards", 2, False, "collection not found")
            return

        items = metabase_get(f"collection/{coll_id}/items?models=dashboard")
        data = items.get("data", items) if isinstance(items, dict) else items
        dash_id = None
        for item in data:
            if item.get("name") == "Hiring Funnel Snapshot":
                dash_id = item["id"]
                break

        if not dash_id:
            check("9. Dashboard cards", 2, False, "dashboard not found")
            return

        dash_detail = metabase_get(f"dashboard/{dash_id}")
        dashcards = dash_detail.get("dashcards", dash_detail.get("ordered_cards", []))
        found_ids = set()
        for dc in dashcards:
            cid = dc.get("card_id") or (dc.get("card") or {}).get("id")
            if cid:
                found_ids.add(cid)

        ok = expected_ids <= found_ids
        check("9. Dashboard cards", 2, ok,
              f"dashcard ids {sorted(found_ids)} contain question ids "
              f"{sorted(expected_ids)}" if ok
              else f"missing card ids: {sorted(expected_ids - found_ids)}, "
                   f"found: {sorted(found_ids)}")
    except Exception as e:
        check("9. Dashboard cards", 2, False, f"exception: {e}")


def check_10_openproject_work_packages() -> None:
    """Exactly one Task WP per approved position in 'Internal Tools'; no extras."""
    try:
        # Find project ID
        proj_row = op_db_query("SELECT id FROM projects WHERE name = 'Internal Tools' LIMIT 1")
        if not proj_row:
            check("10. OpenProject work packages", 2, False, "project 'Internal Tools' not found")
            return
        proj_id = proj_row.strip()

        rows = op_db_query(
            f"SELECT wp.subject, COUNT(*) FROM work_packages wp "
            f"JOIN types t ON wp.type_id = t.id "
            f"WHERE wp.project_id = {proj_id} AND t.name = 'Task' "
            f"AND wp.subject LIKE 'Recruit: %' GROUP BY wp.subject"
        )
        counts = {}
        for line in rows.splitlines():
            if "|" not in line:
                continue
            subj, cnt = line.rsplit("|", 1)
            counts[subj.strip()] = int(cnt)

        expected_subjects = {
            f"Recruit: {p['role']} ({p['level']})" for p in APPROVED_POSITIONS
        }

        problems = []
        missing = expected_subjects - set(counts)
        extra = set(counts) - expected_subjects
        dupes = {s: c for s, c in counts.items()
                 if s in expected_subjects and c != 1}
        if missing:
            problems.append(f"missing: {sorted(missing)}")
        if extra:
            problems.append(f"extra Recruit tasks: {sorted(extra)}")
        if dupes:
            problems.append(f"duplicate subjects: {dupes}")

        check("10. OpenProject work packages", 2, not problems,
              "exactly one Task per approved position (4)" if not problems
              else "; ".join(problems))
    except Exception as e:
        check("10. OpenProject work packages", 2, False, f"exception: {e}")


def check_11_wp_details() -> None:
    """Work packages have correct assignee (Donald Wright), priority (Normal),
    and exact (whitespace-normalized) descriptions."""
    try:
        proj_row = op_db_query("SELECT id FROM projects WHERE name = 'Internal Tools' LIMIT 1")
        if not proj_row:
            check("11. WP assignee & description", 2, False, "project not found")
            return
        proj_id = proj_row.strip()

        expected_map = {}
        for p in APPROVED_POSITIONS:
            subj = f"Recruit: {p['role']} ({p['level']})"
            desc = f"Team: {p['team']}; Hiring Manager: {p['manager']}; Opened: {p['opened']}"
            expected_map[subj] = desc

        def norm_ws(s: str) -> str:
            return " ".join((s or "").split())

        rows = op_db_query(
            f"SELECT wp.id, wp.subject FROM work_packages wp "
            f"JOIN types t ON wp.type_id = t.id "
            f"WHERE wp.project_id = {proj_id} AND t.name = 'Task' "
            f"AND wp.subject LIKE 'Recruit: %'"
        )

        issues = []
        seen = set()
        for line in rows.splitlines():
            if "|" not in line:
                continue
            wp_id, subj = line.split("|", 1)
            wp_id = wp_id.strip()
            subj = subj.strip()
            if subj not in expected_map:
                continue  # extras are penalized by check 10
            if subj in seen:
                issues.append(f"{subj}: duplicate WP")
            seen.add(subj)

            # Per-id single-value queries (descriptions may span lines)
            assignee = op_db_query(
                f"SELECT COALESCE(u.firstname, '') || ' ' || COALESCE(u.lastname, '') "
                f"FROM work_packages wp LEFT JOIN users u ON wp.assigned_to_id = u.id "
                f"WHERE wp.id = {wp_id}"
            ).strip()
            if assignee != "Donald Wright":
                issues.append(f"{subj}: assignee={assignee!r}")

            priority = op_db_query(
                f"SELECT e.name FROM work_packages wp "
                f"JOIN enumerations e ON wp.priority_id = e.id "
                f"WHERE wp.id = {wp_id}"
            ).strip()
            if priority != "Normal":
                issues.append(f"{subj}: priority={priority!r} (expected Normal)")

            desc_text = op_db_query(
                f"SELECT COALESCE(wp.description, '') FROM work_packages wp "
                f"WHERE wp.id = {wp_id}"
            )
            if norm_ws(desc_text) != norm_ws(expected_map[subj]):
                issues.append(f"{subj}: desc mismatch, got {desc_text.strip()!r}")

        missing = set(expected_map) - seen
        if missing:
            issues.append(f"missing WPs: {sorted(missing)}")

        check("11. WP assignee & description", 2, not issues,
              "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("11. WP assignee & description", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_database()
    check_2_open_positions_table()
    check_2b_open_positions_schema()
    check_3_candidate_pipeline_table()
    check_3b_candidate_pipeline_schema()
    check_4_days_in_pipeline()
    check_5_gallery_view()
    check_6_metabase_collection()
    check_6b_metabase_datasource()
    check_7_metabase_questions()
    check_8_metabase_dashboard_exists()
    check_9_dashboard_cards()
    check_10_openproject_work_packages()
    check_11_wp_details()

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
