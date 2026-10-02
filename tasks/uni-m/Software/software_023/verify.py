#!/usr/bin/env python3
"""
Verifier for Software-023-I4: TypeScript upgrade campaign across 4 projects with
Baserow inventory and OpenProject Epic.

Checks: 9 weighted checks (total weight 17) across code-server, baserow, openproject.
Strategy: Baserow API, OpenProject docker exec (DB). The manifest ground truth is
recomputed in a throwaway container from the code-server container's own pristine
image (docker inspect → docker run), so live-workspace edits cannot move it.

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import json
import os
import re
import subprocess
import sys

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

_required_vars = {
    "CODE_SERVER_PORT": None, "CODE_SERVER_CONTAINER": None,
    "BASEROW_PORT": None, "BASEROW_CONTAINER": None, "BASEROW_DB_CONTAINER": None,
    "OPENPROJECT_PORT": None, "OPENPROJECT_CONTAINER": None,
}
for var in _required_vars:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    _required_vars[var] = val

CODE_SERVER_CONTAINER = _required_vars["CODE_SERVER_CONTAINER"]
BASEROW_PORT = _required_vars["BASEROW_PORT"]
BASEROW_DB_CONTAINER = _required_vars["BASEROW_DB_CONTAINER"]
OPENPROJECT_CONTAINER = _required_vars["OPENPROJECT_CONTAINER"]

BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"
BASEROW_EMAIL = "admin@example.com"
BASEROW_PASS = "Admin1234"

OP_DB = "openproject"
OP_USER = "openproject"
OP_PASS = "openproject"

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


def op_sql(query: str) -> str:
    """Run a SQL query against OpenProject's embedded Postgres."""
    r = subprocess.run(
        ["docker", "exec", "-i", OPENPROJECT_CONTAINER,
         "bash", "-c",
         f"PGPASSWORD={OP_PASS} psql -h 127.0.0.1 -U {OP_USER} -d {OP_DB} -t -A"],
        input=query,
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql error: {r.stderr.strip()}")
    return r.stdout.strip()


def baserow_auth() -> str:
    """Get Baserow JWT token."""
    r = requests.post(
        f"{BASEROW_BASE}/api/user/token-auth/",
        json={"email": BASEROW_EMAIL, "password": BASEROW_PASS},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def baserow_get(path: str, token: str) -> dict | list:
    r = requests.get(
        f"{BASEROW_BASE}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ── Shared state across checks ───────────────────────────────────────────────
_br_token: str = ""
_br_table_id: int | None = None
_br_field_map: dict[str, int] = {}  # field name -> field id
_br_rows: list[dict] = []


def _init_baserow() -> bool:
    """Authenticate and locate the database/table. Returns True on success."""
    global _br_token, _br_table_id, _br_field_map, _br_rows
    try:
        _br_token = baserow_auth()
    except Exception as e:
        return False

    # Find database
    apps = baserow_get("applications/", _br_token)
    db_id = None
    for app in apps:
        if app.get("name") == "TypeScript Upgrade Campaign July 2026" and app.get("type") == "database":
            db_id = app["id"]
            break
    if db_id is None:
        return False

    # Find table
    tables = baserow_get(f"database/tables/database/{db_id}/", _br_token)
    for t in tables:
        if t.get("name") == "Upgrade Inventory":
            _br_table_id = t["id"]
            break
    if _br_table_id is None:
        return False

    # Load fields
    fields = baserow_get(f"database/fields/table/{_br_table_id}/", _br_token)
    _br_field_map = {f["name"]: f["id"] for f in fields}

    # Load rows
    resp = baserow_get(f"database/rows/table/{_br_table_id}/?user_field_names=true", _br_token)
    _br_rows = resp.get("results", []) if isinstance(resp, dict) else resp

    return True


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_database_exists() -> None:
    """Baserow database 'TypeScript Upgrade Campaign July 2026' exists."""
    try:
        if not _br_token:
            raise RuntimeError("Baserow auth failed")
        apps = baserow_get("applications/", _br_token)
        found = any(
            a.get("name") == "TypeScript Upgrade Campaign July 2026" and a.get("type") == "database"
            for a in apps
        )
        check("1. Baserow DB exists", 1, found,
              "TypeScript Upgrade Campaign July 2026" if found else "database not found")
    except Exception as e:
        check("1. Baserow DB exists", 1, False, f"exception: {e}")


def check_2_table_and_fields() -> None:
    """Table 'Upgrade Inventory' with correct field schema (names, types, options)."""
    try:
        if _br_table_id is None:
            check("2. Table & fields", 2, False, "table not found")
            return
        fields = baserow_get(f"database/fields/table/{_br_table_id}/", _br_token)
        by_name = {f.get("name"): f for f in fields}
        expected_types = {
            "Project": "text", "Manifest Path": "text", "Current Version": "text",
            "Target Version": "text", "Migration Complexity": "single_select",
            "Status": "single_select", "Captured At": "date",
        }
        expected_options = {
            "Migration Complexity": {"Low", "Medium", "High"},
            "Status": {"Pending", "InProgress", "Done"},
        }
        issues = []
        missing = set(expected_types) - set(by_name)
        if missing:
            issues.append(f"missing fields: {sorted(missing)}")
        for name, ftype in expected_types.items():
            f = by_name.get(name)
            if f is None:
                continue  # already reported as missing
            if f.get("type") != ftype:
                issues.append(f"{name}: type={f.get('type')!r}, expected {ftype!r}")
            if name == "Project" and not f.get("primary"):
                issues.append("Project: not the primary field")
            if name in expected_options:
                opts = {o.get("value") for o in f.get("select_options", [])}
                if opts != expected_options[name]:
                    issues.append(
                        f"{name}: options={sorted(opts)}, "
                        f"expected {sorted(expected_options[name])}"
                    )
        check("2. Table & fields", 2, not issues,
              "schema OK" if not issues else "; ".join(issues))
    except Exception as e:
        check("2. Table & fields", 2, False, f"exception: {e}")



def _high_option_ids() -> set[str]:
    """IDs of select options labelled 'High' on the Migration Complexity field.

    Baserow stores single_select_equal filter values as option IDs, so a
    functionally correct filter carries the ID, not the label.
    """
    try:
        fields = baserow_get(f"database/fields/table/{_br_table_id}/", _br_token)
        for f in fields:
            if f.get("name") == "Migration Complexity":
                return {str(o.get("id")) for o in f.get("select_options", [])
                        if o.get("value") == "High"}
    except Exception:
        pass
    return set()


_WORKSPACE = "/home/coder/workspace"
_TS_PROJECT_DIRS = ("blog-engine", "tabler", "todo-api", "weather-dashboard")

_gt_projects: dict[str, tuple[str, str]] | None = None
_gt_error: str | None = None


def _discover_ts_projects() -> dict[str, tuple[str, str]]:
    """Ground truth: {project: (relative manifest path, pinned typescript version)}.

    Recomputed once at verify time in a throwaway container from the
    code-server container's own PRISTINE image (find over the four project
    dirs excluding node_modules, grep for typescript, then version extraction
    from each hit) so the expected set tracks the data as shipped at task
    release — agent edits to the live workspace cannot move the goalposts.
    If truth acquisition fails (missing project dir, find/grep/parse execution
    error — as opposed to legitimately zero hits), _gt_error is set and every
    dependent check must FAIL instead of passing on an empty expected set.
    """
    global _gt_projects, _gt_error
    if _gt_projects is not None or _gt_error is not None:
        return _gt_projects or {}
    try:
        for proj in _TS_PROJECT_DIRS:
            rc, _, _ = image_exec("test", "-d", f"{_WORKSPACE}/{proj}", timeout=60)
            if rc != 0:
                _gt_error = f"project dir missing: {_WORKSPACE}/{proj}"
                return {}
        dirs = " ".join(f"{_WORKSPACE}/{p}" for p in _TS_PROJECT_DIRS)
        rc, out, err = image_exec(
            "bash", "-c",
            f"find {dirs} \\( -name package.json -o -name requirements.txt \\) "
            f"-not -path '*/node_modules/*' -print",
            timeout=120,
        )
        if rc != 0:
            _gt_error = f"find failed: {err.strip()[:200]}"
            return {}
        manifests = sorted(p for p in out.strip().splitlines() if p.strip())
        found: dict[str, tuple[str, str]] = {}
        for path in manifests:
            rc, _, err = image_exec("grep", "-l", "typescript", path, timeout=60)
            if rc == 1:
                continue  # legitimately no match
            if rc != 0:
                _gt_error = f"grep failed on {path}: {err.strip()[:200]}"
                return {}
            rel = path[len(_WORKSPACE) + 1:] if path.startswith(_WORKSPACE + "/") else path
            proj = rel.split("/", 1)[0]
            if proj in found:
                continue  # first manifest per project wins (sorted order)
            if path.endswith("package.json"):
                rc, vout, verr = image_exec(
                    "python3", "-c",
                    "import json,sys;d=json.load(open(sys.argv[1]));"
                    "print({**d.get('dependencies',{}),**d.get('devDependencies',{})}"
                    ".get('typescript',''))",
                    path,
                    timeout=60,
                )
                if rc != 0:
                    _gt_error = f"manifest parse failed on {path}: {verr.strip()[:200]}"
                    return {}
                version = vout.strip()
            else:  # requirements.txt
                rc, vout, verr = image_exec("cat", path, timeout=60)
                if rc != 0:
                    _gt_error = f"cat failed on {path}: {verr.strip()[:200]}"
                    return {}
                m = re.search(r"^typescript\s*==\s*([^\s#]+)", vout, re.MULTILINE)
                version = m.group(1) if m else ""
            if version:
                found[proj] = (rel, version)
        _gt_projects = found
        return _gt_projects
    except Exception as e:
        _gt_error = f"discovery exception: {e}"
        return {}


def check_3_row_projects() -> None:
    """One row per discovered project, in alphabetical order."""
    try:
        gt = _discover_ts_projects()
        if _gt_error:
            check("3. Row projects (alpha order)", 2, False,
                  f"ground truth unavailable: {_gt_error}")
            return
        projects = [r.get("Project", "") for r in _br_rows]
        expected = sorted(gt)
        ok = projects == expected
        check("3. Row projects (alpha order)", 2, ok,
              f"expected {expected}, got {projects}")
    except Exception as e:
        check("3. Row projects (alpha order)", 2, False, f"exception: {e}")


def check_3b_manifest_and_version() -> None:
    """Manifest Path and Current Version match the recomputed manifest truth."""
    try:
        gt = _discover_ts_projects()
        if _gt_error:
            check("3b. Manifest path & current version", 2, False,
                  f"ground truth unavailable: {_gt_error}")
            return
        rows_by_proj: dict[str, dict] = {}
        for r in _br_rows:
            rows_by_proj.setdefault(str(r.get("Project", "")), r)
        issues = []
        for proj, (gt_path, gt_ver) in sorted(gt.items()):
            row = rows_by_proj.get(proj)
            if row is None:
                issues.append(f"{proj}: no row")
                continue
            mp = str(row.get("Manifest Path") or "").strip()
            if mp.startswith(_WORKSPACE + "/"):
                mp = mp[len(_WORKSPACE) + 1:]
            if mp != gt_path:
                issues.append(f"{proj}: Manifest Path={mp!r}, expected {gt_path!r}")
            cv = str(row.get("Current Version") or "").strip()
            accepted = {gt_ver, gt_ver.lstrip("^~")}
            if cv not in accepted:
                issues.append(f"{proj}: Current Version={cv!r}, expected one of {sorted(accepted)}")
        check("3b. Manifest path & current version", 2, not issues,
              f"truth={gt}" if not issues else "; ".join(issues))
    except Exception as e:
        check("3b. Manifest path & current version", 2, False, f"exception: {e}")


def _get_select_value(row: dict, field_name: str) -> str:
    """Extract the display value from a single-select field."""
    val = row.get(field_name, "")
    if isinstance(val, dict):
        return val.get("value", "")
    return str(val) if val else ""


def check_4_row_field_values() -> None:
    """Target Version, Migration Complexity, Captured At are correct per row."""
    try:
        if not _br_rows:
            check("4. Row field values", 2, False, "no rows found")
            return
        complexity_map = {
            "tabler": "High", "weather-dashboard": "Medium",
            "todo-api": "Low", "blog-engine": "Low",
        }
        issues = []
        for row in _br_rows:
            proj = row.get("Project", "")
            tv = row.get("Target Version", "")
            if tv != "5.4.5":
                issues.append(f"{proj}: Target Version={tv!r}, expected '5.4.5'")
            mc = _get_select_value(row, "Migration Complexity")
            expected_mc = complexity_map.get(proj, "?")
            if mc != expected_mc:
                issues.append(f"{proj}: Migration Complexity={mc!r}, expected {expected_mc!r}")
            ca = row.get("Captured At", "")
            if not (isinstance(ca, str) and ca.startswith("2026-07-08")):
                issues.append(f"{proj}: Captured At={ca!r}, expected 2026-07-08")
        check("4. Row field values", 2, not issues,
              "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("4. Row field values", 2, False, f"exception: {e}")


def check_5_status_values() -> None:
    """Every inserted row has Status=Pending."""
    try:
        if not _br_rows:
            check("5. Status values", 1, False, "no rows found")
            return
        issues = []
        for row in _br_rows:
            proj = row.get("Project", "")
            status = _get_select_value(row, "Status")
            if status != "Pending":
                issues.append(f"{proj}: Status={status!r}, expected 'Pending'")
        check("5. Status values", 1, not issues,
              "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("5. Status values", 1, False, f"exception: {e}")


def check_6_high_complexity_view() -> None:
    """'High Complexity' grid view exists with Migration Complexity=High filter."""
    try:
        if _br_table_id is None:
            check("6. High Complexity view", 2, False, "table not found")
            return
        views = baserow_get(f"database/views/table/{_br_table_id}/", _br_token)
        view = None
        for v in views:
            if v.get("name") == "High Complexity":
                view = v
                break
        if view is None:
            check("6. High Complexity view", 2, False, "view 'High Complexity' not found")
            return
        if view.get("type") != "grid":
            check("6. High Complexity view", 2, False,
                  f"view type={view.get('type')!r}, expected 'grid'")
            return

        filters = baserow_get(f"database/views/{view['id']}/filters/", _br_token)
        # filters could be a list or dict with results
        filter_list = filters if isinstance(filters, list) else filters.get("results", filters)
        mc_field_id = _br_field_map.get("Migration Complexity")
        high_ids = _high_option_ids()
        has_filter = False
        for f in filter_list:
            if (f.get("field") == mc_field_id
                    and f.get("type") == "single_select_equal"
                    and str(f.get("value", "")).strip() in high_ids):
                has_filter = True
                break
        check("6. High Complexity view", 2, has_filter,
              "grid view + single_select_equal High filter OK" if has_filter
              else f"filter not found (filters={filter_list})")
    except Exception as e:
        check("6. High Complexity view", 2, False, f"exception: {e}")


def check_8_openproject_epic() -> None:
    """Exactly one Epic 'Upgrade typescript to 5.4.5' with exact description and Normal priority."""
    try:
        gt = _discover_ts_projects()
        if _gt_error:
            check("8. OpenProject Epic", 2, False,
                  f"ground truth unavailable: {_gt_error}")
            return
        base = (
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Mobile App Redesign' "
            "AND t.name = 'Epic' "
            "AND wp.subject = 'Upgrade typescript to 5.4.5'"
        )
        count = op_sql(f"SELECT COUNT(*) {base}")
        if count.strip() != "1":
            check("8. OpenProject Epic", 2, False,
                  f"expected exactly 1 Epic, found {count.strip() or '0'}")
            return

        # Per-column single-value queries (description may contain '|' / newlines)
        description = op_sql(f"SELECT wp.description {base}")
        priority = op_sql(
            "SELECT e.name FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "LEFT JOIN enumerations e ON wp.priority_id = e.id "
            "WHERE p.name = 'Mobile App Redesign' "
            "AND t.name = 'Epic' "
            "AND wp.subject = 'Upgrade typescript to 5.4.5'"
        )

        expected_desc = f"Campaign Date: 2026-07-08; Target: 5.4.5; Projects: {len(gt)}"
        issues = []
        if description.strip() != expected_desc:
            issues.append(f"description={description.strip()!r}, expected {expected_desc!r}")
        if priority.strip() != "Normal":
            issues.append(f"priority={priority.strip()!r}, expected 'Normal'")
        check("8. OpenProject Epic", 2, not issues,
              f"unique Epic, exact description (Projects: {len(gt)}), priority Normal"
              if not issues else "; ".join(issues))
    except Exception as e:
        check("8. OpenProject Epic", 2, False, f"exception: {e}")


def check_9_openproject_tasks() -> None:
    """One child Task per discovered project: exact subject, assignee login=admin, priority."""
    try:
        gt = _discover_ts_projects()
        if _gt_error:
            check("9. OpenProject Tasks", 3, False,
                  f"ground truth unavailable: {_gt_error}")
            return
        # Get Epic ID
        epic_id = op_sql(
            "SELECT wp.id FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.name = 'Mobile App Redesign' "
            "AND t.name = 'Epic' "
            "AND wp.subject = 'Upgrade typescript to 5.4.5'"
        )
        if not epic_id:
            check("9. OpenProject Tasks", 3, False, "parent Epic not found")
            return

        # Get child tasks
        rows = op_sql(
            f"SELECT wp.subject, u.login, u.firstname, u.lastname, "
            f"e.name AS priority_name "
            f"FROM work_packages wp "
            f"JOIN types t ON wp.type_id = t.id "
            f"LEFT JOIN users u ON wp.assigned_to_id = u.id "
            f"LEFT JOIN enumerations e ON wp.priority_id = e.id "
            f"WHERE wp.parent_id = {epic_id.strip()} "
            f"AND t.name = 'Task' "
            f"ORDER BY wp.subject"
        )
        if not rows:
            check("9. OpenProject Tasks", 3, False, "no child Tasks found")
            return

        tasks = []
        for line in rows.strip().splitlines():
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 5:
                tasks.append({
                    "subject": cols[0],
                    "login": cols[1],
                    "firstname": cols[2],
                    "lastname": cols[3],
                    "priority": cols[4],
                })

        # One child Task per discovered project. Expected subject:
        # [<Project>] Bump typescript <Current Version> → 5.4.5
        # Current Version accepted in both forms (with and without ^~ prefix).
        accepted = {
            proj: {f"[{proj}] Bump typescript {v} → 5.4.5" for v in {ver, ver.lstrip("^~")}}
            for proj, (_path, ver) in gt.items()
        }
        issues = []
        if len(tasks) != len(gt):
            issues.append(f"expected {len(gt)} tasks, found {len(tasks)}")
        subjects = [t["subject"] for t in tasks]
        if len(subjects) != len(set(subjects)):
            issues.append("duplicate task subjects")

        seen_projects: set[str] = set()
        for task in tasks:
            subj = task["subject"]
            # Extract project name from [<Project>]
            m = re.match(r"\[([^\]]+)\]", subj)
            if not m:
                issues.append(f"subject does not match pattern: {subj!r}")
                continue
            proj = m.group(1)
            if proj not in accepted:
                issues.append(f"unexpected project task: {subj!r}")
                continue
            if proj in seen_projects:
                issues.append(f"duplicate task for project {proj}")
            seen_projects.add(proj)

            # Exact subject (both current-version forms accepted)
            if subj not in accepted[proj]:
                issues.append(f"[{proj}] subject={subj!r}, expected one of {sorted(accepted[proj])}")

            # Check assignee login is exactly admin
            if task.get("login", "") != "admin":
                issues.append(f"[{proj}] assignee login={task.get('login', '')!r}, expected 'admin'")

            # Check priority: High for tabler (Migration Complexity=High), Normal for others
            pri = task.get("priority", "")
            expected_pri = "High" if proj == "tabler" else "Normal"
            if pri != expected_pri:
                issues.append(f"[{proj}] priority={pri!r}, expected {expected_pri!r}")

        missing_projects = set(gt) - seen_projects
        if missing_projects:
            issues.append(f"missing projects: {sorted(missing_projects)}")

        check("9. OpenProject Tasks", 3, not issues,
              f"all {len(gt)} tasks correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("9. OpenProject Tasks", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Initialize Baserow connection
    br_ok = _init_baserow()
    if not br_ok:
        print("WARNING: Baserow init failed; Baserow checks will fail", file=sys.stderr)

    check_1_database_exists()
    check_2_table_and_fields()
    check_3_row_projects()
    check_3b_manifest_and_version()
    check_4_row_field_values()
    check_5_status_values()
    check_6_high_complexity_view()
    check_8_openproject_epic()
    check_9_openproject_tasks()

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
