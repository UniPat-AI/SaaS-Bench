"""
Verifier for Software-012-I1: Publish todo-api Endpoint Registry and Deprecation Tracker

Checks: 13 checks, total weight 17 (ck1 and the ck1b source-integrity guard are
0-weight — a FAIL there still blocks all_pass). Ground truth is extracted at
runtime by grepping the todo-api source in a throwaway container from the
code-server container's own pristine image (immune to agent edits) and
cross-validated against the Flask app.url_map imported in that same pristine
image, then every Baserow row's
Method / Path / Source File / Line Number / Version / Status / Deprecation Date
is compared against it in the deterministic (source file, line) order.
Strategy: pristine-image docker run (code-server ground truth),
docker exec (Postgres for baserow/openproject)

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
    """Run a SQL query against Baserow's Postgres DB.

    Pass sep="\\x1f" for row queries so text fields containing '|' don't break
    column splitting (psql -A default '|' separator hardening).
    """
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-F", sep, "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


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
# {field_name: {"col": "field_NNN", "id": NNN, "model": "textfield"|..., "primary": bool, "is_select": bool}}
_field_info: dict[str, dict] = {}
# {option_id_str: option_value} for single-select fields
_select_options: dict[str, str] = {}
# {field_id: {option_value, ...}} for per-field option-set assertions
_select_options_by_field: dict[int, set[str]] = {}
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
        # Get field name, id, content_type model, and primary flag for schema checks
        result = baserow_sql(
            f"SELECT f.name, f.id, ct.model, f.\"primary\" "
            f"FROM database_field f "
            f"JOIN django_content_type ct ON ct.id = f.content_type_id "
            f"WHERE f.table_id = {tid} AND f.trashed = false ORDER BY f.id;"
        )
        for line in result.split("\n"):
            if "|" not in line:
                continue
            parts = line.split("|")
            if len(parts) >= 4:
                name = parts[0].strip()
                fid = parts[1].strip()
                model = parts[2].strip()
                is_select = "singleselectfield" in model.lower()
                _field_info[name] = {
                    "col": f"field_{fid}",
                    "id": int(fid),
                    "model": model,
                    "primary": parts[3].strip() == "t",
                    "is_select": is_select,
                }
        # Load all select options for fields in this table
        field_ids = [str(f["id"]) for f in _field_info.values() if f["is_select"]]
        if field_ids:
            opts = baserow_sql(
                f"SELECT id, value, field_id FROM database_selectoption "
                f"WHERE field_id IN ({','.join(field_ids)});"
            )
            for line in opts.split("\n"):
                if "|" not in line:
                    continue
                parts = line.split("|")
                if len(parts) >= 3:
                    _select_options[parts[0].strip()] = parts[1].strip()
                    _select_options_by_field.setdefault(
                        int(parts[2].strip()), set()).add(parts[1].strip())
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
        # \x1f separator: text fields containing '|' must not break column splits
        result = baserow_sql(
            f"SELECT {col_str} FROM database_table_{tid} WHERE trashed = false ORDER BY id;",
            sep="\x1f",
        )
        rows = []
        for line in result.split("\n"):
            if not line.strip():
                continue
            parts = line.split("\x1f")
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


# ── Check 5: Baserow table field schema ──────────────────────────────────────
def check_5_baserow_fields() -> None:
    """Verify the table's field schema: types, primary, select option sets, decimals."""
    try:
        tid = _get_baserow_table_id()
        if tid is None:
            check("5. Baserow field schema", 2, False, "table not found")
            return
        finfo = _load_field_info()
        required = {"Endpoint ID", "Method", "Path", "Source File", "Line Number",
                    "Version", "Status", "Deprecation Date"}
        problems = []
        missing = required - set(finfo)
        if missing:
            problems.append(f"missing: {sorted(missing)}")

        def model(name: str) -> str:
            return (finfo.get(name) or {}).get("model", "")

        ep = finfo.get("Endpoint ID") or {}
        if ep.get("model") != "textfield" or not ep.get("primary"):
            problems.append("Endpoint ID not primary text")
        for name in ("Path", "Source File"):
            if model(name) != "textfield":
                problems.append(f"{name} not text")
        expected_opts = {
            "Method": {"GET", "POST", "PUT", "PATCH", "DELETE"},
            "Version": {"v1", "v2", "v3"},
            "Status": {"Active", "Deprecated", "Removed"},
        }
        for name, exp in expected_opts.items():
            f = finfo.get(name) or {}
            if f.get("model") != "singleselectfield":
                problems.append(f"{name} not single_select")
            else:
                opts = _select_options_by_field.get(f["id"], set())
                if opts != exp:
                    problems.append(f"{name} options {sorted(opts)}, expected {sorted(exp)}")
        ln = finfo.get("Line Number") or {}
        if ln.get("model") != "numberfield":
            problems.append("Line Number not number")
        else:
            dp = baserow_sql(
                f"SELECT number_decimal_places FROM database_numberfield "
                f"WHERE field_ptr_id = {ln['id']};"
            ).strip()
            if dp != "0":
                problems.append(f"Line Number decimal places {dp or '?'}, expected 0")
        if model("Deprecation Date") != "datefield":
            problems.append("Deprecation Date not date")
        check("5. Baserow field schema", 2, not problems,
              "; ".join(problems[:5]) if problems else "")
    except Exception as e:
        check("5. Baserow field schema", 2, False, f"exception: {e}")


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


# ── Ground truth: Flask routes greped from the pristine code-server image ────
import re

_VERSION_MAP = [("/api/v1/", "v1"), ("/api/v2/", "v2"), ("/api/v3/", "v3"), ("/health", "v1")]
_ROUTE_RE = re.compile(
    r"""@[A-Za-z_][A-Za-z0-9_]*\.route\(\s*["']([^"']+)["'](?:.*?methods\s*=\s*\[([^\]]*)\])?""",
)
_gt_routes: list[dict] | None = None
_gt_base: str | None = None


def _ground_truth_routes() -> list[dict]:
    """Extract route registrations from todo-api source, sorted by (source file, line).

    Runs in a throwaway container from the code-server container's pristine
    image (never the live, agent-touched container)."""
    global _gt_routes, _gt_base
    if _gt_routes is not None:
        return _gt_routes
    routes = []
    for base in ("/home/coder/workspace/todo-api", "/home/coder/todo-api",
                 "/home/coder/project/todo-api"):
        try:
            rc, out, _ = image_exec(
                "grep", "-rn", "-E", r"@[A-Za-z_]*\.route\(", base, "--include=*.py",
                timeout=60,
            )
        except Exception:
            continue  # degrade like a failed grep: empty GT fails downstream checks
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
        _gt_base = base
        break
    routes.sort(key=lambda r: (r["file"], r["line"]))
    _gt_routes = routes
    return routes


# ── Blueprint url_prefix truth (static parse + runtime url_map cross-check) ──
_prefix_cache: dict[str, str] | None = None
_runtime_rules_cache: set[str] | None = None
_runtime_checked = False
_runtime_error = ""
_route_truth_cache: list[dict] | None = None


def _blueprint_prefixes() -> dict[str, str]:
    """Parse blueprint url_prefixes from app/routes/__init__.py (module stem -> prefix)."""
    global _prefix_cache
    if _prefix_cache is not None:
        return _prefix_cache
    _ground_truth_routes()  # ensure _gt_base resolved
    prefixes: dict[str, str] = {}
    if _gt_base:
        try:
            rc, out, _ = image_exec(
                "grep", "-n", "register_blueprint", f"{_gt_base}/app/routes/__init__.py",
                timeout=60,
            )
        except Exception:
            rc, out = 1, ""  # degrade like a failed grep: no prefixes parsed
        if rc == 0:
            for line in out.strip().split("\n"):
                m = re.search(
                    r"register_blueprint\(\s*([A-Za-z_][A-Za-z0-9_]*)"
                    r"(?:.*?url_prefix\s*=\s*[\"']([^\"']*)[\"'])?", line)
                if not m:
                    continue
                var = m.group(1)
                stem = var[:-3] if var.endswith("_bp") else var
                prefixes[stem] = m.group(2) or ""
    _prefix_cache = prefixes
    return prefixes


def _runtime_rules() -> set[str] | None:
    """Authoritative runtime channel: import the Flask app in a throwaway
    container from the pristine code-server image (never the agent-touched live
    container) and print app.url_map rules (excluding /static/<path:filename>).
    None on failure."""
    global _runtime_rules_cache, _runtime_checked, _runtime_error
    if _runtime_checked:
        return _runtime_rules_cache
    _runtime_checked = True
    _ground_truth_routes()  # ensure _gt_base resolved
    base = _gt_base or "/home/coder/workspace/todo-api"
    py = ("from app import create_app; app = create_app(); "
          "[print(r.rule) for r in app.url_map.iter_rules() "
          "if r.rule != '/static/<path:filename>']")
    try:
        rc, out, err = image_exec(
            "bash", "-lc",
            f'cd {base} && python3 -c "{py}"', timeout=180,
        )
        if rc != 0:
            _runtime_error = (err.strip().split("\n")[-1] if err.strip() else f"rc={rc}")
            return None
        _runtime_rules_cache = {ln.strip() for ln in out.split("\n") if ln.strip()}
    except Exception as e:
        _runtime_error = str(e)
        return None
    return _runtime_rules_cache


def _route_truth() -> list[dict]:
    """GT routes + prefix-qualified full path + exact legal Path value set per route.

    Full path = static blueprint prefix + literal, overridden by the runtime
    url_map rule when the static parse disagrees (runtime is authoritative)."""
    global _route_truth_cache
    if _route_truth_cache is not None:
        return _route_truth_cache
    gt = _ground_truth_routes()
    prefixes = _blueprint_prefixes()
    rules = _runtime_rules()
    truth = []
    for g in gt:
        stem = g["file"].rsplit("/", 1)[-1].removesuffix(".py")
        full = prefixes.get(stem, "") + g["path"]
        if rules is not None and full not in rules:
            cands = [r for r in rules if r == g["path"] or r.endswith(g["path"])]
            if len(cands) == 1:
                full = cands[0]
        truth.append({**g, "full": full, "legal": {g["path"], full}})
    _route_truth_cache = truth
    return truth


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
    """0-weight front guard: route GT is discoverable (empty GT fails every
    downstream GT-dependent check via the len(gt) gates)."""
    try:
        gt = _ground_truth_routes()
        check("1. todo-api routes discoverable", 0, len(gt) > 0,
              f"{len(gt)} route registrations found" if gt
              else "no routes found — all GT-dependent checks fail")
    except Exception as e:
        check("1. todo-api routes discoverable", 0, False, f"exception: {e}")


def check_1b_source_integrity() -> None:
    """0-weight guard: the Flask app still imports and its url_map matches the
    static grep+prefix parse (fallback truth stays the static parse on failure)."""
    try:
        truth = _route_truth()
        rules = _runtime_rules()
        if rules is None:
            check("1b. Source integrity (url_map import)", 0, False,
                  f"url_map import failed ({_runtime_error or 'unknown'}); "
                  f"falling back to static grep+prefix parse")
            return
        static_full = {t["full"] for t in truth}
        passed = static_full == rules
        prefixes = _blueprint_prefixes()
        summary = ",".join(f"{k}:'{v}'" for k, v in sorted(prefixes.items()))
        detail = (f"{len(rules)} url_map rules match grep+prefix parse ({summary})"
                  if passed else
                  f"mismatch: url_map-only={sorted(rules - static_full)[:3]} "
                  f"static-only={sorted(static_full - rules)[:3]}")
        check("1b. Source integrity (url_map import)", 0, passed, detail)
    except Exception as e:
        check("1b. Source integrity (url_map import)", 0, False, f"exception: {e}")


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
    """Path must be exactly the route literal or its url_prefix-qualified full
    path (legal set per route); versioned /api/vN/ prefixes are always illegal."""
    try:
        truth, rows = _route_truth(), _rows_in_ep_order()
        if not truth or len(rows) != len(truth):
            check("8. Paths match source", 2, False,
                  f"routes={len(truth)} rows={len(rows)}")
            return
        bad = []
        for i, (g, r) in enumerate(zip(truth, rows)):
            rp = r.get("Path", "").strip()
            if re.match(r"^/api/v[123]/", rp):
                bad.append(f"row{i+1}({rp}: versioned prefix illegal)")
            elif rp not in g["legal"]:
                bad.append(f"row{i+1}({rp} not in {sorted(g['legal'])})")
        prefixes = _blueprint_prefixes()
        summary = "prefixes " + ",".join(f"{k}:'{v}'" for k, v in sorted(prefixes.items()))
        check("8. Paths match source", 2, not bad,
              "; ".join(bad[:4]) if bad else summary)
    except Exception as e:
        check("8. Paths match source", 2, False, f"exception: {e}")


def check_9_file_line() -> None:
    _ordered_compare("9. Source file and line match", 2, lambda g, r: (
        _norm_file(r.get("Source File", "")) == g["file"]
        and _norm_line(r.get("Line Number", "")) == g["line"],
        f"{r.get('Source File')}:{r.get('Line Number')}!={g['file']}:{g['line']}"))


def check_10_versions() -> None:
    """Version keyed on the GT full path (not the agent-entered Path): /health
    must be v1 exactly; routes the spec mapping does not cover only require a
    legal option value (no invented truth for unmapped routes)."""
    try:
        truth, rows = _route_truth(), _rows_in_ep_order()
        if not truth or len(rows) != len(truth):
            check("10. Version assignment", 1, False,
                  f"routes={len(truth)} rows={len(rows)}")
            return
        bad, unmapped = [], 0
        for g, r in zip(truth, rows):
            full = g["full"]
            expected = next((v for prefix, v in _VERSION_MAP if full.startswith(prefix)), None)
            ver = r.get("Version", "").strip()
            if expected is not None:
                if ver != expected:
                    bad.append(f"{full}:{ver}!={expected}")
            else:
                unmapped += 1
                if ver not in ("v1", "v2", "v3"):
                    bad.append(f"{full}:Version={ver!r} not a legal option")
        gap = f"spec gap: {unmapped} routes unmapped"
        check("10. Version assignment", 1, not bad,
              ("; ".join(bad[:4]) + f"; {gap}") if bad else gap)
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
    check_1b_source_integrity()
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
