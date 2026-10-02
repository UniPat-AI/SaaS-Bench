"""
Verifier for Software-032-I1: Kick off Sprint 2025-04 for E-Commerce Platform

Checks: 17 weighted checks across openproject, code-server, baserow (total weight 24).
Strategy: docker exec (OpenProject embedded DB, code-server filesystem), REST API (Baserow)

Ground truth is recomputed by the verifier (OpenProject psql, full-file reads inside
code-server); agent-filled data is never trusted. If a truth recompute fails, the
dependent checks FAIL — no fallback to agent data.

Required env vars:
  SERVER_HOSTNAME, OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER
"""

import os
import re
import sys
import subprocess
import json
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OP_PORT = os.environ.get("OPENPROJECT_PORT")
OP_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")
CS_PORT = os.environ.get("CODE_SERVER_PORT")
CS_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BR_PORT = os.environ.get("BASEROW_PORT")
BR_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BR_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")

for _var_name, _var_val in [
    ("OPENPROJECT_PORT", OP_PORT), ("OPENPROJECT_CONTAINER", OP_CONTAINER),
    ("CODE_SERVER_PORT", CS_PORT), ("CODE_SERVER_CONTAINER", CS_CONTAINER),
    ("BASEROW_PORT", BR_PORT), ("BASEROW_CONTAINER", BR_CONTAINER),
    ("BASEROW_DB_CONTAINER", BR_DB_CONTAINER),
]:
    if not _var_val:
        print(f"FATAL: {_var_name} not set", file=sys.stderr)
        sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
BACKLOG = [
    {"subject": "Fix race condition in cart merge on login", "type": "Bug", "priority": "High", "estimated_hours": 6.0, "assignee": "OpenProject Admin", "target_module": "blog-engine/src/routes/api.js"},
    {"subject": "Add structured logging to checkout routes", "type": "Task", "priority": "Normal", "estimated_hours": 4.5, "assignee": "John Marshall", "target_module": "todo-api/tests/test_categories.py"},
    {"subject": "Slug generation helper supports unicode", "type": "Feature", "priority": "Normal", "estimated_hours": 8.0, "assignee": "Lena Hogan", "target_module": "blog-engine/src/utils/slugify.js"},
    {"subject": "Export analyzer summary as JSON", "type": "Feature", "priority": "Low", "estimated_hours": 5.0, "assignee": "Jane Dradder", "target_module": "data-analyzer/src/analyzer.py"},
    {"subject": "Fix ItemList pagination double-fetch", "type": "Bug", "priority": "Immediate", "estimated_hours": 3.0, "assignee": "Latisha Mazon", "target_module": "vue-hackernews-2.0/src/views/ItemList.vue"},
]

OP_API_AUTH = ("apikey", "AdminPass123!")

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
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


def op_query_raw(sql: str) -> tuple[int, str, str]:
    """Run a psql query inside the OpenProject container; return (rc, stdout, stderr)."""
    return docker_exec(
        OP_CONTAINER, "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1",
        "-U", "openproject", "-d", "openproject", "-t", "-A", "-c", sql
    )


def op_query(sql: str) -> str:
    """Run a psql query inside the OpenProject container (embedded Postgres).

    Raises RuntimeError on psql failure so truth-dependent checks FAIL loudly
    instead of silently treating errors as empty result sets."""
    rc, out, err = op_query_raw(sql)
    if rc != 0:
        raise RuntimeError(f"psql failed: {err.strip()[:150]}")
    return out.strip()


def op_query_rows(sql: str) -> list[list[str]]:
    raw = op_query(sql)
    if not raw:
        return []
    return [line.split("|") for line in raw.split("\n") if line.strip()]


# ── OpenProject checks ───────────────────────────────────────────────────────
def check_1_version():
    """Version Sprint 2025-04 exists with correct dates, status, description."""
    try:
        rows = op_query_rows(
            "SELECT v.name, v.start_date, v.effective_date, v.status, v.description "
            "FROM versions v JOIN projects p ON v.project_id = p.id "
            "WHERE p.name = 'E-Commerce Platform' AND v.name = 'Sprint 2025-04'"
        )
        if not rows:
            check("1. Version Sprint 2025-04", 2, False, "version not found")
            return
        row = rows[0]
        name, start, due, status, desc = row[0], row[1], row[2], row[3], row[4] if len(row) > 4 else ""
        issues = []
        if start != "2025-04-07":
            issues.append(f"start_date={start}")
        if due != "2025-04-18":
            issues.append(f"due_date={due}")
        if status != "open":
            issues.append(f"status={status}")
        expected_desc = "Sprint goal: Stabilize checkout and improve cart conversion"
        if desc.strip() != expected_desc:
            issues.append(f"description mismatch: '{desc.strip()[:60]}...'")
        check("1. Version Sprint 2025-04", 2, not issues,
              "; ".join(issues) if issues else "all fields correct")
    except Exception as e:
        check("1. Version Sprint 2025-04", 2, False, f"exception: {e}")


def check_2_board():
    """Saved public work-packages view 'Sprint 2025-04 Board' exists, is public
    AND is grouped by Status.

    Boards are an Enterprise add-on in OpenProject Community, so the task asks
    for a saved public query grouped by Status. Primary path: queries table
    (name + public + group_by columns). If the group_by column is missing on
    this OpenProject version, fall back to the /api/v3/queries API and assert
    _links.groupBy.href ends with /group_bys/status."""
    label = "2. Board Sprint 2025-04 Board (public, grouped by status)"
    try:
        try:
            # ── SQL-column path ──
            rc, out, err = op_query_raw(
                "SELECT q.name, q.public, q.group_by FROM queries q "
                "WHERE q.project_id = (SELECT id FROM projects WHERE name = 'E-Commerce Platform') "
                "AND q.name = 'Sprint 2025-04 Board'"
            )
            if rc != 0:
                raise RuntimeError(f"psql failed: {err.strip()[:150]}")
            rows = [line.split("|") for line in out.strip().split("\n") if line.strip()]
            if not rows:
                check(label, 1, False,
                      "saved view 'Sprint 2025-04 Board' not found in queries table")
                return
            row = rows[0]
            is_public = len(row) > 1 and row[1].strip().lower() in ("t", "true", "1")
            group_by = row[2].strip() if len(row) > 2 else ""
            grouped = group_by == "status"
            issues = []
            if not is_public:
                issues.append("not public")
            if not grouped:
                issues.append(f"group_by='{group_by}' (expected 'status')")
            check(label, 1, is_public and grouped,
                  "; ".join(issues) if issues else "public and grouped by status")
            return
        except Exception:
            pass  # fall through to API fallback (e.g. group_by column missing)

        # ── API fallback ──
        r = requests.get(
            f"http://{HOST}:{OP_PORT}/api/v3/queries?pageSize=200",
            auth=OP_API_AUTH, timeout=15,
        )
        r.raise_for_status()
        elements = r.json().get("_embedded", {}).get("elements", [])
        q = None
        for e in elements:
            links = e.get("_links", {}) or {}
            proj_title = (links.get("project") or {}).get("title", "")
            if e.get("name") == "Sprint 2025-04 Board" and proj_title == "E-Commerce Platform":
                q = e
                break
        if q is None:
            check(label, 1, False,
                  "saved view 'Sprint 2025-04 Board' not found via /api/v3/queries")
            return
        is_public = q.get("public") is True
        gb_href = ((q.get("_links", {}) or {}).get("groupBy") or {}).get("href") or ""
        grouped = gb_href.endswith("/group_bys/status")
        issues = []
        if not is_public:
            issues.append("not public")
        if not grouped:
            issues.append(f"groupBy href='{gb_href}' (expected .../group_bys/status)")
        check(label, 1, is_public and grouped,
              "; ".join(issues) if issues else "public and grouped by status (via API)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_3_work_packages_exist():
    """5 work packages with correct subjects assigned to Sprint 2025-04."""
    try:
        rows = op_query_rows(
            "SELECT wp.subject "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "WHERE p.name = 'E-Commerce Platform' "
            "AND wp.version_id = ("
            "  SELECT id FROM versions WHERE name = 'Sprint 2025-04' "
            "  AND project_id = (SELECT id FROM projects WHERE name = 'E-Commerce Platform')"
            ") ORDER BY wp.subject"
        )
        found_subjects = {r[0] for r in rows}
        expected_subjects = {item["subject"] for item in BACKLOG}
        missing = expected_subjects - found_subjects
        passed = len(rows) == 5 and not missing
        detail = f"found {len(rows)} WPs"
        if missing:
            detail += f"; missing: {list(missing)[:3]}"
        check("3. 5 work packages with correct subjects", 2, passed, detail)
    except Exception as e:
        check("3. 5 work packages with correct subjects", 2, False, f"exception: {e}")


def check_4_wp_types_priorities():
    """Work packages have correct types and priorities."""
    try:
        rows = op_query_rows(
            "SELECT wp.subject, t.name, e.name "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "LEFT JOIN enumerations e ON wp.priority_id = e.id "
            "WHERE p.name = 'E-Commerce Platform' "
            "AND wp.version_id = ("
            "  SELECT id FROM versions WHERE name = 'Sprint 2025-04' "
            "  AND project_id = (SELECT id FROM projects WHERE name = 'E-Commerce Platform')"
            ")"
        )
        expected = {item["subject"]: (item["type"], item["priority"]) for item in BACKLOG}
        issues = []
        matched = 0
        for row in rows:
            subj, typ, pri = row[0], row[1], row[2] if len(row) > 2 else ""
            if subj in expected:
                exp_type, exp_pri = expected[subj]
                if typ != exp_type:
                    issues.append(f"'{subj[:30]}': type={typ}")
                elif pri != exp_pri:
                    issues.append(f"'{subj[:30]}': priority={pri}")
                else:
                    matched += 1
        passed = matched == 5 and not issues
        check("4. WP types and priorities", 2, passed,
              f"{matched}/5 correct" + (f"; {'; '.join(issues[:3])}" if issues else ""))
    except Exception as e:
        check("4. WP types and priorities", 2, False, f"exception: {e}")


def check_5_wp_hours_assignees():
    """Work packages have correct estimated hours and assignees."""
    try:
        rows = op_query_rows(
            "SELECT wp.subject, wp.estimated_hours, "
            "COALESCE(u.firstname || ' ' || u.lastname, '') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "WHERE p.name = 'E-Commerce Platform' "
            "AND wp.version_id = ("
            "  SELECT id FROM versions WHERE name = 'Sprint 2025-04' "
            "  AND project_id = (SELECT id FROM projects WHERE name = 'E-Commerce Platform')"
            ")"
        )
        expected = {item["subject"]: (item["estimated_hours"], item["assignee"]) for item in BACKLOG}
        issues = []
        matched = 0
        for row in rows:
            subj = row[0]
            hours_str = row[1] if len(row) > 1 else ""
            assignee = row[2].strip() if len(row) > 2 else ""
            if subj in expected:
                exp_hours, exp_assignee = expected[subj]
                try:
                    hours = float(hours_str) if hours_str else 0.0
                except ValueError:
                    hours = 0.0
                if abs(hours - exp_hours) > 0.01:
                    issues.append(f"'{subj[:30]}': hours={hours}, expected {exp_hours}")
                elif assignee != exp_assignee:
                    issues.append(f"'{subj[:30]}': assignee='{assignee}', expected '{exp_assignee}'")
                else:
                    matched += 1
        passed = matched == 5 and not issues
        check("5. WP estimated hours and assignees", 2, passed,
              f"{matched}/5 correct" + (f"; {'; '.join(issues[:3])}" if issues else ""))
    except Exception as e:
        check("5. WP estimated hours and assignees", 2, False, f"exception: {e}")


def check_6_meeting():
    """Exactly one meeting 'Sprint Planning: Sprint 2025-04' with start time
    exactly 2025-04-07 10:00:00 (tolerating the +00 / +00:00 UTC suffix psql
    prints for timestamptz)."""
    try:
        rows = op_query_rows(
            "SELECT COUNT(*), MIN(m.start_time::text) "
            "FROM meetings m JOIN projects p ON m.project_id = p.id "
            "WHERE p.name = 'E-Commerce Platform' "
            "AND m.title = 'Sprint Planning: Sprint 2025-04'"
        )
        if not rows:
            check("6. Meeting Sprint Planning", 2, False, "meeting query returned nothing")
            return
        row = rows[0]
        try:
            count = int(row[0].strip())
        except (ValueError, IndexError):
            count = -1
        start_time = row[1].strip() if len(row) > 1 else ""
        if count == 0:
            check("6. Meeting Sprint Planning", 2, False, "meeting not found")
            return
        issues = []
        if count != 1:
            issues.append(f"expected exactly 1 meeting with this title, found {count}")
        # exact start time, tolerating UTC offset representation
        if not re.fullmatch(r"2025-04-07 10:00:00(\+00(:00)?)?", start_time):
            issues.append(f"start_time='{start_time}' != '2025-04-07 10:00:00'")
        check("6. Meeting Sprint Planning", 2, not issues,
              "; ".join(issues) if issues else f"exactly one meeting, start_time={start_time}")
    except Exception as e:
        check("6. Meeting Sprint Planning", 2, False, f"exception: {e}")


def check_7_agenda_items():
    """Meeting has 5 agenda items with correct titles in order."""
    try:
        rows = op_query_rows(
            "SELECT mai.title "
            "FROM meeting_agenda_items mai "
            "JOIN meetings m ON mai.meeting_id = m.id "
            "JOIN projects p ON m.project_id = p.id "
            "WHERE p.name = 'E-Commerce Platform' "
            "AND m.title = 'Sprint Planning: Sprint 2025-04' "
            "ORDER BY mai.position"
        )
        expected_titles = [f"Review: {item['subject']}" for item in BACKLOG]
        found_titles = [r[0] for r in rows]

        if len(found_titles) != 5:
            check("7. Meeting agenda items", 2, False,
                  f"found {len(found_titles)} items, expected 5")
            return

        issues = []
        for i, (found, expected) in enumerate(zip(found_titles, expected_titles)):
            if found != expected:
                issues.append(f"item {i+1}: '{found[:40]}' != '{expected[:40]}'")
        passed = not issues
        check("7. Meeting agenda items", 2, passed,
              "all 5 in correct order" if passed else "; ".join(issues[:3]))
    except Exception as e:
        check("7. Meeting agenda items", 2, False, f"exception: {e}")


# ── code-server checks ───────────────────────────────────────────────────────
def _find_file_in_container(target_module: str) -> str:
    """Locate a file by its relative path inside the code-server container."""
    rc, out, err = docker_exec(
        CS_CONTAINER, "find", "/home", "-path", f"*/{target_module}", "-type", "f",
        timeout=10,
    )
    if rc == 0 and out.strip():
        return out.strip().split("\n")[0]
    return ""


def _check_todo_comment(check_num: int, item: dict) -> None:
    """Verify the TODO comment line: full-file read via docker exec cat, then
    assert (a) the expected comment line (stripped, exact) occurs EXACTLY once
    in the file, and (b) it is the file's FIRST non-empty line (the task says
    to insert it immediately above the original first non-empty line, so after
    insertion it must be the first non-empty line)."""
    subject = item["subject"]
    target = item["target_module"]
    ext = target.rsplit(".", 1)[-1] if "." in target else ""

    if ext in ("js", "ts", "tsx", "vue"):
        prefix = "//"
    else:
        prefix = "#"

    expected_comment = f"{prefix} TODO [Sprint 2025-04]: {subject}"

    try:
        filepath = _find_file_in_container(target)
        if not filepath:
            check(f"{check_num}. TODO in {target}", 1, False, "file not found in container")
            return

        rc, out, err = docker_exec(CS_CONTAINER, "cat", filepath)
        if rc != 0:
            check(f"{check_num}. TODO in {target}", 1, False,
                  f"cannot read file: {err.strip()[:80]}")
            return

        lines = out.split("\n")
        occurrences = sum(1 for l in lines if l.strip() == expected_comment)
        first_nonempty = next((l.strip() for l in lines if l.strip()), "")

        issues = []
        if occurrences == 0:
            issues.append("comment line not found")
        elif occurrences != 1:
            issues.append(f"comment appears {occurrences} times, expected exactly 1")
        if occurrences > 0 and first_nonempty != expected_comment:
            issues.append(f"first non-empty line is '{first_nonempty[:50]}', not the TODO comment")
        check(f"{check_num}. TODO in {target}", 1, not issues,
              "; ".join(issues) if issues else "comment present exactly once as first non-empty line")
    except Exception as e:
        check(f"{check_num}. TODO in {target}", 1, False, f"exception: {e}")


def check_8_todo_api_js():
    _check_todo_comment(8, BACKLOG[0])


def check_9_todo_test_categories():
    _check_todo_comment(9, BACKLOG[1])


def check_10_todo_slugify():
    _check_todo_comment(10, BACKLOG[2])


def check_11_todo_analyzer():
    _check_todo_comment(11, BACKLOG[3])


def check_12_todo_itemlist():
    _check_todo_comment(12, BACKLOG[4])


# ── Baserow checks (REST API) ────────────────────────────────────────────────
def _baserow_auth() -> str:
    """Authenticate to Baserow and return the JWT access token."""
    r = requests.post(
        f"http://{HOST}:{BR_PORT}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _baserow_headers(token: str) -> dict:
    return {"Authorization": f"JWT {token}"}


def _find_baserow_table(token: str):
    """Find the Sprint Backlog table. Returns (db_id, table_id) or (None, None)."""
    headers = _baserow_headers(token)
    r = requests.get(f"http://{HOST}:{BR_PORT}/api/applications/",
                     headers=headers, timeout=10)
    r.raise_for_status()
    apps = r.json()

    db_id = None
    for app in apps:
        if app.get("name") == "Sprint 2025-04 Tracking" and app.get("type") == "database":
            db_id = app["id"]
            break
    if not db_id:
        return None, None

    r = requests.get(f"http://{HOST}:{BR_PORT}/api/database/tables/database/{db_id}/",
                     headers=headers, timeout=10)
    r.raise_for_status()
    for t in r.json():
        if t.get("name") == "Sprint Backlog":
            return db_id, t["id"]
    return db_id, None


def _baserow_fields(token: str, table_id: int) -> list[dict]:
    r = requests.get(f"http://{HOST}:{BR_PORT}/api/database/fields/table/{table_id}/",
                     headers=_baserow_headers(token), timeout=10)
    r.raise_for_status()
    return r.json()


def _op_wp_id_truth() -> dict:
    """Recompute ground truth: subject -> real OpenProject work package ID for
    all WPs assigned to version 'Sprint 2025-04' in 'E-Commerce Platform'.
    Raises on psql failure (truth-dependent checks must FAIL, no fallback)."""
    rows = op_query_rows(
        "SELECT wp.id, wp.subject "
        "FROM work_packages wp "
        "JOIN projects p ON wp.project_id = p.id "
        "WHERE p.name = 'E-Commerce Platform' "
        "AND wp.version_id = ("
        "  SELECT id FROM versions WHERE name = 'Sprint 2025-04' "
        "  AND project_id = (SELECT id FROM projects WHERE name = 'E-Commerce Platform')"
        ")"
    )
    mapping = {}
    for row in rows:
        if len(row) >= 2:
            mapping[row[1]] = int(row[0].strip())
    return mapping


def check_13_baserow_db_table():
    """Baserow database 'Sprint 2025-04 Tracking' and table 'Sprint Backlog' exist."""
    try:
        token = _baserow_auth()
        db_id, table_id = _find_baserow_table(token)
        if not db_id:
            check("13. Baserow DB and Sprint Backlog table", 1, False, "database not found")
        elif not table_id:
            check("13. Baserow DB and Sprint Backlog table", 1, False,
                  f"db found (id={db_id}) but table 'Sprint Backlog' not found")
        else:
            check("13. Baserow DB and Sprint Backlog table", 1, True,
                  f"db_id={db_id}, table_id={table_id}")
    except Exception as e:
        check("13. Baserow DB and Sprint Backlog table", 1, False, f"exception: {e}")


def check_14_baserow_rows_subjects():
    """5 rows with correct subjects in backlog order."""
    try:
        token = _baserow_auth()
        headers = _baserow_headers(token)
        db_id, table_id = _find_baserow_table(token)
        if not table_id:
            check("14. Baserow 5 rows with correct subjects", 1, False, "table not found")
            return

        fields = _baserow_fields(token, table_id)
        subject_field = None
        for f in fields:
            if f.get("name") == "Subject":
                subject_field = f"field_{f['id']}"
                break

        # Get rows (ordered by id to preserve insertion order)
        r = requests.get(
            f"http://{HOST}:{BR_PORT}/api/database/rows/table/{table_id}/?size=100",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("results", [])

        if len(rows) != 5:
            check("14. Baserow 5 rows with correct subjects", 1, False,
                  f"found {len(rows)} rows, expected 5")
            return

        expected_subjects = [item["subject"] for item in BACKLOG]
        found_subjects = []
        for row in rows:
            if subject_field:
                found_subjects.append(row.get(subject_field, ""))
            else:
                found_subjects.append("(Subject field not found)")

        issues = []
        for i, (found, expected) in enumerate(zip(found_subjects, expected_subjects)):
            if found != expected:
                issues.append(f"row {i+1}: '{found[:40]}' != '{expected[:40]}'")
        passed = not issues and subject_field is not None
        check("14. Baserow 5 rows with correct subjects", 1, passed,
              "all subjects match in order" if passed else "; ".join(issues[:3]))
    except Exception as e:
        check("14. Baserow 5 rows with correct subjects", 1, False, f"exception: {e}")


def check_15_baserow_row_fields():
    """Rows have correct Item ID (SB-01..SB-05 by row order), Type, Priority,
    Estimated Hours, Assignee, Target Module — and OpenProject WP ID reconciled
    against the real WP ids in the OpenProject database (subject -> id mapping
    recomputed via psql; numeric equality per row)."""
    try:
        token = _baserow_auth()
        headers = _baserow_headers(token)
        db_id, table_id = _find_baserow_table(token)
        if not table_id:
            check("15. Baserow row field values", 2, False, "table not found")
            return

        field_map = {}
        for f in _baserow_fields(token, table_id):
            field_map[f["name"]] = f"field_{f['id']}"

        # Get rows
        r = requests.get(
            f"http://{HOST}:{BR_PORT}/api/database/rows/table/{table_id}/?size=100",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        rows = r.json().get("results", [])

        issues = []
        if len(rows) != 5:
            issues.append(f"found {len(rows)} rows, expected exactly 5")

        # Ground truth: subject -> real OP work package id (no fallback on failure)
        wp_truth = None
        try:
            wp_truth = _op_wp_id_truth()
            if not wp_truth:
                issues.append("OP WP ID truth recompute returned no work packages")
                wp_truth = None
        except Exception as e:
            issues.append(f"OP WP ID truth recompute failed: {str(e)[:80]}")
            wp_truth = None

        required_fields = ["Item ID", "Type", "Priority", "Estimated Hours",
                           "Assignee", "Target Module", "OpenProject WP ID"]
        for fname in required_fields:
            if fname not in field_map:
                issues.append(f"field '{fname}' missing from table")

        for i, item in enumerate(BACKLOG):
            if i >= len(rows):
                issues.append(f"row {i+1} missing")
                continue
            row = rows[i]

            # Item ID (primary text, SB-01..SB-05 in row order)
            iid_key = field_map.get("Item ID")
            if iid_key:
                expected_iid = f"SB-{i+1:02d}"
                iid_val = str(row.get(iid_key) or "").strip()
                if iid_val != expected_iid:
                    issues.append(f"row {i+1} Item ID: '{iid_val}' != '{expected_iid}'")

            # Type (single-select → dict with "value")
            type_key = field_map.get("Type")
            if type_key:
                type_val = row.get(type_key)
                if isinstance(type_val, dict):
                    type_val = type_val.get("value", "")
                if str(type_val) != item["type"]:
                    issues.append(f"row {i+1} Type: '{type_val}' != '{item['type']}'")

            # Priority (single-select)
            pri_key = field_map.get("Priority")
            if pri_key:
                pri_val = row.get(pri_key)
                if isinstance(pri_val, dict):
                    pri_val = pri_val.get("value", "")
                if str(pri_val) != item["priority"]:
                    issues.append(f"row {i+1} Priority: '{pri_val}' != '{item['priority']}'")

            # Estimated Hours (number)
            hours_key = field_map.get("Estimated Hours")
            if hours_key:
                hours_val = row.get(hours_key)
                try:
                    if abs(float(hours_val or 0) - item["estimated_hours"]) > 0.01:
                        issues.append(f"row {i+1} Hours: {hours_val} != {item['estimated_hours']}")
                except (ValueError, TypeError):
                    issues.append(f"row {i+1} Hours: '{hours_val}' invalid")

            # Assignee
            assignee_key = field_map.get("Assignee")
            if assignee_key:
                if row.get(assignee_key, "") != item["assignee"]:
                    issues.append(f"row {i+1} Assignee: '{row.get(assignee_key)}' != '{item['assignee']}'")

            # Target Module
            tm_key = field_map.get("Target Module")
            if tm_key:
                if row.get(tm_key, "") != item["target_module"]:
                    issues.append(f"row {i+1} Target: '{row.get(tm_key)}' != '{item['target_module']}'")

            # OpenProject WP ID — reconcile against real OP DB id for this subject
            wpid_key = field_map.get("OpenProject WP ID")
            if wpid_key:
                if wp_truth is None:
                    issues.append(f"row {i+1} WP ID: cannot verify (truth recompute failed)")
                else:
                    expected_id = wp_truth.get(item["subject"])
                    if expected_id is None:
                        issues.append(f"row {i+1} WP ID: subject not found in OpenProject version")
                    else:
                        wpid_val = row.get(wpid_key)
                        try:
                            if wpid_val is None or int(float(wpid_val)) != expected_id:
                                issues.append(f"row {i+1} WP ID: '{wpid_val}' != OP id {expected_id}")
                        except (ValueError, TypeError):
                            issues.append(f"row {i+1} WP ID: '{wpid_val}' invalid")

        passed = not issues
        check("15. Baserow row field values", 2, passed,
              "all fields correct incl. Item ID seq and real OP WP IDs" if passed
              else "; ".join(issues[:5]))
    except Exception as e:
        check("15. Baserow row field values", 2, False, f"exception: {e}")


def check_16_baserow_gallery_view():
    """Gallery view 'By Priority' exists on Sprint Backlog table."""
    try:
        token = _baserow_auth()
        headers = _baserow_headers(token)
        db_id, table_id = _find_baserow_table(token)
        if not table_id:
            check("16. Baserow Gallery view By Priority", 1, False, "table not found")
            return

        r = requests.get(f"http://{HOST}:{BR_PORT}/api/database/views/table/{table_id}/",
                         headers=headers, timeout=10)
        r.raise_for_status()
        views = r.json()

        gallery = None
        for v in views:
            if v.get("name") == "By Priority" and v.get("type") == "gallery":
                gallery = v
                break

        passed = gallery is not None
        check("16. Baserow Gallery view By Priority", 1, passed,
              "found" if passed else "gallery view 'By Priority' not found")
    except Exception as e:
        check("16. Baserow Gallery view By Priority", 1, False, f"exception: {e}")


def check_17_baserow_field_schema():
    """Field schema of Sprint Backlog matches the task spec exactly:
    field name set == the 8 specified fields; Item ID primary text;
    Subject/Assignee/Target Module text; Type single_select with options
    exactly {Task,Bug,Feature,Epic}; Priority single_select with options
    exactly {Low,Normal,High,Immediate}; Estimated Hours number with 1
    decimal place; OpenProject WP ID number."""
    label = "17. Baserow Sprint Backlog field schema"
    try:
        token = _baserow_auth()
        db_id, table_id = _find_baserow_table(token)
        if not table_id:
            check(label, 1, False, "table not found")
            return

        fields = _baserow_fields(token, table_id)
        by_name = {f.get("name"): f for f in fields}

        expected_names = {"Item ID", "Subject", "Type", "Priority",
                          "Estimated Hours", "Assignee", "Target Module",
                          "OpenProject WP ID"}
        issues = []
        actual_names = set(by_name.keys())
        if actual_names != expected_names:
            extra = sorted(actual_names - expected_names)
            missing = sorted(expected_names - actual_names)
            parts = []
            if missing:
                parts.append(f"missing fields: {missing}")
            if extra:
                parts.append(f"unexpected fields: {extra}")
            issues.append("; ".join(parts))

        def _opts(f: dict) -> set:
            return {o.get("value") for o in f.get("select_options", [])}

        # Item ID: primary + text
        f = by_name.get("Item ID")
        if f:
            if not f.get("primary"):
                issues.append("Item ID not primary")
            if f.get("type") != "text":
                issues.append(f"Item ID type={f.get('type')} (expected text)")

        # Plain text fields
        for name in ("Subject", "Assignee", "Target Module"):
            f = by_name.get(name)
            if f and f.get("type") != "text":
                issues.append(f"{name} type={f.get('type')} (expected text)")

        # Type single-select, exact option set
        f = by_name.get("Type")
        if f:
            if f.get("type") != "single_select":
                issues.append(f"Type type={f.get('type')} (expected single_select)")
            elif _opts(f) != {"Task", "Bug", "Feature", "Epic"}:
                issues.append(f"Type options={sorted(_opts(f))} != [Bug, Epic, Feature, Task]")

        # Priority single-select, exact option set
        f = by_name.get("Priority")
        if f:
            if f.get("type") != "single_select":
                issues.append(f"Priority type={f.get('type')} (expected single_select)")
            elif _opts(f) != {"Low", "Normal", "High", "Immediate"}:
                issues.append(f"Priority options={sorted(_opts(f))} != [High, Immediate, Low, Normal]")

        # Estimated Hours: number, 1 decimal place
        f = by_name.get("Estimated Hours")
        if f:
            if f.get("type") != "number":
                issues.append(f"Estimated Hours type={f.get('type')} (expected number)")
            elif f.get("number_decimal_places") != 1:
                issues.append(f"Estimated Hours decimal_places={f.get('number_decimal_places')} (expected 1)")

        # OpenProject WP ID: number
        f = by_name.get("OpenProject WP ID")
        if f and f.get("type") != "number":
            issues.append(f"OpenProject WP ID type={f.get('type')} (expected number)")

        check(label, 1, not issues,
              "; ".join(issues[:5]) if issues else "field names, types, primary and option sets all match")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_version()
    check_2_board()
    check_3_work_packages_exist()
    check_4_wp_types_priorities()
    check_5_wp_hours_assignees()
    check_6_meeting()
    check_7_agenda_items()
    check_8_todo_api_js()
    check_9_todo_test_categories()
    check_10_todo_slugify()
    check_11_todo_analyzer()
    check_12_todo_itemlist()
    check_13_baserow_db_table()
    check_14_baserow_rows_subjects()
    check_15_baserow_row_fields()
    check_16_baserow_gallery_view()
    check_17_baserow_field_schema()

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
