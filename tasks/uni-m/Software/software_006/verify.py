"""
Verifier for Software-006-I2: Build SLO Registry and Error-Budget Dashboard

Checks: 14 weighted checks (total weight 24) across baserow, metabase, openproject.
Strategy: Baserow API + Baserow Postgres (docker exec), Metabase API, OpenProject DB (docker exec).

Required env vars:
  SERVER_HOSTNAME, {BASEROW,CODE_SERVER,METABASE,OPENPROJECT}_PORT,
  {BASEROW,CODE_SERVER,METABASE,OPENPROJECT}_CONTAINER,
  BASEROW_DB_CONTAINER, OPENPROJECT uses embedded DB.
"""

import json
import os
import re
import subprocess
import sys
import time

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
METABASE_PORT = os.environ.get("METABASE_PORT")
METABASE_CONTAINER = os.environ.get("METABASE_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_required = {
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "METABASE_PORT": METABASE_PORT,
    "METABASE_CONTAINER": METABASE_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"
METABASE_BASE = f"http://{HOST}:{METABASE_PORT}"

# psql field separator that cannot appear in text values (avoids '|' splitting bugs)
PSQL_SEP = "\x1f"

# ── Expected data ─────────────────────────────────────────────────────────────
SERVICES = ["payments-gateway", "auth-service", "inventory-api"]
SLO_DATA = [
    ("Availability", 99.95, 99.88),
    ("Latency", 180.00, 165.40),
    ("ErrorRate", 0.50, 0.72),
]

EXPECTED_ROWS = []
for svc, (slo_type, target, current) in zip(SERVICES, SLO_DATA):
    if slo_type == "Availability":
        budget = round(target - current, 2)
    else:
        budget = round(current - target, 2)
    breaching = budget < 0
    EXPECTED_ROWS.append({
        "service": svc,
        "slo_type": slo_type,
        "target": target,
        "current": current,
        "budget": budget,
        "breaching": breaching,
    })
# payments-gateway: 0.07, false; auth-service: -14.60, true; inventory-api: 0.22, false

BREACHING_ROWS = [r for r in EXPECTED_ROWS if r["breaching"]]

REQUIRED_COLUMNS = ("Service", "SLO Type", "Target", "Current", "Budget Remaining")

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


def baserow_sql(query: str, sep: str = "|") -> str:
    """Run a SQL query against the Baserow Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc",
        "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-F", sep, "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def baserow_auth() -> str:
    """Get Baserow JWT token."""
    r = requests.post(
        f"{BASEROW_BASE}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["token"]


def baserow_get(path: str, token: str) -> dict:
    r = requests.get(
        f"{BASEROW_BASE}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def metabase_auth() -> str:
    """Get Metabase session token."""
    r = requests.post(
        f"{METABASE_BASE}/api/session",
        json={"username": "admin@metabase.local", "password": "mw-admin-123"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


def metabase_get(path: str, token: str) -> dict | list:
    r = requests.get(
        f"{METABASE_BASE}/api/{path}",
        headers={"X-Metabase-Session": token},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def metabase_post(path: str, token: str, payload=None, timeout: int = 60):
    r = requests.post(
        f"{METABASE_BASE}/api/{path}",
        headers={"X-Metabase-Session": token},
        json=payload if payload is not None else {},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def op_db_query(sql: str) -> str:
    """Query OpenProject embedded Postgres DB."""
    rc, stdout, stderr = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
        "-t", "-A", "-c", sql,
        timeout=15,
    )
    return stdout.strip()


# ── Metabase card normalization (Metabase v0.58 pMBQL + legacy MBQL) ──────────
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


# ── Baserow checks ───────────────────────────────────────────────────────────

def check_1_table_exists() -> dict | None:
    """Check that database 'Platform SLO Registry' with table 'Service SLOs' exists."""
    try:
        token = baserow_auth()
        apps = baserow_get("applications/", token)
        db_id = None
        for app in apps:
            if app.get("name") == "Platform SLO Registry" and app.get("type") == "database":
                db_id = app["id"]
                break
        if db_id is None:
            check("1. Baserow table exists", 1, False, "database 'Platform SLO Registry' not found")
            return None

        tables = baserow_get(f"database/tables/database/{db_id}/", token)
        table_id = None
        for t in tables:
            if t.get("name") == "Service SLOs":
                table_id = t["id"]
                break
        if table_id is None:
            check("1. Baserow table exists", 1, False, "table 'Service SLOs' not found in database")
            return None

        check("1. Baserow table exists", 1, True, f"db_id={db_id}, table_id={table_id}")
        return {"token": token, "table_id": table_id}
    except Exception as e:
        check("1. Baserow table exists", 1, False, f"exception: {e}")
        return None


def check_S_field_schema(ctx: dict | None) -> dict | None:
    """Check Service SLOs field schema: types, primary flag, select options, decimal places.
    Returns {field name: field dict} for downstream id resolution."""
    label = "S. Baserow field schema"
    if ctx is None:
        check(label, 2, False, "skipped: table not found")
        return None
    try:
        fields = baserow_get(f"database/fields/table/{ctx['table_id']}/", ctx["token"])
        by_name = {f.get("name"): f for f in fields}
        issues = []

        svc = by_name.get("Service")
        if svc is None:
            issues.append("Service: missing")
        else:
            if svc.get("type") != "text":
                issues.append(f"Service: type={svc.get('type')} (expected text)")
            if not svc.get("primary"):
                issues.append("Service: not the primary field")

        slo = by_name.get("SLO Type")
        if slo is None:
            issues.append("SLO Type: missing")
        else:
            if slo.get("type") != "single_select":
                issues.append(f"SLO Type: type={slo.get('type')} (expected single_select)")
            options = {o.get("value") for o in slo.get("select_options") or []}
            if options != {"Availability", "Latency", "ErrorRate"}:
                issues.append(f"SLO Type: options={sorted(options)} "
                              f"(expected Availability/ErrorRate/Latency)")

        for name in ("Target", "Current", "Budget Remaining"):
            fld = by_name.get(name)
            if fld is None:
                issues.append(f"{name}: missing")
            elif fld.get("type") != "number":
                issues.append(f"{name}: type={fld.get('type')} (expected number)")
            elif fld.get("number_decimal_places") != 2:
                issues.append(f"{name}: number_decimal_places={fld.get('number_decimal_places')} "
                              f"(expected 2)")

        br = by_name.get("Breaching")
        if br is None:
            issues.append("Breaching: missing")
        elif br.get("type") != "boolean":
            issues.append(f"Breaching: type={br.get('type')} (expected boolean)")

        check(label, 2, not issues,
              "all 6 fields have correct types/options" if not issues else "; ".join(issues))
        return by_name
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return None


def check_2_row_count(ctx: dict | None) -> list | None:
    """Check exactly 3 rows in Service SLOs."""
    if ctx is None:
        check("2. Exactly 3 rows", 1, False, "skipped: table not found")
        return None
    try:
        data = baserow_get(
            f"database/rows/table/{ctx['table_id']}/?user_field_names=true&size=100",
            ctx["token"],
        )
        rows = data.get("results", [])
        check("2. Exactly 3 rows", 1, len(rows) == 3, f"found {len(rows)} rows")
        return rows
    except Exception as e:
        check("2. Exactly 3 rows", 1, False, f"exception: {e}")
        return None


def _find_row(rows: list, service_name: str) -> dict | None:
    for r in rows:
        if r.get("Service") == service_name:
            return r
    return None


def _check_row(check_num: int, rows: list | None, expected: dict) -> None:
    """Check a single service row's values."""
    label = f"{check_num}. Row '{expected['service']}'"
    if rows is None:
        check(label, 2, False, "skipped: no rows")
        return
    try:
        row = _find_row(rows, expected["service"])
        if row is None:
            check(label, 2, False, f"row not found for service '{expected['service']}'")
            return

        # SLO Type: single-select field returns dict with "value" key
        slo_type_raw = row.get("SLO Type")
        if isinstance(slo_type_raw, dict):
            slo_type = slo_type_raw.get("value", "")
        else:
            slo_type = str(slo_type_raw or "")

        target = row.get("Target")
        current = row.get("Current")
        budget = row.get("Budget Remaining")
        breaching = row.get("Breaching")

        issues = []
        if slo_type != expected["slo_type"]:
            issues.append(f"SLO Type: expected '{expected['slo_type']}', got '{slo_type}'")

        def approx(a, b, tol=0.005):
            try:
                return abs(float(a) - float(b)) < tol
            except (TypeError, ValueError):
                return False

        if not approx(target, expected["target"]):
            issues.append(f"Target: expected {expected['target']}, got {target}")
        if not approx(current, expected["current"]):
            issues.append(f"Current: expected {expected['current']}, got {current}")
        if not approx(budget, expected["budget"]):
            issues.append(f"Budget: expected {expected['budget']}, got {budget}")
        if not isinstance(breaching, bool):
            issues.append(f"Breaching: expected {expected['breaching']}, "
                          f"got {breaching!r} (not a boolean)")
        elif breaching != expected["breaching"]:
            issues.append(f"Breaching: expected {expected['breaching']}, got {breaching}")

        if issues:
            check(label, 2, False, "; ".join(issues))
        else:
            check(label, 2, True, f"all fields correct")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_row_payments(rows: list | None) -> None:
    _check_row(3, rows, EXPECTED_ROWS[0])


def check_4_row_auth(rows: list | None) -> None:
    _check_row(4, rows, EXPECTED_ROWS[1])


def check_5_row_inventory(rows: list | None) -> None:
    _check_row(5, rows, EXPECTED_ROWS[2])


# ── Metabase checks ──────────────────────────────────────────────────────────

def check_D_metabase_datasource(ctx: dict | None) -> dict | None:
    """Check the 'Baserow Postgres' datasource exists (engine postgres, dbname baserow)
    and the Service SLOs table is visible in its metadata (schema sync done)."""
    label = "D. Metabase datasource + sync"
    if ctx is None:
        check(label, 2, False, "skipped: Baserow table not found (nothing to sync-check)")
        return None
    try:
        token = metabase_auth()
        db = mb_find_pg_db(token, name="Baserow Postgres")
        if db is None:
            check(label, 2, False,
                  "no postgres datasource named 'Baserow Postgres' with dbname=baserow")
            return None
        physical = f"database_table_{ctx['table_id']}"
        mb_table_id, fmap = mb_resolve_table(token, db["id"], physical)
        if mb_table_id is None:
            check(label, 2, False,
                  f"datasource id={db['id']} found but {physical} not in metadata (schema not synced)")
            return None
        check(label, 2, True, f"db_id={db['id']}, {physical} synced as table {mb_table_id}")
        return {"db_id": db["id"], "mb_table_id": mb_table_id, "fmap": fmap}
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return None


def check_7_metabase_collection() -> int | None:
    """Check collection 'Platform Reliability' exists in Metabase."""
    try:
        token = metabase_auth()
        collections = metabase_get("collection", token)
        coll_id = None
        for c in collections:
            if c.get("name") == "Platform Reliability":
                coll_id = c["id"]
                break
        if coll_id is None:
            check("7. Metabase collection exists", 1, False, "'Platform Reliability' not found")
            return None
        check("7. Metabase collection exists", 1, True, f"id={coll_id}")
        return coll_id
    except Exception as e:
        check("7. Metabase collection exists", 1, False, f"exception: {e}")
        return None


def _metabase_collection_items(coll_id: int, token: str) -> list:
    """Get items in a Metabase collection."""
    data = metabase_get(f"collection/{coll_id}/items", token)
    if isinstance(data, dict):
        return data.get("data", [])
    return data


def _slo_option_map(br_fields: dict) -> dict[str, str]:
    """{option id (str): option text} for the SLO Type single-select field, via psql.
    Executed card rows carry the raw physical value, which is the option id."""
    slo_fid = (br_fields.get("SLO Type") or {}).get("id")
    if not slo_fid:
        return {}
    raw = baserow_sql(
        f"SELECT id, value FROM database_selectoption WHERE field_id = {slo_fid}",
        sep=PSQL_SEP,
    )
    out = {}
    for line in raw.split("\n"):
        if PSQL_SEP in line:
            oid, val = line.split(PSQL_SEP, 1)
            out[oid.strip()] = val.strip()
    return out


def _compare_executed_rows(cols: list, rows: list, br_fields: dict | None) -> list[str]:
    """Compare executed card output against EXPECTED_ROWS, aligned by Service. Returns issues."""
    if len(rows) != 3:
        return [f"execution returned {len(rows)} row(s), expected exactly 3"]
    if br_fields is None:
        return ["cannot align executed rows: Baserow field ids unavailable (see check S)"]
    col_idx = {}
    for name in REQUIRED_COLUMNS:
        fid = (br_fields.get(name) or {}).get("id")
        phys = f"field_{fid}" if fid else None
        if phys is None or phys not in cols:
            return [f"executed result missing column for '{name}' "
                    f"(expected physical col {phys}, cols={cols})"]
        col_idx[name] = cols.index(phys)

    try:
        option_map = _slo_option_map(br_fields)
    except Exception as e:
        return [f"cannot resolve SLO Type option ids: {e}"]

    issues = []
    actual_by_service = {str(row[col_idx["Service"]]): row for row in rows}
    for exp in EXPECTED_ROWS:
        row = actual_by_service.get(exp["service"])
        if row is None:
            issues.append(f"executed rows missing service '{exp['service']}' "
                          f"(got {sorted(actual_by_service)})")
            continue
        slo_raw = row[col_idx["SLO Type"]]
        slo_val = option_map.get(str(slo_raw), str(slo_raw) if slo_raw is not None else "")
        if slo_val != exp["slo_type"]:
            issues.append(f"{exp['service']}: SLO Type expected '{exp['slo_type']}', got '{slo_val}'")
        for key, col in (("target", "Target"), ("current", "Current"), ("budget", "Budget Remaining")):
            raw = row[col_idx[col]]
            try:
                ok = abs(float(raw) - exp[key]) < 0.005
            except (TypeError, ValueError):
                ok = False
            if not ok:
                issues.append(f"{exp['service']}: {col} expected {exp[key]}, got {raw}")
    return issues


def check_8_metabase_question(coll_id: int | None, br_fields: dict | None, mb: dict | None) -> int | None:
    """Check saved question 'Platform SLO Targets vs Current': table display, MBQL over the
    synced Baserow table (no aggregation/breakout, required columns), and server-side
    execution returning exactly the 3 expected rows."""
    label = "8. Metabase saved question"
    if coll_id is None:
        check(label, 3, False, "skipped: collection not found")
        return None
    try:
        token = metabase_auth()
        items = _metabase_collection_items(coll_id, token)
        question_id = None
        for item in items:
            if item.get("model") in ("card", "question") and item.get("name") == "Platform SLO Targets vs Current":
                question_id = item["id"]
                break
        if question_id is None:
            # Also search all cards
            cards = metabase_get("card", token)
            for card in cards:
                if card.get("name") == "Platform SLO Targets vs Current" and card.get("collection_id") == coll_id:
                    question_id = card["id"]
                    break
        if question_id is None:
            check(label, 3, False, "question not found in collection")
            return None

        issues = []
        card_detail = metabase_get(f"card/{question_id}", token)
        shape = mb_shape(card_detail)
        if shape["display"] != "table":
            issues.append(f"display={shape['display']} (expected table)")
        if shape["is_native"]:
            issues.append(f"dataset_query is native SQL, MBQL table question required: "
                          f"{str(shape['native_sql'])[:100]}")
        else:
            if mb is None:
                issues.append("cannot verify datasource/table: "
                              "'Baserow Postgres' not resolved (see check D)")
            else:
                if shape["database"] != mb["db_id"]:
                    issues.append(f"database={shape['database']} "
                                  f"(expected Baserow Postgres id={mb['db_id']})")
                if shape["source_table"] != mb["mb_table_id"]:
                    issues.append(f"source_table={shape['source_table']} "
                                  f"(expected {mb['mb_table_id']})")
            if shape["agg_ops"]:
                issues.append(f"unexpected aggregation: {shape['agg_ops']}")
            if shape["breakout_fids"]:
                issues.append(f"unexpected breakout: {shape['breakout_fids']}")
            if shape["fields_fids"]:
                if br_fields is None or mb is None:
                    issues.append("fields clause present but required column ids "
                                  "could not be resolved")
                else:
                    unresolved = []
                    required_fids = set()
                    for name in REQUIRED_COLUMNS:
                        br_fid = (br_fields.get(name) or {}).get("id")
                        mb_fid = mb["fmap"].get(f"field_{br_fid}") if br_fid else None
                        if mb_fid is None:
                            unresolved.append(name)
                        else:
                            required_fids.add(mb_fid)
                    present_fids = {f for f in shape["fields_fids"] if f is not None}
                    if unresolved:
                        issues.append(f"cannot resolve Metabase field ids for: {unresolved}")
                    elif not required_fids <= present_fids:
                        issues.append(f"fields clause {sorted(present_fids)} missing required "
                                      f"columns {sorted(required_fids - present_fids)}")

        # Execution-level comparison: the card must read the correct rows from Baserow Postgres
        try:
            cols, rows = mb_run_card(question_id, token)
            issues.extend(_compare_executed_rows(cols, rows, br_fields))
        except Exception as e:
            issues.append(f"card execution failed: {e}")

        check(label, 3, not issues,
              f"card_id={question_id}: table question over Baserow table, "
              f"execution matches the 3 expected rows"
              if not issues else "; ".join(issues)[:700])
        return question_id
    except Exception as e:
        check(label, 3, False, f"exception: {e}")
        return None


def check_9_metabase_dashboard(coll_id: int | None) -> int | None:
    """Check dashboard 'Platform Error Budget Tracker' exists in collection."""
    if coll_id is None:
        check("9. Metabase dashboard", 2, False, "skipped: collection not found")
        return None
    try:
        token = metabase_auth()
        items = _metabase_collection_items(coll_id, token)
        dash_id = None
        for item in items:
            if item.get("model") == "dashboard" and item.get("name") == "Platform Error Budget Tracker":
                dash_id = item["id"]
                break
        if dash_id is None:
            # Search all dashboards
            dashboards = metabase_get("dashboard", token)
            for d in dashboards:
                if d.get("name") == "Platform Error Budget Tracker" and d.get("collection_id") == coll_id:
                    dash_id = d["id"]
                    break
        if dash_id is None:
            check("9. Metabase dashboard", 2, False, "dashboard not found in collection")
            return None
        check("9. Metabase dashboard", 2, True, f"dash_id={dash_id}")
        return dash_id
    except Exception as e:
        check("9. Metabase dashboard", 2, False, f"exception: {e}")
        return None


def check_10_dashboard_has_card(dash_id: int | None, question_id: int | None) -> None:
    """Check dashboard contains the saved question as a card (matched by card_id)."""
    if dash_id is None:
        check("10. Dashboard contains question card", 1, False, "skipped: dashboard not found")
        return
    if question_id is None:
        check("10. Dashboard contains question card", 1, False,
              "question card not found (see check 8)")
        return
    try:
        token = metabase_auth()
        dash = metabase_get(f"dashboard/{dash_id}", token)
        cards = dash.get("dashcards", dash.get("ordered_cards", []))
        if not cards:
            check("10. Dashboard contains question card", 1, False, "no cards on dashboard")
            return
        found = any(c.get("card_id") == question_id or (c.get("card") or {}).get("id") == question_id for c in cards)
        if found:
            check("10. Dashboard contains question card", 1, True, "question card found on dashboard")
        else:
            check("10. Dashboard contains question card", 1, False,
                  f"question card_id={question_id} not among dashboard cards")
    except Exception as e:
        check("10. Dashboard contains question card", 1, False, f"exception: {e}")


# ── OpenProject checks ───────────────────────────────────────────────────────

def check_11_op_bug_exists() -> None:
    """Check exactly one Bug WP 'SLO breach: auth-service (Latency)' exists in
    'Infrastructure Upgrade' and its priority is High (LEFT JOIN so an unset
    priority reads 'priority not set', not 'not found')."""
    label = "11. OpenProject bug WP with High priority"
    try:
        where = """
            FROM work_packages wp
            JOIN projects p ON wp.project_id = p.id
            JOIN types t ON wp.type_id = t.id
            WHERE p.name = 'Infrastructure Upgrade'
              AND wp.subject = 'SLO breach: auth-service (Latency)'
              AND t.name = 'Bug'
        """
        count_raw = op_db_query(f"SELECT count(*) {where}")
        count = int(count_raw) if count_raw else 0
        if count != 1:
            check(label, 2, False, f"expected exactly 1 bug work package, found {count}")
            return
        priority = op_db_query(f"""
            SELECT COALESCE(e.name, '')
            FROM work_packages wp
            JOIN projects p ON wp.project_id = p.id
            JOIN types t ON wp.type_id = t.id
            LEFT JOIN enumerations e ON e.id = wp.priority_id
            WHERE p.name = 'Infrastructure Upgrade'
              AND wp.subject = 'SLO breach: auth-service (Latency)'
              AND t.name = 'Bug'
        """).strip()
        if not priority:
            check(label, 2, False, "work package exists but priority not set")
            return
        check(label, 2, priority == "High", f"priority={priority}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_op_bug_description() -> None:
    """Check bug work package description contains the metric values in Key=Value form."""
    try:
        sql = """
            SELECT wp.description
            FROM work_packages wp
            JOIN projects p ON wp.project_id = p.id
            JOIN types t ON wp.type_id = t.id
            WHERE p.name = 'Infrastructure Upgrade'
              AND wp.subject = 'SLO breach: auth-service (Latency)'
              AND t.name = 'Bug'
        """
        result = op_db_query(sql)
        if not result:
            check("12. Bug WP description", 2, False, "work package not found")
            return

        desc = result.strip()
        issues = []
        # Key=Value format with numeric boundary (rejects e.g. -14.65 for -14.60)
        if not re.search(r"Current\s*=\s*165\.40?(?!\d)", desc):
            issues.append("missing 'Current=165.40'")
        if not re.search(r"Target\s*=\s*180\.00?(?!\d)", desc):
            issues.append("missing 'Target=180.00'")
        if not re.search(r"Budget Remaining\s*=\s*-14\.60?(?!\d)", desc):
            issues.append("missing 'Budget Remaining=-14.60'")

        check("12. Bug WP description", 2, not issues,
              "all values present" if not issues else "; ".join(issues))
    except Exception as e:
        check("12. Bug WP description", 2, False, f"exception: {e}")


def check_13_op_no_extra_bugs() -> None:
    """Check no extra SLO breach bug work packages beyond expected ones."""
    try:
        sql = """
            SELECT wp.subject
            FROM work_packages wp
            JOIN projects p ON wp.project_id = p.id
            JOIN types t ON wp.type_id = t.id
            WHERE p.name = 'Infrastructure Upgrade'
              AND t.name = 'Bug'
              AND wp.subject LIKE 'SLO breach:%'
        """
        result = op_db_query(sql)
        rows = [r.strip() for r in result.strip().split("\n") if r.strip()] if result.strip() else []
        expected_count = len(BREACHING_ROWS)  # 1
        check("13. No extra SLO breach bugs", 1, len(rows) == expected_count,
              f"expected {expected_count} SLO breach bug(s), found {len(rows)}")
    except Exception as e:
        check("13. No extra SLO breach bugs", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Baserow
    ctx = check_1_table_exists()
    br_fields = check_S_field_schema(ctx)
    rows = check_2_row_count(ctx)
    check_3_row_payments(rows)
    check_4_row_auth(rows)
    check_5_row_inventory(rows)

    # Metabase
    mb = check_D_metabase_datasource(ctx)
    coll_id = check_7_metabase_collection()
    question_id = check_8_metabase_question(coll_id, br_fields, mb)
    dash_id = check_9_metabase_dashboard(coll_id)
    check_10_dashboard_has_card(dash_id, question_id)

    # OpenProject
    check_11_op_bug_exists()
    check_12_op_bug_description()
    check_13_op_no_extra_bugs()

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
