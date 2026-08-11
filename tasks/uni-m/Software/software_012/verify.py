"""
Verifier for Software-012-I1: Publish todo-api Endpoint Registry and Deprecation Tracker

Checks: 12 weighted checks — ground truth is extracted at runtime by grepping
the todo-api source inside the code-server container, then every Baserow row's
Method / Path / Source File / Line Number / Version / Status / Deprecation Date
is compared against it in the deterministic (source file, line) order.
Strategy: docker exec (filesystem for code-server, Postgres for baserow)

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import subprocess

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

for var_name, val in [
    ("CODE_SERVER_CONTAINER", CODE_SERVER_CONTAINER),
    ("BASEROW_DB_CONTAINER", BASEROW_DB_CONTAINER),
    ("OPENPROJECT_CONTAINER", OPENPROJECT_CONTAINER),
]:
    if not val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

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
    """Run a SQL query against Baserow's Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip()


def openproject_sql(query: str) -> str:
    """Run a SQL query against OpenProject's embedded Postgres DB via TCP with password."""
    # Use stdin to pass query to avoid shell quoting issues with special chars
    r = subprocess.run(
        ["docker", "exec", "-i", OPENPROJECT_CONTAINER,
         "bash", "-c",
         "PGPASSWORD=openproject psql -U openproject -d openproject -h 127.0.0.1 -t -A"],
        input=query, capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql error: {r.stderr.strip()}")
    return r.stdout.strip()


# ── Baserow shared state ─────────────────────────────────────────────────────
_baserow_table_id: int | None = None
# {field_name: {"col": "field_NNN", "id": NNN, "type": "text"|"single_select"|...}}
_field_info: dict[str, dict] = {}
# {option_id_str: option_value} for single-select fields
_select_options: dict[str, str] = {}
_rows_cache: list[dict] | None = None


def _get_baserow_table_id() -> int | None:
    global _baserow_table_id
    if _baserow_table_id is not None:
        return _baserow_table_id
    try:
        result = baserow_sql(
            "SELECT dt.id FROM database_table dt "
            "JOIN database_database d ON d.application_ptr_id = dt.database_id "
            "JOIN core_application a ON a.id = d.application_ptr_id "
            "WHERE dt.name = 'API Endpoint Registry' "
            "AND a.name = 'TodoAPI Endpoint Governance';"
        )
        if result:
            _baserow_table_id = int(result.split("\n")[0].strip())
            return _baserow_table_id
    except Exception:
        pass
    return None


def _load_field_info() -> dict[str, dict]:
    """Load field names, IDs, types, and select options for the table."""
    global _field_info, _select_options
    if _field_info:
        return _field_info
    tid = _get_baserow_table_id()
    if tid is None:
        return {}
    try:
        # Get field name, id, and content_type for type detection
        result = baserow_sql(
            f"SELECT f.name, f.id, ct.model "
            f"FROM database_field f "
            f"JOIN django_content_type ct ON ct.id = f.content_type_id "
            f"WHERE f.table_id = {tid} AND f.trashed = false ORDER BY f.id;"
        )
        for line in result.split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                name = parts[0].strip()
                fid = parts[1].strip()
                model = parts[2].strip()
                is_select = "singleselectfield" in model.lower()
                _field_info[name] = {
                    "col": f"field_{fid}",
                    "id": int(fid),
                    "is_select": is_select,
                }
        # Load all select options for fields in this table
        field_ids = [str(f["id"]) for f in _field_info.values() if f["is_select"]]
        if field_ids:
            opts = baserow_sql(
                f"SELECT id, value FROM database_selectoption "
                f"WHERE field_id IN ({','.join(field_ids)});"
            )
            for line in opts.split("\n"):
                if "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) >= 2:
                    _select_options[parts[0].strip()] = parts[1].strip()
    except Exception:
        pass
    return _field_info


def _get_all_rows() -> list[dict]:
    """Return all rows as dicts with field names as keys, select fields resolved to text."""
    global _rows_cache
    if _rows_cache is not None:
        return _rows_cache
    tid = _get_baserow_table_id()
    if tid is None:
        return []
    finfo = _load_field_info()
    if not finfo:
        return []
    cols = []
    col_order = []  # (field_name, db_col, is_select)
    for name, info in finfo.items():
        cols.append(info["col"])
        col_order.append((name, info["col"], info["is_select"]))
    col_str = ", ".join(cols)
    try:
        result = baserow_sql(
            f"SELECT {col_str} FROM database_table_{tid} WHERE trashed = false ORDER BY id;"
        )
        rows = []
        for line in result.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) == len(cols):
                row = {}
                for i, (name, _col, is_select) in enumerate(col_order):
                    val = parts[i].strip()
                    if is_select and val:
                        # Resolve select option ID to text value
                        val = _select_options.get(val, val)
                    row[name] = val
                rows.append(row)
        _rows_cache = rows
        return rows
    except Exception:
        return []


# ── Check 3: Baserow database exists ─────────────────────────────────────────
def check_3_baserow_database() -> None:
    """Verify database 'TodoAPI Endpoint Governance' exists in Baserow."""
    try:
        result = baserow_sql(
            "SELECT a.name FROM core_application a "
            "JOIN database_database d ON d.application_ptr_id = a.id "
            "WHERE a.name = 'TodoAPI Endpoint Governance';"
        )
        passed = "TodoAPI Endpoint Governance" in result
        check("3. Baserow database exists", 1, passed,
              "" if passed else f"got: '{result}'")
    except Exception as e:
        check("3. Baserow database exists", 1, False, f"exception: {e}")


# ── Check 4: Baserow table exists ────────────────────────────────────────────
def check_4_baserow_table() -> None:
    """Verify table 'API Endpoint Registry' exists in the database."""
    try:
        tid = _get_baserow_table_id()
        passed = tid is not None
        check("4. Baserow table exists", 1, passed,
              "" if passed else "table 'API Endpoint Registry' not found")
    except Exception as e:
        check("4. Baserow table exists", 1, False, f"exception: {e}")


# ── Check 5: Baserow table has required fields ───────────────────────────────
def check_5_baserow_fields() -> None:
    """Verify the table has the expected fields."""
    try:
        tid = _get_baserow_table_id()
        if tid is None:
            check("5. Baserow table fields", 2, False, "table not found")
            return
        result = baserow_sql(
            f"SELECT f.name FROM database_field f "
            f"WHERE f.table_id = {tid} AND f.trashed = false ORDER BY f.id;"
        )
        fields = {line.strip() for line in result.split("\n") if line.strip()}
        required = {"Endpoint ID", "Method", "Path", "Source File", "Line Number",
                    "Version", "Status", "Deprecation Date"}
        missing = required - fields
        passed = len(missing) == 0
        check("5. Baserow table fields", 2, passed,
              f"missing: {missing}" if not passed else "")
    except Exception as e:
        check("5. Baserow table fields", 2, False, f"exception: {e}")


# ── Check 6: Rows have sequential EP-NNN IDs ─────────────────────────────────
def check_6_endpoint_ids() -> None:
    """Verify rows have sequential EP-001, EP-002, ... Endpoint IDs."""
    try:
        rows = _get_all_rows()
        if not rows:
            check("6. Sequential Endpoint IDs", 2, False, "no rows found")
            return
        ids = sorted([r.get("Endpoint ID", "") for r in rows])
        expected = [f"EP-{i:03d}" for i in range(1, len(ids) + 1)]
        passed = ids == expected
        detail = "" if passed else f"expected {expected[:3]}..., got {ids[:3]}..."
        check("6. Sequential Endpoint IDs", 2, passed, detail)
    except Exception as e:
        check("6. Sequential Endpoint IDs", 2, False, f"exception: {e}")


# ── Ground truth: Flask routes greped from the code-server container ─────────
import re

_VERSION_MAP = [("/api/v1/", "v1"), ("/api/v2/", "v2"), ("/api/v3/", "v3"), ("/health", "v1")]
_ROUTE_RE = re.compile(
    r"""@[A-Za-z_][A-Za-z0-9_]*\.route\(\s*["']([^"']+)["'](?:.*?methods\s*=\s*\[([^\]]*)\])?""",
)
_gt_routes: list[dict] | None = None


def _ground_truth_routes() -> list[dict]:
    """Extract route registrations from todo-api source, sorted by (source file, line)."""
    global _gt_routes
    if _gt_routes is not None:
        return _gt_routes
    routes = []
    for base in ("/home/coder/workspace/todo-api", "/home/coder/todo-api",
                 "/home/coder/project/todo-api"):
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER,
            "grep", "-rn", "-E", r"@[A-Za-z_]*\.route\(", base, "--include=*.py",
        )
        if rc != 0 or not out.strip():
            continue
        for line in out.strip().split("\n"):
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath, lineno, content = parts[0], parts[1], parts[2]
            m = _ROUTE_RE.search(content)
            if not m:
                continue
            path = m.group(1)
            methods_raw = m.group(2) or ""
            methods = [t.strip().strip("\"'") for t in methods_raw.split(",") if t.strip()]
            rel = fpath.split("todo-api/", 1)[1] if "todo-api/" in fpath else fpath
            routes.append({
                "file": rel,
                "line": int(lineno),
                "path": path,
                "method": methods[0] if methods else "GET",
            })
        break
    routes.sort(key=lambda r: (r["file"], r["line"]))
    _gt_routes = routes
    return routes


def _rows_in_ep_order() -> list[dict]:
    return sorted(_get_all_rows(), key=lambda r: r.get("Endpoint ID", ""))


def _norm_file(value: str) -> str:
    value = value.strip()
    return value.split("todo-api/", 1)[1] if "todo-api/" in value else value.lstrip("/")


def _norm_line(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def check_1_ground_truth() -> None:
    """Route registrations are discoverable in the code-server todo-api project."""
    try:
        gt = _ground_truth_routes()
        check("1. todo-api routes discoverable", 1, len(gt) > 0,
              f"{len(gt)} route registrations found")
    except Exception as e:
        check("1. todo-api routes discoverable", 1, False, f"exception: {e}")


def check_2_row_count() -> None:
    """Exactly one Baserow row per discovered route."""
    try:
        gt, rows = _ground_truth_routes(), _get_all_rows()
        check("2. One row per route", 2, len(gt) > 0 and len(rows) == len(gt),
              f"routes={len(gt)} rows={len(rows)}")
    except Exception as e:
        check("2. One row per route", 2, False, f"exception: {e}")


def _ordered_compare(label: str, weight: int, extract) -> None:
    """Compare the k-th row (by Endpoint ID) against the k-th ground-truth route."""
    try:
        gt, rows = _ground_truth_routes(), _rows_in_ep_order()
        if not gt or len(rows) != len(gt):
            check(label, weight, False, f"routes={len(gt)} rows={len(rows)}")
            return
        bad = []
        for i, (g, r) in enumerate(zip(gt, rows)):
            ok, detail = extract(g, r)
            if not ok:
                bad.append(f"row{i+1}({detail})")
        check(label, weight, not bad, "; ".join(bad[:4]) if bad else "")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_7_methods() -> None:
    _ordered_compare("7. Methods match source", 2, lambda g, r: (
        r.get("Method", "").strip().upper() == g["method"].upper(),
        f"{r.get('Method')}!={g['method']}"))


def check_8_paths() -> None:
    def cmp(g, r):
        rp = r.get("Path", "").strip()
        # Accept the literal route string or a url_prefix-qualified full path.
        ok = rp == g["path"] or (rp.startswith("/") and rp.endswith(g["path"]))
        return ok, f"{rp}!={g['path']}"
    _ordered_compare("8. Paths match source", 2, cmp)


def check_9_file_line() -> None:
    _ordered_compare("9. Source file and line match", 2, lambda g, r: (
        _norm_file(r.get("Source File", "")) == g["file"]
        and _norm_line(r.get("Line Number", "")) == g["line"],
        f"{r.get('Source File')}:{r.get('Line Number')}!={g['file']}:{g['line']}"))


def check_10_versions() -> None:
    """Version follows the path-prefix mapping (rows whose path maps to no rule are skipped)."""
    try:
        rows = _rows_in_ep_order()
        if not rows:
            check("10. Version assignment", 1, False, "no rows")
            return
        bad, unmapped = [], 0
        for r in rows:
            rp = r.get("Path", "").strip()
            expected = next((v for prefix, v in _VERSION_MAP if rp.startswith(prefix)), None)
            if expected is None:
                unmapped += 1
                continue
            if r.get("Version", "").strip() != expected:
                bad.append(f"{rp}:{r.get('Version')}!={expected}")
        check("10. Version assignment", 1, not bad,
              "; ".join(bad[:4]) if bad else f"{unmapped} paths outside mapping skipped")
    except Exception as e:
        check("10. Version assignment", 1, False, f"exception: {e}")


def check_11_status() -> None:
    try:
        rows = _get_all_rows()
        bad = [r.get("Endpoint ID", "?") for r in rows if r.get("Status", "").strip() != "Active"]
        check("11. Status=Active on all rows", 1, bool(rows) and not bad,
              f"non-Active: {bad[:5]}" if bad else "")
    except Exception as e:
        check("11. Status=Active on all rows", 1, False, f"exception: {e}")


def check_12_deprecation_null() -> None:
    try:
        rows = _get_all_rows()
        bad = [r.get("Endpoint ID", "?") for r in rows if r.get("Deprecation Date", "").strip()]
        check("12. Deprecation Date empty on all rows", 1, bool(rows) and not bad,
              f"non-empty: {bad[:5]}" if bad else "")
    except Exception as e:
        check("12. Deprecation Date empty on all rows", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_ground_truth()
    check_2_row_count()
    check_3_baserow_database()
    check_4_baserow_table()
    check_5_baserow_fields()
    check_6_endpoint_ids()
    check_7_methods()
    check_8_paths()
    check_9_file_line()
    check_10_versions()
    check_11_status()
    check_12_deprecation_null()

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
