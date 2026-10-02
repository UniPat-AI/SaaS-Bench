"""
Verifier for Software-039-I5: Cross-team integration testing infrastructure for Sprint-2026-Q4-W1

Checks: 14 weighted checks across code-server, openproject, baserow (total weight 22).
Strategy: docker exec (filesystem + git for code-server, DB for openproject/baserow).
Tightened per docs/tightening/software_b.md (software_039 section):
  ck1/ck2 line-level comment-block assertions; ck3 three-stage git audit;
  ck4 exact description; ckP (new) priority High on all 5 Feature WPs;
  ck7 exact assignee; ck8 exactly-2-follows gate; ck9 recurring meeting +
  first occurrence 2026-10-06 11:00 (TZ-tolerant), no LIKE fallback;
  ck11 full-field Baserow<->OpenProject reconciliation; ck12 grid type + sort;
  ckS (new) field schema.
Weight rebalance to fund ckP(2)+ckS(1) at constant total 22:
  ck5 2->1, ck6 2->1, ck7 2->1 (all three assert single fields of the same
  5 WPs, now additionally cross-checked by ckP and ck11).

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER
"""

import os
import re
import sys
import subprocess

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")

for var in ["CODE_SERVER_CONTAINER", "OPENPROJECT_CONTAINER", "BASEROW_CONTAINER", "BASEROW_DB_CONTAINER"]:
    if not os.environ.get(var):
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

for var in ["CODE_SERVER_PORT", "OPENPROJECT_PORT", "BASEROW_PORT"]:
    if not os.environ.get(var):
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

OPENPROJECT_PORT = os.environ["OPENPROJECT_PORT"]

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
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def op_sql(query: str) -> str:
    """Run SQL against OpenProject's embedded Postgres (peer auth via su - postgres)."""
    r = subprocess.run(
        ["docker", "exec", "-i", OPENPROJECT_CONTAINER,
         "su", "-", "postgres", "-c", "psql -d openproject -t -A"],
        input=query, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"OpenProject psql failed: {r.stderr.strip()}")
    return r.stdout.strip()


def baserow_sql(query: str) -> str:
    """Run SQL against Baserow's Postgres DB container."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"Baserow psql failed: {err.strip()}")
    return out.strip()


def find_file_in_container(container: str, filename: str, search_root: str = "/") -> str:
    """Find a file path inside a container."""
    rc, out, err = docker_exec(
        container, "find", search_root, "-path", f"*/{filename}", "-type", "f",
        timeout=30,
    )
    paths = [p.strip() for p in out.strip().splitlines() if p.strip()]
    return paths[0] if paths else ""


def find_project_dir(project: str) -> str:
    """Locate a project git-repo directory inside the code-server container."""
    fixed = f"/home/coder/workspace/{project}"
    rc, out, _ = docker_exec(
        CODE_SERVER_CONTAINER, "bash", "-c",
        f"test -d {fixed}/.git && echo {fixed}", timeout=15,
    )
    if out.strip():
        return out.strip().splitlines()[0].strip()
    for root in ("/home", "/"):
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER,
            "find", root, "-type", "d", "-name", project, "-maxdepth", "4",
            timeout=30,
        )
        dirs = [d.strip() for d in out.strip().splitlines() if d.strip()]
        if dirs:
            return dirs[0]
    return ""


def read_container_file(path: str) -> str | None:
    rc, content, _ = docker_exec(CODE_SERVER_CONTAINER, "cat", path)
    return content if rc == 0 else None


def git_in(repo_dir: str, git_args: str, timeout: int = 15) -> tuple[int, str, str]:
    """Run a git command inside the code-server container with safe.directory set."""
    cmd = f"cd {repo_dir} && git -c safe.directory={repo_dir} {git_args}"
    return docker_exec(CODE_SERVER_CONTAINER, "bash", "-c", cmd, timeout=timeout)


def comment_block_check(content: str, anchor_pred, block: list[str]) -> tuple[bool, str]:
    """Assert the comment block appears exactly once and immediately below the anchor line."""
    lines = content.split("\n")
    stripped = [l.strip() for l in lines]
    blk = [b.strip() for b in block]
    k = len(blk)
    occ = [i for i in range(len(stripped) - k + 1) if stripped[i:i + k] == blk]
    if len(occ) != 1:
        return False, f"comment block found {len(occ)} time(s), expected exactly once"
    anchors = [i for i, l in enumerate(lines) if anchor_pred(l)]
    if not anchors:
        return False, "anchor line not found"
    a = anchors[0]
    if occ[0] != a + 1:
        return False, (f"comment block starts at line {occ[0] + 1}, "
                       f"expected immediately below anchor (line {a + 1})")
    return True, ""


def _ext_anchor(line: str) -> bool:
    return line.strip() == "migrate = Migrate()"


def _main_anchor(line: str) -> bool:
    # Anchor line is "def main(" with the parameter list continuing on
    # following lines -- match by prefix, never full-line equality.
    return line.strip().startswith("def main(")


# ── Constants from task ───────────────────────────────────────────────────────
SPRINT = "Sprint-2026-Q4-W1"
COMMIT_MSG = "docs: mark integration point for Sprint-2026-Q4-W1"
OP_PROJECT = "demo-project"
VERSION_DESC = "Cross-team sprint: Producer API Team ↔ Insights Engineering Team"
PRODUCER_TEAM = "Producer API Team"
CONSUMER_TEAM = "Insights Engineering Team"
CONTRACTS = ["CTR-REMIND-EVT", "CTR-SUBTASK-GR"]  # alphabetical order
DB_NAME = "Q4W1 Integration Contracts Workspace"
TABLE_NAME = "Integration Contracts"
MEETING_TITLE = f"Cross-team sync: {SPRINT}"

EXTENSIONS_COMMENTS = [
    f"# INTEGRATION-POINT {SPRINT}: consumed by Insights Engineering Team/data-analyzer",
    "# Contract owner: Yuki Tanaka",
    "# Review cadence: every sprint",
]
RUN_ANALYSIS_COMMENTS = [
    f"# INTEGRATION-POINT {SPRINT}: consumes Producer API Team/todo-api",
    "# Contract owner: Olivia Bennett",
]
WP_SPECS = [
    {"subject": "Publish reminder-events topic [CTR-REMIND-EVT]", "hours": 13,
     "assignee": "Yuki Tanaka", "team": PRODUCER_TEAM, "contract": "CTR-REMIND-EVT"},
    {"subject": "Expose subtask-graph API [CTR-SUBTASK-GR]", "hours": 11,
     "assignee": "Yuki Tanaka", "team": PRODUCER_TEAM, "contract": "CTR-SUBTASK-GR"},
    {"subject": "Provide attachment-index export [CTR-ATTACH-IDX]", "hours": 8,
     "assignee": "Yuki Tanaka", "team": PRODUCER_TEAM, "contract": None},
    {"subject": "Consume reminder-events topic for engagement metrics [CTR-REMIND-EVT]", "hours": 12,
     "assignee": "Olivia Bennett", "team": CONSUMER_TEAM, "contract": "CTR-REMIND-EVT"},
    {"subject": "Ingest subtask-graph API for dependency analytics [CTR-SUBTASK-GR]", "hours": 10,
     "assignee": "Olivia Bennett", "team": CONSUMER_TEAM, "contract": "CTR-SUBTASK-GR"},
]
AGENDA_ITEMS = [
    "Contract changes since last sync",
    "Integration test results",
    "Blockers and escalations",
]

GIT_TARGETS = [
    ("todo-api", "app/extensions.py", EXTENSIONS_COMMENTS, _ext_anchor),
    ("data-analyzer", "scripts/run_analysis.py", RUN_ANALYSIS_COMMENTS, _main_anchor),
]


# ── Individual checks ─────────────────────────────────────────────────────────
def check_1_extensions_comments() -> None:
    """extensions.py: 3 comment lines exactly once, immediately below 'migrate = Migrate()'."""
    try:
        path = "/home/coder/workspace/todo-api/app/extensions.py"
        if read_container_file(path) is None:
            path = find_file_in_container(
                CODE_SERVER_CONTAINER, "todo-api/app/extensions.py", search_root="/home",
            )
        content = read_container_file(path) if path else None
        if content is None:
            check("1. extensions.py integration comments", 2, False, "file not found in container")
            return
        ok, detail = comment_block_check(content, _ext_anchor, EXTENSIONS_COMMENTS)
        check("1. extensions.py integration comments", 2, ok, detail)
    except Exception as e:
        check("1. extensions.py integration comments", 2, False, f"exception: {e}")


def check_2_run_analysis_comments() -> None:
    """run_analysis.py: 2 comment lines exactly once, immediately below the 'def main(' line."""
    try:
        path = "/home/coder/workspace/data-analyzer/scripts/run_analysis.py"
        if read_container_file(path) is None:
            path = find_file_in_container(
                CODE_SERVER_CONTAINER, "data-analyzer/scripts/run_analysis.py", search_root="/home",
            )
        content = read_container_file(path) if path else None
        if content is None:
            check("2. run_analysis.py integration comments", 2, False, "file not found in container")
            return
        ok, detail = comment_block_check(content, _main_anchor, RUN_ANALYSIS_COMMENTS)
        check("2. run_analysis.py integration comments", 2, ok, detail)
    except Exception as e:
        check("2. run_analysis.py integration comments", 2, False, f"exception: {e}")


def check_3_git_commits() -> None:
    """Three-stage git audit per repo: exact subject, single staged file, committed content."""
    try:
        issues = []
        for project, rel_path, block, anchor_pred in GIT_TARGETS:
            repo = find_project_dir(project)
            if not repo:
                issues.append(f"{project}: repo dir not found")
                continue
            # Stage (a): exact-subject commit lookup (rejects superset messages)
            rc, log_out, err = git_in(repo, "log --all --format='%H%x00%s'")
            if rc != 0:
                issues.append(f"{project}: git log failed: {err.strip()[:60]}")
                continue
            shas = []
            for line in log_out.splitlines():
                if "\x00" not in line:
                    continue
                sha, subj = line.split("\x00", 1)
                if subj == COMMIT_MSG:
                    shas.append(sha.strip())
            if not shas:
                issues.append(f"{project}: no commit with exact subject '{COMMIT_MSG}'")
                continue
            repo_ok = False
            last_err = ""
            for sha in shas:
                # Stage (b): commit touches exactly the one target file
                rc, names_out, _ = git_in(repo, f"show --name-only --format= {sha}")
                files = [l.strip() for l in names_out.splitlines() if l.strip()]
                if files != [rel_path]:
                    last_err = f"{project}: commit {sha[:8]} touches {files}, expected exactly ['{rel_path}']"
                    continue
                # Stage (c): comment block present in the committed blob
                rc, blob, _ = git_in(repo, f"show {sha}:{rel_path}")
                if rc != 0:
                    last_err = f"{project}: git show {sha[:8]}:{rel_path} failed"
                    continue
                ok, detail = comment_block_check(blob, anchor_pred, block)
                if ok:
                    repo_ok = True
                    break
                last_err = f"{project}: committed content: {detail}"
            if not repo_ok:
                issues.append(last_err or f"{project}: no valid commit")
        if issues:
            check("3. Git commits (exact subject, single file, committed content)", 2, False,
                  "; ".join(issues)[:300])
        else:
            check("3. Git commits (exact subject, single file, committed content)", 2, True)
    except Exception as e:
        check("3. Git commits (exact subject, single file, committed content)", 2, False, f"exception: {e}")


def check_4_op_version() -> None:
    """OpenProject version Sprint-2026-Q4-W1: exact dates, status and exact description."""
    try:
        row = op_sql(
            "SELECT v.name, v.description, v.start_date, v.effective_date, v.status "
            "FROM versions v "
            "JOIN projects p ON v.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}'"
        )
        if not row:
            check("4. Version Sprint-2026-Q4-W1", 2, False, "version not found")
            return
        parts = row.splitlines()[0].split("|")
        if len(parts) < 5:
            check("4. Version Sprint-2026-Q4-W1", 2, False, f"unexpected row format: {row[:100]}")
            return
        name, desc, start, due, status = parts[0], parts[1], parts[2], parts[3], parts[4]
        issues = []
        if desc.strip() != VERSION_DESC:
            issues.append(f"description mismatch: '{desc[:80]}'")
        if start.strip() != "2026-10-05":
            issues.append(f"start_date={start}, expected 2026-10-05")
        if due.strip() != "2026-10-16":
            issues.append(f"due_date={due}, expected 2026-10-16")
        if status.strip() != "open":
            issues.append(f"status={status}, expected open")
        if issues:
            check("4. Version Sprint-2026-Q4-W1", 2, False, "; ".join(issues))
        else:
            check("4. Version Sprint-2026-Q4-W1", 2, True)
    except Exception as e:
        check("4. Version Sprint-2026-Q4-W1", 2, False, f"exception: {e}")


def check_5_wp_subjects() -> None:
    """Exactly 5 Feature work packages with the exact subject set in the version."""
    try:
        rows = op_sql(
            "SELECT wp.subject "
            "FROM work_packages wp "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}' "
            "AND t.name = 'Feature' "
            "ORDER BY wp.subject"
        )
        found_subjects = [r.strip() for r in rows.splitlines() if r.strip()]
        expected_subjects = set(s["subject"] for s in WP_SPECS)
        missing = expected_subjects - set(found_subjects)
        extra = set(found_subjects) - expected_subjects
        if not missing and not extra and len(found_subjects) == 5:
            check("5. Five Feature WPs with correct subjects", 1, True)
        else:
            detail = f"found {len(found_subjects)}, expected exactly 5"
            if missing:
                detail += f"; missing: {sorted(missing)[0][:50]}..."
            if extra:
                detail += f"; extra: {sorted(extra)[0][:50]}..."
            check("5. Five Feature WPs with correct subjects", 1, False, detail)
    except Exception as e:
        check("5. Five Feature WPs with correct subjects", 1, False, f"exception: {e}")


def check_P_wp_priorities() -> None:
    """All 5 Feature WPs have priority High (SQL via enumerations; API fallback)."""
    label = "P. All 5 Feature WPs priority High"
    prios: list[tuple[str, str]] = []
    source = "sql"
    try:
        rows = op_sql(
            "SELECT wp.subject, e.name "
            "FROM work_packages wp "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN enumerations e ON e.id = wp.priority_id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}' "
            "AND t.name = 'Feature'"
        )
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit("|", 1)
            if len(parts) == 2:
                prios.append((parts[0].strip(), parts[1].strip()))
    except Exception:
        # API fallback: GET /api/v3/work_packages with basic auth apikey:AdminPass123!
        try:
            import requests
            base = f"http://{HOST}:{OPENPROJECT_PORT}"
            r = requests.get(
                f"{base}/api/v3/work_packages", params={"pageSize": "200"},
                auth=("apikey", "AdminPass123!"), timeout=20,
            )
            r.raise_for_status()
            expected_subjects = set(s["subject"] for s in WP_SPECS)
            for el in r.json().get("_embedded", {}).get("elements", []):
                subj = el.get("subject", "")
                if subj in expected_subjects:
                    prios.append((subj, el.get("_links", {}).get("priority", {}).get("title", "")))
            source = "api"
        except Exception as e2:
            check(label, 2, False, f"priority truth unavailable (sql and api failed): {e2}")
            return
    try:
        issues = []
        for spec in WP_SPECS:
            matches = [pr for subj, pr in prios if subj == spec["subject"]]
            if not matches:
                issues.append(f"'{spec['subject'][:30]}' not found")
            else:
                bad = [pr for pr in matches if pr != "High"]
                if bad:
                    issues.append(f"'{spec['subject'][:30]}': priority='{bad[0]}', expected 'High'")
        if issues:
            check(label, 2, False, "; ".join(issues[:3]))
        else:
            check(label, 2, True, f"5/5 High via {source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_wp_estimated_hours() -> None:
    """Verify WPs have correct estimated hours."""
    try:
        rows = op_sql(
            "SELECT wp.subject, wp.estimated_hours "
            "FROM work_packages wp "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}' "
            "AND t.name = 'Feature'"
        )
        wp_hours = {}
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit("|", 1)
            if len(parts) == 2:
                wp_hours[parts[0].strip()] = parts[1].strip()

        issues = []
        for spec in WP_SPECS:
            subj = spec["subject"]
            expected_h = spec["hours"]
            actual_h = wp_hours.get(subj, None)
            if actual_h is None:
                issues.append(f"WP '{subj[:30]}' not found")
            else:
                try:
                    if abs(float(actual_h) - expected_h) > 0.01:
                        issues.append(f"'{subj[:30]}': {actual_h}h, expected {expected_h}h")
                except ValueError:
                    issues.append(f"'{subj[:30]}': hours='{actual_h}'")

        if issues:
            check("6. WP estimated hours", 1, False, "; ".join(issues[:3]))
        else:
            check("6. WP estimated hours", 1, True)
    except Exception as e:
        check("6. WP estimated hours", 1, False, f"exception: {e}")


def check_7_wp_assignees() -> None:
    """Verify WPs have exactly the correct assignees."""
    try:
        rows = op_sql(
            "SELECT wp.subject, u.firstname || ' ' || u.lastname AS assignee "
            "FROM work_packages wp "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}' "
            "AND t.name = 'Feature'"
        )
        wp_assignees = {}
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.rsplit("|", 1)
            if len(parts) == 2:
                wp_assignees[parts[0].strip()] = parts[1].strip()

        issues = []
        for spec in WP_SPECS:
            subj = spec["subject"]
            expected_a = spec["assignee"]
            actual_a = wp_assignees.get(subj, "")
            if actual_a != expected_a:
                issues.append(f"'{subj[:30]}': assignee='{actual_a}', expected '{expected_a}'")

        if issues:
            check("7. WP assignees", 1, False, "; ".join(issues[:3]))
        else:
            check("7. WP assignees", 1, True)
    except Exception as e:
        check("7. WP assignees", 1, False, f"exception: {e}")


def check_8_follows_relations() -> None:
    """Both producer/consumer follows relations exist, and exactly 2 in total."""
    try:
        rows = op_sql(
            "SELECT wp.id, wp.subject "
            "FROM work_packages wp "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}'"
        )
        subj_ids: dict[str, list[str]] = {}
        all_ids: list[str] = []
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                subj_ids.setdefault(parts[1].strip(), []).append(parts[0].strip())
                all_ids.append(parts[0].strip())

        issues = []
        pairs = []
        for cid in CONTRACTS:
            prod_subj = next(s["subject"] for s in WP_SPECS
                             if s["contract"] == cid and s["team"] == PRODUCER_TEAM)
            cons_subj = next(s["subject"] for s in WP_SPECS
                             if s["contract"] == cid and s["team"] == CONSUMER_TEAM)
            prod_ids = subj_ids.get(prod_subj, [])
            cons_ids = subj_ids.get(cons_subj, [])
            if len(prod_ids) != 1 or len(cons_ids) != 1:
                issues.append(f"{cid}: producer x{len(prod_ids)}, consumer x{len(cons_ids)} "
                              "(expected exactly 1 each)")
                continue
            pairs.append((cid, prod_ids[0], cons_ids[0]))

        if not issues:
            for cid, producer_id, consumer_id in pairs:
                rel = op_sql(
                    "SELECT count(*) FROM relations "
                    f"WHERE ((from_id = {consumer_id} AND to_id = {producer_id} AND relation_type = 'follows') "
                    f"OR (from_id = {producer_id} AND to_id = {consumer_id} AND relation_type = 'precedes'))"
                )
                if not rel or int(rel) == 0:
                    issues.append(f"{cid}: follows relation missing")
            # Hard gate: exactly 2 follows relations among the sprint WPs, no extras
            if all_ids:
                ids_csv = ",".join(all_ids)
                total = op_sql(
                    "SELECT count(*) FROM relations "
                    "WHERE relation_type IN ('follows','precedes') "
                    f"AND from_id IN ({ids_csv}) AND to_id IN ({ids_csv})"
                )
                if int(total) != 2:
                    issues.append(f"{total} follows relations among sprint WPs, expected exactly 2")

        if issues:
            check("8. Follows relations (exactly 2 pairs)", 2, False, "; ".join(issues[:3]))
        else:
            check("8. Follows relations (exactly 2 pairs)", 2, True, "2/2")
    except Exception as e:
        check("8. Follows relations (exactly 2 pairs)", 2, False, f"exception: {e}")


def _first_occurrence_ok(ts_text: str, tz: str) -> tuple[bool, str]:
    """TZ-tolerant check that a timestamp is 2026-10-06 11:00 (local or stored-UTC)."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})", ts_text.strip())
    if not m:
        return False, f"unparseable start_time '{ts_text[:30]}'"
    date_part, hm = m.groups()
    if date_part == "2026-10-06" and hm == "11:00":
        return True, ""
    if tz:
        try:
            conv = op_sql(
                f"SELECT to_char((TIMESTAMPTZ '{ts_text}') AT TIME ZONE '{tz}', "
                "'YYYY-MM-DD HH24:MI')"
            )
            if conv.strip() == "2026-10-06 11:00":
                return True, ""
        except Exception:
            pass
    return False, f"start_time='{ts_text[:30]}' (tz='{tz}'), expected 2026-10-06 11:00"


def check_9_meeting_and_agenda() -> None:
    """Exactly one recurring meeting, first occurrence 2026-10-06 11:00, 3 agenda items in order."""
    try:
        issues = []

        # Probe recurring_meetings table existence defensively (varies with OP version)
        has_rm = False
        try:
            reg = op_sql("SELECT to_regclass('public.recurring_meetings')")
            has_rm = bool(reg.strip())
        except Exception:
            has_rm = False

        ts_text, tz = "", ""
        if has_rm:
            cnt = op_sql(
                "SELECT count(*) FROM recurring_meetings rm "
                "JOIN projects p ON rm.project_id = p.id "
                f"WHERE p.identifier = '{OP_PROJECT}' AND rm.title = '{MEETING_TITLE}'"
            )
            if int(cnt) != 1:
                issues.append(f"recurring meetings titled '{MEETING_TITLE}': {cnt}, expected exactly 1")
            else:
                try:
                    row = op_sql(
                        "SELECT rm.start_time::text, COALESCE(rm.time_zone, '') "
                        "FROM recurring_meetings rm "
                        "JOIN projects p ON rm.project_id = p.id "
                        f"WHERE p.identifier = '{OP_PROJECT}' AND rm.title = '{MEETING_TITLE}' "
                        "LIMIT 1"
                    )
                    parts = row.splitlines()[0].split("|")
                    ts_text = parts[0].strip()
                    tz = parts[1].strip() if len(parts) > 1 else ""
                except Exception:
                    row = op_sql(
                        "SELECT rm.start_time::text FROM recurring_meetings rm "
                        "JOIN projects p ON rm.project_id = p.id "
                        f"WHERE p.identifier = '{OP_PROJECT}' AND rm.title = '{MEETING_TITLE}' "
                        "LIMIT 1"
                    )
                    ts_text = row.splitlines()[0].strip() if row else ""
        else:
            # Fallback for OP versions without recurring_meetings: use meetings.start_time
            cnt = op_sql(
                "SELECT count(*) FROM meetings m "
                "JOIN projects p ON m.project_id = p.id "
                f"WHERE p.identifier = '{OP_PROJECT}' AND m.title = '{MEETING_TITLE}'"
            )
            if int(cnt) < 1:
                issues.append(f"no meeting titled exactly '{MEETING_TITLE}'")
            else:
                row = op_sql(
                    "SELECT min(m.start_time)::text FROM meetings m "
                    "JOIN projects p ON m.project_id = p.id "
                    f"WHERE p.identifier = '{OP_PROJECT}' AND m.title = '{MEETING_TITLE}'"
                )
                ts_text = row.splitlines()[0].strip() if row else ""

        if ts_text:
            ok, tdetail = _first_occurrence_ok(ts_text, tz)
            if not ok:
                issues.append(tdetail)
        elif not issues:
            issues.append("could not read meeting start_time")

        # Agenda: exact-title meetings only (no LIKE fallback); one meeting must have
        # exactly the 3 items in order (recurring templates/occurrences share the title).
        meeting_ids_raw = op_sql(
            "SELECT m.id FROM meetings m "
            "JOIN projects p ON m.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' AND m.title = '{MEETING_TITLE}'"
        )
        meeting_ids = [r.strip() for r in meeting_ids_raw.splitlines() if r.strip()]
        agenda_ok = False
        agenda_detail = "no meeting with exact title found for agenda"
        for mid in meeting_ids:
            agenda_rows = op_sql(
                "SELECT title FROM meeting_agenda_items "
                f"WHERE meeting_id = {mid} "
                "ORDER BY position ASC, id ASC"
            )
            found_items = [r.strip() for r in agenda_rows.splitlines() if r.strip()]
            if found_items == AGENDA_ITEMS:
                agenda_ok = True
                break
            agenda_detail = f"meeting {mid}: agenda={found_items[:4]}, expected exactly {AGENDA_ITEMS}"
        if not agenda_ok:
            issues.append(agenda_detail)

        if issues:
            check("9. Recurring meeting, first occurrence, agenda", 2, False, "; ".join(issues)[:300])
        else:
            check("9. Recurring meeting, first occurrence, agenda", 2, True)
    except Exception as e:
        check("9. Recurring meeting, first occurrence, agenda", 2, False, f"exception: {e}")


# ── Baserow helpers ───────────────────────────────────────────────────────────
def contracts_table_id() -> str:
    table_id_raw = baserow_sql(
        "SELECT dt.id FROM database_table dt "
        "JOIN core_application ca ON dt.database_id = ca.id "
        f"WHERE ca.name = '{DB_NAME}' AND dt.name = '{TABLE_NAME}'"
    )
    if not table_id_raw:
        return ""
    return table_id_raw.strip().splitlines()[0].strip()


def contracts_field_map(table_id: str) -> dict[str, str]:
    fields_raw = baserow_sql(
        f"SELECT df.id, df.name FROM database_field df "
        f"WHERE df.table_id = {table_id} ORDER BY df.id"
    )
    field_map = {}
    for line in fields_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|", 1)
        if len(parts) == 2:
            field_map[parts[1].strip()] = parts[0].strip()
    return field_map


def check_10_baserow_db_and_table() -> None:
    """Verify Baserow database and table exist."""
    try:
        db_row = baserow_sql(
            f"SELECT id FROM core_application WHERE name = '{DB_NAME}'"
        )
        if not db_row:
            check("10. Baserow DB and table exist", 1, False, f"database '{DB_NAME}' not found")
            return
        if not contracts_table_id():
            check("10. Baserow DB and table exist", 1, False, f"table '{TABLE_NAME}' not found")
            return
        check("10. Baserow DB and table exist", 1, True)
    except Exception as e:
        check("10. Baserow DB and table exist", 1, False, f"exception: {e}")


def check_S_field_schema() -> None:
    """Field schema: exact 8-field name set, types, primary flag, exact option sets."""
    label = "S. Integration Contracts field schema"
    expected = {
        "Contract ID": ("textfield", "t"),
        "Producer Team": ("singleselectfield", "f"),
        "Consumer Team": ("singleselectfield", "f"),
        "Contract Name": ("textfield", "f"),
        "Producer WP ID": ("numberfield", "f"),
        "Consumer WP ID": ("numberfield", "f"),
        "Status": ("singleselectfield", "f"),
        "Last Validated": ("datefield", "f"),
    }
    option_sets = {
        "Producer Team": {PRODUCER_TEAM, CONSUMER_TEAM},
        "Consumer Team": {PRODUCER_TEAM, CONSUMER_TEAM},
        "Status": {"Planned", "InProgress", "Verified", "Broken"},
    }
    try:
        table_id = contracts_table_id()
        if not table_id:
            check(label, 1, False, "table not found")
            return
        rows = baserow_sql(
            'SELECT df.name, ct.model, df."primary" FROM database_field df '
            "JOIN django_content_type ct ON ct.id = df.content_type_id "
            f"WHERE df.table_id = {table_id}"
        )
        actual: dict[str, tuple[str, str]] = {}
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                actual[parts[0].strip()] = (parts[1].strip(), parts[2].strip())

        issues = []
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            issues.append(f"field names mismatch: missing={missing}, extra={extra}")
        else:
            for fname, (etype, eprim) in expected.items():
                atype, aprim = actual[fname]
                if atype != etype:
                    issues.append(f"'{fname}' type={atype}, expected {etype}")
                if aprim != eprim:
                    issues.append(f"'{fname}' primary={aprim}, expected {eprim}")
            for fname, opts in option_sets.items():
                vals = baserow_sql(
                    "SELECT so.value FROM database_selectoption so "
                    "JOIN database_field df ON df.id = so.field_id "
                    f"WHERE df.table_id = {table_id} AND df.name = '{fname}'"
                )
                actual_opts = set(v.strip() for v in vals.splitlines() if v.strip())
                if actual_opts != opts:
                    issues.append(f"'{fname}' options={sorted(actual_opts)}, expected {sorted(opts)}")

        if issues:
            check(label, 1, False, "; ".join(issues[:4])[:300])
        else:
            check(label, 1, True)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_11_baserow_rows() -> None:
    """Full-field reconciliation of the 2 contract rows against OpenProject truth."""
    label = "11. Baserow contract rows reconcile with OpenProject"
    try:
        table_id = contracts_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return

        # ── Truth: WP ids by exact subject from OpenProject (never from agent data)
        rows = op_sql(
            "SELECT wp.id, wp.subject "
            "FROM work_packages wp "
            "JOIN versions v ON wp.version_id = v.id "
            "JOIN projects p ON wp.project_id = p.id "
            f"WHERE p.identifier = '{OP_PROJECT}' "
            f"AND v.name = '{SPRINT}'"
        )
        subj_ids: dict[str, list[str]] = {}
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                subj_ids.setdefault(parts[1].strip(), []).append(parts[0].strip())

        truth: dict[str, tuple[str, str]] = {}
        truth_issues = []
        for cid in CONTRACTS:
            prod_subj = next(s["subject"] for s in WP_SPECS
                             if s["contract"] == cid and s["team"] == PRODUCER_TEAM)
            cons_subj = next(s["subject"] for s in WP_SPECS
                             if s["contract"] == cid and s["team"] == CONSUMER_TEAM)
            prod_ids = subj_ids.get(prod_subj, [])
            cons_ids = subj_ids.get(cons_subj, [])
            if len(prod_ids) != 1 or len(cons_ids) != 1:
                truth_issues.append(f"{cid}: OP truth WPs producer x{len(prod_ids)}, "
                                    f"consumer x{len(cons_ids)} (expected exactly 1 each)")
            else:
                truth[cid] = (prod_ids[0], cons_ids[0])
        if truth_issues:
            check(label, 2, False, "; ".join(truth_issues))
            return

        # ── Baserow side
        field_map = contracts_field_map(table_id)
        required = ["Contract ID", "Producer Team", "Consumer Team", "Contract Name",
                    "Producer WP ID", "Consumer WP ID", "Status", "Last Validated"]
        missing_fields = [f for f in required if f not in field_map]
        if missing_fields:
            check(label, 2, False, f"fields missing: {missing_fields}")
            return

        row_count = int(baserow_sql(f"SELECT count(*) FROM database_table_{table_id}"))
        if row_count != 2:
            check(label, 2, False, f"found {row_count} rows, expected exactly 2")
            return

        f = {k: field_map[k] for k in required}
        try:
            rows_raw = baserow_sql(
                "SELECT concat_ws(E'\\x01', "
                f"COALESCE(t.field_{f['Contract ID']}, ''), "
                "COALESCE(pt.value, ''), "
                "COALESCE(cn.value, ''), "
                f"COALESCE(t.field_{f['Contract Name']}, ''), "
                f"COALESCE(t.field_{f['Producer WP ID']}::text, ''), "
                f"COALESCE(t.field_{f['Consumer WP ID']}::text, ''), "
                "COALESCE(st.value, ''), "
                f"COALESCE(t.field_{f['Last Validated']}::text, '')) "
                f"FROM database_table_{table_id} t "
                f"LEFT JOIN database_selectoption pt ON pt.id = t.field_{f['Producer Team']} "
                f"LEFT JOIN database_selectoption cn ON cn.id = t.field_{f['Consumer Team']} "
                f"LEFT JOIN database_selectoption st ON st.id = t.field_{f['Status']} "
                f"ORDER BY t.field_{f['Contract ID']} ASC"
            )
        except RuntimeError as e:
            # Agent-caused schema mismatch (e.g. team/status not single-select) makes
            # the joins fail -> agent FAIL, not a verifier error.
            check(label, 2, False, f"row query failed, field types likely wrong: {str(e)[:150]}")
            return
        parsed = []
        for line in rows_raw.splitlines():
            if "\x01" in line:
                parsed.append(line.split("\x01"))
        if len(parsed) != 2:
            check(label, 2, False, f"parsed {len(parsed)} rows, expected 2")
            return

        def num_eq(text: str, expected_id: str) -> bool:
            try:
                return abs(float(text) - float(expected_id)) < 0.001
            except (ValueError, TypeError):
                return False

        issues = []
        for i, cid in enumerate(CONTRACTS):  # alphabetical: IC-01 ↔ CTR-REMIND-EVT, IC-02 ↔ CTR-SUBTASK-GR
            row = parsed[i]
            if len(row) != 8:
                issues.append(f"row {i + 1}: unexpected column count {len(row)}")
                continue
            r_cid, r_pteam, r_cteam, r_cname, r_pwp, r_cwp, r_status, r_lv = [c.strip() for c in row]
            expected_label = f"IC-{i + 1:02d}"
            prod_id, cons_id = truth[cid]
            if r_cid != expected_label:
                issues.append(f"row {i + 1}: Contract ID='{r_cid}', expected '{expected_label}'")
            if r_pteam != PRODUCER_TEAM:
                issues.append(f"{expected_label}: Producer Team='{r_pteam}', expected '{PRODUCER_TEAM}'")
            if r_cteam != CONSUMER_TEAM:
                issues.append(f"{expected_label}: Consumer Team='{r_cteam}', expected '{CONSUMER_TEAM}'")
            if not r_cname:
                # Task spec leaves Contract Name value undefined -> weak non-empty assertion only
                issues.append(f"{expected_label}: Contract Name is empty")
            if not num_eq(r_pwp, prod_id):
                issues.append(f"{expected_label}: Producer WP ID='{r_pwp}', expected {prod_id} (OP truth)")
            if not num_eq(r_cwp, cons_id):
                issues.append(f"{expected_label}: Consumer WP ID='{r_cwp}', expected {cons_id} (OP truth)")
            if r_status != "Planned":
                issues.append(f"{expected_label}: Status='{r_status}', expected 'Planned'")
            if r_lv:
                issues.append(f"{expected_label}: Last Validated='{r_lv}', expected NULL")

        if issues:
            check(label, 2, False, "; ".join(issues[:4])[:300])
        else:
            check(label, 2, True, "Contract Name asserted non-empty only (undefined in spec)")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_baserow_grid_view() -> None:
    """Grid view 'Contract Matrix': grid type + exactly one sort, Contract ID ASC."""
    label = "12. Grid view Contract Matrix (grid, sorted Contract ID ASC)"
    try:
        table_id = contracts_table_id()
        if not table_id:
            check(label, 1, False, "table not found")
            return

        view_row = baserow_sql(
            f"SELECT v.id FROM database_view v "
            f"WHERE v.table_id = {table_id} AND v.name = 'Contract Matrix'"
        )
        if not view_row:
            check(label, 1, False, "view 'Contract Matrix' not found")
            return
        view_id = view_row.strip().splitlines()[0].strip()

        issues = []
        grid_cnt = baserow_sql(
            f"SELECT count(*) FROM database_gridview WHERE view_ptr_id = {view_id}"
        )
        if int(grid_cnt) != 1:
            issues.append("view is not a grid view")

        sorts_raw = baserow_sql(
            'SELECT f.name, s."order" FROM database_viewsort s '
            "JOIN database_field f ON f.id = s.field_id "
            f"WHERE s.view_id = {view_id}"
        )
        sorts = [l.strip().split("|") for l in sorts_raw.splitlines() if l.strip()]
        if len(sorts) != 1:
            issues.append(f"found {len(sorts)} sorts, expected exactly 1")
        elif sorts[0][0].strip() != "Contract ID" or sorts[0][1].strip().upper() != "ASC":
            issues.append(f"sort={sorts[0]}, expected Contract ID ASC")

        if issues:
            check(label, 1, False, "; ".join(issues))
        else:
            check(label, 1, True)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_extensions_comments()
    check_2_run_analysis_comments()
    check_3_git_commits()
    check_4_op_version()
    check_5_wp_subjects()
    check_P_wp_priorities()
    check_6_wp_estimated_hours()
    check_7_wp_assignees()
    check_8_follows_relations()
    check_9_meeting_and_agenda()
    check_10_baserow_db_and_table()
    check_S_field_schema()
    check_11_baserow_rows()
    check_12_baserow_grid_view()

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
