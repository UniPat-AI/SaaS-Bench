"""
Verifier for Software-018-I1: CVE Remediation Sprint for todo-api and blog-engine

Checks: 5 checks, total weight 7 (ck4 is a 0-weight dual-manifest readability
gate — a FAIL there still blocks all_pass and forces ck5 to 0). Ground truth is
computed at runtime: BOTH dependency manifests inside the code-server container
must load from the same base and are intersected with the vulnerable-version
list from the task description; the CVE Registry rows must correspond exactly
to that match set (which may legitimately be empty), so an agent that
fabricates findings fails and an agent that correctly reports zero matches
passes.
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
    """Full field schema: types, primary flag, select options, decimal places."""
    try:
        if not _br_fields:
            check("3. Field types and select options", 2, False, "fields not loaded")
            return
        problems = []

        def opts(name):
            f = _br_fields.get(name) or {}
            return {o.get("value") for o in f.get("select_options", [])}

        def ftype(name):
            return (_br_fields.get(name) or {}).get("type")

        cve_f = _br_fields.get("CVE ID") or {}
        if cve_f.get("type") != "text" or not cve_f.get("primary"):
            problems.append("CVE ID not primary text")
        for name in ("Library Name", "Vulnerable Version", "Fixed Version"):
            if ftype(name) != "text":
                problems.append(f"{name} not text")
        if (_br_fields.get("Project") or {}).get("type") != "single_select":
            problems.append("Project not single_select")
        elif opts("Project") != {"todo-api", "blog-engine"}:
            problems.append(f"Project options {sorted(opts('Project'))}")
        if (_br_fields.get("Severity") or {}).get("type") != "single_select":
            problems.append("Severity not single_select")
        elif opts("Severity") != {"Critical", "High", "Medium", "Low"}:
            problems.append(f"Severity options {sorted(opts('Severity'))}")
        cvss_f = _br_fields.get("CVSS Score") or {}
        if cvss_f.get("type") != "number":
            problems.append("CVSS Score not number")
        elif int(cvss_f.get("number_decimal_places") or 0) != 1:
            problems.append(
                f"CVSS Score decimal places {cvss_f.get('number_decimal_places')}, expected 1")
        if (_br_fields.get("Discovered Date") or {}).get("type") != "date":
            problems.append("Discovered Date not date")
        check("3. Field types and select options", 2, not problems,
              "; ".join(problems[:5]))
    except Exception as e:
        check("3. Field types and select options", 2, False, f"exception: {e}")


_scan_detail: str = ""


def _scan_fixture_matches() -> set[tuple[str, str, str]] | None:
    """Ground truth: (project, library, version) pins matching the vulnerable list.

    Dual-manifest gate: BOTH manifests must load from the SAME base —
    todo-api/requirements.txt must parse >=1 '==' pin AND blog-engine/package.json
    must JSON-parse with non-empty dependencies|devDependencies. Any failure
    (including a package.json parse error, previously swallowed) returns None
    so ck5 is forced to 0. The match set may legitimately be empty.
    Computed once; a truth summary is left in _scan_detail.
    """
    import re as _re
    global _scan_detail
    for base in ("/home/coder/workspace", "/home/coder", "/home/coder/project"):
        rc, out, _ = docker_exec(CODE_SERVER_CONTAINER, "cat",
                                 f"{base}/todo-api/requirements.txt")
        if rc != 0:
            continue
        pins: list[tuple[str, str]] = []
        for line in out.split("\n"):
            m = _re.match(r"\s*([A-Za-z0-9_.-]+)\s*==\s*([0-9][0-9A-Za-z.]*)", line)
            if m:
                pins.append((m.group(1), m.group(2)))
        if not pins:
            _scan_detail = f"no ==-pins parsed from {base}/todo-api/requirements.txt"
            return None
        rc, out, _ = docker_exec(CODE_SERVER_CONTAINER, "cat",
                                 f"{base}/blog-engine/package.json")
        if rc != 0:
            _scan_detail = f"{base}/blog-engine/package.json not readable"
            return None
        try:
            pkg = json.loads(out)
        except json.JSONDecodeError as e:
            _scan_detail = f"{base}/blog-engine/package.json JSON parse failed: {e}"
            return None
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        if not deps:
            _scan_detail = f"{base}/blog-engine/package.json has no dependencies/devDependencies"
            return None
        matches: set[tuple[str, str, str]] = set()
        for name, ver in pins:
            if VULNERABLE.get(("todo-api", name)) == ver:
                matches.add(("todo-api", name, ver))
        for lib, ver in deps.items():
            pinned = str(ver).lstrip("^~=v")
            if VULNERABLE.get(("blog-engine", lib)) == pinned:
                matches.add(("blog-engine", lib, pinned))
        _scan_detail = (f"base={base}; {len(pins)} python pins, {len(deps)} node deps, "
                        f"{len(matches)} vulnerable matches")
        return matches
    _scan_detail = "todo-api/requirements.txt not readable in any base"
    return None


def check_4_dependency_scan() -> None:
    """0-weight gate: both manifests load from the same base; GT match set computed."""
    global _expected_matches
    try:
        _expected_matches = _scan_fixture_matches()
        check("4. Dual-manifest readability gate", 0, _expected_matches is not None,
              _scan_detail)
    except Exception as e:
        _expected_matches = None
        check("4. Dual-manifest readability gate", 0, False, f"exception: {e}")


def check_5_rows_match_scan() -> None:
    """CVE Registry rows correspond exactly to the computed vulnerable match set.

    An empty match set requires an empty table; fabricated rows fail the check.
    Fixture limitation (verified on the current code-server-bundle image): the
    seeded manifests intersect the VULNERABLE table in zero pins, so the correct
    answer is an empty table and 7/7 is achievable without any audit footprint.
    Tightening the verifier cannot change that ceiling — it would require
    seeding vulnerable pins into the fixture manifests (out of scope here).
    """
    import re as _re
    try:
        if _br_table_id is None or _br_fields is None or _expected_matches is None:
            check("5. Rows match fixture scan", 3, False,
                  "prerequisites missing (table/fields/dual-manifest gate) — forced 0")
            return
        resp = req_lib.get(
            f"{BASEROW_URL}/api/database/rows/table/{_br_table_id}/?size=200",
            headers=_br_headers, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("results", [])
        all_fields = ["CVE ID", "Project", "Library Name", "Vulnerable Version",
                      "Fixed Version", "CVSS Score", "Severity", "Discovered Date"]

        def _s(v) -> str:
            return "" if v is None else str(v).strip()

        got = set()
        nonblank = 0
        row_fields: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            vals = {f: _get_field_value(row, f) for f in all_fields}
            # A blank placeholder row (Baserow auto-creates these) only counts
            # as blank when ALL 8 fields are empty/default; a row with just a
            # CVE ID or Severity filled is garbage, not a placeholder.
            if all(_s(v) in ("", "false", "False", "None") for v in vals.values()):
                continue
            nonblank += 1
            entry = (_s(vals["Project"]), _s(vals["Library Name"]),
                     _s(vals["Vulnerable Version"]))
            got.add(entry)
            row_fields[entry] = vals
        missing = _expected_matches - got
        extra = got - _expected_matches
        issues = []
        if nonblank != len(got):
            issues.append(f"{nonblank - len(got)} duplicate rows")
        # When GT is non-empty (future fixture changes), each expected row must
        # also carry a plausible CVE record, not just the matching triple.
        for entry in sorted(_expected_matches & got):
            vals = row_fields[entry]
            cve_id = _s(vals["CVE ID"])
            if not _re.fullmatch(r"CVE-\d{4}-\d{4,}", cve_id):
                issues.append(f"{entry[1]}: CVE ID {cve_id!r} not CVE-YYYY-NNNN")
            if not _s(vals["Severity"]):
                issues.append(f"{entry[1]}: Severity not selected")
            try:
                cvss = float(_s(vals["CVSS Score"]))
                if not 0.0 <= cvss <= 10.0:
                    raise ValueError
            except ValueError:
                issues.append(f"{entry[1]}: CVSS {_s(vals['CVSS Score'])!r} not in [0.0,10.0]")
            if not _s(vals["Discovered Date"]):
                issues.append(f"{entry[1]}: Discovered Date empty")
        passed = not missing and not extra and not issues
        detail = (f"expected {len(_expected_matches)} rows, got {len(got)}"
                  + (f"; missing {sorted(missing)[:3]}" if missing else "")
                  + (f"; unexpected {sorted(extra)[:3]}" if extra else "")
                  + (f"; {'; '.join(issues[:3])}" if issues else ""))
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
