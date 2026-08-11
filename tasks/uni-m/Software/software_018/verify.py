"""
Verifier for Software-018-I1: CVE Remediation Sprint for todo-api and blog-engine

Checks: 5 weighted checks. Ground truth is computed at runtime: the dependency
files inside the code-server container are parsed and intersected with the
vulnerable-version list from the task description; the CVE Registry rows must
correspond exactly to that match set (which may legitimately be empty), so an
agent that fabricates findings fails and an agent that correctly reports zero
matches passes.
Strategy: Baserow REST API + code-server docker exec.

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import subprocess
import json

try:
    import requests as req_lib
except ImportError:
    print("FATAL: 'requests' library not available", file=sys.stderr)
    sys.exit(1)

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

REQUIRED_VARS = [
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
]
for _var in REQUIRED_VARS:
    if not os.environ.get(_var):
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

CODE_SERVER_CONTAINER = os.environ["CODE_SERVER_CONTAINER"]
BASEROW_PORT = os.environ["BASEROW_PORT"]
BASEROW_DB_CONTAINER = os.environ["BASEROW_DB_CONTAINER"]
OPENPROJECT_PORT = os.environ["OPENPROJECT_PORT"]
OPENPROJECT_CONTAINER = os.environ["OPENPROJECT_CONTAINER"]

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
OP_URL = f"http://{HOST}:{OPENPROJECT_PORT}"

# ── Vulnerable-version list from the task description ────────────────────────
VULNERABLE = {
    ("todo-api", "Flask"): "2.0.1",
    ("todo-api", "Jinja2"): "3.0.1",
    ("todo-api", "SQLAlchemy"): "1.4.22",
    ("todo-api", "requests"): "2.25.1",
    ("blog-engine", "express"): "4.17.1",
    ("blog-engine", "ejs"): "3.1.6",
    ("blog-engine", "marked"): "2.0.0",
    ("blog-engine", "lodash"): "4.17.20",
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


def baserow_auth() -> dict:
    """Get Baserow auth token and return headers."""
    resp = req_lib.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["token"]
    return {"Authorization": f"JWT {token}"}


def op_auth() -> tuple:
    """Return (username, password) for OpenProject basic auth."""
    return ("admin", "AdminPass123!")


def op_get(path: str, params: dict | None = None):
    resp = req_lib.get(f"{OP_URL}{path}", auth=op_auth(), params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


# ── Shared state across Baserow checks ────────────────────────────────────────
_br_headers: dict | None = None
_br_table_id: int | None = None
_br_fields: dict | None = None   # field_name -> field_info
_br_rows: list | None = None


def _init_baserow():
    global _br_headers
    if _br_headers is not None:
        return
    _br_headers = baserow_auth()


def _get_field_value(row: dict, field_name: str):
    """Extract a field's value from a Baserow row by field name."""
    if not _br_fields:
        return None
    field = _br_fields.get(field_name)
    if not field:
        return None
    val = row.get(f"field_{field['id']}")
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


# ── Baserow checks ────────────────────────────────────────────────────────────

def check_1_baserow_db_exists() -> None:
    """Verify Baserow database 'Dependency Security Audit 2025Q1' exists."""
    global _br_table_id, _br_fields
    try:
        _init_baserow()
        resp = req_lib.get(f"{BASEROW_URL}/api/applications/",
                           headers=_br_headers, timeout=15)
        resp.raise_for_status()
        db = None
        for app in resp.json():
            if (app.get("name") == "Dependency Security Audit 2025Q1"
                    and app.get("type") == "database"):
                db = app
                break
        if not db:
            check("1. Baserow DB exists", 1, False, "database not found")
            return
        check("1. Baserow DB exists", 1, True)

        # Also find the CVE Registry table for subsequent checks
        tables_resp = req_lib.get(
            f"{BASEROW_URL}/api/database/tables/database/{db['id']}/",
            headers=_br_headers, timeout=15,
        )
        tables_resp.raise_for_status()
        for t in tables_resp.json():
            if t["name"] == "CVE Registry":
                _br_table_id = t["id"]
                break
    except Exception as e:
        check("1. Baserow DB exists", 1, False, f"exception: {e}")


def check_2_baserow_table_and_fields() -> None:
    """Verify table 'CVE Registry' exists with required fields."""
    global _br_fields
    try:
        if _br_table_id is None:
            check("2. CVE Registry table with fields", 1, False, "table not found")
            return
        fields_resp = req_lib.get(
            f"{BASEROW_URL}/api/database/fields/table/{_br_table_id}/",
            headers=_br_headers, timeout=15,
        )
        fields_resp.raise_for_status()
        _br_fields = {f["name"]: f for f in fields_resp.json()}

        required = ["CVE ID", "Project", "Library Name", "Vulnerable Version",
                     "Fixed Version", "CVSS Score", "Severity", "Discovered Date"]
        missing = [f for f in required if f not in _br_fields]
        check("2. CVE Registry table with fields", 1, len(missing) == 0,
              f"missing fields: {missing}" if missing else "")
    except Exception as e:
        check("2. CVE Registry table with fields", 1, False, f"exception: {e}")



_expected_matches: set[tuple[str, str, str]] | None = None


def check_3_field_types() -> None:
    """Field types and select options match the specified schema."""
    try:
        if not _br_fields:
            check("3. Field types and select options", 1, False, "fields not loaded")
            return
        problems = []

        def opts(name):
            f = _br_fields.get(name) or {}
            return {o.get("value") for o in f.get("select_options", [])}

        if (_br_fields.get("Project") or {}).get("type") != "single_select":
            problems.append("Project not single_select")
        elif opts("Project") != {"todo-api", "blog-engine"}:
            problems.append(f"Project options {sorted(opts('Project'))}")
        if (_br_fields.get("Severity") or {}).get("type") != "single_select":
            problems.append("Severity not single_select")
        elif opts("Severity") != {"Critical", "High", "Medium", "Low"}:
            problems.append(f"Severity options {sorted(opts('Severity'))}")
        if (_br_fields.get("CVSS Score") or {}).get("type") != "number":
            problems.append("CVSS Score not number")
        if (_br_fields.get("Discovered Date") or {}).get("type") != "date":
            problems.append("Discovered Date not date")
        check("3. Field types and select options", 1, not problems,
              "; ".join(problems))
    except Exception as e:
        check("3. Field types and select options", 1, False, f"exception: {e}")


def _scan_fixture_matches() -> set[tuple[str, str, str]] | None:
    """Ground truth: (project, library, version) pins matching the vulnerable list.

    Parsed from the dependency files inside the code-server container so the
    check tracks whatever the seeded fixture actually contains (the set may
    legitimately be empty).
    """
    import re as _re
    matches: set[tuple[str, str, str]] = set()
    read_any = False
    for base in ("/home/coder/workspace", "/home/coder", "/home/coder/project"):
        rc, out, _ = docker_exec(CODE_SERVER_CONTAINER, "cat",
                                 f"{base}/todo-api/requirements.txt")
        if rc != 0:
            continue
        read_any = True
        for line in out.split("\n"):
            m = _re.match(r"\s*([A-Za-z0-9_.-]+)\s*==\s*([0-9][0-9A-Za-z.]*)", line)
            if m and VULNERABLE.get(("todo-api", m.group(1))) == m.group(2):
                matches.add(("todo-api", m.group(1), m.group(2)))
        rc, out, _ = docker_exec(CODE_SERVER_CONTAINER, "cat",
                                 f"{base}/blog-engine/package.json")
        if rc == 0:
            try:
                pkg = json.loads(out)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                for lib, ver in deps.items():
                    pinned = ver.lstrip("^~=v")
                    if VULNERABLE.get(("blog-engine", lib)) == pinned:
                        matches.add(("blog-engine", lib, pinned))
            except json.JSONDecodeError:
                pass
        break
    return matches if read_any else None


def check_4_dependency_scan() -> None:
    """Fixture dependency files are readable; vulnerable match set computed."""
    global _expected_matches
    try:
        _expected_matches = _scan_fixture_matches()
        check("4. Fixture dependency files readable", 1, _expected_matches is not None,
              f"{len(_expected_matches or set())} vulnerable pins in fixture"
              if _expected_matches is not None else "could not read dependency files")
    except Exception as e:
        _expected_matches = None
        check("4. Fixture dependency files readable", 1, False, f"exception: {e}")


def check_5_rows_match_scan() -> None:
    """CVE Registry rows correspond exactly to the computed vulnerable match set.

    An empty match set requires an empty table; fabricated rows fail the check.
    """
    try:
        if _br_table_id is None or _br_fields is None or _expected_matches is None:
            check("5. Rows match fixture scan", 3, False, "prerequisites missing")
            return
        resp = req_lib.get(
            f"{BASEROW_URL}/api/database/rows/table/{_br_table_id}/?size=200",
            headers=_br_headers, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", [])
        got = set()
        for row in rows:
            project = _get_field_value(row, "Project") or ""
            lib = _get_field_value(row, "Library Name") or ""
            ver = _get_field_value(row, "Vulnerable Version") or ""
            got.add((str(project).strip(), str(lib).strip(), str(ver).strip()))
        missing = _expected_matches - got
        extra = got - _expected_matches
        passed = not missing and not extra
        detail = (f"expected {len(_expected_matches)} rows, got {len(rows)}"
                  + (f"; missing {sorted(missing)[:3]}" if missing else "")
                  + (f"; unexpected {sorted(extra)[:3]}" if extra else ""))
        check("5. Rows match fixture scan", 3, passed, detail)
    except Exception as e:
        check("5. Rows match fixture scan", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_db_exists()
    check_2_baserow_table_and_fields()
    check_3_field_types()
    check_4_dependency_scan()
    check_5_rows_match_scan()

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
