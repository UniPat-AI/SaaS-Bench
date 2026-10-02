"""
Verifier for Software-009-I2: Establish ADR Governance Workflow

Checks: 13 checks across code-server, baserow, openproject (total weight 20;
ck1 and ck10 are 0-weight diagnostics — a FAIL there still blocks all_pass).
Strategy: docker exec (code-server filesystem, openproject DB), REST API (baserow)

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import subprocess
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_required = {
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# ── Slot values ───────────────────────────────────────────────────────────────
ADR_FILENAMES = [
    "005-secrets-management.md",
    "006-container-orchestration.md",
    "007-backup-strategy.md",
]
ADR_TITLES = [
    "Secrets Management Solution for Production Workloads",
    "Container Orchestration Platform Selection",
    "Backup and Disaster Recovery Strategy",
]
ADR_CONTEXTS = [
    "We need to centralize secret storage and rotation to eliminate plaintext credentials from source control and improve auditability.",
    "We need to select a container orchestration platform capable of running stateful and stateless workloads with automated scaling and self-healing.",
    "We need to define a comprehensive backup and disaster recovery strategy that meets our RTO and RPO targets across all tier-1 systems.",
]
ADR_NUMBERS = ["005", "006", "007"]
ADR_REVIEWERS = ["Emma Wilson", "Frank Nguyen", "Grace Patel"]
ADR_DATE = "2025-06-10"
ADR_AUTHOR = "DevOps Engineering Guild"
ADR_STATUS = "Review"

# Expected file contents (5 lines each)
EXPECTED_CONTENTS = {}
for i in range(3):
    EXPECTED_CONTENTS[ADR_FILENAMES[i]] = (
        f"# ADR-{ADR_NUMBERS[i]}: {ADR_TITLES[i]}\n"
        f"Status: {ADR_STATUS}\n"
        f"Date: {ADR_DATE}\n"
        f"Author: {ADR_AUTHOR}\n"
        f"{ADR_CONTEXTS[i]}"
    )


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


def baserow_auth() -> str:
    """Get Baserow JWT access_token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    # Baserow returns both 'token' and 'access_token'; use access_token with JWT prefix
    return data.get("access_token", data.get("token", ""))


def baserow_get(path: str, token: str) -> requests.Response:
    return requests.get(
        f"{BASEROW_URL}/api{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )


def op_db_query(sql: str) -> str:
    """Run a SQL query against the OpenProject embedded Postgres DB."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER, "bash", "-c",
        f"PGPASSWORD=openproject psql -U openproject -d openproject -h 127.0.0.1 -t -A -c \"{sql}\"",
        timeout=20,
    )
    if rc != 0:
        raise RuntimeError(f"psql failed (rc={rc}): {err.strip()}")
    return out.strip()


# ── Individual checks ─────────────────────────────────────────────────────────

ADR_DIR = "/home/coder/workspace/devops-configs/docs/adr"


def check_1_adr_directory_exists() -> None:
    """0-weight diagnostic: devops-configs/docs/adr/ directory exists (implied by ck2-5)."""
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "test", "-d", ADR_DIR)
        check("1. ADR directory exists", 0, rc == 0,
              "directory not found" if rc != 0 else "")
    except Exception as e:
        check("1. ADR directory exists", 0, False, f"exception: {e}")


def check_2_file_005_content() -> None:
    """Check 005-secrets-management.md has correct 5-line content."""
    fname = ADR_FILENAMES[0]
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", f"{ADR_DIR}/{fname}")
        if rc != 0:
            check("2. 005-secrets-management.md content", 2, False, "file not found")
            return
        actual = out.rstrip("\n")
        expected = EXPECTED_CONTENTS[fname]
        passed = actual == expected
        detail = "" if passed else f"content mismatch: got {len(actual)} chars, expected {len(expected)}"
        check("2. 005-secrets-management.md content", 2, passed, detail)
    except Exception as e:
        check("2. 005-secrets-management.md content", 2, False, f"exception: {e}")


def check_3_file_006_content() -> None:
    """Check 006-container-orchestration.md has correct 5-line content."""
    fname = ADR_FILENAMES[1]
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", f"{ADR_DIR}/{fname}")
        if rc != 0:
            check("3. 006-container-orchestration.md content", 2, False, "file not found")
            return
        actual = out.rstrip("\n")
        expected = EXPECTED_CONTENTS[fname]
        passed = actual == expected
        detail = "" if passed else f"content mismatch: got {len(actual)} chars, expected {len(expected)}"
        check("3. 006-container-orchestration.md content", 2, passed, detail)
    except Exception as e:
        check("3. 006-container-orchestration.md content", 2, False, f"exception: {e}")


def check_4_file_007_content() -> None:
    """Check 007-backup-strategy.md has correct 5-line content."""
    fname = ADR_FILENAMES[2]
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", f"{ADR_DIR}/{fname}")
        if rc != 0:
            check("4. 007-backup-strategy.md content", 2, False, "file not found")
            return
        actual = out.rstrip("\n")
        expected = EXPECTED_CONTENTS[fname]
        passed = actual == expected
        detail = "" if passed else f"content mismatch: got {len(actual)} chars, expected {len(expected)}"
        check("4. 007-backup-strategy.md content", 2, passed, detail)
    except Exception as e:
        check("4. 007-backup-strategy.md content", 2, False, f"exception: {e}")


def check_5_exactly_3_files() -> None:
    """Check adr directory exists and contains exactly the 3 expected .md entries."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER, "bash", "-c", f"ls -A {ADR_DIR}"
        )
        if rc != 0:
            check("5. adr dir has exactly the 3 ADR files", 1, False,
                  "directory not found or ls failed")
            return
        entries = [e for e in out.split("\n") if e.strip()]
        expected = set(ADR_FILENAMES)
        passed = len(entries) == 3 and set(entries) == expected
        detail = "" if passed else f"entries {sorted(entries)}, expected exactly {sorted(expected)}"
        check("5. adr dir has exactly the 3 ADR files", 1, passed, detail)
    except Exception as e:
        check("5. adr dir has exactly the 3 ADR files", 1, False, f"exception: {e}")


def check_6_baserow_database_exists() -> None:
    """Check Baserow database 'ADR Decision Registry' exists."""
    try:
        token = baserow_auth()
        resp = baserow_get("/applications/", token)
        resp.raise_for_status()
        apps = resp.json()
        found = any(
            a.get("name") == "ADR Decision Registry"
            for a in apps
        )
        check("6. Baserow DB 'ADR Decision Registry' exists", 1, found,
              "database not found" if not found else "")
    except Exception as e:
        check("6. Baserow DB 'ADR Decision Registry' exists", 1, False, f"exception: {e}")


def _get_baserow_table(token: str):
    """Find the ADR Registry table and return table_id or None."""
    resp = baserow_get("/applications/", token)
    resp.raise_for_status()
    apps = resp.json()
    for app in apps:
        if app.get("name") == "ADR Decision Registry":
            for table in app.get("tables", []):
                if table.get("name") == "ADR Registry":
                    return table["id"]
    return None


def check_7_baserow_table_fields() -> None:
    """Check ADR Registry table has exactly the 7 specified fields with correct schema."""
    try:
        token = baserow_auth()
        table_id = _get_baserow_table(token)
        if table_id is None:
            check("7. Baserow table 'ADR Registry' schema", 2, False,
                  "table not found")
            return
        resp = baserow_get(f"/database/fields/table/{table_id}/", token)
        resp.raise_for_status()
        fields = {f["name"]: f for f in resp.json()}
        expected_fields = {"ADR ID", "Title", "Status", "Author", "Reviewer",
                           "Created Date", "Review Duration Days"}
        problems = []
        actual_names = set(fields)
        if actual_names != expected_fields:
            missing = expected_fields - actual_names
            extra = actual_names - expected_fields
            if missing:
                problems.append(f"missing fields: {sorted(missing)}")
            if extra:
                problems.append(f"extra fields: {sorted(extra)}")

        def ftype(name: str) -> str:
            return (fields.get(name) or {}).get("type", "")

        adr_f = fields.get("ADR ID") or {}
        if adr_f.get("type") != "text" or not adr_f.get("primary"):
            problems.append("ADR ID not primary text")
        for name in ("Title", "Author", "Reviewer"):
            if ftype(name) != "text":
                problems.append(f"{name} not text")
        status_f = fields.get("Status") or {}
        if status_f.get("type") != "single_select":
            problems.append("Status not single_select")
        else:
            opts = {o.get("value") for o in status_f.get("select_options", [])}
            if opts != {"Draft", "Review", "Approved", "Implemented"}:
                problems.append(f"Status options {sorted(opts)}")
        if ftype("Created Date") != "date":
            problems.append("Created Date not date")
        dur_f = fields.get("Review Duration Days") or {}
        if dur_f.get("type") != "number":
            problems.append("Review Duration Days not number")
        elif int(dur_f.get("number_decimal_places") or 0) != 0:
            problems.append(
                f"Review Duration Days decimal places {dur_f.get('number_decimal_places')}, expected 0")
        passed = not problems
        check("7. Baserow table 'ADR Registry' schema", 2, passed,
              "; ".join(problems[:4]) if problems else "")
    except Exception as e:
        check("7. Baserow table 'ADR Registry' schema", 2, False, f"exception: {e}")


def check_8_baserow_rows_adr_ids_titles() -> None:
    """Check ADR Registry has exactly 3 rows with correct ADR IDs and titles."""
    try:
        token = baserow_auth()
        table_id = _get_baserow_table(token)
        if table_id is None:
            check("8. Baserow 3 rows with ADR IDs/Titles", 2, False, "table not found")
            return
        resp = baserow_get(f"/database/rows/table/{table_id}/?user_field_names=true", token)
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("results", [])
        if len(rows) != 3:
            check("8. Baserow 3 rows with ADR IDs/Titles", 2, False,
                  f"expected 3 rows, got {len(rows)}")
            return
        expected_pairs = {(f"ADR-{ADR_NUMBERS[i]}", ADR_TITLES[i]) for i in range(3)}
        actual_pairs = {
            (str(row.get("ADR ID", "")).strip(), str(row.get("Title", "")).strip())
            for row in rows
        }
        passed = actual_pairs == expected_pairs
        details = []
        if not passed:
            missing_pairs = expected_pairs - actual_pairs
            extra_pairs = actual_pairs - expected_pairs
            if missing_pairs:
                details.append(f"missing (ADR ID, Title) pairs: {sorted(missing_pairs)}")
            if extra_pairs:
                details.append(f"unexpected pairs: {sorted(extra_pairs)}")
        check("8. Baserow 3 rows with ADR IDs/Titles", 2, passed,
              "; ".join(details) if details else "")
    except Exception as e:
        check("8. Baserow 3 rows with ADR IDs/Titles", 2, False, f"exception: {e}")


def check_9_baserow_rows_status_reviewer_date() -> None:
    """Check rows have Status=Review, correct Reviewers, Date=2025-06-10, Duration=0."""
    try:
        token = baserow_auth()
        table_id = _get_baserow_table(token)
        if table_id is None:
            check("9. Baserow row details (Status/Reviewer/Date/Duration)", 2, False,
                  "table not found")
            return
        resp = baserow_get(f"/database/rows/table/{table_id}/?user_field_names=true", token)
        resp.raise_for_status()
        rows = resp.json().get("results", [])
        issues = []
        # Reviewer is keyed positionally to the ADR (ADR-005 -> Emma Wilson, ...).
        expected_reviewer_by_id = {
            f"ADR-{ADR_NUMBERS[i]}": ADR_REVIEWERS[i] for i in range(3)
        }
        for row in rows:
            adr_id = str(row.get("ADR ID", "")).strip()

            # Status - single select field returns dict with value key
            status = row.get("Status", {})
            if isinstance(status, dict):
                status_val = status.get("value", "")
            else:
                status_val = str(status)
            if status_val != ADR_STATUS:
                issues.append(f"{adr_id or 'row'} Status={status_val}, expected {ADR_STATUS}")

            # Reviewer keyed by the row's ADR ID (unknown IDs count as failures)
            reviewer = str(row.get("Reviewer", "")).strip()
            expected_rev = expected_reviewer_by_id.get(adr_id)
            if expected_rev is None:
                issues.append(f"unknown ADR ID '{adr_id}'")
            elif reviewer != expected_rev:
                issues.append(f"{adr_id} Reviewer={reviewer}, expected {expected_rev}")

            # Created Date
            date_val = str(row.get("Created Date", "")).strip()
            if not date_val.startswith(ADR_DATE):
                issues.append(f"{adr_id or 'row'} date={date_val}, expected {ADR_DATE}")

            # Review Duration Days
            duration = row.get("Review Duration Days")
            duration_str = str(duration).strip() if duration is not None else ""
            if duration_str not in ("0", "0.0"):
                issues.append(f"{adr_id or 'row'} duration={duration}, expected 0")

        passed = len(issues) == 0 and bool(rows)
        if not rows:
            issues.append("no rows found")
        check("9. Baserow row details (Status/Reviewer/Date/Duration)", 2, passed,
              "; ".join(issues[:3]) if issues else "")
    except Exception as e:
        check("9. Baserow row details (Status/Reviewer/Date/Duration)", 2, False,
              f"exception: {e}")


_op_project_id: int | None = None
_op_epics_cache: list[dict] | None = None


def check_10_openproject_project_exists() -> None:
    """0-weight diagnostic: locate seeded 'DevOps Automation' project id for ck11-13."""
    global _op_project_id
    try:
        result = op_db_query("SELECT id FROM projects WHERE name = 'DevOps Automation'")
        pid = result.split("\n")[0].strip() if result else ""
        _op_project_id = int(pid) if pid.isdigit() else None
        check("10. OpenProject project 'DevOps Automation' exists", 0,
              _op_project_id is not None,
              f"project id {pid}" if _op_project_id is not None else "project not found")
    except Exception as e:
        check("10. OpenProject project 'DevOps Automation' exists", 0, False,
              f"exception: {e}")


def _get_op_epics() -> list[dict]:
    """Get 'Implement ADR-*' Epic-type work packages in 'DevOps Automation' from DB.
    Epic type id is resolved by name (never hardcoded); computed once and cached.
    Returns list of dicts with subject, assigned_to_id, priority, description.
    """
    global _op_epics_cache
    if _op_epics_cache is not None:
        return _op_epics_cache
    if _op_project_id is None:
        raise RuntimeError("project 'DevOps Automation' not found (see check 10)")
    type_result = op_db_query("SELECT id FROM types WHERE name = 'Epic'")
    type_id = type_result.split("\n")[0].strip() if type_result else ""
    if not type_id.isdigit():
        raise RuntimeError("Epic type not found in OpenProject types table")
    sql = (
        "SELECT wp.subject, wp.assigned_to_id, e.name AS priority, wp.description "
        "FROM work_packages wp "
        "JOIN enumerations e ON wp.priority_id = e.id "
        f"WHERE wp.project_id = {_op_project_id} AND wp.type_id = {int(type_id)} "
        "AND wp.subject LIKE 'Implement ADR-%'"
    )
    result = op_db_query(sql)
    if not result:
        _op_epics_cache = []
        return []
    epics = []
    for line in result.split("\n"):
        parts = line.split("|", 3)
        if len(parts) >= 4:
            epics.append({
                "subject": parts[0],
                "assigned_to_id": parts[1],
                "priority": parts[2],
                "description": parts[3],
            })
    _op_epics_cache = epics
    return epics


def check_11_openproject_3_epics_subjects() -> None:
    """Check exactly 3 'Implement ADR-%' Epic WPs with correct subjects (extras fail)."""
    try:
        epics = _get_op_epics()
        if len(epics) != 3:
            check("11. 3 Epic WPs with correct subjects", 2, False,
                  f"expected exactly 3 'Implement ADR-%' epics, got {len(epics)}")
            return
        expected_subjects = {
            f"Implement ADR-{ADR_NUMBERS[i]}: {ADR_TITLES[i]}" for i in range(3)
        }
        actual_subjects = {e["subject"] for e in epics}
        passed = expected_subjects == actual_subjects
        detail = ""
        if not passed:
            missing = expected_subjects - actual_subjects
            detail = f"missing subjects: {missing}" if missing else f"unexpected: {actual_subjects - expected_subjects}"
        check("11. 3 Epic WPs with correct subjects", 2, passed, detail)
    except Exception as e:
        check("11. 3 Epic WPs with correct subjects", 2, False, f"exception: {e}")


def check_12_openproject_epics_assignee_priority() -> None:
    """Check epics have assignee=OpenProject Admin (user id for 'admin') and priority=Normal."""
    try:
        # Get admin user id
        admin_id = op_db_query(
            "SELECT id FROM users WHERE login = 'admin' AND admin = true"
        )
        if not admin_id:
            check("12. Epics assignee/priority", 2, False, "admin user not found in DB")
            return

        epics = _get_op_epics()
        if not epics:
            check("12. Epics assignee/priority", 2, False, "no epics found")
            return
        issues = []
        for ep in epics:
            if ep["assigned_to_id"] != admin_id:
                issues.append(f"'{ep['subject'][:30]}...' assignee_id={ep['assigned_to_id']}, expected {admin_id}")
            if ep["priority"] != "Normal":
                issues.append(f"'{ep['subject'][:30]}...' priority={ep['priority']}")
        passed = len(issues) == 0
        check("12. Epics assignee/priority", 2, passed,
              "; ".join(issues[:3]) if issues else "")
    except Exception as e:
        check("12. Epics assignee/priority", 2, False, f"exception: {e}")


def check_13_openproject_epics_description() -> None:
    """Check epic descriptions contain 'Linked ADR file: devops-configs/docs/adr/<filename>'."""
    try:
        epics = _get_op_epics()
        if not epics:
            check("13. Epic descriptions contain ADR file path", 2, False,
                  "no epics found")
            return
        issues = []
        for ep in epics:
            subj = ep["subject"]
            desc = ep["description"] or ""
            matched = False
            for i in range(3):
                expected_subj = f"Implement ADR-{ADR_NUMBERS[i]}: {ADR_TITLES[i]}"
                if subj == expected_subj:
                    expected_path = f"Linked ADR file: devops-configs/docs/adr/{ADR_FILENAMES[i]}"
                    if expected_path not in desc:
                        issues.append(
                            f"'{ADR_FILENAMES[i]}' path not in description"
                        )
                    matched = True
                    break
            if not matched:
                issues.append(f"unrecognized epic subject: '{subj[:40]}...'")
        passed = len(issues) == 0
        check("13. Epic descriptions contain ADR file path", 2, passed,
              "; ".join(issues) if issues else "")
    except Exception as e:
        check("13. Epic descriptions contain ADR file path", 2, False,
              f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_adr_directory_exists()
    check_2_file_005_content()
    check_3_file_006_content()
    check_4_file_007_content()
    check_5_exactly_3_files()
    check_6_baserow_database_exists()
    check_7_baserow_table_fields()
    check_8_baserow_rows_adr_ids_titles()
    check_9_baserow_rows_status_reviewer_date()
    check_10_openproject_project_exists()
    check_11_openproject_3_epics_subjects()
    check_12_openproject_epics_assignee_priority()
    check_13_openproject_epics_description()

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
