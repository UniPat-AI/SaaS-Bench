"""
Verifier for Software-025-I1: Cross-project lint audit with Baserow import,
Metabase dashboard, and OpenProject remediation tasks.

Checks: 15 weighted checks (total weight 31) across code-server, baserow, metabase,
openproject.
Strategy: the lint ground truth is recomputed at verify time by re-running flake8
          (todo-api, data-analyzer) and — only when a config exists — eslint
          (blog-engine, weather-dashboard) in a throwaway container from the
          code-server container's own pristine image (docker inspect → docker run;
          computed once, cached; recompute failure fails every dependent check).
          docker exec (code-server filesystem, baserow DB, openproject DB),
          REST API (baserow views/fields, metabase incl. card execution).

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import csv
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def require_env(name: str) -> str:
    val = os.getenv(name, "")
    if not val:
        print(f"FATAL: {name} not set", file=sys.stderr)
        sys.exit(1)
    return val


CODE_SERVER_CONTAINER = require_env("CODE_SERVER_CONTAINER")
CODE_SERVER_PORT = require_env("CODE_SERVER_PORT")

BASEROW_PORT = require_env("BASEROW_PORT")
BASEROW_CONTAINER = require_env("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = require_env("BASEROW_DB_CONTAINER")

METABASE_PORT = require_env("METABASE_PORT")
METABASE_CONTAINER = require_env("METABASE_CONTAINER")

OPENPROJECT_PORT = require_env("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = require_env("OPENPROJECT_CONTAINER")

WORKSPACE = "/home/coder/workspace"
PY_PROJECTS = ["todo-api", "data-analyzer"]
JS_PROJECTS = ["blog-engine", "weather-dashboard"]
ALL_PROJECTS = PY_PROJECTS + JS_PROJECTS

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
    """Query Baserow's Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER, "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-c", sql,
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def baserow_db_query_sep(sql: str) -> str:
    """Query Baserow's Postgres DB with a \\x1f field separator (safe for text
    containing '|')."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER, "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-F", "\x1f", "-c", sql,
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def openproject_db_query(sql: str) -> str:
    """Query OpenProject's embedded Postgres DB."""
    escaped = shlex.quote(sql)
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER, "bash", "-c",
        f"PGPASSWORD=openproject psql -h localhost -U openproject -d openproject -t -A -c {escaped}",
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


_metabase_token: str | None = None


def get_metabase_token() -> str:
    global _metabase_token
    if _metabase_token:
        return _metabase_token
    base = f"http://{HOST}:{METABASE_PORT}"
    r = requests.post(
        f"{base}/api/session",
        json={"username": "admin@metabase.local", "password": "mw-admin-123"},
        timeout=10,
    )
    r.raise_for_status()
    _metabase_token = r.json()["id"]
    return _metabase_token


def metabase_get(path: str):
    base = f"http://{HOST}:{METABASE_PORT}"
    token = get_metabase_token()
    r = requests.get(
        f"{base}{path}", headers={"X-Metabase-Session": token}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def metabase_post(path: str, payload=None, timeout: int = 60):
    base = f"http://{HOST}:{METABASE_PORT}"
    token = get_metabase_token()
    r = requests.post(
        f"{base}{path}",
        headers={"X-Metabase-Session": token},
        json=payload if payload is not None else {},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


_baserow_token: str | None = None


def get_baserow_token() -> str:
    global _baserow_token
    if _baserow_token:
        return _baserow_token
    base = f"http://{HOST}:{BASEROW_PORT}"
    r = requests.post(
        f"{base}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=10,
    )
    r.raise_for_status()
    _baserow_token = r.json()["token"]
    return _baserow_token


def baserow_api_get(path: str):
    base = f"http://{HOST}:{BASEROW_PORT}"
    token = get_baserow_token()
    r = requests.get(
        f"{base}{path}", headers={"Authorization": f"JWT {token}"}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def _get_baserow_table_id() -> str | None:
    """Return the Baserow internal table id for 'Lint Violations'."""
    result = baserow_db_query(
        "SELECT dt.id FROM database_table dt "
        "JOIN core_application ca ON dt.database_id = ca.id "
        "WHERE ca.name = 'Code Quality Audit Q2 2025' "
        "AND dt.name = 'Lint Violations'"
    )
    return result.split("\n")[0].strip() if result else None


_field_ids_cache: dict | None = None


def _get_field_ids() -> dict:
    """{field name: field id} for the Lint Violations table via psql (cached)."""
    global _field_ids_cache
    if _field_ids_cache is not None:
        return _field_ids_cache
    ids: dict = {}
    table_id = _get_baserow_table_id()
    if table_id:
        out = baserow_db_query_sep(
            f"SELECT df.id, df.name FROM database_field df "
            f"WHERE df.table_id = {table_id} AND df.trashed = false"
        )
        for line in out.split("\n"):
            parts = line.split("\x1f")
            if len(parts) == 2 and parts[0].strip().isdigit():
                ids[parts[1]] = int(parts[0])
    _field_ids_cache = ids
    return ids


def _select_option_map(field_id: int) -> dict[str, str]:
    """{option id (as text): option value} for a single-select field."""
    out = baserow_db_query_sep(
        f"SELECT id, value FROM database_selectoption WHERE field_id = {field_id}"
    )
    opts: dict[str, str] = {}
    for line in out.split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 2:
            opts[parts[0].strip()] = parts[1]
    return opts


def _norm_path(path: str, project: str) -> str:
    """Normalize a file path: strip './' prefix and an optional '<project>/' prefix."""
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    if project and p.startswith(project + "/"):
        p = p[len(project) + 1:]
    return p


def _norm_path_any(path: str) -> str:
    """Normalize a path whose project is unknown (subjects): strip './' and any
    known '<project>/' prefix."""
    p = path.strip()
    if p.startswith("./"):
        p = p[2:]
    for proj in ALL_PROJECTS:
        if p.startswith(proj + "/"):
            return p[len(proj) + 1:]
    return p


# ── Ground truth: lint recompute (once, cached) ───────────────────────────────
_lint_truth: dict | None = None
_lint_truth_done = False
_lint_truth_error = ""

_FLAKE8_RE = re.compile(r"^(?P<path>.+?):(?P<row>\d+):(?P<col>\d+): (?P<code>[EWFC]\d+)")


def _severity_for_code(code: str) -> str:
    """flake8 severity map: E*/F*/C* -> Error, W* -> Warning."""
    return "Warning" if code.startswith("W") else "Error"


def get_lint_truth() -> dict | None:
    """Recompute the cross-project lint ground truth ONCE in a throwaway
    container from the code-server container's own PRISTINE image (so agent
    edits to the live source tree cannot move the goalposts). flake8 for the
    two Python projects; eslint for the two JS projects ONLY when a config
    file exists (no config -> 0 violations, deterministic and offline — npx
    is never invoked). Returns None on failure; every dependent check must
    then FAIL (never trust agent-entered data)."""
    global _lint_truth, _lint_truth_done, _lint_truth_error
    if _lint_truth_done:
        return _lint_truth
    _lint_truth_done = True
    violations: list[tuple[str, str, str, str]] = []
    per_project: dict[str, int] = {}
    try:
        for project in PY_PROJECTS:
            rc, out, err = image_exec(
                "bash", "-lc",
                f"cd {WORKSPACE}/{project} && flake8 .",
                timeout=240,
            )
            if rc not in (0, 1):
                _lint_truth_error = f"flake8 failed in {project}: rc={rc}, {err.strip()[:120]}"
                return None
            n = 0
            for line in out.split("\n"):
                m = _FLAKE8_RE.match(line.strip())
                if not m:
                    continue
                relpath = _norm_path(m.group("path"), project)
                code = m.group("code")
                violations.append((project, relpath, code, _severity_for_code(code)))
                n += 1
            if rc == 1 and n == 0:
                _lint_truth_error = f"flake8 rc=1 in {project} but no parseable violations"
                return None
            per_project[project] = n
        for project in JS_PROJECTS:
            rc, out, err = image_exec(
                "bash", "-lc",
                f"cd {WORKSPACE}/{project} && ls eslint.config.* .eslintrc* 2>/dev/null",
                timeout=90,
            )
            if not out.strip():
                # No eslint config -> this project contributes 0 violations.
                per_project[project] = 0
                continue
            rc, out, err = image_exec(
                "bash", "-lc",
                f"cd {WORKSPACE}/{project} && npx eslint . -f json",
                timeout=180,
            )
            try:
                reports = json.loads(out.strip() or "[]")
            except json.JSONDecodeError:
                if "couldn't find a configuration" in (out + err).lower():
                    per_project[project] = 0
                    continue
                _lint_truth_error = (
                    f"eslint output unparseable in {project}: {(out + err).strip()[:120]}")
                return None
            n = 0
            prefix = f"{WORKSPACE}/{project}/"
            for rep in reports:
                fpath = rep.get("filePath", "")
                relpath = fpath[len(prefix):] if fpath.startswith(prefix) else fpath.lstrip("/")
                for msg in rep.get("messages", []):
                    code = str(msg.get("ruleId") or "unknown")
                    severity = "Error" if msg.get("severity") == 2 else "Warning"
                    violations.append((project, relpath, code, severity))
                    n += 1
            per_project[project] = n
    except Exception as e:
        _lint_truth_error = f"lint recompute failed: {e}"
        return None
    if not violations:
        _lint_truth_error = "lint recompute produced zero violations"
        return None
    file_error_counts: dict[tuple[str, str], int] = {}
    file_rule_counts: dict[tuple[str, str], dict[str, int]] = {}
    rule_totals: dict[str, int] = {}
    for project, relpath, code, severity in violations:
        rule_totals[code] = rule_totals.get(code, 0) + 1
        key = (project, relpath)
        file_rule_counts.setdefault(key, {})
        file_rule_counts[key][code] = file_rule_counts[key].get(code, 0) + 1
        if severity == "Error":
            file_error_counts[key] = file_error_counts.get(key, 0) + 1
    top5 = sorted(file_error_counts.items(), key=lambda kv: (-kv[1], kv[0][1], kv[0][0]))[:5]
    _lint_truth = {
        "violations": violations,
        "total": len(violations),
        "file_error_counts": file_error_counts,
        "file_rule_counts": file_rule_counts,
        "rule_totals": rule_totals,
        "top5": [(proj, relpath, n) for (proj, relpath), n in top5],
        "summary": (f"total={len(violations)} ("
                    + ", ".join(f"{p}={per_project[p]}" for p in ALL_PROJECTS) + ")"),
    }
    return _lint_truth


# ── CSV location + parse (pinned once, cached) ────────────────────────────────
_csv_paths: list[str] | None = None
_csv_paths_done = False


def get_csv_paths() -> list[str]:
    """All lint_violations.csv hits under /home, sorted (pinned once).
    The FIRST path is used by every CSV-reading check."""
    global _csv_paths, _csv_paths_done
    if _csv_paths_done:
        return _csv_paths or []
    _csv_paths_done = True
    rc, out, err = docker_exec(
        CODE_SERVER_CONTAINER, "bash", "-c",
        "find /home -name 'lint_violations.csv' -type f 2>/dev/null | sort",
        timeout=30,
    )
    _csv_paths = [l.strip() for l in out.split("\n") if l.strip()]
    return _csv_paths


_csv_rows_cache: list | None = None
_csv_rows_done = False
_csv_error = ""


def get_csv_rows() -> list | None:
    """Parsed CSV rows [(project, normalized path, rule, severity)] from the
    pinned CSV path, via the csv module. None on failure."""
    global _csv_rows_cache, _csv_rows_done, _csv_error
    if _csv_rows_done:
        return _csv_rows_cache
    _csv_rows_done = True
    paths = get_csv_paths()
    if not paths:
        _csv_error = "lint_violations.csv not found"
        return None
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", paths[0], timeout=30)
        if rc != 0:
            _csv_error = f"cat {paths[0]} failed: {err.strip()[:80]}"
            return None
        rows = list(csv.reader(io.StringIO(out)))
        if not rows:
            _csv_error = "CSV is empty"
            return None
        idx = {}
        for i, name in enumerate(rows[0]):
            idx[re.sub(r"[\s_]+", "", name.strip().lower())] = i
        needed = ["project", "filepath", "ruleid", "severity"]
        missing = [n for n in needed if n not in idx]
        if missing:
            _csv_error = f"CSV missing columns {missing} (header={rows[0]})"
            return None
        max_idx = max(idx[n] for n in needed)
        parsed = []
        for r in rows[1:]:
            if not any(c.strip() for c in r):
                continue
            if len(r) <= max_idx:
                _csv_error = f"short CSV row: {r}"
                return None
            project = r[idx["project"]].strip()
            parsed.append((
                project,
                _norm_path(r[idx["filepath"]], project),
                r[idx["ruleid"]].strip(),
                r[idx["severity"]].strip(),
            ))
        _csv_rows_cache = parsed
        return parsed
    except Exception as e:
        _csv_error = f"CSV parse failed: {e}"
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


def mb_run_card(card_id: int) -> tuple[list, list]:
    """Execute a saved card server-side. Returns (col_names, rows).
    Raises RuntimeError on failure (one retry for sync lag). Works for native SQL cards too."""
    last_err = None
    for attempt in range(2):
        try:
            res = metabase_post(f"/api/card/{card_id}/query", timeout=60)
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


def mb_find_pg_db(name: str | None = None):
    """Locate the Baserow Postgres datasource. Never hardcode db ids."""
    resp = metabase_get("/api/database")
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


def mb_resolve_table(mb_db_id: int, physical_table_name: str):
    """(mb_table_id, {physical_field_name: mb_field_id}) via GET /api/database/<id>/metadata.
    physical_table_name is e.g. f"database_table_{tid}"; field names are f"field_{baserow_fid}"."""
    meta = metabase_get(f"/api/database/{mb_db_id}/metadata")
    for tbl in meta.get("tables", []) or []:
        if tbl.get("name") == physical_table_name:
            fmap = {f.get("name"): f.get("id") for f in tbl.get("fields", []) or []}
            return tbl.get("id"), fmap
    return None, {}


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_csv_exists() -> None:
    """lint_violations.csv exists under /home; the found path is pinned for ck2/ck5."""
    try:
        paths = get_csv_paths()
        check("1. CSV file exists in code-server", 1, bool(paths),
              f"pinned={paths[0]}" + (f" (+{len(paths) - 1} more)" if len(paths) > 1 else "")
              if paths else "lint_violations.csv not found")
    except Exception as e:
        check("1. CSV file exists in code-server", 1, False, f"exception: {e}")


def check_2_csv_content() -> None:
    """CSV content == recomputed lint truth (multiset of (Project, File Path, Rule ID, Severity))."""
    label = "2. CSV rows match recomputed lint truth"
    try:
        truth = get_lint_truth()
        if truth is None:
            check(label, 3, False, f"lint truth unavailable: {_lint_truth_error}")
            return
        rows = get_csv_rows()
        if rows is None:
            check(label, 3, False, _csv_error)
            return
        got = Counter(rows)
        exp = Counter(truth["violations"])
        passed = got == exp
        detail = f"csv_rows={len(rows)}, truth {truth['summary']}"
        if not passed:
            missing = list((exp - got).items())[:3]
            extra = list((got - exp).items())[:3]
            if missing:
                detail += f"; missing e.g. {missing}"
            if extra:
                detail += f"; extra e.g. {extra}"
        check(label, 3, passed, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_3_baserow_database() -> None:
    """Baserow database 'Code Quality Audit Q2 2025' exists."""
    try:
        result = baserow_db_query(
            "SELECT ca.id FROM core_application ca "
            "WHERE ca.name = 'Code Quality Audit Q2 2025'"
        )
        check("3. Baserow database exists", 1, bool(result),
              f"app_id={result}" if result else "database not found")
    except Exception as e:
        check("3. Baserow database exists", 1, False, f"exception: {e}")


def check_4_baserow_table_rows() -> None:
    """Full 'Lint Violations' table content == recomputed lint truth (multiset)."""
    label = "4. Baserow rows match recomputed lint truth"
    try:
        truth = get_lint_truth()
        if truth is None:
            check(label, 3, False, f"lint truth unavailable: {_lint_truth_error}")
            return
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 3, False, "table not found")
            return
        fids = _get_field_ids()
        needed = ["Project", "File Path", "Rule ID", "Severity"]
        missing_fields = [n for n in needed if n not in fids]
        if missing_fields:
            check(label, 3, False, f"missing fields: {missing_fields}")
            return
        proj_opts = _select_option_map(fids["Project"])
        sev_opts = _select_option_map(fids["Severity"])
        out = baserow_db_query_sep(
            f"SELECT field_{fids['Project']}, field_{fids['File Path']}, "
            f"field_{fids['Rule ID']}, field_{fids['Severity']} "
            f"FROM database_table_{table_id} WHERE trashed = false"
        )
        got: Counter = Counter()
        bad_rows = 0
        for line in out.split("\n"):
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) != 4:
                bad_rows += 1
                continue
            proj = proj_opts.get(parts[0].strip(), parts[0].strip())
            sev = sev_opts.get(parts[3].strip(), parts[3].strip())
            got[(proj, _norm_path(parts[1], proj), parts[2].strip(), sev)] += 1
        exp = Counter(truth["violations"])
        passed = bad_rows == 0 and got == exp
        detail = f"table_id={table_id}, rows={sum(got.values())}, truth {truth['summary']}"
        if bad_rows:
            detail += f"; {bad_rows} unparseable rows"
        if got != exp:
            missing = list((exp - got).items())[:3]
            extra = list((got - exp).items())[:3]
            if missing:
                detail += f"; missing e.g. {missing}"
            if extra:
                detail += f"; extra e.g. {extra}"
        check(label, 3, passed, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_5_violation_ids() -> None:
    """Violation IDs LV-NNN sequential, count == truth total, rows in CSV order."""
    label = "5. Violation IDs sequential and rows in CSV order"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return

        primary_field = baserow_db_query(
            f"SELECT df.id FROM database_field df "
            f"WHERE df.table_id = {table_id} AND df.\"primary\" = true"
        ).strip()
        if not primary_field:
            check(label, 2, False, "primary field not found")
            return
        fids = _get_field_ids()
        needed = ["Project", "File Path", "Rule ID"]
        missing_fields = [n for n in needed if n not in fids]
        if missing_fields:
            check(label, 2, False, f"missing fields: {missing_fields}")
            return
        proj_opts = _select_option_map(fids["Project"])
        out = baserow_db_query_sep(
            f"SELECT field_{primary_field}, field_{fids['Project']}, "
            f"field_{fids['File Path']}, field_{fids['Rule ID']} "
            f"FROM database_table_{table_id} WHERE trashed = false "
            f"ORDER BY \"order\" ASC, id ASC"
        )
        rows = []
        for line in out.split("\n"):
            if not line:
                continue
            parts = line.split("\x1f")
            if len(parts) != 4:
                rows = None
                break
            proj = proj_opts.get(parts[1].strip(), parts[1].strip())
            rows.append((parts[0].strip(), proj, _norm_path(parts[2], proj), parts[3].strip()))
        if rows is None:
            check(label, 2, False, "unparseable psql row")
            return
        if not rows:
            check(label, 2, False, "no rows")
            return

        errors = []
        for i, (vid, _, _, _) in enumerate(rows):
            expected = f"LV-{i + 1:03d}"
            if vid != expected:
                errors.append(f"index {i}: id '{vid}' expected '{expected}'")
                break
        truth = get_lint_truth()
        if truth is None:
            errors.append(f"lint truth unavailable: {_lint_truth_error}")
        elif len(rows) != truth["total"]:
            errors.append(f"count={len(rows)} expected {truth['total']}")
        csv_rows = get_csv_rows()
        if csv_rows is None:
            errors.append(f"CSV unavailable: {_csv_error}")
        else:
            if len(rows) != len(csv_rows):
                errors.append(f"row count {len(rows)} != CSV rows {len(csv_rows)}")
            for i, (_, proj, path, rule) in enumerate(rows):
                if i >= len(csv_rows):
                    break
                if (proj, path, rule) != csv_rows[i][:3]:
                    errors.append(
                        f"row {i} ({proj}, {path}, {rule}) != CSV {csv_rows[i][:3]}")
                    break
        passed = len(errors) == 0
        check(label, 2, passed,
              "; ".join(errors[:4]) if errors else
              f"count={len(rows)}, first={rows[0][0]}, last={rows[-1][0]}, CSV order matches")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_captured_at() -> None:
    """Captured At = 2025-05-14 for every row, with total == truth total."""
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check("6. Captured At date correct", 1, False, "table not found")
            return

        cap_field = baserow_db_query(
            f"SELECT df.id FROM database_field df "
            f"WHERE df.table_id = {table_id} AND df.name = 'Captured At'"
        ).strip()
        if not cap_field:
            check("6. Captured At date correct", 1, False, "field not found")
            return

        total = baserow_db_query(
            f"SELECT count(*) FROM database_table_{table_id} WHERE trashed = false"
        )
        wrong = baserow_db_query(
            f"SELECT count(*) FROM database_table_{table_id} "
            f"WHERE trashed = false AND field_{cap_field}::text NOT LIKE '2025-05-14%'"
        )
        truth = get_lint_truth()
        if truth is None:
            check("6. Captured At date correct", 1, False,
                  f"lint truth unavailable: {_lint_truth_error}")
            return
        passed = wrong == "0" and total == str(truth["total"])
        check("6. Captured At date correct", 1, passed,
              f"total={total} (expected {truth['total']}), wrong_date={wrong}")
    except Exception as e:
        check("6. Captured At date correct", 1, False, f"exception: {e}")


def check_6b_field_schema() -> None:
    """Field schema: types, primary flag, and select-option sets."""
    label = "6b. Field schema (types/primary/options)"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return
        fields = baserow_api_get(f"/api/database/fields/table/{table_id}/")
        by_name = {f.get("name"): f for f in fields}
        expected_types = {
            "Violation ID": "text",
            "Project": "single_select",
            "File Path": "text",
            "Rule ID": "text",
            "Severity": "single_select",
            "Captured At": "date",
        }
        expected_options = {
            "Project": {"todo-api", "data-analyzer", "blog-engine", "weather-dashboard"},
            "Severity": {"Error", "Warning", "Info"},
        }
        errors = []
        for name, ftype in expected_types.items():
            f = by_name.get(name)
            if not f:
                errors.append(f"missing field '{name}'")
                continue
            if f.get("type") != ftype:
                errors.append(f"'{name}' type={f.get('type')} expected {ftype}")
        f = by_name.get("Violation ID")
        if f and not f.get("primary"):
            errors.append("'Violation ID' is not the primary field")
        for name, exp_opts in expected_options.items():
            f = by_name.get(name)
            if f and f.get("type") == "single_select":
                opts = {o.get("value") for o in f.get("select_options", [])}
                if opts != exp_opts:
                    errors.append(f"'{name}' options {sorted(opts)} != {sorted(exp_opts)}")
        passed = len(errors) == 0
        check(label, 2, passed,
              "; ".join(errors[:6]) if errors else "all 6 fields with correct types/options")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_top_offenders_view() -> None:
    """'Top Offenders' grid view with group-by File Path and filter Severity=Error."""
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check("7. Top Offenders view", 2, False, "table not found")
            return

        views = baserow_api_get(f"/api/database/views/table/{table_id}/")
        top_view = None
        for v in views:
            if v.get("name") == "Top Offenders":
                top_view = v
                break

        if not top_view:
            check("7. Top Offenders view", 2, False, "view not found")
            return

        view_id = top_view["id"]

        # Check filter: Severity = Error. For single-select fields the filter
        # `value` holds the select OPTION ID, not the label — resolve ids via
        # database_selectoption before comparing.
        filters = baserow_api_get(f"/api/database/views/{view_id}/filters/")
        if isinstance(filters, dict):
            filters = filters.get("results", [])
        has_severity_filter = False
        for f in filters:
            field_id = f.get("field")
            if field_id:
                fname = baserow_db_query(
                    f"SELECT df.name FROM database_field df WHERE df.id = {field_id}"
                ).strip()
                if fname != "Severity":
                    continue
                raw_value = str(f.get("value", "")).strip()
                if "Error" in raw_value:
                    has_severity_filter = True
                    continue
                # Resolve option id(s) to their label(s)
                for part in raw_value.split(","):
                    part = part.strip()
                    if not part.isdigit():
                        continue
                    label = baserow_db_query(
                        f"SELECT value FROM database_selectoption WHERE id = {part}"
                    ).strip()
                    if label == "Error":
                        has_severity_filter = True

        # Check group-by: File Path (try DB table for group_bys)
        has_grouping = False
        try:
            group_rows = baserow_db_query(
                f"SELECT df.name FROM database_viewgroupby vg "
                f"JOIN database_field df ON vg.field_id = df.id "
                f"WHERE vg.view_id = {view_id}"
            ).strip()
            if "File Path" in group_rows:
                has_grouping = True
        except Exception:
            pass

        # Fallback: check via API view detail for group_bys
        if not has_grouping:
            try:
                detail = baserow_api_get(f"/api/database/views/{view_id}/")
                for gb in detail.get("group_bys", []):
                    fid = gb.get("field")
                    if fid:
                        fname = baserow_db_query(
                            f"SELECT df.name FROM database_field df WHERE df.id = {fid}"
                        ).strip()
                        if fname == "File Path":
                            has_grouping = True
            except Exception:
                pass

        check("7. Top Offenders view", 2, has_severity_filter and has_grouping,
              f"severity_error_filter={has_severity_filter}, grouped_file_path={has_grouping}")
    except Exception as e:
        check("7. Top Offenders view", 2, False, f"exception: {e}")


def check_8_metabase_collection() -> None:
    """Collection 'Lint Audit Q2 2025' exists."""
    try:
        collections = metabase_get("/api/collection")
        found = any(c.get("name") == "Lint Audit Q2 2025" for c in collections)
        check("8. Metabase collection exists", 1, found,
              "found" if found else "not found")
    except Exception as e:
        check("8. Metabase collection exists", 1, False, f"exception: {e}")


def _find_collection_id() -> int | None:
    collections = metabase_get("/api/collection")
    for c in collections:
        if c.get("name") == "Lint Audit Q2 2025":
            return c["id"]
    return None


def _get_collection_cards(col_id: int) -> list:
    """Return card items in a collection."""
    resp = metabase_get(f"/api/collection/{col_id}/items?models=card")
    if isinstance(resp, dict):
        return resp.get("data", [])
    return resp


def _resolve_mb_fields() -> tuple[dict | None, dict, str]:
    """(pg_db, {baserow field name: metabase field id}, error). Resolves the
    Baserow Postgres datasource and maps this table's fields into Metabase ids."""
    pg_db = mb_find_pg_db()
    if pg_db is None:
        return None, {}, "no Baserow Postgres datasource in Metabase"
    table_id = _get_baserow_table_id()
    if not table_id:
        return pg_db, {}, "Baserow table not found"
    fids = _get_field_ids()
    _, fmap = mb_resolve_table(pg_db["id"], f"database_table_{table_id}")
    if not fmap:
        return pg_db, {}, f"database_table_{table_id} not synced into Metabase"
    resolved = {}
    for name, fid in fids.items():
        mb_fid = fmap.get(f"field_{fid}")
        if mb_fid is not None:
            resolved[name] = mb_fid
    return pg_db, resolved, ""


def check_9_violations_by_project() -> None:
    """'Violations by Project': bar chart, MBQL count broken out by Project+Severity
    on the Baserow Postgres datasource."""
    label = "9. Violations by Project question"
    try:
        col_id = _find_collection_id()
        if not col_id:
            check(label, 3, False, "collection not found")
            return

        cards = _get_collection_cards(col_id)
        found = next((c for c in cards if c.get("name") == "Violations by Project"), None)
        if not found:
            check(label, 3, False, "question not found")
            return

        detail_card = metabase_get(f"/api/card/{found['id']}")
        shape = mb_shape(detail_card)
        errors = []
        if shape["display"] != "bar":
            errors.append(f"display={shape['display']} expected bar")
        pg_db, mb_fields, resolve_err = _resolve_mb_fields()
        if pg_db is None:
            errors.append(resolve_err)
        elif shape["database"] != pg_db["id"]:
            errors.append(f"card database={shape['database']} expected {pg_db['id']}")
        if shape["is_native"]:
            errors.append("expected a query-builder (MBQL) question, got native SQL")
        else:
            if shape["agg_ops"] != {"count"}:
                errors.append(f"aggregation={shape['agg_ops'] or 'none'} expected count")
            if resolve_err:
                errors.append(resolve_err)
            elif "Project" not in mb_fields or "Severity" not in mb_fields:
                errors.append("cannot resolve Project/Severity fields in Metabase metadata")
            else:
                exp = {mb_fields["Project"], mb_fields["Severity"]}
                if len(shape["breakout_fids"]) != 2 or set(shape["breakout_fids"]) != exp:
                    errors.append(
                        f"breakout={shape['breakout_fids']} expected Project+Severity {sorted(exp)}")
        passed = len(errors) == 0
        check(label, 3, passed,
              "; ".join(str(e) for e in errors[:5]) if errors else
              "bar chart, count by Project+Severity on Baserow Postgres")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_10_rule_frequency() -> None:
    """'Rule Frequency': table with limit 10, count by Rule ID ordered desc, and
    executed rows equal to the recomputed per-rule totals."""
    label = "10. Rule Frequency question"
    try:
        col_id = _find_collection_id()
        if not col_id:
            check(label, 3, False, "collection not found")
            return

        cards = _get_collection_cards(col_id)
        found = next((c for c in cards if c.get("name") == "Rule Frequency"), None)
        if not found:
            check(label, 3, False, "question not found")
            return

        detail_card = metabase_get(f"/api/card/{found['id']}")
        shape = mb_shape(detail_card)
        errors = []
        if shape["display"] != "table":
            errors.append(f"display={shape['display']} expected table")

        # Limit 10 (pMBQL stage limit, legacy query limit, or LIMIT 10 in native SQL)
        dq = detail_card.get("dataset_query", {})
        limit = None
        if dq.get("lib/type") == "mbql/query" and dq.get("stages"):
            limit = dq["stages"][0].get("limit")
        elif isinstance(dq.get("query"), dict):
            limit = dq["query"].get("limit")
        if shape["is_native"]:
            if not re.search(r"(?i)\blimit\s+10\b", shape["native_sql"] or ""):
                errors.append("native SQL has no LIMIT 10")
        elif limit != 10:
            errors.append(f"limit={limit} expected 10")

        pg_db, mb_fields, resolve_err = _resolve_mb_fields()
        if pg_db is None:
            errors.append(resolve_err)
        elif shape["database"] != pg_db["id"]:
            errors.append(f"card database={shape['database']} expected {pg_db['id']}")

        if not shape["is_native"]:
            if shape["agg_ops"] != {"count"}:
                errors.append(f"aggregation={shape['agg_ops'] or 'none'} expected count")
            if resolve_err:
                errors.append(resolve_err)
            elif "Rule ID" not in mb_fields:
                errors.append("cannot resolve Rule ID field in Metabase metadata")
            elif shape["breakout_fids"] != [mb_fields["Rule ID"]]:
                errors.append(
                    f"breakout={shape['breakout_fids']} expected [Rule ID {mb_fields['Rule ID']}]")
            # order-by count desc: pMBQL encodes an aggregation reference, not a
            # plain field ref — be lenient on encoding, assert direction desc.
            if not any(d == "desc" and fid is None for d, fid in shape["order_by"]):
                errors.append(f"no descending count order-by (order_by={shape['order_by']})")

        # Execution-level comparison (native or MBQL alike) vs recomputed truth.
        truth = get_lint_truth()
        if truth is None:
            errors.append(f"lint truth unavailable: {_lint_truth_error}")
        else:
            expected = sorted(truth["rule_totals"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]
            try:
                cols, rows = mb_run_card(found["id"])
                got = []
                bad = 0
                for r in rows:
                    if len(r) < 2:
                        bad += 1
                        continue
                    try:
                        got.append((str(r[0]).strip(), int(float(r[-1]))))
                    except (TypeError, ValueError):
                        bad += 1
                if bad:
                    errors.append(f"{bad} unparseable executed rows")
                elif Counter(got) != Counter(expected):
                    errors.append(f"executed rows {got} != expected {expected}")
                elif any(got[i][1] < got[i + 1][1] for i in range(len(got) - 1)):
                    errors.append(f"executed rows not in descending count order: {got}")
            except Exception as e:
                errors.append(f"card execution failed: {e}")
        passed = len(errors) == 0
        check(label, 3, passed,
              "; ".join(str(e) for e in errors[:5]) if errors else
              "table, count by Rule ID desc with limit 10; execution matches truth")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_11_metabase_dashboard() -> None:
    """Dashboard 'Code Quality Audit Dashboard' with correct description in collection."""
    try:
        col_id = _find_collection_id()
        if not col_id:
            check("11. Metabase dashboard", 2, False, "collection not found")
            return

        dashboards = metabase_get("/api/dashboard")
        found = next(
            (d for d in dashboards if d.get("name") == "Code Quality Audit Dashboard"),
            None,
        )
        if not found:
            check("11. Metabase dashboard", 2, False, "dashboard not found")
            return

        dash = metabase_get(f"/api/dashboard/{found['id']}")
        desc = dash.get("description", "") or ""
        expected_desc = "Lint audit 2025-05-14 across 4 projects"
        desc_ok = desc.strip() == expected_desc
        in_col = dash.get("collection_id") == col_id

        check("11. Metabase dashboard", 2, desc_ok and in_col,
              f"desc='{desc}', in_collection={in_col}")
    except Exception as e:
        check("11. Metabase dashboard", 2, False, f"exception: {e}")


def check_12_dashboard_cards() -> None:
    """Dashboard contains both question cards."""
    try:
        dashboards = metabase_get("/api/dashboard")
        found = next(
            (d for d in dashboards if d.get("name") == "Code Quality Audit Dashboard"),
            None,
        )
        if not found:
            check("12. Dashboard has both cards", 1, False, "dashboard not found")
            return

        dash = metabase_get(f"/api/dashboard/{found['id']}")
        dc = dash.get("dashcards", dash.get("ordered_cards", []))
        card_names = []
        for c in dc:
            card = c.get("card", {})
            if card and card.get("name"):
                card_names.append(card["name"])

        has_v = "Violations by Project" in card_names
        has_r = "Rule Frequency" in card_names
        check("12. Dashboard has both cards", 1, has_v and has_r,
              f"cards={card_names}")
    except Exception as e:
        check("12. Dashboard has both cards", 1, False, f"exception: {e}")


def check_13_openproject_work_packages() -> None:
    """'Security Audit' WP subjects == the recomputed top-5 lint-offender subjects."""
    label = "13. OpenProject top-5 WP subjects"
    try:
        truth = get_lint_truth()
        if truth is None:
            check(label, 3, False, f"lint truth unavailable: {_lint_truth_error}")
            return
        project_id = openproject_db_query(
            "SELECT id FROM projects WHERE name = 'Security Audit'"
        ).strip()
        if not project_id:
            check(label, 3, False, "project not found")
            return

        task_type_id = openproject_db_query(
            "SELECT id FROM types WHERE name = 'Task'"
        ).strip()
        if not task_type_id:
            check(label, 3, False, "Task type not found")
            return

        # Only the task-created WPs ("Fix lint errors: ..."); the project ships
        # with seed Task work packages that must be ignored.
        result = openproject_db_query(
            f"SELECT subject FROM work_packages "
            f"WHERE project_id = {project_id} AND type_id = {task_type_id} "
            f"AND subject LIKE 'Fix lint errors:%'"
        )
        subjects = [s.strip() for s in result.split("\n") if s.strip()]
        expected = Counter((relpath, n) for _, relpath, n in truth["top5"])
        got: Counter = Counter()
        errors = []
        if len(subjects) != len(truth["top5"]):
            errors.append(f"count={len(subjects)} expected {len(truth['top5'])}")
        for subject in subjects:
            m = re.match(r"^Fix lint errors: (.+) \((\d+) errors?\)$", subject)
            if not m:
                errors.append(f"bad subject format: {subject[:60]}")
                continue
            got[(_norm_path_any(m.group(1)), int(m.group(2)))] += 1
        if got != expected:
            missing = list((expected - got).keys())
            extra = list((got - expected).keys())
            if missing:
                errors.append(f"missing subjects for {missing}")
            if extra:
                errors.append(f"unexpected subjects for {extra}")
        passed = len(errors) == 0
        check(label, 3, passed,
              "; ".join(errors[:4]) if errors else
              f"5 subjects match recomputed top-5 {truth['top5']}")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_14_wp_details() -> None:
    """Work packages have correct assignee (admin), priority (High), and a description
    whose Project and per-rule counts match the recomputed lint truth."""
    label = "14. WP details correct"
    try:
        truth = get_lint_truth()
        if truth is None:
            check(label, 3, False, f"lint truth unavailable: {_lint_truth_error}")
            return
        project_id = openproject_db_query(
            "SELECT id FROM projects WHERE name = 'Security Audit'"
        ).strip()
        task_type_id = openproject_db_query(
            "SELECT id FROM types WHERE name = 'Task'"
        ).strip()
        admin_id = openproject_db_query(
            "SELECT id FROM users WHERE login = 'admin'"
        ).strip()
        high_pri_id = openproject_db_query(
            "SELECT id FROM enumerations WHERE name = 'High' AND type = 'IssuePriority'"
        ).strip()

        if not all([project_id, task_type_id, admin_id, high_pri_id]):
            check(label, 3, False,
                  f"proj={project_id}, type={task_type_id}, admin={admin_id}, pri={high_pri_id}")
            return

        ids_raw = openproject_db_query(
            f"SELECT id FROM work_packages "
            f"WHERE project_id = {project_id} AND type_id = {task_type_id} "
            f"AND subject LIKE 'Fix lint errors:%' "
            f"ORDER BY id"
        )
        wp_ids = [i.strip() for i in ids_raw.split("\n") if i.strip()]
        if not wp_ids:
            check(label, 3, False, "no work packages found")
            return

        top5_by_path = {relpath: proj for proj, relpath, _ in truth["top5"]}
        issues: list[str] = []
        for wp_id in wp_ids:
            # Fetch each WP field individually BY ID so multi-line descriptions
            # cannot pollute row parsing.
            subject = openproject_db_query(
                f"SELECT subject FROM work_packages WHERE id = {wp_id}")
            assignee_id = openproject_db_query(
                f"SELECT COALESCE(assigned_to_id::text, '') FROM work_packages WHERE id = {wp_id}")
            priority_id = openproject_db_query(
                f"SELECT COALESCE(priority_id::text, '') FROM work_packages WHERE id = {wp_id}")
            desc = openproject_db_query(
                f"SELECT COALESCE(description, '') FROM work_packages WHERE id = {wp_id}")

            if assignee_id != admin_id:
                issues.append(f"wrong assignee for '{subject[:40]}': {assignee_id}")
            if priority_id != high_pri_id:
                issues.append(f"wrong priority for '{subject[:40]}': {priority_id}")

            m = re.match(r"^Fix lint errors: (.+) \((\d+) errors?\)$", subject)
            if not m:
                issues.append(f"bad subject format: {subject[:60]}")
                continue
            norm = _norm_path_any(m.group(1))
            proj = top5_by_path.get(norm)
            if proj is None:
                issues.append(f"subject file '{norm}' not in recomputed top-5")
                continue
            rule_counts = truth["file_rule_counts"].get((proj, norm), {})

            pm = re.search(r"Project:\s*([^;\n]+)", desc)
            if not pm:
                issues.append(f"no 'Project:' in description for '{norm}'")
            elif pm.group(1).strip() != proj:
                issues.append(
                    f"description Project='{pm.group(1).strip()}' for '{norm}' expected '{proj}'")
            for code in sorted(rule_counts):
                if code not in desc:
                    issues.append(f"description for '{norm}' missing rule {code}")
            for code, num in re.findall(r"\b([EWFC]\d+):\s*(\d+)\b", desc):
                if rule_counts.get(code) != int(num):
                    issues.append(
                        f"description for '{norm}' says {code}: {num}, "
                        f"truth {rule_counts.get(code, 0)}")

        check(label, 3, not issues,
              "all correct" if not issues else "; ".join(issues[:4]))
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_csv_exists()
    check_2_csv_content()
    check_3_baserow_database()
    check_4_baserow_table_rows()
    check_5_violation_ids()
    check_6_captured_at()
    check_6b_field_schema()
    check_7_top_offenders_view()
    check_8_metabase_collection()
    check_9_violations_by_project()
    check_10_rule_frequency()
    check_11_metabase_dashboard()
    check_12_dashboard_cards()
    check_13_openproject_work_packages()
    check_14_wp_details()

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
