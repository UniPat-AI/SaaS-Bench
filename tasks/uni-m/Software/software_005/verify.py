"""
Verifier for Software-005-I2: Dependency Audit Across blog-engine and weather-dashboard Projects

Checks: 13 checks (check 1 is a 0-weight ground-truth bootstrap), total weight 23,
across code-server, baserow, metabase, openproject.
Strategy: docker exec (baserow DB, openproject DB) + REST API (metabase);
package.json ground truth is read in a throwaway container from the code-server
container's own pristine image, so agent edits to the live workspace cannot skew it.

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
import shlex
import time
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

_required = {
    "CODE_SERVER_CONTAINER": None,
    "BASEROW_DB_CONTAINER": None,
    "METABASE_PORT": None,
    "OPENPROJECT_CONTAINER": None,
}
for var in _required:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    _required[var] = val

CODE_SERVER_CONTAINER = _required["CODE_SERVER_CONTAINER"]
BASEROW_DB_CONTAINER = _required["BASEROW_DB_CONTAINER"]
METABASE_PORT = _required["METABASE_PORT"]
OPENPROJECT_CONTAINER = _required["OPENPROJECT_CONTAINER"]

METABASE_BASE = f"http://{HOST}:{METABASE_PORT}"

# Slot values
BASEROW_DB_NAME = "Frontend Dependency Audit 2026"
TABLE_NAME = "Dependency Inventory"
AUDIT_DATE = "2026-04-15"
STALE_MAJOR_THRESHOLD = 3
KNOWN_STALE_LIST = ["express", "ejs", "react"]
METABASE_COLLECTION = "Frontend Audit Insights"
DASHBOARD_NAME = "Frontend Dependency Health"
OP_PROJECT = "Marketing Website"
WP_SUBJECT = "Upgrade stale dependencies: 2026-04-15"

# psql field separator that cannot appear in text values (avoids '|' splitting bugs)
PSQL_SEP = "\x1f"

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


def baserow_sql(query: str, sep: str = "|") -> str:
    """Run a SQL query against the Baserow Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-F", sep, "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def openproject_sql(query: str) -> str:
    """Run a SQL query against the OpenProject embedded Postgres DB."""
    cmd = f"PGPASSWORD=openproject psql -h 127.0.0.1 -U openproject -d openproject -t -A -c {shlex.quote(query)}"
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "bash", "-c", cmd,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def metabase_session() -> str:
    """Get a Metabase session token."""
    r = requests.post(
        f"{METABASE_BASE}/api/session",
        json={"username": "admin@metabase.local", "password": "mw-admin-123"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["id"]


def metabase_get(path: str, token: str) -> dict:
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


# ── Ground truth: read package.json files from the pristine code-server image ─
def get_ground_truth_deps() -> dict[str, dict[str, str]]:
    """Returns {project_name: {dep_name: version_string}} read in a throwaway
    container from the code-server container's pristine image (never the live,
    agent-touched container)."""
    deps = {}
    for project in ("blog-engine", "weather-dashboard"):
        rc, out, err = image_exec(
            "cat", f"/home/coder/workspace/{project}/package.json",
            timeout=45,
        )
        if rc != 0:
            # Try workspace root
            rc, out, err = image_exec(
                "bash", "-c", f"find /home -name package.json -path '*{project}*' 2>/dev/null | head -1 | xargs cat",
                timeout=45,
            )
        if rc == 0 and out.strip():
            pkg = json.loads(out)
            project_deps = {}
            for section in ("dependencies",):
                if section in pkg:
                    project_deps.update(pkg[section])
            deps[project] = project_deps
        else:
            deps[project] = {}
    return deps


def is_stale(dep_name: str, version_str: str) -> bool:
    """Determine if a dependency is stale per the task rules."""
    if dep_name.lower() in [s.lower() for s in KNOWN_STALE_LIST]:
        return True
    # Extract major version from version string (strip ^, ~, etc.)
    clean = re.sub(r'^[^0-9]*', '', version_str)
    match = re.match(r'(\d+)', clean)
    if match:
        major = int(match.group(1))
        if major < STALE_MAJOR_THRESHOLD:
            return True
    return False


def truth_ok(ground_truth: dict[str, dict[str, str]]) -> bool:
    """Ground truth is usable only if both package.json files were read."""
    return bool(ground_truth.get("blog-engine")) and bool(ground_truth.get("weather-dashboard"))


def stale_truth(ground_truth: dict[str, dict[str, str]]) -> list[tuple[str, str, str]]:
    """Sorted [(project, dep, version)] of the stale dependencies per ground truth."""
    out = []
    for project, deps in sorted(ground_truth.items()):
        for dep_name, version in sorted(deps.items()):
            if is_stale(dep_name, version):
                out.append((project, dep_name, version))
    return out


_field_ids_cache: dict[int, dict[str, str]] = {}


def baserow_field_ids(table_id: int) -> dict[str, str]:
    """{field name: field id (str)} for a Baserow table, resolved via psql once and cached."""
    if table_id not in _field_ids_cache:
        raw = baserow_sql(
            f"SELECT f.name, f.id FROM database_field f "
            f"WHERE f.table_id = {table_id} AND f.trashed = false",
            sep=PSQL_SEP,
        )
        fmap = {}
        for line in raw.split('\n'):
            if PSQL_SEP in line:
                name, fid = line.split(PSQL_SEP, 1)
                fmap[name.strip()] = fid.strip()
        _field_ids_cache[table_id] = fmap
    return _field_ids_cache[table_id]


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_package_json_files() -> dict[str, dict[str, str]]:
    """Ground-truth bootstrap (0pt diagnostic): read package.json files from the
    pristine code-server image. On failure, all truth-dependent checks (4, 6, 13) FAIL."""
    try:
        deps = get_ground_truth_deps()
        has_both = truth_ok(deps)
        stale = stale_truth(deps)
        check("1. package.json ground truth readable", 0, has_both,
              f"blog-engine={len(deps.get('blog-engine', {}))} deps, "
              f"weather-dashboard={len(deps.get('weather-dashboard', {}))} deps, "
              f"stale={len(stale)}")
        return deps
    except Exception as e:
        check("1. package.json ground truth readable", 0, False, f"exception: {e}")
        return {}


def check_2_baserow_database_exists() -> int | None:
    """Check Baserow database 'Frontend Dependency Audit 2026' exists."""
    try:
        row = baserow_sql(
            f"SELECT a.id FROM core_application a "
            f"JOIN database_database d ON d.application_ptr_id = a.id "
            f"WHERE a.name = '{BASEROW_DB_NAME}'"
        )
        db_id = int(row.split('\n')[0]) if row else None
        check("2. Baserow database exists", 1, db_id is not None,
              f"db_id={db_id}" if db_id else "not found")
        return db_id
    except Exception as e:
        check("2. Baserow database exists", 1, False, f"exception: {e}")
        return None


def check_3_baserow_table_exists(db_id: int | None) -> int | None:
    """Check table 'Dependency Inventory' exists with required fields, types and primary flag."""
    try:
        if db_id is None:
            check("3. Baserow table with fields", 2, False, "no database found")
            return None
        row = baserow_sql(
            f"SELECT t.id FROM database_table t WHERE t.database_id = {db_id} AND t.name = '{TABLE_NAME}'"
        )
        table_id = int(row.split('\n')[0]) if row else None
        if table_id is None:
            check("3. Baserow table with fields", 2, False, "table not found")
            return None

        # Check fields exist with the correct type model and primary flag
        fields_raw = baserow_sql(
            f"SELECT f.name, ct.model, f.\"primary\" FROM database_field f "
            f"JOIN django_content_type ct ON ct.id = f.content_type_id "
            f"WHERE f.table_id = {table_id} AND f.trashed = false",
            sep=PSQL_SEP,
        )
        actual: dict[str, tuple[str, bool]] = {}
        for line in fields_raw.split('\n'):
            parts = line.split(PSQL_SEP)
            if len(parts) == 3:
                actual[parts[0].strip()] = (parts[1].strip(), parts[2].strip() == 't')
        expected_types = {
            "Project": "textfield",
            "Dependency Name": "textfield",
            "Current Version": "textfield",
            "Manifest File": "textfield",
            "Captured At": "datefield",
            "Stale": "booleanfield",
        }
        issues = []
        for name, model in expected_types.items():
            if name not in actual:
                issues.append(f"{name}: missing")
            elif actual[name][0] != model:
                issues.append(f"{name}: type={actual[name][0]} (expected {model})")
        if "Project" in actual and not actual["Project"][1]:
            issues.append("Project: not the primary field")
        check("3. Baserow table with fields", 2, not issues,
              "all fields present with correct types, Project primary" if not issues
              else "; ".join(issues))
        return table_id
    except Exception as e:
        check("3. Baserow table with fields", 2, False, f"exception: {e}")
        return None


def check_4_baserow_row_count(table_id: int | None, ground_truth: dict[str, dict[str, str]]) -> None:
    """Check row count matches total dependencies."""
    try:
        if not truth_ok(ground_truth):
            check("4. Correct row count", 2, False, "ground truth unavailable (package.json read failed)")
            return
        if table_id is None:
            check("4. Correct row count", 2, False, "no table found")
            return
        expected_count = sum(len(v) for v in ground_truth.values())
        actual = baserow_sql(f"SELECT count(*) FROM database_table_{table_id}")
        actual_count = int(actual) if actual else 0
        check("4. Correct row count", 2, actual_count == expected_count,
              f"expected={expected_count}, actual={actual_count}")
    except Exception as e:
        check("4. Correct row count", 2, False, f"exception: {e}")


def check_5_baserow_captured_at(table_id: int | None) -> None:
    """Check all rows have Captured At = 2026-04-15."""
    try:
        if table_id is None:
            check("5. Captured At dates", 2, False, "no table found")
            return

        # Find the Captured At field column
        field_info = baserow_sql(
            f"SELECT f.id FROM database_field f "
            f"WHERE f.table_id = {table_id} AND f.name = 'Captured At' AND f.trashed = false"
        )
        if not field_info:
            check("5. Captured At dates", 2, False, "Captured At field not found")
            return
        db_column = f"field_{field_info.strip()}"

        total = baserow_sql(f"SELECT count(*) FROM database_table_{table_id}")
        correct = baserow_sql(
            f"SELECT count(*) FROM database_table_{table_id} WHERE {db_column}::text LIKE '2026-04-15%'"
        )
        total_n = int(total)
        correct_n = int(correct)
        check("5. Captured At dates", 2, total_n > 0 and total_n == correct_n,
              f"{correct_n}/{total_n} rows have 2026-04-15")
    except Exception as e:
        check("5. Captured At dates", 2, False, f"exception: {e}")


def check_6_baserow_row_set(table_id: int | None, ground_truth: dict[str, dict[str, str]]) -> None:
    """Full row-set diff of the inventory table against package.json ground truth:
    key set (Project, Dependency Name) equal, per-row Current Version exact string,
    Stale == is_stale(truth version), Manifest File == '<project>/package.json'."""
    try:
        if not truth_ok(ground_truth):
            check("6. Row set matches ground truth", 4, False,
                  "ground truth unavailable (package.json read failed)")
            return
        if table_id is None:
            check("6. Row set matches ground truth", 4, False, "no table found")
            return

        field_map = baserow_field_ids(table_id)
        needed = ("Project", "Dependency Name", "Current Version", "Manifest File", "Stale")
        cols = {name: f"field_{field_map[name]}" for name in needed if name in field_map}
        if len(cols) < len(needed):
            missing_fields = [n for n in needed if n not in cols]
            check("6. Row set matches ground truth", 4, False, f"missing fields: {missing_fields}")
            return

        expected: dict[tuple[str, str], tuple[str, bool, str]] = {}
        for project, deps in ground_truth.items():
            for dep, ver in deps.items():
                expected[(project, dep)] = (ver, is_stale(dep, ver), f"{project}/package.json")

        rows_raw = baserow_sql(
            f"SELECT {cols['Project']}, {cols['Dependency Name']}, {cols['Current Version']}, "
            f"{cols['Manifest File']}, {cols['Stale']} FROM database_table_{table_id}",
            sep=PSQL_SEP,
        )
        actual: dict[tuple[str, str], list[tuple[str, bool, str]]] = {}
        malformed = 0
        for line in rows_raw.split('\n'):
            if not line.strip():
                continue
            parts = line.split(PSQL_SEP)
            if len(parts) != 5:
                malformed += 1
                continue
            proj, dep, ver, manifest, stale_raw = (p.strip() for p in parts)
            actual.setdefault((proj, dep), []).append(
                (ver, stale_raw.lower() in ('true', 't', '1', 'yes'), manifest)
            )

        problems = []
        if malformed:
            problems.append(f"{malformed} unparseable row(s)")
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        dupes = sorted(k for k, v in actual.items() if len(v) > 1)
        if missing:
            problems.append("missing rows: " + ", ".join(f"{p}/{d}" for p, d in missing[:5]))
        if extra:
            problems.append("extra rows: " + ", ".join(f"{p}/{d}" for p, d in extra[:5]))
        if dupes:
            problems.append("duplicate rows: " + ", ".join(f"{p}/{d}" for p, d in dupes[:5]))
        for key in sorted(set(expected) & set(actual)):
            exp_ver, exp_stale, exp_manifest = expected[key]
            act_ver, act_stale, act_manifest = actual[key][0]
            kname = f"{key[0]}/{key[1]}"
            if act_ver != exp_ver:
                problems.append(f"{kname}: Current Version expected '{exp_ver}' got '{act_ver}'")
            if act_stale != exp_stale:
                problems.append(f"{kname}: Stale expected {exp_stale} got {act_stale}")
            if act_manifest != exp_manifest:
                problems.append(f"{kname}: Manifest File expected '{exp_manifest}' got '{act_manifest}'")

        check("6. Row set matches ground truth", 4, not problems,
              f"all {len(expected)} rows match package.json truth" if not problems
              else "; ".join(problems)[:800])
    except Exception as e:
        check("6. Row set matches ground truth", 4, False, f"exception: {e}")


def check_7_metabase_collection(token: str) -> int | None:
    """Check Metabase collection 'Frontend Audit Insights' exists."""
    try:
        collections = metabase_get("collection", token)
        coll = None
        for c in collections:
            if c.get("name") == METABASE_COLLECTION:
                coll = c
                break
        check("7. Metabase collection exists", 1, coll is not None,
              f"id={coll['id']}" if coll else "not found")
        return coll["id"] if coll else None
    except Exception as e:
        check("7. Metabase collection exists", 1, False, f"exception: {e}")
        return None


def resolve_mb_target(token: str, table_id: int | None) -> tuple[dict | None, str]:
    """Resolve the Baserow Postgres datasource + synced inventory table in Metabase (once).
    Returns ({db_id, mb_table_id, fmap}, "") or (None, reason)."""
    if table_id is None:
        check_msg = f"Baserow table '{TABLE_NAME}' not found"
        return None, check_msg
    try:
        db = mb_find_pg_db(token)
        if db is None:
            return None, "no postgres datasource with dbname=baserow in Metabase"
        mb_tid, fmap = mb_resolve_table(token, db["id"], f"database_table_{table_id}")
        if mb_tid is None:
            return None, f"database_table_{table_id} not synced into Metabase db id={db['id']}"
        return {"db_id": db["id"], "mb_table_id": mb_tid, "fmap": fmap}, ""
    except Exception as e:
        return None, f"metabase resolution error: {e}"


def _check_chart(label: str, card_name: str, expected_display: str, breakout_field: str,
                 token: str, coll_id: int | None, table_id: int | None,
                 mb_res: dict | None, mb_err: str) -> int | None:
    """Shared semantic assertion for the bar/pie questions: display, MBQL (non-native),
    Baserow Postgres datasource, source-table, count aggregation, single breakout field."""
    try:
        if coll_id is None:
            check(label, 2, False, "no collection found")
            return None
        items = metabase_get(f"collection/{coll_id}/items", token)
        card = None
        for item in items.get("data", items) if isinstance(items, dict) else items:
            if item.get("name") == card_name and item.get("model") == "card":
                card = item
                break
        if card is None:
            check(label, 2, False, "question not found")
            return None
        card_detail = metabase_get(f"card/{card['id']}", token)
        shape = mb_shape(card_detail)

        issues = []
        if shape["display"] != expected_display:
            issues.append(f"display={shape['display']} (expected {expected_display})")
        if shape["is_native"]:
            issues.append(f"dataset_query is native SQL, MBQL question required: "
                          f"{str(shape['native_sql'])[:100]}")
        else:
            if mb_res is None:
                issues.append(f"cannot verify datasource/table/breakout: {mb_err}")
            else:
                if shape["database"] != mb_res["db_id"]:
                    issues.append(f"database={shape['database']} "
                                  f"(expected Baserow Postgres id={mb_res['db_id']})")
                if shape["source_table"] != mb_res["mb_table_id"]:
                    issues.append(f"source_table={shape['source_table']} "
                                  f"(expected {mb_res['mb_table_id']} = database_table_{table_id})")
                try:
                    br_fid = baserow_field_ids(table_id).get(breakout_field)
                except Exception as e:
                    br_fid = None
                    issues.append(f"cannot resolve Baserow '{breakout_field}' field id: {e}")
                else:
                    if br_fid is None:
                        issues.append(f"cannot resolve Baserow '{breakout_field}' field id")
                if br_fid is not None:
                    expected_fid = mb_res["fmap"].get(f"field_{br_fid}")
                    if expected_fid is None:
                        issues.append(f"field_{br_fid} ({breakout_field}) not in Metabase metadata")
                    elif shape["breakout_fids"] != [expected_fid]:
                        issues.append(f"breakout={shape['breakout_fids']} "
                                      f"(expected [{expected_fid}] = field_{br_fid} '{breakout_field}')")
            if shape["agg_ops"] != {"count"}:
                issues.append(f"aggregation={shape['agg_ops'] or 'none'} (expected count)")

        check(label, 2, not issues,
              f"{expected_display} chart: count by {breakout_field} on database_table_{table_id}"
              if not issues else "; ".join(issues)[:600])
        return card["id"]
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return None


def check_8_metabase_bar_chart(token: str, coll_id: int | None, table_id: int | None,
                               mb_res: dict | None, mb_err: str) -> int | None:
    """Check 'Dependencies by Project': bar chart, count broken out by Project."""
    return _check_chart("8. Bar chart question", "Dependencies by Project", "bar", "Project",
                        token, coll_id, table_id, mb_res, mb_err)


def check_9_metabase_pie_chart(token: str, coll_id: int | None, table_id: int | None,
                               mb_res: dict | None, mb_err: str) -> int | None:
    """Check 'Stale vs Current': pie chart, count broken out by Stale."""
    return _check_chart("9. Pie chart question", "Stale vs Current", "pie", "Stale",
                        token, coll_id, table_id, mb_res, mb_err)


def check_10_metabase_dashboard(token: str, coll_id: int | None, bar_id: int | None, pie_id: int | None) -> None:
    """Check dashboard 'Frontend Dependency Health' exists with both cards."""
    try:
        if coll_id is None:
            check("10. Metabase dashboard with cards", 2, False, "no collection found")
            return
        items = metabase_get(f"collection/{coll_id}/items", token)
        dash = None
        for item in items.get("data", items) if isinstance(items, dict) else items:
            if item.get("name") == DASHBOARD_NAME and item.get("model") == "dashboard":
                dash = item
                break
        if dash is None:
            check("10. Metabase dashboard with cards", 2, False, "dashboard not found")
            return

        # Fetch dashboard detail to check cards
        dash_detail = metabase_get(f"dashboard/{dash['id']}", token)
        card_ids_on_dash = set()
        for dc in dash_detail.get("dashcards", dash_detail.get("ordered_cards", [])):
            cid = dc.get("card_id") or (dc.get("card") or {}).get("id")
            if cid:
                card_ids_on_dash.add(cid)

        has_bar = bar_id is not None and bar_id in card_ids_on_dash
        has_pie = pie_id is not None and pie_id in card_ids_on_dash
        check("10. Metabase dashboard with cards", 2, has_bar and has_pie,
              f"bar={'yes' if has_bar else 'no'}, pie={'yes' if has_pie else 'no'}, cards_on_dash={card_ids_on_dash}")
    except Exception as e:
        check("10. Metabase dashboard with cards", 2, False, f"exception: {e}")


def check_11_openproject_wp_exists() -> int | None:
    """Check exactly one work package with the subject and Task type in Marketing Website."""
    try:
        where = (
            f"FROM work_packages wp "
            f"JOIN projects p ON p.id = wp.project_id "
            f"JOIN types t ON t.id = wp.type_id "
            f"WHERE p.name = '{OP_PROJECT}' "
            f"AND wp.subject = '{WP_SUBJECT}' "
            f"AND t.name = 'Task'"
        )
        count_raw = openproject_sql(f"SELECT count(*) {where}")
        count = int(count_raw.split('\n')[0]) if count_raw else 0
        if count != 1:
            check("11. OpenProject work package exists", 1, False,
                  f"expected exactly 1 work package, found {count}")
            return None
        row = openproject_sql(f"SELECT wp.id {where}")
        wp_id = int(row.split('\n')[0])
        check("11. OpenProject work package exists", 1, True, f"exactly 1, wp_id={wp_id}")
        return wp_id
    except Exception as e:
        check("11. OpenProject work package exists", 1, False, f"exception: {e}")
        return None


def check_12_openproject_priority(wp_id: int | None) -> None:
    """Check work package has priority High."""
    try:
        if wp_id is None:
            check("12. Work package priority High", 1, False, "no work package found")
            return
        row = openproject_sql(
            f"SELECT e.name FROM enumerations e "
            f"JOIN work_packages wp ON wp.priority_id = e.id "
            f"WHERE wp.id = {wp_id}"
        )
        priority = row.strip() if row else ""
        check("12. Work package priority High", 1, priority.lower() == "high",
              f"priority={priority}")
    except Exception as e:
        check("12. Work package priority High", 1, False, f"exception: {e}")


def check_13_openproject_description(wp_id: int | None, ground_truth: dict[str, dict[str, str]]) -> None:
    """Check WP description lists exactly the stale deps, one '<proj> / <dep> @ <ver>' line each,
    and no extra lines of that form."""
    try:
        if not truth_ok(ground_truth):
            check("13. WP description lists stale deps", 3, False,
                  "ground truth unavailable (package.json read failed)")
            return
        if wp_id is None:
            check("13. WP description lists stale deps", 3, False, "no work package found")
            return

        # Read description directly from work_packages table
        desc_raw = openproject_sql(
            f"SELECT wp.description FROM work_packages wp WHERE wp.id = {wp_id}"
        )
        description = desc_raw.strip() if desc_raw else ""

        stale_deps = stale_truth(ground_truth)
        problems = []
        for proj, dep, ver in stale_deps:
            pattern = rf"^\s*{re.escape(proj)}\s*/\s*{re.escape(dep)}\b\s*@\s*{re.escape(ver)}\s*$"
            if not re.search(pattern, description, re.MULTILINE):
                problems.append(f"no line matching '{proj} / {dep} @ {ver}'")
        line_count = len(re.findall(r"^\s*\S+\s*/\s*\S+\s*@\s*\S+\s*$", description, re.MULTILINE))
        if line_count != len(stale_deps):
            problems.append(f"{line_count} '<proj> / <dep> @ <ver>' line(s), "
                            f"expected exactly {len(stale_deps)}")

        check("13. WP description lists stale deps", 3, not problems,
              f"all {len(stale_deps)} stale deps listed, no extras" if not problems
              else "; ".join(problems)[:600])
    except Exception as e:
        check("13. WP description lists stale deps", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Step 1: Get ground truth from the pristine code-server image (0pt bootstrap; failure fails 4/6/13)
    ground_truth = check_1_package_json_files()

    # Steps 2-6: Baserow checks
    db_id = check_2_baserow_database_exists()
    table_id = check_3_baserow_table_exists(db_id)
    check_4_baserow_row_count(table_id, ground_truth)
    check_5_baserow_captured_at(table_id)
    check_6_baserow_row_set(table_id, ground_truth)

    # Steps 7-10: Metabase checks
    try:
        token = metabase_session()
    except Exception as e:
        print(f"FATAL: cannot get Metabase session: {e}", file=sys.stderr)
        check("7. Metabase collection exists", 1, False, f"auth failed: {e}")
        check("8. Bar chart question", 2, False, "auth failed")
        check("9. Pie chart question", 2, False, "auth failed")
        check("10. Metabase dashboard with cards", 2, False, "auth failed")
        token = None

    if token:
        coll_id = check_7_metabase_collection(token)
        mb_res, mb_err = resolve_mb_target(token, table_id)
        bar_id = check_8_metabase_bar_chart(token, coll_id, table_id, mb_res, mb_err)
        pie_id = check_9_metabase_pie_chart(token, coll_id, table_id, mb_res, mb_err)
        check_10_metabase_dashboard(token, coll_id, bar_id, pie_id)

    # Steps 11-13: OpenProject checks
    wp_id = check_11_openproject_wp_exists()
    check_12_openproject_priority(wp_id)
    check_13_openproject_description(wp_id, ground_truth)

    # Summary
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
