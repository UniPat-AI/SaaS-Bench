#!/usr/bin/env python3
"""
Verifier for Software-035-I3: Coordinate v1.5.0 Release Across vue-hackernews-2.0 and tabler

Checks: 14 weighted checks across openproject, code-server, baserow.
Strategy: docker exec (OpenProject DB, code-server filesystem), Baserow REST API.

Required env vars:
  SERVER_HOSTNAME, OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER.
"""

import json
import os
import re
import subprocess
import sys

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")
CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")

_missing = []
for _var in [
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
]:
    if not os.environ.get(_var):
        _missing.append(_var)
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

OPENPROJECT_BASE = f"http://{HOST}:{OPENPROJECT_PORT}"
BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"

# ── Task constants ────────────────────────────────────────────────────────────
OP_PROJECT = "API Gateway"
VERSION_NAME = "v1.5.0"
VERSION_DESC = "Release v1.5.0 coordinated across vue-hackernews-2.0 and tabler"
VERSION_START = "2025-08-05"
VERSION_DUE = "2025-09-20"

PROJECT_A = "vue-hackernews-2.0"
PROJECT_B = "tabler"
FEATURES_A = [
    "Infinite scroll pagination for story lists",
    "Dark mode toggle with localStorage persistence",
]
FEATURES_B = [
    "Accessible color contrast audit across all components",
    "New timeline component with responsive variants",
]
COMMIT_MSG = "docs(changelog): prepare v1.5.0"

GATES = ["CodeFreeze", "QASignoff", "StagingDeploy", "ProductionDeploy"]
GATE_DATES = {
    "CodeFreeze": "2025-08-28",
    "QASignoff": "2025-09-06",
    "StagingDeploy": "2025-09-13",
    "ProductionDeploy": "2025-09-20",
}
GATE_OWNERS = {
    "CodeFreeze": "Eric Rothman",
    "QASignoff": "Richard Rethman",
    "StagingDeploy": "Thomas Nickson",
    "ProductionDeploy": "Sandra Love",
}
PROJECTS = [PROJECT_A, PROJECT_B]

# Expected 8 rows in order
EXPECTED_ROWS = []
_row_idx = 1
for gate in GATES:
    for proj in PROJECTS:
        EXPECTED_ROWS.append({
            "gate_id": f"G-{_row_idx:02d}",
            "gate_name": gate,
            "project": proj,
            "target_date": GATE_DATES[gate],
            "status": "NotStarted",
            "owner": GATE_OWNERS[gate],
        })
        _row_idx += 1


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


def op_sql(query: str) -> str:
    """Run a SQL query against OpenProject's embedded Postgres."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql error: {r.stderr.strip()}")
    return r.stdout.strip()


def baserow_sql(query: str) -> str:
    """Run a SQL query against Baserow's Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip()


def baserow_auth() -> str:
    """Authenticate to Baserow API and return JWT access token."""
    r = requests.post(
        f"{BASEROW_BASE}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("access_token") or data["token"]


def _find_project_dir(project_name: str) -> str:
    """Find the project directory inside code-server container."""
    rc, out, _ = docker_exec(
        CODE_SERVER_CONTAINER,
        "find", "/home", "-maxdepth", "4", "-type", "d", "-name", project_name,
        timeout=10,
    )
    if rc == 0 and out.strip():
        return out.strip().split("\n")[0]
    # Fallback: try /config/workspace
    rc2, out2, _ = docker_exec(
        CODE_SERVER_CONTAINER,
        "find", "/config", "-maxdepth", "4", "-type", "d", "-name", project_name,
        timeout=10,
    )
    if rc2 == 0 and out2.strip():
        return out2.strip().split("\n")[0]
    return ""


def _expected_changelog_lines(features: list[str]) -> list[str]:
    """The exact 5-line release block the finished CHANGELOG.md must contain."""
    return [
        "## [Unreleased]",
        f"## [{VERSION_NAME}] - {VERSION_DUE}",
        "### Added",
        f"- {features[0]}",
        f"- {features[1]}",
    ]


def _changelog_block_issues(content: str, features: list[str]) -> list[str]:
    """Whole-file contiguous-block assertion for a finished CHANGELOG.md.

    Seed CHANGELOG.md in both repos is exactly one line ("## [Unreleased]",
    verified untracked/single-line in code-server-bundle:latest), so the
    finished file is fully determined: exactly 5 lines, in order. Each line is
    rstrip()ed and trailing blank lines are dropped before comparison.

    NOTE (plan risk ①): if a future image rebuild ships a longer seed
    CHANGELOG, downgrade this to the anchored contiguous-block regex instead
    of reverting to scattered substring checks, e.g.:
        re.search(
            r"(?m)^## \\[Unreleased\\]\\n## \\[v1\\.5\\.0\\] - 2025-09-20\\n"
            r"### Added\\n- <feat1>\\n- <feat2>\\s*$",
            content,
        )
    """
    lines = [ln.rstrip() for ln in content.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    expected = _expected_changelog_lines(features)
    if len(lines) != len(expected):
        return [f"expected exactly {len(expected)} lines, got {len(lines)}"]
    issues = []
    for i, (got, exp) in enumerate(zip(lines, expected), 1):
        if got != exp:
            issues.append(f"line {i}: {got!r} != {exp!r}")
    return issues


def _find_table_id(headers: dict) -> int | None:
    """Locate the 'Release Readiness' table id via the Baserow API."""
    r = requests.get(f"{BASEROW_BASE}/api/applications/", headers=headers, timeout=10)
    r.raise_for_status()
    for app in r.json():
        if app.get("name") == "Release v1.5.0 Coordination":
            for tbl in app.get("tables", []):
                if tbl.get("name") == "Release Readiness":
                    return tbl["id"]
            return None
    return None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_op_version() -> None:
    """OpenProject version v1.5.0 exists with correct attributes."""
    try:
        row = op_sql(
            f"SELECT v.name, v.status, v.start_date, v.effective_date, v.description "
            f"FROM versions v JOIN projects p ON v.project_id = p.id "
            f"WHERE p.name = '{OP_PROJECT}' AND v.name = '{VERSION_NAME}';"
        )
        if not row:
            check("1. OP version v1.5.0", 2, False, "version not found")
            return
        parts = row.split("|")
        name = parts[0]
        status = parts[1]
        start = parts[2]
        due = parts[3]
        desc = parts[4] if len(parts) > 4 else ""
        issues = []
        if name != VERSION_NAME:
            issues.append(f"name={name}")
        if status != "open":
            issues.append(f"status={status}")
        if start != VERSION_START:
            issues.append(f"start={start}")
        if due != VERSION_DUE:
            issues.append(f"due={due}")
        if desc.strip() != VERSION_DESC:
            issues.append(f"desc mismatch: {desc.strip()!r}")
        check("1. OP version v1.5.0", 2, not issues,
              "all attributes correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("1. OP version v1.5.0", 2, False, f"exception: {e}")


def _check_changelog(check_num: int, project_name: str, features: list[str]) -> None:
    """Verify CHANGELOG.md is exactly the 5-line release block (contiguous, in order)."""
    label = f"{check_num}. {project_name} CHANGELOG.md"
    try:
        proj_dir = _find_project_dir(project_name)
        if not proj_dir:
            check(label, 2, False, "project directory not found in container")
            return
        changelog_path = f"{proj_dir}/CHANGELOG.md"
        rc, content, _ = docker_exec(CODE_SERVER_CONTAINER, "cat", changelog_path)
        if rc != 0:
            check(label, 2, False, f"file not found: {changelog_path}")
            return
        issues = _changelog_block_issues(content, features)
        check(label, 2, not issues,
              "exact 5-line release block" if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_changelog_a() -> None:
    _check_changelog(2, PROJECT_A, FEATURES_A)


def check_3_changelog_b() -> None:
    _check_changelog(3, PROJECT_B, FEATURES_B)


def _check_git_commit(check_num: int, project_name: str, features: list[str]) -> None:
    """Three-stage git assertion for the changelog commit.

    (a) `git log --all` contains a commit whose subject is EXACTLY the
        required message; (b) that commit touches exactly one file,
        CHANGELOG.md ("stage only that file" — for untracked-new files
        `git show --name-only` still lists the path); (c) the committed
        blob (`git show <sha>:CHANGELOG.md`) passes the same 5-line block
        assertion as the working tree (commit content, not just message).
    """
    label = f"{check_num}. Git commit {project_name}"
    try:
        proj_dir = _find_project_dir(project_name)
        if not proj_dir:
            check(label, 1, False, "project directory not found")
            return
        git = ["git", "-c", f"safe.directory={proj_dir}", "-C", proj_dir]
        # (a) locate commit(s) with the exact subject
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER, *git,
            "log", "--all", "--format=%H%x00%s",
            timeout=10,
        )
        if rc != 0:
            check(label, 1, False, f"git log failed: {err.strip()[:120]}")
            return
        shas = []
        for line in out.splitlines():
            if "\x00" not in line:
                continue
            sha, subj = line.split("\x00", 1)
            if subj == COMMIT_MSG:
                shas.append(sha.strip())
        if not shas:
            check(label, 1, False, f"no commit with exact subject {COMMIT_MSG!r}")
            return
        # (b)+(c): pass if any exact-subject commit satisfies both stages
        fail_details = []
        for sha in shas:
            issues = []
            rc, out, err = docker_exec(
                CODE_SERVER_CONTAINER, *git,
                "show", "--name-only", "--format=", sha,
                timeout=10,
            )
            if rc != 0:
                issues.append(f"git show --name-only failed: {err.strip()[:80]}")
            else:
                names = [ln.strip() for ln in out.splitlines() if ln.strip()]
                if names != ["CHANGELOG.md"]:
                    issues.append(
                        f"commit touches {names!r}, expected exactly ['CHANGELOG.md']")
            rc, blob, err = docker_exec(
                CODE_SERVER_CONTAINER, *git,
                "show", f"{sha}:CHANGELOG.md",
                timeout=10,
            )
            if rc != 0:
                issues.append("CHANGELOG.md missing from commit tree")
            else:
                issues.extend(
                    f"committed {i}" for i in _changelog_block_issues(blob, features))
            if not issues:
                check(label, 1, True,
                      f"commit {sha[:10]}: single-file CHANGELOG.md, 5-line block OK")
                return
            fail_details.append(f"{sha[:10]}: {'; '.join(issues[:3])}")
        check(label, 1, False, " | ".join(fail_details[:2]))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_4_git_commit_a() -> None:
    _check_git_commit(4, PROJECT_A, FEATURES_A)


def check_5_git_commit_b() -> None:
    _check_git_commit(5, PROJECT_B, FEATURES_B)


def check_6_baserow_database() -> None:
    """Baserow database 'Release v1.5.0 Coordination' exists."""
    try:
        row = baserow_sql(
            "SELECT a.id FROM core_application a "
            "WHERE a.name = 'Release v1.5.0 Coordination';"
        )
        found = bool(row.strip())
        check("6. Baserow database exists", 1, found,
              f"db_id={row.strip()}" if found else "database not found")
    except Exception as e:
        check("6. Baserow database exists", 1, False, f"exception: {e}")


def check_7_baserow_table_fields() -> None:
    """Field schema: EXACTLY the 6 specified fields, correct types/options.

    Field name set must equal the 6 spec fields (residual default fields
    like Notes/Active fail); Gate ID primary text; Gate Name/Project/Status
    single-selects with exact option sets; Target Date date; Owner text.
    """
    label = "7. Baserow table & fields"
    try:
        token = baserow_auth()
        headers = {"Authorization": f"JWT {token}"}
        table_id = _find_table_id(headers)
        if table_id is None:
            check(label, 1, False, "table not found via API")
            return
        r = requests.get(
            f"{BASEROW_BASE}/api/database/fields/table/{table_id}/",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        by_name = {f.get("name"): f for f in r.json()}
        issues = []
        expected_names = {"Gate ID", "Gate Name", "Project", "Target Date", "Status", "Owner"}
        actual_names = set(by_name)
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            extra = sorted(actual_names - expected_names)
            if missing:
                issues.append(f"missing fields: {missing}")
            if extra:
                issues.append(f"extra fields: {extra}")

        def _opts(f: dict) -> set:
            return {o.get("value") for o in (f.get("select_options") or [])}

        expected_types = {
            "Gate ID": "text",
            "Gate Name": "single_select",
            "Project": "single_select",
            "Target Date": "date",
            "Status": "single_select",
            "Owner": "text",
        }
        expected_options = {
            "Gate Name": set(GATES),
            "Project": {PROJECT_A, PROJECT_B, "Both"},
            "Status": {"NotStarted", "InProgress", "Done", "Blocked"},
        }
        for name, exp_type in expected_types.items():
            f = by_name.get(name)
            if f is None:
                continue  # already reported as missing
            if f.get("type") != exp_type:
                issues.append(f"{name}: type={f.get('type')!r}, expected {exp_type!r}")
            elif name in expected_options and _opts(f) != expected_options[name]:
                issues.append(
                    f"{name}: options={sorted(_opts(f))}, "
                    f"expected {sorted(expected_options[name])}")
        gate_id = by_name.get("Gate ID")
        if gate_id is not None and not gate_id.get("primary"):
            issues.append("Gate ID is not the primary field")
        check(label, 1, not issues,
              "schema exact (6 fields, types, option sets)" if not issues
              else "; ".join(issues[:4]))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_8_baserow_rows() -> None:
    """Baserow table has exactly 8 rows with correct data."""
    try:
        token = baserow_auth()
        headers = {"Authorization": f"JWT {token}"}
        table_id = _find_table_id(headers)
        if table_id is None:
            check("8. Baserow 8 rows", 2, False, "table not found via API")
            return

        # Get rows
        r = requests.get(
            f"{BASEROW_BASE}/api/database/rows/table/{table_id}/?user_field_names=true&size=50",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("results", [])

        if len(rows) != 8:
            check("8. Baserow 8 rows", 2, False, f"expected 8 rows, got {len(rows)}")
            return

        issues = []
        for i, (row, exp) in enumerate(zip(rows, EXPECTED_ROWS)):
            row_issues = []
            # Gate ID
            gate_id_val = str(row.get("Gate ID", "")).strip()
            if gate_id_val != exp["gate_id"]:
                row_issues.append(f"gate_id={gate_id_val!r} expected {exp['gate_id']!r}")
            # Gate Name (single-select → dict with "value")
            gate_name_val = row.get("Gate Name")
            if isinstance(gate_name_val, dict):
                gate_name_val = gate_name_val.get("value", "")
            gate_name_val = str(gate_name_val or "").strip()
            if gate_name_val != exp["gate_name"]:
                row_issues.append(f"gate_name={gate_name_val!r}")
            # Project
            proj_val = row.get("Project")
            if isinstance(proj_val, dict):
                proj_val = proj_val.get("value", "")
            proj_val = str(proj_val or "").strip()
            if proj_val != exp["project"]:
                row_issues.append(f"project={proj_val!r}")
            # Target Date
            date_val = str(row.get("Target Date", "")).strip()
            if not date_val.startswith(exp["target_date"]):
                row_issues.append(f"date={date_val!r}")
            # Status
            status_val = row.get("Status")
            if isinstance(status_val, dict):
                status_val = status_val.get("value", "")
            status_val = str(status_val or "").strip()
            if status_val != exp["status"]:
                row_issues.append(f"status={status_val!r}")
            # Owner
            owner_val = str(row.get("Owner", "")).strip()
            if owner_val != exp["owner"]:
                row_issues.append(f"owner={owner_val!r}")

            if row_issues:
                issues.append(f"row {i+1} ({exp['gate_id']}): {', '.join(row_issues)}")

        check("8. Baserow 8 rows", 2, not issues,
              "all 8 rows correct" if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("8. Baserow 8 rows", 2, False, f"exception: {e}")


def check_9_baserow_gallery_view() -> None:
    """Baserow Gallery view 'Gate Progress' exists on Release Readiness table."""
    try:
        token = baserow_auth()
        headers = {"Authorization": f"JWT {token}"}
        table_id = _find_table_id(headers)
        if table_id is None:
            check("9. Baserow Gallery view", 1, False, "table not found")
            return

        # List views
        r = requests.get(
            f"{BASEROW_BASE}/api/database/views/table/{table_id}/",
            headers=headers, timeout=10,
        )
        r.raise_for_status()
        views = r.json()
        gallery_views = [v for v in views if v.get("name") == "Gate Progress"]
        if not gallery_views:
            view_names = [v.get("name") for v in views]
            check("9. Baserow Gallery view", 1, False, f"view not found; existing views: {view_names}")
            return
        v = gallery_views[0]
        is_gallery = v.get("type") == "gallery"
        check("9. Baserow Gallery view", 1, is_gallery,
              f"type={v.get('type')}" if not is_gallery else "gallery view found")
    except Exception as e:
        check("9. Baserow Gallery view", 1, False, f"exception: {e}")


def check_10_op_milestones_exist() -> None:
    """OpenProject has 8 Milestone work packages with correct subjects."""
    try:
        rows_raw = op_sql(
            f"SELECT wp.subject FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"JOIN versions v ON wp.version_id = v.id "
            f"WHERE p.name = '{OP_PROJECT}' AND t.name = 'Milestone' "
            f"AND v.name = '{VERSION_NAME}' ORDER BY wp.subject;"
        )
        found_subjects = set(
            line.strip() for line in rows_raw.split("\n") if line.strip()
        )
        expected_subjects = set()
        for gate in GATES:
            for proj in PROJECTS:
                expected_subjects.add(f"[{gate}] {proj}: {VERSION_NAME}")

        missing = expected_subjects - found_subjects
        extra = found_subjects - expected_subjects
        issues = []
        if missing:
            issues.append(f"missing: {missing}")
        if extra:
            issues.append(f"extra: {extra}")
        check("10. OP 8 Milestones exist", 1, not issues,
              f"all 8 found" if not issues else "; ".join(issues))
    except Exception as e:
        check("10. OP 8 Milestones exist", 1, False, f"exception: {e}")


def check_11_op_milestone_priorities() -> None:
    """OP milestones have correct priorities: High for StagingDeploy/ProductionDeploy, Normal otherwise."""
    try:
        rows_raw = op_sql(
            f"SELECT wp.subject, e.name AS priority "
            f"FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"JOIN versions v ON wp.version_id = v.id "
            f"LEFT JOIN enumerations e ON wp.priority_id = e.id "
            f"WHERE p.name = '{OP_PROJECT}' AND t.name = 'Milestone' "
            f"AND v.name = '{VERSION_NAME}';"
        )
        issues = []
        lines_found = 0
        for line in rows_raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            lines_found += 1
            parts = line.split("|")
            subject = parts[0].strip()
            priority = parts[1].strip() if len(parts) > 1 else ""
            # Determine expected priority from subject
            is_high = any(g in subject for g in ["StagingDeploy", "ProductionDeploy"])
            expected_priority = "High" if is_high else "Normal"
            if priority != expected_priority:
                issues.append(f"{subject}: priority={priority}, expected={expected_priority}")
        if lines_found == 0:
            issues.append("no milestone work packages found")
        check("11. OP Milestone priorities", 2, not issues,
              "all correct" if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("11. OP Milestone priorities", 2, False, f"exception: {e}")


def check_12_op_milestone_descriptions() -> None:
    """OP milestones have correct descriptions."""
    try:
        rows_raw = op_sql(
            f"SELECT wp.subject, wp.description "
            f"FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"JOIN versions v ON wp.version_id = v.id "
            f"WHERE p.name = '{OP_PROJECT}' AND t.name = 'Milestone' "
            f"AND v.name = '{VERSION_NAME}';"
        )
        issues = []
        lines_found = 0
        for line in rows_raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            lines_found += 1
            parts = line.split("|", 1)
            subject = parts[0].strip()
            desc = parts[1].strip() if len(parts) > 1 else ""
            # Parse gate and project from subject: "[GateName] Project: v1.5.0"
            m = re.match(r"\[(\w+)\]\s+(.+?):\s+v1\.5\.0", subject)
            if not m:
                issues.append(f"cannot parse subject: {subject}")
                continue
            gate = m.group(1)
            proj = m.group(2)
            owner = GATE_OWNERS.get(gate, "?")
            expected_desc = (
                f"Release: {VERSION_NAME}; Gate: {gate}; Project: {proj}; Owner: {owner}"
            )
            # OpenProject may store description as HTML; normalize
            desc_clean = re.sub(r"<[^>]+>", "", desc).strip()
            if desc_clean != expected_desc:
                issues.append(
                    f"{subject}: desc={desc_clean!r}, expected={expected_desc!r}"
                )
        if lines_found == 0:
            issues.append("no milestone work packages found")
        check("12. OP Milestone descriptions", 2, not issues,
              "all correct" if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("12. OP Milestone descriptions", 2, False, f"exception: {e}")


def check_13_op_milestones_version() -> None:
    """All OP milestones are assigned to version v1.5.0."""
    try:
        # Count milestones in API Gateway with type Milestone assigned to v1.5.0
        count_str = op_sql(
            f"SELECT COUNT(*) FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"JOIN versions v ON wp.version_id = v.id "
            f"WHERE p.name = '{OP_PROJECT}' AND t.name = 'Milestone' "
            f"AND v.name = '{VERSION_NAME}';"
        )
        count = int(count_str.strip())
        check("13. OP Milestones → v1.5.0", 1, count == 8,
              f"{count}/8 assigned to v1.5.0")
    except Exception as e:
        check("13. OP Milestones → v1.5.0", 1, False, f"exception: {e}")


def check_a_op_milestone_dates() -> None:
    """Each Milestone's start date == its gate's Target Date (spec: 'start date =
    Target Date'); Milestone type also implies due_date == start_date."""
    label = "A. OP Milestone start dates"
    try:
        rows_raw = op_sql(
            f"SELECT wp.subject, wp.start_date, wp.due_date "
            f"FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"JOIN versions v ON wp.version_id = v.id "
            f"WHERE p.name = '{OP_PROJECT}' AND t.name = 'Milestone' "
            f"AND v.name = '{VERSION_NAME}';"
        )
        issues = []
        lines_found = 0
        for line in rows_raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            lines_found += 1
            parts = line.split("|")
            subject = parts[0].strip()
            start = parts[1].strip() if len(parts) > 1 else ""
            due = parts[2].strip() if len(parts) > 2 else ""
            m = re.match(r"\[(\w+)\]", subject)
            if not m:
                issues.append(f"cannot parse subject: {subject}")
                continue
            gate = m.group(1)
            expected = GATE_DATES.get(gate)
            if expected is None:
                issues.append(f"unknown gate in subject: {subject}")
                continue
            if start != expected:
                issues.append(f"{subject}: start_date={start!r}, expected={expected!r}")
            if due != start:
                issues.append(f"{subject}: due_date={due!r} != start_date={start!r}")
        if lines_found == 0:
            issues.append("no milestone work packages found")
        check(label, 2, not issues,
              "all start dates == Target Date" if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_op_version()
    check_2_changelog_a()
    check_3_changelog_b()
    check_4_git_commit_a()
    check_5_git_commit_b()
    check_6_baserow_database()
    check_7_baserow_table_fields()
    check_8_baserow_rows()
    check_9_baserow_gallery_view()
    check_10_op_milestones_exist()
    check_11_op_milestone_priorities()
    check_12_op_milestone_descriptions()
    check_13_op_milestones_version()
    check_a_op_milestone_dates()

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
