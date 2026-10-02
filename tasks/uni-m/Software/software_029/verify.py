"""
Verifier for Software-029-I4: Data Platform Migration Portfolio Analysis Q4 2026

Checks: 19 weighted checks (total weight 38) across baserow, code-server,
metabase, openproject.
Strategy: Baserow API, docker exec filesystem, Metabase API, OpenProject DB.

Required env vars:
  SERVER_HOSTNAME, BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  METABASE_PORT, METABASE_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER.
"""

import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.request
import urllib.error

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
for var_name, var_val in _required.items():
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
METABASE_URL = f"http://{HOST}:{METABASE_PORT}"
OPENPROJECT_URL = f"http://{HOST}:{OPENPROJECT_PORT}"

# ── Expected data ─────────────────────────────────────────────────────────────
EXPECTED_CANDIDATES = [
    {"id": "MC-01", "name": "Migrate Hadoop Cluster to EMR Serverless",
     "current": 450000.00, "projected": 270000.00, "savings": 180000.00,
     "effort": 15.0, "risk": 5.5, "alignment": "High", "roi": 1.71, "decision": "Approve"},
    {"id": "MC-02", "name": "Replace Talend ETL with dbt Cloud",
     "current": 210000.00, "projected": 96000.00, "savings": 114000.00,
     "effort": 9.0, "risk": 4.0, "alignment": "High", "roi": 1.81, "decision": "Approve"},
    {"id": "MC-03", "name": "Migrate Tableau Server to Tableau Cloud",
     "current": 156000.00, "projected": 120000.00, "savings": 36000.00,
     "effort": 5.0, "risk": 3.5, "alignment": "Medium", "roi": 0.86, "decision": "Defer"},
    {"id": "MC-04", "name": "Retire Legacy SSIS Packages",
     "current": 78000.00, "projected": 72000.00, "savings": 6000.00,
     "effort": 7.0, "risk": 8.8, "alignment": "Low", "roi": 0.08, "decision": "Reject"},
    {"id": "MC-05", "name": "Move Airflow Self-hosted to MWAA",
     "current": 132000.00, "projected": 78000.00, "savings": 54000.00,
     "effort": 6.0, "risk": 4.5, "alignment": "Medium", "roi": 1.07, "decision": "Defer"},
    {"id": "MC-06", "name": "Consolidate Data Catalogs onto AWS Glue",
     "current": 96000.00, "projected": 84000.00, "savings": 12000.00,
     "effort": 8.0, "risk": 7.0, "alignment": "Low", "roi": 0.14, "decision": "Reject"},
]

APPROVED = [c for c in EXPECTED_CANDIDATES if c["decision"] == "Approve"]

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


def http_request(url: str, method: str = "GET", data: dict | None = None,
                 headers: dict | None = None, timeout: int = 15) -> tuple[int, dict | str]:
    """Make an HTTP request and return (status_code, parsed_json_or_text)."""
    hdrs = headers or {}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            try:
                return resp.status, json.loads(raw)
            except json.JSONDecodeError:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else ""
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw


def baserow_auth() -> str:
    """Authenticate to Baserow API and return JWT token."""
    status, resp = http_request(
        f"{BASEROW_URL}/api/user/token-auth/",
        method="POST",
        data={"email": "admin@example.com", "password": "Admin1234"},
    )
    if status != 200:
        raise RuntimeError(f"Baserow auth failed: {status} {resp}")
    return resp["access_token"]


def metabase_auth() -> str:
    """Authenticate to Metabase API and return session token."""
    status, resp = http_request(
        f"{METABASE_URL}/api/session",
        method="POST",
        data={"username": "admin@metabase.local", "password": "mw-admin-123"},
    )
    if status != 200:
        raise RuntimeError(f"Metabase auth failed: {status} {resp}")
    return resp["id"]


def op_db_query(sql: str) -> str:
    """Query OpenProject embedded Postgres via TCP with password auth."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"OpenProject DB query failed: {r.stderr.strip()}")
    return r.stdout.strip()


def baserow_sql(sql: str) -> str:
    """Run a SQL query against the Baserow Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc",
        "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"baserow_sql failed (rc={rc}): {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def _num_re(x: float, max_dec_zeros: int = 1) -> str:
    """Regex fragment for a numeric constant tolerating trailing-zero
    rendering variants: 15.0 -> '15' or '15.0' (or '15.00' when
    max_dec_zeros=2, e.g. 180000.00); non-integral constants must render
    exactly as stored (1.71)."""
    if float(x) == int(x):
        return rf"{int(x)}(?:\.0{{1,{max_dec_zeros}}})?"
    return re.escape(str(x))


# ── Baserow checks ───────────────────────────────────────────────────────────
_baserow_token = None
_baserow_table_id = None
_baserow_rows = None


def _get_baserow_token():
    global _baserow_token
    if _baserow_token is None:
        _baserow_token = baserow_auth()
    return _baserow_token


def _baserow_get(path: str) -> tuple[int, dict | str]:
    token = _get_baserow_token()
    return http_request(
        f"{BASEROW_URL}{path}",
        headers={"Authorization": f"JWT {token}"},
    )


def check_1_baserow_database_exists() -> None:
    """Check that the Baserow database 'Data Platform Migration Portfolio Q4 2026' exists."""
    try:
        status, apps = _baserow_get("/api/applications/")
        if status != 200:
            check("1. Baserow database exists", 1, False, f"API returned {status}")
            return
        db_name = "Data Platform Migration Portfolio Q4 2026"
        found = [a for a in apps if a.get("name") == db_name and a.get("type") == "database"]
        if found:
            check("1. Baserow database exists", 1, True)
        else:
            names = [a.get("name") for a in apps if a.get("type") == "database"]
            check("1. Baserow database exists", 1, False, f"not found among {names}")
    except Exception as e:
        check("1. Baserow database exists", 1, False, f"exception: {e}")


def _find_table_id() -> int | None:
    """Find the table ID for 'Migration Candidates' in the target database."""
    global _baserow_table_id
    if _baserow_table_id is not None:
        return _baserow_table_id
    status, apps = _baserow_get("/api/applications/")
    if status != 200:
        return None
    db_name = "Data Platform Migration Portfolio Q4 2026"
    for app in apps:
        if app.get("name") == db_name and app.get("type") == "database":
            for tbl in app.get("tables", []):
                if tbl.get("name") == "Migration Candidates":
                    _baserow_table_id = tbl["id"]
                    return _baserow_table_id
    return None


def _get_rows() -> list[dict] | None:
    """Fetch all rows from the Migration Candidates table with user field names."""
    global _baserow_rows
    if _baserow_rows is not None:
        return _baserow_rows
    table_id = _find_table_id()
    if table_id is None:
        return None
    status, resp = _baserow_get(f"/api/database/rows/table/{table_id}/?user_field_names=true&size=100")
    if status != 200:
        return None
    _baserow_rows = resp.get("results", [])
    return _baserow_rows


_baserow_fields = None


def _get_table_fields() -> list[dict] | None:
    """Fetch (once) the field definitions of the Migration Candidates table."""
    global _baserow_fields
    if _baserow_fields is not None:
        return _baserow_fields
    table_id = _find_table_id()
    if table_id is None:
        return None
    status, resp = _baserow_get(f"/api/database/fields/table/{table_id}/")
    if status != 200 or not isinstance(resp, list):
        return None
    _baserow_fields = resp
    return _baserow_fields


def _field_id(name: str) -> int | None:
    for f in _get_table_fields() or []:
        if f.get("name") == name:
            return f.get("id")
    return None


def check_2_table_and_row_count() -> None:
    """Check table exists with exactly 6 rows."""
    try:
        table_id = _find_table_id()
        if table_id is None:
            check("2. Table 'Migration Candidates' with 6 rows", 1, False, "table not found")
            return
        rows = _get_rows()
        if rows is None:
            check("2. Table 'Migration Candidates' with 6 rows", 1, False, "could not fetch rows")
            return
        count = len(rows)
        check("2. Table 'Migration Candidates' with 6 rows", 1, count == 6,
              f"found {count} rows")
    except Exception as e:
        check("2. Table 'Migration Candidates' with 6 rows", 1, False, f"exception: {e}")


def _match_row(rows: list[dict], candidate_id: str) -> dict | None:
    """Find a row matching the given candidate ID."""
    for row in rows:
        # Try common field name variants
        for key in ["Candidate ID", "candidate_id", "Candidate Id"]:
            if row.get(key) == candidate_id:
                return row
        # Also check the primary field (first visible text) — Baserow might use 'order' key
        # or the value might be in the first text column
        for key, val in row.items():
            if isinstance(val, str) and val.strip() == candidate_id:
                return row
    return None


def _get_field_value(row: dict, *field_names: str) -> str | float | None:
    """Get field value trying multiple name variants."""
    for name in field_names:
        if name in row:
            val = row[name]
            # Single-select fields in Baserow API return {"id": ..., "value": ..., "color": ...}
            if isinstance(val, dict) and "value" in val:
                return val["value"]
            return val
    return None


def check_3_annual_savings() -> None:
    """Verify Annual Savings = Current - Projected for all 6 rows."""
    try:
        rows = _get_rows()
        if not rows:
            check("3. Annual Savings correct", 3, False, "no rows available")
            return
        errors = []
        for cand in EXPECTED_CANDIDATES:
            row = _match_row(rows, cand["id"])
            if row is None:
                errors.append(f"{cand['id']} not found")
                continue
            savings = _get_field_value(row, "Annual Savings", "annual_savings")
            if savings is None:
                errors.append(f"{cand['id']}: field not found")
                continue
            try:
                actual = float(savings)
            except (ValueError, TypeError):
                errors.append(f"{cand['id']}: non-numeric '{savings}'")
                continue
            if abs(actual - cand["savings"]) > 0.1:
                errors.append(f"{cand['id']}: expected {cand['savings']}, got {actual}")
        passed = len(errors) == 0
        check("3. Annual Savings correct", 3, passed,
              "; ".join(errors) if errors else "all 6 correct")
    except Exception as e:
        check("3. Annual Savings correct", 3, False, f"exception: {e}")


def check_4_roi_score() -> None:
    """Verify ROI Score for all 6 rows."""
    try:
        rows = _get_rows()
        if not rows:
            check("4. ROI Score correct", 3, False, "no rows available")
            return
        errors = []
        for cand in EXPECTED_CANDIDATES:
            row = _match_row(rows, cand["id"])
            if row is None:
                errors.append(f"{cand['id']} not found")
                continue
            roi = _get_field_value(row, "ROI Score", "roi_score")
            if roi is None:
                errors.append(f"{cand['id']}: field not found")
                continue
            try:
                actual = float(roi)
            except (ValueError, TypeError):
                errors.append(f"{cand['id']}: non-numeric '{roi}'")
                continue
            if abs(actual - cand["roi"]) > 0.02:
                errors.append(f"{cand['id']}: expected {cand['roi']}, got {actual}")
        passed = len(errors) == 0
        check("4. ROI Score correct", 3, passed,
              "; ".join(errors) if errors else "all 6 correct")
    except Exception as e:
        check("4. ROI Score correct", 3, False, f"exception: {e}")


def check_5_decision() -> None:
    """Verify Decision field for all 6 rows."""
    try:
        rows = _get_rows()
        if not rows:
            check("5. Decision correct", 2, False, "no rows available")
            return
        errors = []
        for cand in EXPECTED_CANDIDATES:
            row = _match_row(rows, cand["id"])
            if row is None:
                errors.append(f"{cand['id']} not found")
                continue
            decision = _get_field_value(row, "Decision", "decision")
            if decision is None:
                errors.append(f"{cand['id']}: field not found")
                continue
            actual = str(decision).strip()
            if actual.lower() != cand["decision"].lower():
                errors.append(f"{cand['id']}: expected {cand['decision']}, got {actual}")
        passed = len(errors) == 0
        check("5. Decision correct", 2, passed,
              "; ".join(errors) if errors else "all 6 correct")
    except Exception as e:
        check("5. Decision correct", 2, False, f"exception: {e}")


def check_6_ranked_candidates_view() -> None:
    """Grid view 'Ranked Candidates' exists and carries exactly one sorting:
    ROI Score descending."""
    try:
        table_id = _find_table_id()
        if table_id is None:
            check("6. View 'Ranked Candidates' + sort", 2, False, "table not found")
            return
        status, views = _baserow_get(f"/api/database/views/table/{table_id}/")
        if status != 200:
            check("6. View 'Ranked Candidates' + sort", 2, False, f"API returned {status}")
            return
        views = views.get("results", views) if isinstance(views, dict) else views
        view = next((v for v in views if v.get("name") == "Ranked Candidates"), None)
        if view is None:
            check("6. View 'Ranked Candidates' + sort", 2, False,
                  f"view not found among {[v.get('name') for v in views]}")
            return
        roi_fid = _field_id("ROI Score")
        if roi_fid is None:
            check("6. View 'Ranked Candidates' + sort", 2, False,
                  "ROI Score field not found")
            return
        status, sortings = _baserow_get(f"/api/database/views/{view['id']}/sortings/")
        if status != 200:
            check("6. View 'Ranked Candidates' + sort", 2, False,
                  f"sortings API returned {status}")
            return
        sortings = sortings.get("results", sortings) if isinstance(sortings, dict) else sortings
        got = [(s.get("field"), str(s.get("order", "")).upper()) for s in sortings]
        ok = got == [(roi_fid, "DESC")]
        check("6. View 'Ranked Candidates' + sort", 2, ok,
              f"sortings={got}, expected [({roi_fid}, 'DESC')] (ROI Score DESC)")
    except Exception as e:
        check("6. View 'Ranked Candidates' + sort", 2, False, f"exception: {e}")


# Field spec: name -> (type, select option set or None, decimal places or None)
_MC_FIELD_SPEC = {
    "Candidate ID": ("text", None, None),
    "Candidate Name": ("text", None, None),
    "Current Annual Cost": ("number", None, 2),
    "Projected Annual Cost": ("number", None, 2),
    "Annual Savings": ("number", None, 2),
    "Effort Weeks": ("number", None, 1),
    "Risk Score": ("number", None, 1),
    "Strategic Alignment": ("single_select", {"Low", "Medium", "High"}, None),
    "ROI Score": ("number", None, 2),
    "Decision": ("single_select", {"Approve", "Defer", "Reject"}, None),
}


def check_6b_field_schema() -> None:
    """Migration Candidates fields carry the task's types, decimal places and
    select options, with Candidate ID as primary (REST fields API)."""
    try:
        fields = _get_table_fields()
        if not fields:
            check("6b. Field schema", 2, False, "table fields not loaded")
            return
        by_name = {f.get("name"): f for f in fields}
        issues = []
        for name, (ftype, options, decimals) in _MC_FIELD_SPEC.items():
            f = by_name.get(name)
            if f is None:
                issues.append(f"{name}: field missing")
                continue
            if f.get("type") != ftype:
                issues.append(f"{name}: type={f.get('type')!r}, expected {ftype!r}")
            if options is not None:
                got = {o.get("value") for o in f.get("select_options", [])}
                if got != options:
                    issues.append(f"{name}: options={sorted(got)}, expected {sorted(options)}")
            if decimals is not None and f.get("number_decimal_places") != decimals:
                issues.append(f"{name}: decimals={f.get('number_decimal_places')}, "
                              f"expected {decimals}")
            if name == "Candidate ID" and not f.get("primary"):
                issues.append("Candidate ID: not the primary field")
        check("6b. Field schema", 2, not issues,
              "schema OK" if not issues else "; ".join(issues[:5]))
    except Exception as e:
        check("6b. Field schema", 2, False, f"exception: {e}")


# ── Code-server checks ───────────────────────────────────────────────────────
def _find_alertmanager_file() -> str | None:
    """Find the alertmanager.yml file path in the code-server container."""
    rc, out, _ = docker_exec(
        CODE_SERVER_CONTAINER,
        "find", "/home", "-maxdepth", "6", "-path", "*/devops-configs/monitoring/alertmanager.yml",
        "-type", "f",
        timeout=10,
    )
    if rc == 0 and out.strip():
        return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep.split("\n")[0]
    # Also try /config/workspace or other common paths
    rc2, out2, _ = docker_exec(
        CODE_SERVER_CONTAINER,
        "find", "/", "-maxdepth", "6", "-path", "*/devops-configs/monitoring/alertmanager.yml",
        "-type", "f",
        timeout=10,
    )
    if rc2 == 0 and out2.strip():
        return out2.strip().split("\n")[0]
    return None


def check_7_section_marker() -> None:
    """Check alertmanager.yml contains the section marker."""
    try:
        filepath = _find_alertmanager_file()
        if filepath is None:
            check("7. Section marker in alertmanager.yml", 1, False, "file not found")
            return
        rc, content, _ = docker_exec(CODE_SERVER_CONTAINER, "cat", filepath)
        if rc != 0:
            check("7. Section marker in alertmanager.yml", 1, False, "cannot read file")
            return
        marker = "# === DATA PLATFORM MIGRATION Q4 NOTES ==="
        found = marker in content
        check("7. Section marker in alertmanager.yml", 1, found,
              "" if found else "marker line not found")
    except Exception as e:
        check("7. Section marker in alertmanager.yml", 1, False, f"exception: {e}")


def check_8_comment_lines() -> None:
    """Exactly the 6 template comment lines sit immediately below the marker:
    lines i+1..i+6 must be MC-01..MC-06 in strict order, each matching the
    template '# MIGRATION-CANDIDATE {id}: {name} — Effort {effort}w, ROI
    {roi}, Decision {decision}' (NFC-normalized; the em dash is required,
    '--' is rejected; trailing-zero numeric variants tolerated)."""
    try:
        filepath = _find_alertmanager_file()
        if filepath is None:
            check("8. 6 migration comment lines", 3, False, "file not found")
            return
        rc, content, _ = docker_exec(CODE_SERVER_CONTAINER, "cat", filepath)
        if rc != 0:
            check("8. 6 migration comment lines", 3, False, "cannot read file")
            return
        marker = "# === DATA PLATFORM MIGRATION Q4 NOTES ==="
        lines = content.split("\n")
        marker_idx = None
        for i, line in enumerate(lines):
            if marker in line:
                marker_idx = i
                break
        if marker_idx is None:
            check("8. 6 migration comment lines", 3, False, "marker not found")
            return

        window = lines[marker_idx + 1:marker_idx + 7]
        errors = []
        if len(window) < 6:
            errors.append(f"only {len(window)} line(s) after marker")
        for k, cand in enumerate(EXPECTED_CANDIDATES):
            if k >= len(window):
                errors.append(f"{cand['id']}: line missing")
                continue
            actual = unicodedata.normalize("NFC", window[k]).strip()
            pattern = (
                rf"^# MIGRATION-CANDIDATE {re.escape(cand['id'])}: "
                rf"{re.escape(cand['name'])} — "
                rf"Effort {_num_re(cand['effort'])}w, "
                rf"ROI {_num_re(cand['roi'])}, "
                rf"Decision {re.escape(cand['decision'])}$"
            )
            if not re.match(pattern, actual):
                errors.append(f"line marker+{k + 1} is not the {cand['id']} "
                              f"template line: {actual!r:.90}")
        passed = len(errors) == 0
        check("8. 6 migration comment lines", 3, passed,
              "; ".join(errors[:3]) if errors else
              "6 anchored template lines exact (MC-01..MC-06)")
    except Exception as e:
        check("8. 6 migration comment lines", 3, False, f"exception: {e}")


# ── Metabase checks ──────────────────────────────────────────────────────────
_metabase_session = None
_metabase_collection_id = None


def _get_metabase_session():
    global _metabase_session
    if _metabase_session is None:
        _metabase_session = metabase_auth()
    return _metabase_session


def _metabase_get(path: str) -> tuple[int, dict | str | list]:
    session = _get_metabase_session()
    return http_request(
        f"{METABASE_URL}{path}",
        headers={"X-Metabase-Session": session},
    )


def _metabase_post(path: str, data: dict | None = None,
                   timeout: int = 60) -> tuple[int, dict | str]:
    session = _get_metabase_session()
    return http_request(
        f"{METABASE_URL}/api/{path}",
        method="POST",
        data=data if data is not None else {},
        headers={"X-Metabase-Session": session},
        timeout=timeout,
    )


def _find_metabase_collection() -> int | None:
    global _metabase_collection_id
    if _metabase_collection_id is not None:
        return _metabase_collection_id
    status, collections = _metabase_get("/api/collection")
    if status != 200:
        return None
    target = "Data Platform Migration Analysis Q4 2026"
    for c in collections:
        if c.get("name") == target:
            _metabase_collection_id = c["id"]
            return _metabase_collection_id
    return None


# ── Metabase card normalization helpers ──────────────────────────────────────
# Probe-verified against mw-metabase:latest (Metabase v0.58.5.2): GET
# /api/card/<id> returns dataset_query in pMBQL ({"lib/type":"mbql/query",
# "database":<id>,"stages":[...]}); field refs carry the integer id in the
# LAST position; legacy MBQL POSTs are normalized to pMBQL on readback.


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
    Raises RuntimeError on failure (one retry for sync lag). Works for native
    SQL cards too."""
    last_err = None
    for attempt in range(2):
        try:
            status, res = _metabase_post(f"card/{card_id}/query", timeout=60)
            if isinstance(res, dict) and res.get("status") == "completed":
                data = res.get("data") or {}
                cols = [c.get("name") for c in data.get("cols", [])]
                return cols, data.get("rows", [])
            if isinstance(res, dict):
                last_err = (f"status={res.get('status')} "
                            f"error={str(res.get('error'))[:200]}")
            else:
                last_err = f"http={status} body={str(res)[:200]}"
        except Exception as e:  # noqa: BLE001
            last_err = str(e)
        if attempt == 0:
            time.sleep(5)
    raise RuntimeError(f"card {card_id} execution failed: {last_err}")


def mb_find_pg_db(name: str | None = None) -> dict | None:
    """Locate the Baserow Postgres datasource. Never hardcode db ids."""
    status, resp = _metabase_get("/api/database")
    if status != 200:
        return None
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


def mb_resolve_table(mb_db_id: int, physical_table_name: str) -> tuple[int | None, dict]:
    """(mb_table_id, {physical_field_name: mb_field_id}) via GET
    /api/database/<id>/metadata. physical_table_name is e.g.
    f"database_table_{tid}"; field names are f"field_{baserow_fid}"."""
    status, meta = _metabase_get(f"/api/database/{mb_db_id}/metadata")
    if status != 200 or not isinstance(meta, dict):
        return None, {}
    for tbl in meta.get("tables", []) or []:
        if tbl.get("name") == physical_table_name:
            fmap = {f.get("name"): f.get("id") for f in tbl.get("fields", []) or []}
            return tbl.get("id"), fmap
    return None, {}


def check_9_metabase_collection() -> None:
    """Check that the Metabase collection exists."""
    try:
        cid = _find_metabase_collection()
        check("9. Metabase collection exists", 1, cid is not None,
              "" if cid else "collection not found")
    except Exception as e:
        check("9. Metabase collection exists", 1, False, f"exception: {e}")


_EXPECTED_CARD_NAMES = [
    "Effort vs ROI",
    "Decisions Breakdown",
    "Total Projected Annual Savings (Approved)",
]

_collection_cards = None


def _collection_card_ids() -> dict | None:
    """{card name: card id} for the target collection (cached)."""
    global _collection_cards
    if _collection_cards is not None:
        return _collection_cards
    cid = _find_metabase_collection()
    if cid is None:
        return None
    status, items = _metabase_get(f"/api/collection/{cid}/items?models=card")
    if status != 200:
        return None
    item_data = items.get("data", items) if isinstance(items, dict) else items
    _collection_cards = {
        item.get("name"): item.get("id")
        for item in item_data if item.get("model") == "card"
    }
    return _collection_cards


_decision_opts = None


def _decision_option_map() -> dict:
    """{option id (str): option text} for the Decision single-select field.
    Baserow's physical single-select columns (and MBQL filter values) store
    option ids, so executed rows/filters must be normalized to text."""
    global _decision_opts
    if _decision_opts is not None:
        return _decision_opts
    fid = _field_id("Decision")
    out = {}
    if fid is not None:
        try:
            rows = baserow_sql(
                f"SELECT id, value FROM database_selectoption WHERE field_id = {fid}"
            )
            for line in rows.split("\n"):
                line = line.strip()
                if line and "|" in line:
                    oid, _, val = line.partition("|")
                    out[oid.strip()] = val
        except RuntimeError:
            # Fallback: the REST fields API carries the same id/value pairs.
            for f in _get_table_fields() or []:
                if f.get("id") == fid:
                    for o in f.get("select_options", []):
                        out[str(o.get("id"))] = o.get("value")
    _decision_opts = out
    return out


def _norm_decision(cell) -> str:
    """Map an executed cell (option id as int/float/str, or plain text) to
    the Decision option text."""
    if isinstance(cell, float) and cell == int(cell):
        cell = int(cell)
    return _decision_option_map().get(str(cell).strip(), str(cell).strip())


def _scatter_pairs_from_execution(card_id: int) -> str | None:
    """Execute a (native) scatter card; some column pair must equal the 6
    expected (effort, roi) pairs as a multiset. Returns an error string or
    None on success."""
    expected = sorted(
        (round(c["effort"], 2), round(c["roi"], 2)) for c in EXPECTED_CANDIDATES
    )
    try:
        _cols, rows = mb_run_card(card_id)
    except RuntimeError as e:
        return str(e)
    ncols = max((len(r) for r in rows), default=0)
    for ai in range(ncols):
        for bi in range(ncols):
            if ai == bi:
                continue
            pairs = []
            try:
                for r in rows:
                    pairs.append((round(float(r[ai]), 2), round(float(r[bi]), 2)))
            except (TypeError, ValueError, IndexError):
                continue
            if sorted(pairs) == expected:
                return None
    return (f"no executed column pair matches the 6 (effort, roi) pairs; "
            f"got {len(rows)} row(s)")


def _pie_rows_from_execution(card_id: int) -> str | None:
    """Execute a (native) pie card; rows must equal the per-Decision counts
    computed from EXPECTED_CANDIDATES (option ids normalized to text)."""
    expected = {}
    for c in EXPECTED_CANDIDATES:
        expected[c["decision"]] = expected.get(c["decision"], 0) + 1
    try:
        _cols, rows = mb_run_card(card_id)
    except RuntimeError as e:
        return str(e)
    got = {}
    try:
        for r in rows:
            got[_norm_decision(r[0])] = got.get(_norm_decision(r[0]), 0) + int(float(r[1]))
    except (TypeError, ValueError, IndexError):
        return f"cannot parse executed rows as (decision, count): {rows[:4]}"
    if got != expected:
        return f"executed decision counts {got} != expected {expected}"
    return None


def check_10_metabase_questions() -> None:
    """The 3 questions exist AND are semantically correct: scatter binds
    Effort Weeks + ROI Score (MBQL fids or viz dimensions/metrics; native
    fallback by execution), pie is count broken out by Decision (native
    fallback by execution), scalar is sum(Annual Savings) filtered to
    Decision=Approve and EXECUTES to the constant-derived total; all three
    query the Baserow Postgres datasource."""
    W = 4
    try:
        cards = _collection_card_ids()
        if cards is None:
            check("10. 3 Metabase questions correct", W, False,
                  "collection not found / cannot list cards")
            return
        missing = [n for n in _EXPECTED_CARD_NAMES if n not in cards]
        if missing:
            check("10. 3 Metabase questions correct", W, False,
                  f"missing cards: {missing}")
            return

        # Resolution chain (never hardcoded): Baserow table/field ids ->
        # physical names database_table_<tid>/field_<fid> -> Metabase table/
        # field ids via GET /api/database/<db_id>/metadata.
        table_id = _find_table_id()
        needed = ["Effort Weeks", "ROI Score", "Decision", "Annual Savings"]
        br_fid = {n: _field_id(n) for n in needed}
        if table_id is None or any(v is None for v in br_fid.values()):
            check("10. 3 Metabase questions correct", W, False,
                  f"cannot resolve Baserow table/fields: table={table_id}, fids={br_fid}")
            return
        pg_db = mb_find_pg_db()
        if pg_db is None:
            check("10. 3 Metabase questions correct", W, False,
                  "no postgres datasource for the 'baserow' DB in Metabase")
            return
        pg_db_id = pg_db.get("id")
        mb_tid, fmap = mb_resolve_table(pg_db_id, f"database_table_{table_id}")
        col = {n: f"field_{br_fid[n]}" for n in needed}
        mb_fid = {n: fmap.get(col[n]) for n in needed}
        sync_ok = mb_tid is not None and all(v is not None for v in mb_fid.values())

        issues = []
        shapes = {}
        for name in _EXPECTED_CARD_NAMES:
            status, card = _metabase_get(f"/api/card/{cards[name]}")
            if status != 200 or not isinstance(card, dict):
                issues.append(f"{name}: GET card -> {status}")
                shapes[name] = None
            else:
                shapes[name] = mb_shape(card)

        def _refs_column(strings, logical) -> bool:
            targets = {col[logical].lower(), logical.lower()}
            return any(str(s).lower() in targets for s in strings)

        # 1) "Effort vs ROI" — scatter over Effort Weeks / ROI Score.
        sh = shapes.get("Effort vs ROI")
        if sh is not None:
            if sh["database"] != pg_db_id:
                issues.append(f"Effort vs ROI: database={sh['database']}, expected {pg_db_id}")
            if sh["display"] != "scatter":
                issues.append(f"Effort vs ROI: display={sh['display']!r}, expected 'scatter'")
            if not sh["is_native"]:
                if not sync_ok:
                    issues.append("Effort vs ROI: Migration Candidates table not "
                                  "synced into Metabase (cannot resolve field ids)")
                else:
                    fids = set(sh["breakout_fids"]) | set(sh["fields_fids"])
                    mbql_ok = (sh["source_table"] == mb_tid
                               and mb_fid["Effort Weeks"] in fids
                               and mb_fid["ROI Score"] in fids)
                    viz = sh["visualization_settings"]
                    axes = list(viz.get("graph.dimensions") or []) + \
                        list(viz.get("graph.metrics") or [])
                    viz_ok = (sh["source_table"] == mb_tid
                              and _refs_column(axes, "Effort Weeks")
                              and _refs_column(axes, "ROI Score"))
                    if not (mbql_ok or viz_ok):
                        issues.append("Effort vs ROI: neither MBQL breakout/fields "
                                      "nor viz dimensions/metrics bind "
                                      "Effort Weeks + ROI Score")
            else:
                err = _scatter_pairs_from_execution(cards["Effort vs ROI"])
                if err:
                    issues.append(f"Effort vs ROI (native): {err}")

        # 2) "Decisions Breakdown" — pie of count by Decision.
        sh = shapes.get("Decisions Breakdown")
        if sh is not None:
            if sh["database"] != pg_db_id:
                issues.append(f"Decisions Breakdown: database={sh['database']}, expected {pg_db_id}")
            if sh["display"] != "pie":
                issues.append(f"Decisions Breakdown: display={sh['display']!r}, expected 'pie'")
            if not sh["is_native"]:
                if not sync_ok:
                    issues.append("Decisions Breakdown: table not synced into Metabase")
                else:
                    if sh["source_table"] != mb_tid:
                        issues.append(f"Decisions Breakdown: source_table={sh['source_table']}, expected {mb_tid}")
                    if sh["agg_ops"] != {"count"}:
                        issues.append(f"Decisions Breakdown: agg={sh['agg_ops']}, expected count")
                    if sh["breakout_fids"] != [mb_fid["Decision"]]:
                        issues.append(f"Decisions Breakdown: breakout={sh['breakout_fids']}, "
                                      f"expected [{mb_fid['Decision']}] (Decision)")
            else:
                err = _pie_rows_from_execution(cards["Decisions Breakdown"])
                if err:
                    issues.append(f"Decisions Breakdown (native): {err}")

        # 3) Scalar — sum(Annual Savings) where Decision = Approve. The
        # execution assertion applies to MBQL and native forms alike.
        scalar_name = "Total Projected Annual Savings (Approved)"
        sh = shapes.get(scalar_name)
        if sh is not None:
            if sh["database"] != pg_db_id:
                issues.append(f"scalar: database={sh['database']}, expected {pg_db_id}")
            if sh["display"] != "scalar":
                issues.append(f"scalar: display={sh['display']!r}, expected 'scalar'")
            if not sh["is_native"]:
                if not sync_ok:
                    issues.append("scalar: table not synced into Metabase")
                else:
                    if sh["source_table"] != mb_tid:
                        issues.append(f"scalar: source_table={sh['source_table']}, expected {mb_tid}")
                    if sh["agg_ops"] != {("sum", mb_fid["Annual Savings"])}:
                        issues.append(f"scalar: agg={sh['agg_ops']}, expected "
                                      f"sum(fid {mb_fid['Annual Savings']} = Annual Savings)")
                    fsum = mb_filter_summary(sh["filters"])
                    approve_ids = {oid for oid, val in _decision_option_map().items()
                                   if val == "Approve"}
                    filter_ok = False
                    if len(fsum) == 1:
                        op, fid, vals = fsum[0]
                        if op == "=" and fid == mb_fid["Decision"] and len(vals) == 1:
                            v = vals[0]
                            if isinstance(v, float) and v == int(v):
                                v = int(v)
                            # single-select filter value may be the option id
                            # or the option text — accept both.
                            filter_ok = str(v) == "Approve" or str(v) in approve_ids
                    if not filter_ok:
                        issues.append(f"scalar: filter {fsum} != Decision == 'Approve'")
            expected_total = round(sum(c["savings"] for c in APPROVED), 2)
            try:
                _cols, rows = mb_run_card(cards[scalar_name])
                if len(rows) != 1 or not rows[0]:
                    issues.append(f"scalar execution returned {len(rows)} row(s), expected 1")
                else:
                    val = float(rows[0][0])
                    if abs(val - expected_total) > 0.01:
                        issues.append(f"scalar executes to {val}, expected {expected_total}")
            except (RuntimeError, TypeError, ValueError) as e:
                issues.append(f"scalar execution failed: {e}")

        passed = len(issues) == 0
        check("10. 3 Metabase questions correct", W, passed,
              "; ".join(issues[:4]) if issues else
              f"all 3 cards verified against pg db {pg_db_id} "
              f"(table database_table_{table_id})")
    except Exception as e:
        check("10. 3 Metabase questions correct", W, False, f"exception: {e}")


def check_11_metabase_dashboard() -> None:
    """Check dashboard exists with correct name and description."""
    try:
        cid = _find_metabase_collection()
        if cid is None:
            check("11. Metabase dashboard exists with description", 2, False, "collection not found")
            return
        status, items = _metabase_get(f"/api/collection/{cid}/items?models=dashboard")
        if status != 200:
            check("11. Metabase dashboard exists with description", 2, False, f"API returned {status}")
            return
        item_data = items.get("data", items) if isinstance(items, dict) else items
        dashboards = [d for d in item_data if d.get("model") == "dashboard"]
        target_name = "Data Platform Migration Dashboard Q4 2026"
        target_desc = "Platform migration portfolio as of 2026-10-08"
        found_dash = None
        for d in dashboards:
            if d.get("name") == target_name:
                found_dash = d
                break
        if found_dash is None:
            check("11. Metabase dashboard exists with description", 2, False,
                  f"dashboard not found; found: {[d.get('name') for d in dashboards]}")
            return
        # Fetch full dashboard to get description
        dash_id = found_dash["id"]
        status2, dash_detail = _metabase_get(f"/api/dashboard/{dash_id}")
        if status2 != 200:
            check("11. Metabase dashboard exists with description", 2, False, f"cannot fetch dashboard detail: {status2}")
            return
        actual_desc = (dash_detail.get("description") or "").strip()
        passed = actual_desc == target_desc
        check("11. Metabase dashboard exists with description", 2, passed,
              f"description: '{actual_desc}'" if not passed else "")
    except Exception as e:
        check("11. Metabase dashboard exists with description", 2, False, f"exception: {e}")


def check_12_dashboard_cards() -> None:
    """Check dashboard has 3 cards."""
    try:
        cid = _find_metabase_collection()
        if cid is None:
            check("12. Dashboard has 3 cards", 1, False, "collection not found")
            return
        status, items = _metabase_get(f"/api/collection/{cid}/items?models=dashboard")
        if status != 200:
            check("12. Dashboard has 3 cards", 1, False, f"API returned {status}")
            return
        item_data = items.get("data", items) if isinstance(items, dict) else items
        target_name = "Data Platform Migration Dashboard Q4 2026"
        dash_id = None
        for d in item_data:
            if d.get("name") == target_name and d.get("model") == "dashboard":
                dash_id = d["id"]
                break
        if dash_id is None:
            check("12. Dashboard has 3 cards", 1, False, "dashboard not found")
            return
        status2, dash_detail = _metabase_get(f"/api/dashboard/{dash_id}")
        if status2 != 200:
            check("12. Dashboard has 3 cards", 1, False, f"cannot fetch dashboard: {status2}")
            return
        # Question cards only (not text/heading cards), bound by card id
        # where available and resolved to the card's name.
        dashcards = dash_detail.get("dashcards", dash_detail.get("ordered_cards", []))
        id_to_name = {v: k for k, v in (_collection_card_ids() or {}).items()}
        names = []
        for c in dashcards:
            card_obj = c.get("card") or {}
            card_id = c.get("card_id") or card_obj.get("id")
            if not card_id:
                continue
            name = id_to_name.get(card_id) or card_obj.get("name")
            if name is None:
                status3, card_json = _metabase_get(f"/api/card/{card_id}")
                name = (card_json.get("name")
                        if status3 == 200 and isinstance(card_json, dict)
                        else f"<card {card_id}>")
            names.append(name)
        expected = set(_EXPECTED_CARD_NAMES)
        ok = set(names) == expected and len(names) == 3
        check("12. Dashboard has 3 cards", 1, ok,
              f"dashcards={sorted(names)}, expected exactly {sorted(expected)}")
    except Exception as e:
        check("12. Dashboard has 3 cards", 1, False, f"exception: {e}")


# ── OpenProject checks ───────────────────────────────────────────────────────
def check_13_op_version() -> None:
    """Check version 'Migration-Portfolio-2026-10-08' exists with correct dates."""
    try:
        sql = (
            "SELECT v.name, v.start_date, v.effective_date, v.status "
            "FROM versions v "
            "JOIN projects p ON v.project_id = p.id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND v.name = 'Migration-Portfolio-2026-10-08'"
        )
        result = op_db_query(sql)
        if not result:
            check("13. OpenProject version exists", 2, False, "version not found")
            return
        parts = result.split("|")
        errors = []
        if len(parts) >= 3:
            start_date = parts[1].strip()
            due_date = parts[2].strip()
            if start_date != "2026-10-08":
                errors.append(f"start_date={start_date}, expected 2026-10-08")
            if due_date != "2027-03-31":
                errors.append(f"due_date={due_date}, expected 2027-03-31")
        else:
            errors.append(f"unexpected format: {result}")
        if len(parts) >= 4:
            status_val = parts[3].strip()
            if status_val != "open":
                errors.append(f"status={status_val}, expected open")
        passed = len(errors) == 0
        check("13. OpenProject version exists", 2, passed,
              "; ".join(errors) if errors else "")
    except Exception as e:
        check("13. OpenProject version exists", 2, False, f"exception: {e}")


def _expected_epic_subjects() -> list[str]:
    return [f"Migrate: {c['name']}" for c in APPROVED]


def check_14_op_epic_subjects() -> None:
    """The set of Epic subjects LIKE 'Migrate:%' (type Epic) equals exactly
    the expected Approve-derived subjects — extras fail."""
    try:
        sql = (
            "SELECT wp.subject "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND t.name = 'Epic' "
            "AND wp.subject LIKE 'Migrate:%' "
            "ORDER BY wp.subject"
        )
        result = op_db_query(sql)
        subjects = {s.strip() for s in result.split("\n") if s.strip()}
        expected = set(_expected_epic_subjects())
        missing = sorted(expected - subjects)
        extra = sorted(subjects - expected)
        passed = not missing and not extra
        detail = f"{len(subjects)} 'Migrate:' epic(s)"
        if missing:
            detail += f"; missing: {missing}"
        if extra:
            detail += f"; extra: {extra}"
        if passed:
            detail = "exactly the 2 expected epics"
        check("14. 2 Epic work packages for approved candidates", 2, passed, detail)
    except Exception as e:
        check("14. 2 Epic work packages for approved candidates", 2, False, f"exception: {e}")


def check_15_op_descriptions() -> None:
    """Each approved Epic's description (fetched per id) matches the full
    anchored template 'Effort: <E> weeks; Annual Savings: <S>; ROI: <R>;
    Strategic Alignment: <A>' with tolerant numeric variants."""
    try:
        errors = []
        for cand in APPROVED:
            subj = f"Migrate: {cand['name']}".replace("'", "''")
            wp_id = op_db_query(
                "SELECT wp.id FROM work_packages wp "
                "JOIN projects p ON wp.project_id = p.id "
                "JOIN types t ON wp.type_id = t.id "
                "WHERE p.identifier = 'data-analytics-pipeline' "
                "AND t.name = 'Epic' "
                f"AND wp.subject = '{subj}' ORDER BY wp.id LIMIT 1"
            )
            if not wp_id:
                errors.append(f"{cand['id']}: epic 'Migrate: {cand['name']}' not found")
                continue
            desc = op_db_query(
                f"SELECT COALESCE(description, '') FROM work_packages WHERE id = {wp_id}"
            )
            desc_clean = re.sub(r"\s+", " ", desc).strip()
            pattern = (
                rf"^Effort: {_num_re(cand['effort'])} weeks; "
                rf"Annual Savings: {_num_re(cand['savings'], max_dec_zeros=2)}; "
                rf"ROI: {_num_re(cand['roi'])}; "
                rf"Strategic Alignment: {re.escape(cand['alignment'])}$"
            )
            if not re.match(pattern, desc_clean):
                errors.append(f"{cand['id']}: desc={desc_clean!r:.90} "
                              f"does not match the exact template")
        passed = len(errors) == 0
        check("15. Epic descriptions correct", 2, passed,
              "; ".join(errors) if errors else "both descriptions match the template")
    except Exception as e:
        check("15. Epic descriptions correct", 2, False, f"exception: {e}")


def check_16_op_priorities() -> None:
    """Check work package priorities: High for Hadoop (risk>=5.0), Normal for Talend."""
    try:
        sql = (
            "SELECT wp.subject, e.name AS priority "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN enumerations e ON wp.priority_id = e.id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND t.name = 'Epic' "
            "AND (wp.subject LIKE 'Migrate:%')"
        )
        result = op_db_query(sql)
        if not result:
            check("16. Epic priorities correct", 1, False, "no epics found")
            return
        rows = [r.strip() for r in result.split("\n") if r.strip()]
        errors = []
        expected_priorities = {
            "Migrate: Migrate Hadoop Cluster to EMR Serverless": "High",
            "Migrate: Replace Talend ETL with dbt Cloud": "Normal",
        }
        for subj, expected_pri in expected_priorities.items():
            found = False
            for row in rows:
                if subj in row:
                    found = True
                    parts = row.split("|")
                    actual_pri = parts[-1].strip() if len(parts) >= 2 else "unknown"
                    if actual_pri.lower() != expected_pri.lower():
                        errors.append(f"'{subj}': expected {expected_pri}, got {actual_pri}")
                    break
            if not found:
                errors.append(f"'{subj}' not found")
        passed = len(errors) == 0
        check("16. Epic priorities correct", 1, passed,
              "; ".join(errors) if errors else "")
    except Exception as e:
        check("16. Epic priorities correct", 1, False, f"exception: {e}")


_op_assignment_rows = None
_op_assignment_err = None


def _epic_assignment_rows() -> list[tuple[str, str, str]]:
    """(subject, assignee login, version name) for every 'Migrate:' Epic,
    queried once with a \\x1f field separator (subjects may contain '|')."""
    global _op_assignment_rows, _op_assignment_err
    if _op_assignment_err:
        raise RuntimeError(_op_assignment_err)
    if _op_assignment_rows is not None:
        return _op_assignment_rows
    sql = (
        "SELECT wp.subject, COALESCE(u.login, '<none>'), COALESCE(v.name, '<none>') "
        "FROM work_packages wp "
        "JOIN projects p ON p.id = wp.project_id AND p.identifier = 'data-analytics-pipeline' "
        "JOIN types t    ON t.id = wp.type_id    AND t.name = 'Epic' "
        "LEFT JOIN users    u ON u.id = wp.assigned_to_id "
        "LEFT JOIN versions v ON v.id = wp.version_id "
        "WHERE wp.subject LIKE 'Migrate:%'"
    )
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-F", "\x1f", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        _op_assignment_err = f"OpenProject DB query failed: {r.stderr.strip()}"
        raise RuntimeError(_op_assignment_err)
    rows = []
    for line in r.stdout.strip().split("\n"):
        parts = line.split("\x1f")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    _op_assignment_rows = rows
    return rows


def check_17_op_epic_assignee() -> None:
    """Every expected 'Migrate:' Epic (ck14's row set) is assigned to the
    login user9 — missing Epic or assignee scores 0."""
    try:
        by_subject = {s: (login, ver) for s, login, ver in _epic_assignment_rows()}
        errors = []
        for subj in _expected_epic_subjects():
            if subj not in by_subject:
                errors.append(f"'{subj}': epic missing")
            elif by_subject[subj][0] != "user9":
                errors.append(f"'{subj}': assignee={by_subject[subj][0]}, expected user9")
        passed = len(errors) == 0
        check("17. Epic assignee user9", 2, passed,
              "; ".join(errors) if errors else "both epics assigned to user9")
    except Exception as e:
        check("17. Epic assignee user9", 2, False, f"exception: {e}")


def check_18_op_epic_version() -> None:
    """Every expected 'Migrate:' Epic is assigned to the version
    'Migration-Portfolio-2026-10-08' — missing Epic/version or a different
    version scores 0."""
    try:
        expected_version = "Migration-Portfolio-2026-10-08"
        by_subject = {s: (login, ver) for s, login, ver in _epic_assignment_rows()}
        errors = []
        for subj in _expected_epic_subjects():
            if subj not in by_subject:
                errors.append(f"'{subj}': epic missing")
            elif by_subject[subj][1] != expected_version:
                errors.append(f"'{subj}': version={by_subject[subj][1]}, "
                              f"expected {expected_version}")
        passed = len(errors) == 0
        check("18. Epic version assignment", 3, passed,
              "; ".join(errors) if errors else
              f"both epics in version {expected_version}")
    except Exception as e:
        check("18. Epic version assignment", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_database_exists()
    check_2_table_and_row_count()
    check_3_annual_savings()
    check_4_roi_score()
    check_5_decision()
    check_6_ranked_candidates_view()
    check_6b_field_schema()
    check_7_section_marker()
    check_8_comment_lines()
    check_9_metabase_collection()
    check_10_metabase_questions()
    check_11_metabase_dashboard()
    check_12_dashboard_cards()
    check_13_op_version()
    check_14_op_epic_subjects()
    check_15_op_descriptions()
    check_16_op_priorities()
    check_17_op_epic_assignee()
    check_18_op_epic_version()

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
