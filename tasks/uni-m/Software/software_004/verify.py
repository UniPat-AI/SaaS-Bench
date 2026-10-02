#!/usr/bin/env python3
"""
Verifier for Software-004-I1: Plan synchronized Backend/Data sprint across
OpenProject, code-server, and Baserow.

Checks: 11 weighted checks (22 pts) across openproject, code-server, baserow.
Strategy: docker exec (DB queries + filesystem) for all checks. Baserow row
expectations are recomputed by the verifier from OpenProject SQL (cached) —
agent-entered numbers are never trusted.

Required env vars:
  SERVER_HOSTNAME,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER
"""

import json
import os
import re
import subprocess
import sys

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
for var in [
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER",
]:
    if not os.environ.get(var):
        _missing.append(var)
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

# ── Slot values (from instance) ──────────────────────────────────────────────
SPRINT_NAME = "Sprint Synchronize 2025-W10"
TEAM_A = "Backend"
TEAM_B = "Data"
SPRINT_START = "2025-03-03"
SPRINT_END = "2025-03-17"
NUM_WP_PER_TEAM = 4
TEAM_A_HOURS = 32
TEAM_B_HOURS = 28
OP_PROJECT = "Data Analytics Pipeline"
BASEROW_DB_NAME = "Sprint Capacity Planner"
BASEROW_TABLE_NAME = "Sprint Capacity"
COMMENT_LINE = "# Sprint Sprint Synchronize 2025-W10: integration touchpoint"
APP_PY_PATH = "/home/coder/workspace/todo-api/app.py"

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
    """Run a SQL query against the embedded OpenProject Postgres DB."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-U", "openproject", "-h", "127.0.0.1", "-d", "openproject",
         "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"op_sql failed (rc={r.returncode}): {r.stderr.strip()}")
    return r.stdout.strip()


def baserow_sql(query: str) -> str:
    """Run a SQL query against the Baserow Postgres DB."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"baserow_sql failed (rc={rc}): {err.strip()}")
    return out.strip()


# ── OpenProject truth recompute (queried once per team, cached) ──────────────
_team_wps_cache: dict[str, object] = {}
_cross_rel_cache: object = None


def _get_team_wps(prefix: str) -> list[dict]:
    """Get work packages whose subject starts with the given prefix, assigned
    to the sprint version. Queried ONCE per team and cached; a cached failure
    re-raises so every dependent check fails with the root cause."""
    cached = _team_wps_cache.get(prefix)
    if cached is not None:
        if isinstance(cached, Exception):
            raise RuntimeError(f"cached OpenProject truth failure: {cached}")
        return cached
    try:
        rows = op_sql(
            f"SELECT wp.id, wp.subject, t.name AS type_name, wp.estimated_hours "
            f"FROM work_packages wp "
            f"JOIN projects p ON wp.project_id = p.id "
            f"JOIN types t ON wp.type_id = t.id "
            f"LEFT JOIN versions v ON wp.version_id = v.id "
            f"WHERE p.name = '{OP_PROJECT}' "
            f"AND wp.subject LIKE '[{prefix}]%' "
            f"AND v.name = '{SPRINT_NAME}'"
        )
    except Exception as e:
        _team_wps_cache[prefix] = e
        raise
    wps = []
    for line in rows.splitlines():
        if not line.strip():
            continue
        parts = line.split("|")
        wps.append({
            "id": int(parts[0]),
            "subject": parts[1],
            "type": parts[2] if len(parts) > 2 else "",
            # None (not 0.0) when estimated_hours is NULL, so per-WP checks see it
            "hours": float(parts[3]) if len(parts) > 3 and parts[3] else None,
        })
    _team_wps_cache[prefix] = wps
    return wps


def _get_cross_relations() -> list[tuple[int, int]]:
    """Cross-team 'follows' relations between the two version WP sets, as
    (from_id, to_id) pairs. Queried ONCE and cached."""
    global _cross_rel_cache
    if _cross_rel_cache is not None:
        if isinstance(_cross_rel_cache, Exception):
            raise RuntimeError(f"cached relations truth failure: {_cross_rel_cache}")
        return _cross_rel_cache
    try:
        backend_ids = {wp["id"] for wp in _get_team_wps(TEAM_A)}
        data_ids = {wp["id"] for wp in _get_team_wps(TEAM_B)}
        all_ids = backend_ids | data_ids
        cross_team: list[tuple[int, int]] = []
        if all_ids:
            id_list = ",".join(str(i) for i in all_ids)
            rows = op_sql(
                f"SELECT r.from_id, r.to_id, r.relation_type "
                f"FROM relations r "
                f"WHERE r.relation_type = 'follows' "
                f"AND (r.from_id IN ({id_list}) OR r.to_id IN ({id_list}))"
            )
            for line in rows.splitlines():
                if not line.strip():
                    continue
                parts = line.split("|")
                from_id = int(parts[0])
                to_id = int(parts[1])
                from_backend = from_id in backend_ids
                from_data = from_id in data_ids
                to_backend = to_id in backend_ids
                to_data = to_id in data_ids
                if (from_backend and to_data) or (from_data and to_backend):
                    cross_team.append((from_id, to_id))
    except Exception as e:
        _cross_rel_cache = e
        raise
    _cross_rel_cache = cross_team
    return cross_team


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_version_exists() -> None:
    """Version 'Sprint Synchronize 2025-W10' exists in project with correct dates and status."""
    try:
        row = op_sql(
            f"SELECT v.name, v.start_date, v.effective_date, v.status "
            f"FROM versions v "
            f"JOIN projects p ON v.project_id = p.id "
            f"WHERE p.name = '{OP_PROJECT}' AND v.name = '{SPRINT_NAME}'"
        )
        if not row:
            check("1. Version exists", 2, False, "version not found")
            return
        parts = row.split("|")
        name = parts[0]
        start = parts[1] if len(parts) > 1 else ""
        end = parts[2] if len(parts) > 2 else ""
        status = parts[3] if len(parts) > 3 else ""
        ok = (
            name == SPRINT_NAME
            and start == SPRINT_START
            and end == SPRINT_END
            and status == "open"
        )
        check("1. Version exists", 2, ok,
              f"name={name}, start={start}, end={end}, status={status}")
    except Exception as e:
        check("1. Version exists", 2, False, f"exception: {e}")


def _project_wide_count(prefix: str) -> int:
    """Count ALL WPs with the prefix in the project, regardless of version."""
    out = op_sql(
        f"SELECT count(*) FROM work_packages wp "
        f"JOIN projects p ON wp.project_id = p.id "
        f"WHERE p.name = '{OP_PROJECT}' AND wp.subject LIKE '[{prefix}]%'"
    )
    return int(out) if out else 0


def _check_team_wp_count(num: int, prefix: str) -> None:
    """Exactly 4 Feature WPs with the prefix on the version, and exactly 4
    project-wide (global exclusivity — no extra unassigned prefixed WPs)."""
    label = f"{num}. {prefix} WP count"
    try:
        wps = _get_team_wps(prefix)
        count = len(wps)
        all_feature = all(wp["type"] == "Feature" for wp in wps)
        global_count = _project_wide_count(prefix)
        ok = count == NUM_WP_PER_TEAM and all_feature and global_count == NUM_WP_PER_TEAM
        types = set(wp["type"] for wp in wps)
        check(label, 2, ok,
              f"version count={count}, types={types}, project-wide count={global_count}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_backend_wp_count() -> None:
    """Exactly 4 Feature work packages with [Backend] prefix (version + global)."""
    _check_team_wp_count(2, TEAM_A)


def check_3_data_wp_count() -> None:
    """Exactly 4 Feature work packages with [Data] prefix (version + global)."""
    _check_team_wp_count(3, TEAM_B)


def _check_team_hours(num: int, prefix: str, expected_hours: float) -> None:
    """Team hours sum matches AND every WP has a positive estimated_hours."""
    label = f"{num}. {prefix} hours"
    try:
        wps = _get_team_wps(prefix)
        bad_wps = [wp["id"] for wp in wps if wp["hours"] is None or wp["hours"] <= 0]
        total = sum(wp["hours"] or 0.0 for wp in wps)
        ok = abs(total - expected_hours) < 0.01 and not bad_wps
        detail = f"expected sum={expected_hours}, got={total}"
        if bad_wps:
            detail += f", WPs without positive estimated_hours: {bad_wps}"
        check(label, 2, ok, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_backend_hours() -> None:
    """Backend WPs total 32 hours and each has positive estimated hours."""
    _check_team_hours(4, TEAM_A, TEAM_A_HOURS)


def check_5_data_hours() -> None:
    """Data WPs total 28 hours and each has positive estimated hours."""
    _check_team_hours(5, TEAM_B, TEAM_B_HOURS)


def check_6_follows_relation() -> None:
    """Exactly one 'follows' relation linking a Backend WP and a Data WP.

    Both directions are accepted (OpenProject relation normalization varies);
    the actual stored direction is printed in the detail for auditing."""
    try:
        backend_ids = {wp["id"] for wp in _get_team_wps(TEAM_A)}
        data_ids = {wp["id"] for wp in _get_team_wps(TEAM_B)}

        if not (backend_ids | data_ids):
            check("6. Cross-team follows relation", 3, False, "no WPs found")
            return

        cross_team = _get_cross_relations()
        directions = []
        for from_id, to_id in cross_team:
            from_team = TEAM_A if from_id in backend_ids else TEAM_B
            to_team = TEAM_A if to_id in backend_ids else TEAM_B
            directions.append(f"from={from_id}({from_team}) to={to_id}({to_team})")
        ok = len(cross_team) == 1
        check("6. Cross-team follows relation", 3, ok,
              f"cross-team follows count={len(cross_team)}; "
              f"direction: {'; '.join(directions) if directions else 'none'} "
              f"(bidirectional accepted)")
    except Exception as e:
        check("6. Cross-team follows relation", 3, False, f"exception: {e}")


def check_7_code_server_comment() -> None:
    """todo-api/app.py (fixed path, no fallback) has the sprint comment as its
    first non-empty line."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "head", "-n", "5", APP_PY_PATH,
            timeout=10,
        )
        if rc != 0:
            check("7. Code-server comment", 2, False,
                  f"cannot read {APP_PY_PATH}: {err.strip()} (no fallback path accepted)")
            return

        found = COMMENT_LINE in out
        # Check it's at the top (first non-empty line)
        at_top = False
        if found:
            for line in out.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                at_top = stripped == COMMENT_LINE.strip()
                break

        ok = found and at_top
        detail = "comment at top" if ok else f"found={found}, at_top={at_top}"
        check("7. Code-server comment", 2, ok, detail)
    except Exception as e:
        check("7. Code-server comment", 2, False, f"exception: {e}")


def check_8_baserow_db_table_exist() -> None:
    """Baserow has database 'Sprint Capacity Planner' with table 'Sprint Capacity'."""
    try:
        # Find the database by name (database_database.application_ptr_id == core_application.id)
        db_row = baserow_sql(
            f"SELECT a.id FROM core_application a "
            f"JOIN database_database d ON d.application_ptr_id = a.id "
            f"WHERE a.name = '{BASEROW_DB_NAME}'"
        )
        if not db_row.strip():
            check("8. Baserow DB+table exist", 1, False, "database not found")
            return

        db_id = db_row.strip().splitlines()[0]

        # Find the table by name within that database
        tbl_row = baserow_sql(
            f"SELECT t.id FROM database_table t "
            f"WHERE t.database_id = {db_id} AND t.name = '{BASEROW_TABLE_NAME}'"
        )
        if not tbl_row.strip():
            check("8. Baserow DB+table exist", 1, False,
                  f"database found (id={db_id}) but table '{BASEROW_TABLE_NAME}' not found")
            return

        check("8. Baserow DB+table exist", 1, True, f"db_id={db_id}, table_id={tbl_row.strip()}")
    except Exception as e:
        check("8. Baserow DB+table exist", 1, False, f"exception: {e}")


def _get_baserow_table_id() -> int | None:
    """Return the Baserow internal table ID for 'Sprint Capacity'."""
    db_row = baserow_sql(
        f"SELECT a.id FROM core_application a "
        f"JOIN database_database d ON d.application_ptr_id = a.id "
        f"WHERE a.name = '{BASEROW_DB_NAME}'"
    )
    if not db_row.strip():
        return None
    db_id = db_row.strip().splitlines()[0]
    tbl_row = baserow_sql(
        f"SELECT t.id FROM database_table t "
        f"WHERE t.database_id = {db_id} AND t.name = '{BASEROW_TABLE_NAME}'"
    )
    if not tbl_row.strip():
        return None
    return int(tbl_row.strip().splitlines()[0])


def check_s_baserow_field_schema() -> None:
    """Baserow 'Sprint Capacity' field schema: Team primary text, Planned Hours
    number, Work Package Count number, Has Cross-Team Dep boolean."""
    label = "S. Baserow field schema"
    try:
        table_id = _get_baserow_table_id()
        if table_id is None:
            check(label, 2, False, "table not found")
            return
        rows = baserow_sql(
            f'SELECT f.name, ct.model, f."primary" FROM database_field f '
            f"JOIN django_content_type ct ON ct.id = f.content_type_id "
            f"WHERE f.table_id = {table_id} AND f.trashed = false"
        )
        schema = {}  # name -> (model, primary)
        for line in rows.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                schema[parts[0].strip()] = (parts[1].strip(), parts[2].strip() == "t")
        expected = {
            "Team": ("textfield", True),
            "Planned Hours": ("numberfield", None),
            "Work Package Count": ("numberfield", None),
            "Has Cross-Team Dep": ("booleanfield", None),
        }
        issues = []
        for name, (model, must_be_primary) in expected.items():
            got = schema.get(name)
            if got is None:
                issues.append(f"missing field '{name}'")
                continue
            if got[0] != model:
                issues.append(f"{name}: expected {model}, got {got[0]}")
            if must_be_primary and not got[1]:
                issues.append(f"{name}: expected primary=true")
        ok = not issues
        check(label, 2, ok,
              "; ".join(issues) if issues else f"fields={sorted(schema)}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_baserow_row_count() -> None:
    """Baserow 'Sprint Capacity' table has exactly 2 rows."""
    try:
        table_id = _get_baserow_table_id()
        if table_id is None:
            check("9. Baserow row count", 1, False, "table not found")
            return

        count_str = baserow_sql(
            f"SELECT COUNT(*) FROM database_table_{table_id}"
        )
        count = int(count_str.strip())
        ok = count == 2
        check("9. Baserow row count", 1, ok, f"expected=2, got={count}")
    except Exception as e:
        check("9. Baserow row count", 1, False, f"exception: {e}")


def check_10_baserow_row_data() -> None:
    """Baserow rows match values the verifier recomputes from OpenProject:
    Planned Hours = SUM(estimated_hours), Work Package Count = COUNT(*) per
    team prefix within the version; Has Cross-Team Dep = team participates in
    the cross-team follows relation (with the required single relation, both
    teams are expected true)."""
    label = "10. Baserow row data"
    try:
        # Recompute expected values from OpenProject (cached truth)
        try:
            team_wps = {TEAM_A: _get_team_wps(TEAM_A), TEAM_B: _get_team_wps(TEAM_B)}
            cross_team = _get_cross_relations()
        except Exception as e:
            check(label, 3, False, f"OpenProject truth recompute failed: {e}")
            return
        endpoint_ids = {i for pair in cross_team for i in pair}
        expected = {}
        for team, wps in team_wps.items():
            ids = {wp["id"] for wp in wps}
            expected[team] = {
                "hours": sum(wp["hours"] or 0.0 for wp in wps),
                "count": len(wps),
                "dep": bool(ids & endpoint_ids),
            }
        truth = "; ".join(
            f"{team}: hours={e['hours']}, count={e['count']}, dep={e['dep']}"
            for team, e in expected.items()
        ) + f"; cross-team follows={len(cross_team)}"

        table_id = _get_baserow_table_id()
        if table_id is None:
            check(label, 3, False, f"table not found [truth: {truth}]")
            return

        # Get field mappings for the table
        fields_raw = baserow_sql(
            f"SELECT f.id, f.name FROM database_field f "
            f"WHERE f.table_id = {table_id} ORDER BY f.id"
        )
        field_map = {}  # name -> field_<id> column name
        for line in fields_raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            fid = parts[0].strip()
            fname = parts[1].strip() if len(parts) > 1 else ""
            field_map[fname] = f"field_{fid}"

        team_col = field_map.get("Team", "")
        hours_col = field_map.get("Planned Hours", "")
        count_col = field_map.get("Work Package Count", "")
        dep_col = field_map.get("Has Cross-Team Dep", "")

        if not all([team_col, hours_col, count_col, dep_col]):
            check(label, 3, False,
                  f"missing fields: team={team_col}, hours={hours_col}, "
                  f"count={count_col}, dep={dep_col}")
            return

        rows_raw = baserow_sql(
            f"SELECT {team_col}, {hours_col}, {count_col}, {dep_col} "
            f"FROM database_table_{table_id} ORDER BY {team_col}"
        )
        rows = []
        for line in rows_raw.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            rows.append({
                "team": parts[0].strip(),
                "hours": float(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else 0,
                "count": int(float(parts[2].strip())) if len(parts) > 2 and parts[2].strip() else 0,
                "dep": parts[3].strip().lower() in ("t", "true", "1") if len(parts) > 3 else False,
            })

        if len(rows) != 2:
            check(label, 3, False,
                  f"expected 2 rows, parsed {len(rows)} [truth: {truth}]")
            return

        row_by_team = {r["team"]: r for r in rows}
        issues = []
        if set(row_by_team) != {TEAM_A, TEAM_B}:
            issues.append(f"team set {sorted(row_by_team)} != ['{TEAM_A}', '{TEAM_B}']")
        for team in (TEAM_A, TEAM_B):
            row = row_by_team.get(team)
            if row is None:
                continue  # already reported via team-set mismatch
            exp = expected[team]
            if abs(row["hours"] - exp["hours"]) > 0.01:
                issues.append(f"{team} hours: recomputed {exp['hours']}, got {row['hours']}")
            if row["count"] != exp["count"]:
                issues.append(f"{team} WP count: recomputed {exp['count']}, got {row['count']}")
            if row["dep"] != exp["dep"]:
                issues.append(f"{team} Has Cross-Team Dep: recomputed {exp['dep']}, got {row['dep']}")

        ok = not issues
        detail = "all fields match recompute" if ok else "; ".join(issues)
        check(label, 3, ok, f"{detail} [truth: {truth}]")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_version_exists()
    check_2_backend_wp_count()
    check_3_data_wp_count()
    check_4_backend_hours()
    check_5_data_hours()
    check_6_follows_relation()
    check_7_code_server_comment()
    check_8_baserow_db_table_exist()
    check_s_baserow_field_schema()
    check_9_baserow_row_count()
    check_10_baserow_row_data()

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
