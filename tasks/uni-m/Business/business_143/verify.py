"""
Verifier for Business-143-I1: End-to-end performance appraisal cycle

Checks: 13 weighted checks across hrms, bigcapital, twenty (total weight 22).
Strategy: docker exec (MariaDB for HRMS & BigCapital, Postgres for Twenty)

Required env vars:
  SERVER_HOSTNAME,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import html
import os
import re
import sys
import subprocess
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.environ.get("HRMS_DB_CONTAINER")

BIGCAPITAL_PORT = os.environ.get("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")

_required = {
    "HRMS_PORT": HRMS_PORT, "HRMS_CONTAINER": HRMS_CONTAINER, "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT, "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BIGCAPITAL_DB_CONTAINER,
    "TWENTY_PORT": TWENTY_PORT, "TWENTY_CONTAINER": TWENTY_CONTAINER,
    "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
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


def normalize_text(s: str) -> str:
    """HTML-unescape, normalize em/en dashes to '--', unicode hyphens to '-',
    collapse whitespace. Used for exact-equality comparison of titles/memos."""
    s = html.unescape(s)
    s = s.replace("—", "--").replace("–", "--")
    s = s.replace("−", "-").replace("‐", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", s).strip()


def normalize_body(s: str) -> str:
    """Normalize a note/task body for keyword checks: HTML-unescape, dash
    normalize, strip thousands separators between digits, collapse whitespace."""
    s = normalize_text(s)
    return re.sub(r"(?<=\d),(?=\d)", "", s)


# Verifier host local timezone offset (minutes), for dueAt date conversion.
_LOCAL_TZ_OFFSET_MIN = int(
    ((datetime.now().astimezone().utcoffset()) or timedelta()).total_seconds() // 60
)


def _pg_due_date_ok(date_str: str) -> str:
    """SQL boolean expr: dueAt::date == D OR (dueAt + local offset)::date == D."""
    return (
        f"(\"dueAt\" IS NOT NULL AND ((\"dueAt\")::date = DATE '{date_str}' "
        f"OR (\"dueAt\" + interval '{_LOCAL_TZ_OFFSET_MIN} minutes')::date = DATE '{date_str}'))"
    )


def _pg_norm_title() -> str:
    """SQL expr normalizing em/en dashes to '--' and collapsing whitespace in title."""
    return (
        "btrim(regexp_replace("
        "replace(replace(title, '—', '--'), '–', '--'), "
        r"'\s+', ' ', 'g'))"
    )


# ── DB credential & name discovery ────────────────────────────────────────────
def _get_container_env(container: str, var: str) -> str:
    """Read an environment variable from inside a container."""
    rc, out, _ = docker_exec(container, "printenv", var, timeout=10)
    return out.strip() if rc == 0 else ""


# Cache values discovered at runtime
_hrms_db_name: str | None = None
_hrms_db_pass: str | None = None
_bc_db_name: str | None = None
_bc_db_pass: str | None = None


def _mysql_cmd(container: str, password: str, *extra: str) -> tuple[int, str, str]:
    """Run mysql command with password."""
    cmd = ["mysql", "-u", "root"]
    if password:
        cmd.append(f"-p{password}")
    cmd.extend(extra)
    return docker_exec(container, *cmd, timeout=15)


def _find_hrms_db() -> tuple[str, str]:
    """Dynamically find the Frappe bench database name and root password."""
    global _hrms_db_name, _hrms_db_pass
    if _hrms_db_name and _hrms_db_pass is not None:
        return _hrms_db_name, _hrms_db_pass

    # Get root password from container env
    _hrms_db_pass = _get_container_env(HRMS_DB_CONTAINER, "MYSQL_ROOT_PASSWORD") or ""

    rc, out, err = _mysql_cmd(
        HRMS_DB_CONTAINER, _hrms_db_pass, "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE '\\_%' "
        "AND SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys');",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot list HRMS databases: {err.strip()}")
    candidates = [l.strip() for l in out.strip().splitlines() if l.strip() and l.strip() != "SCHEMA_NAME"]
    if not candidates:
        raise RuntimeError("No Frappe database found in HRMS MariaDB")
    for db in candidates:
        rc2, out2, _ = _mysql_cmd(
            HRMS_DB_CONTAINER, _hrms_db_pass, "-D", db, "-N", "-B", "-e",
            "SHOW TABLES LIKE 'tabEmployee';",
        )
        if rc2 == 0 and "tabEmployee" in out2:
            _hrms_db_name = db
            return db, _hrms_db_pass
    _hrms_db_name = candidates[0]
    return _hrms_db_name, _hrms_db_pass


def hrms_sql(query: str) -> str:
    """Execute a MariaDB query against the HRMS Frappe database."""
    db, pw = _find_hrms_db()
    cmd = ["mysql", "-u", "root"]
    if pw:
        cmd.append(f"-p{pw}")
    cmd.extend(["--default-character-set=utf8mb4", "-D", db, "-N", "-B", "-e", query])
    rc, out, err = docker_exec(HRMS_DB_CONTAINER, *cmd, timeout=15)
    if rc != 0:
        raise RuntimeError(f"HRMS SQL error: {err.strip()}")
    return out.strip()


def _find_bigcapital_db() -> tuple[str, str]:
    """Find the BigCapital tenant database in MariaDB."""
    global _bc_db_name, _bc_db_pass
    if _bc_db_name and _bc_db_pass is not None:
        return _bc_db_name, _bc_db_pass

    # BigCapital uses bigcapital / bigcapital123 credentials
    _bc_db_pass = "bigcapital123"

    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "-u", "bigcapital", f"-p{_bc_db_pass}", "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE 'bigcapital_tenant_%' OR SCHEMA_NAME = 'bigcapital';",
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"Cannot list BigCapital databases: {err.strip()}")
    candidates = [l.strip() for l in out.strip().splitlines() if l.strip() and l.strip() != "SCHEMA_NAME"]
    for c in candidates:
        if c.startswith("bigcapital_tenant_"):
            _bc_db_name = c
            return c, _bc_db_pass
    if candidates:
        _bc_db_name = candidates[0]
        return _bc_db_name, _bc_db_pass
    raise RuntimeError("No BigCapital database found")


def bigcapital_sql(query: str) -> str:
    """Execute a MariaDB query against the BigCapital tenant database."""
    db, pw = _find_bigcapital_db()
    cmd = ["mysql", "-u", "bigcapital", f"-p{pw}",
           "--default-character-set=utf8mb4", "-D", db, "-N", "-B", "-e", query]
    rc, out, err = docker_exec(BIGCAPITAL_DB_CONTAINER, *cmd, timeout=15)
    if rc != 0:
        raise RuntimeError(f"BigCapital SQL error: {err.strip()}")
    return out.strip()


# Cache for Twenty workspace schema
_twenty_schema: str | None = None


def twenty_sql(query: str) -> str:
    """Execute a Postgres query against the Twenty database (workspace schema)."""
    global _twenty_schema
    if not _twenty_schema:
        rc, out, err = docker_exec(
            TWENTY_DB_CONTAINER,
            "psql", "-U", "postgres", "-d", "default",
            "-t", "-A", "-c",
            # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
            # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
            # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
            # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
            'SELECT ds.schema FROM core."dataSource" ds '
            'JOIN core.workspace w ON w.id = ds."workspaceId" '
            "WHERE w.subdomain = 'yc';",
            timeout=15,
        )
        if rc != 0:
            raise RuntimeError(f"Twenty schema lookup error: {err.strip()}")
        _twenty_schema = out.strip()
        if not _twenty_schema:
            raise RuntimeError("No workspace schema found in Twenty DB")

    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c",
        f'SET search_path TO "{_twenty_schema}"; {query}',
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"Twenty SQL error: {err.strip()}")
    # Filter out 'SET' lines from SET search_path output
    lines = [l for l in out.strip().splitlines() if l.strip() != "SET"]
    return "\n".join(lines).strip()


CYCLE = "H2 2025 Engineering Performance Review"
TEMPLATE = "Engineering Performance Template 2025"
KRA_TDE = "Technical Delivery Excellence"
KRA_CSR = "Customer Satisfaction & Retention"


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_kras_exist() -> None:
    """Verify KRAs 'Technical Delivery Excellence' and 'Customer Satisfaction & Retention' exist."""
    try:
        result = hrms_sql(
            "SELECT name FROM `tabKRA` WHERE name IN "
            "('Technical Delivery Excellence', 'Customer Satisfaction & Retention') "
            "ORDER BY name;"
        )
        found = set(result.splitlines()) if result else set()
        expected = {"Technical Delivery Excellence", "Customer Satisfaction & Retention"}
        missing = expected - found
        check("1. KRAs exist", 1, not missing,
              f"missing: {missing}" if missing else "")
    except Exception as e:
        check("1. KRAs exist", 1, False, f"exception: {e}")


def check_2_appraisal_template() -> None:
    """Verify Appraisal Template with correct KRA weightages 60/40."""
    try:
        result = hrms_sql(
            "SELECT g.key_result_area, g.per_weightage FROM `tabAppraisal Template` t "
            "JOIN `tabAppraisal Template Goal` g ON g.parent = t.name "
            "WHERE t.name = 'Engineering Performance Template 2025' "
            "ORDER BY g.per_weightage DESC;"
        )
        rows = result.splitlines() if result else []
        parsed = {}
        for row in rows:
            parts = row.split("\t")
            if len(parts) == 2:
                parsed[parts[0].strip()] = float(parts[1].strip())

        ok = (
            parsed.get("Technical Delivery Excellence") == 60.0
            and parsed.get("Customer Satisfaction & Retention") == 40.0
        )
        check("2. Appraisal template with weightages", 2, ok,
              f"found: {parsed}" if not ok else "")
    except Exception as e:
        check("2. Appraisal template with weightages", 2, False, f"exception: {e}")


def _goal_row_count(employee: str, goal_name: str, kra: str, target_instrs: list[str]) -> int:
    """COUNT rows in tabGoal where goal_name + kra + target text match on the same row.

    target_instrs: alternative renderings of the target text (e.g. HTML-escaped);
    any single one matching counts. INSTR (not LIKE) because targets contain %/>=.
    Goal.description is Text Editor HTML, so keyphrase INSTR, not full equality.
    """
    goal_esc = goal_name.replace("'", "''")
    kra_esc = kra.replace("'", "''")
    instr_clauses = " OR ".join(
        f"INSTR(IFNULL(description, ''), '{t.replace(chr(39), chr(39) * 2)}') > 0"
        for t in target_instrs
    )
    result = hrms_sql(
        f"SELECT COUNT(*) FROM `tabGoal` "
        f"WHERE employee = '{employee}' "
        f"AND goal_name = '{goal_esc}' "
        f"AND kra = '{kra_esc}' "
        f"AND ({instr_clauses});"
    )
    return int(result.strip() or "0")


def check_3_goals_vikram() -> None:
    """Verify Vikram Singh's goals: title + KRA link + target text, same-row."""
    try:
        c1 = _goal_row_count(
            "HR-EMP-00005", "Reduce sprint defect rate by 30%", KRA_TDE,
            ["30% reduction in defects per sprint"],
        )
        c2 = _goal_row_count(
            "HR-EMP-00005", "Improve client NPS score", KRA_CSR,
            ["NPS score >= 75", "NPS score &gt;= 75"],
        )
        missing = []
        if c1 < 1:
            missing.append("Reduce sprint defect rate by 30% (kra+target)")
        if c2 < 1:
            missing.append("Improve client NPS score (kra+target)")
        check("3. Goals for Vikram Singh", 2, not missing,
              f"missing: {missing}" if missing else "")
    except Exception as e:
        check("3. Goals for Vikram Singh", 2, False, f"exception: {e}")


def check_4_goals_ananya() -> None:
    """Verify Ananya Reddy's goals: title + KRA link + target text, same-row."""
    try:
        c1 = _goal_row_count(
            "HR-EMP-00007", "Deliver all project milestones on time", KRA_TDE,
            ["100% on-time milestone delivery"],
        )
        c2 = _goal_row_count(
            "HR-EMP-00007", "Achieve zero escalations", KRA_CSR,
            ["Zero client escalations in H2 2025"],
        )
        missing = []
        if c1 < 1:
            missing.append("Deliver all project milestones on time (kra+target)")
        if c2 < 1:
            missing.append("Achieve zero escalations (kra+target)")
        check("4. Goals for Ananya Reddy", 2, not missing,
              f"missing: {missing}" if missing else "")
    except Exception as e:
        check("4. Goals for Ananya Reddy", 2, False, f"exception: {e}")


# Expected per-KRA ratings (tabAppraisal Goal.score, manual-rating path)
_EXPECTED_KRA_RATINGS = {
    "HR-EMP-00005": {KRA_TDE: 5.0, KRA_CSR: 4.0},
    "HR-EMP-00007": {KRA_TDE: 3.0, KRA_CSR: 4.0},
}
# Expected per-KRA goal_score contributions (tabAppraisal KRA fallback path):
# rating * weightage / 100 → Vikram 5*0.6=3.0 / 4*0.4=1.6; Ananya 3*0.6=1.8 / 4*0.4=1.6
_EXPECTED_KRA_CONTRIB = {
    "HR-EMP-00005": {KRA_TDE: 3.0, KRA_CSR: 1.6},
    "HR-EMP-00007": {KRA_TDE: 1.8, KRA_CSR: 1.6},
}


def _parse_emp_kra_scores(result: str) -> dict[str, dict[str, float]]:
    """Parse 'employee \\t kra \\t score' rows into {emp: {kra: score}}."""
    out: dict[str, dict[str, float]] = {}
    for row in (result.splitlines() if result else []):
        parts = row.split("\t")
        if len(parts) >= 3 and parts[2].strip() not in ("", "NULL"):
            out.setdefault(parts[0].strip(), {})[parts[1].strip()] = float(parts[2].strip())
    return out


def _kra_map_matches(found: dict[str, float] | None, expected: dict[str, float]) -> bool:
    if not found:
        return False
    return all(
        kra in found and abs(found[kra] - exp) < 0.05
        for kra, exp in expected.items()
    )


def check_5_appraisals_submitted() -> None:
    """Verify appraisals: submitted, correct total scores (4.6/3.4), linked to the
    appraisal template, and correct per-KRA ratings (Appraisal Goal, with
    Appraisal KRA goal_score contributions as fallback)."""
    try:
        result = hrms_sql(
            "SELECT employee, docstatus, total_score, IFNULL(appraisal_template, '') "
            "FROM `tabAppraisal` "
            f"WHERE appraisal_cycle = '{CYCLE}' "
            "AND employee IN ('HR-EMP-00005', 'HR-EMP-00007') "
            "ORDER BY employee;"
        )
        headers: dict[str, tuple[int, float, str]] = {}
        for row in (result.splitlines() if result else []):
            parts = row.split("\t")
            if len(parts) >= 4:
                headers[parts[0].strip()] = (
                    int(parts[1].strip()),
                    float(parts[2].strip() or "0"),
                    parts[3].strip(),
                )

        # Per-KRA ratings: primary = tabAppraisal Goal (manual rating), fallback =
        # tabAppraisal KRA goal_score contributions (goal-progress path).
        goal_scores = _parse_emp_kra_scores(hrms_sql(
            "SELECT a.employee, g.kra, g.score "
            "FROM `tabAppraisal Goal` g "
            "JOIN `tabAppraisal` a ON g.parent = a.name AND g.parenttype = 'Appraisal' "
            f"WHERE a.appraisal_cycle = '{CYCLE}' AND a.docstatus = 1 "
            "AND a.employee IN ('HR-EMP-00005', 'HR-EMP-00007');"
        ))
        kra_scores = _parse_emp_kra_scores(hrms_sql(
            "SELECT a.employee, k.kra, k.goal_score "
            "FROM `tabAppraisal KRA` k "
            "JOIN `tabAppraisal` a ON k.parent = a.name AND k.parenttype = 'Appraisal' "
            f"WHERE a.appraisal_cycle = '{CYCLE}' AND a.docstatus = 1 "
            "AND a.employee IN ('HR-EMP-00005', 'HR-EMP-00007');"
        ))

        expected_totals = {"HR-EMP-00005": 4.6, "HR-EMP-00007": 3.4}
        details = []
        all_ok = True
        for emp, exp_total in expected_totals.items():
            docstatus, total, template = headers.get(emp, (None, None, None))
            header_ok = (
                docstatus == 1
                and total is not None and abs(total - exp_total) < 0.05
                and template == TEMPLATE
            )
            ratings_ok = (
                _kra_map_matches(goal_scores.get(emp), _EXPECTED_KRA_RATINGS[emp])
                or _kra_map_matches(kra_scores.get(emp), _EXPECTED_KRA_CONTRIB[emp])
            )
            if not (header_ok and ratings_ok):
                all_ok = False
                details.append(
                    f"{emp}: docstatus={docstatus}, total={total}, "
                    f"template={template!r}, goal_ratings={goal_scores.get(emp)}, "
                    f"kra_scores={kra_scores.get(emp)}"
                )
        check("5. Appraisals submitted with correct scores", 2, all_ok,
              "; ".join(details) if details else "")
    except Exception as e:
        check("5. Appraisals submitted with correct scores", 2, False, f"exception: {e}")


def check_5b_appraisal_cycle() -> None:
    """Verify Appraisal Cycle record with start date 2025-07-01 and end date 2025-12-31."""
    try:
        result = hrms_sql(
            "SELECT start_date, end_date FROM `tabAppraisal Cycle` "
            f"WHERE name = '{CYCLE}';"
        )
        if not result:
            check("5b. Appraisal Cycle record", 1, False, "cycle not found")
            return
        parts = result.splitlines()[0].split("\t")
        start = parts[0].strip() if len(parts) >= 1 else ""
        end = parts[1].strip() if len(parts) >= 2 else ""
        ok = start == "2025-07-01" and end == "2025-12-31"
        check("5b. Appraisal Cycle record", 1, ok,
              f"start={start}, end={end}, expected 2025-07-01/2025-12-31" if not ok else "")
    except Exception as e:
        check("5b. Appraisal Cycle record", 1, False, f"exception: {e}")


def check_6_salary_component() -> None:
    """Verify salary component 'Performance Bonus' exists and is an Earning."""
    try:
        result = hrms_sql(
            "SELECT name, type FROM `tabSalary Component` "
            "WHERE name = 'Performance Bonus';"
        )
        if not result:
            check("6. Salary component Performance Bonus", 1, False, "not found")
            return
        parts = result.splitlines()[0].split("\t")
        comp_type = parts[1].strip() if len(parts) >= 2 else ""
        ok = comp_type == "Earning"
        check("6. Salary component Performance Bonus", 1, ok,
              f"type={comp_type!r}, expected 'Earning'" if not ok else "")
    except Exception as e:
        check("6. Salary component Performance Bonus", 1, False, f"exception: {e}")


def check_7_employee_incentives() -> None:
    """Verify Employee Incentives for both employees with correct amounts and submitted status."""
    try:
        result = hrms_sql(
            "SELECT employee, incentive_amount, docstatus "
            "FROM `tabEmployee Incentive` "
            "WHERE salary_component = 'Performance Bonus' "
            "AND payroll_date = '2026-01-31' "
            "ORDER BY employee;"
        )
        rows = result.splitlines() if result else []
        incentives = {}
        for row in rows:
            parts = row.split("\t")
            if len(parts) >= 3:
                incentives[parts[0].strip()] = (float(parts[1].strip()), int(parts[2].strip()))

        vikram_ok = incentives.get("HR-EMP-00005") == (15000.0, 1)
        ananya_ok = incentives.get("HR-EMP-00007") == (7500.0, 1)
        details = []
        if not vikram_ok:
            details.append(f"Vikram: {incentives.get('HR-EMP-00005', 'not found')}")
        if not ananya_ok:
            details.append(f"Ananya: {incentives.get('HR-EMP-00007', 'not found')}")
        check("7. Employee Incentives submitted", 3,
              vikram_ok and ananya_ok, "; ".join(details) if details else "")
    except Exception as e:
        check("7. Employee Incentives submitted", 3, False, f"exception: {e}")


def check_8_bigcapital_accounts() -> None:
    """Verify accounts exist with the required types: 'Performance Bonus Expense'
    (expense) and 'Accrued Performance Bonus Payable' (other-current-liability)."""
    try:
        result = bigcapital_sql(
            "SELECT NAME, ACCOUNT_TYPE FROM ACCOUNTS "
            "WHERE NAME IN ('Performance Bonus Expense', 'Accrued Performance Bonus Payable');"
        )
        found: dict[str, str] = {}
        for row in (result.splitlines() if result else []):
            parts = row.split("\t")
            if len(parts) >= 2:
                found[parts[0].strip()] = parts[1].strip()
        expected = {
            "Performance Bonus Expense": "expense",
            "Accrued Performance Bonus Payable": "other-current-liability",
        }
        problems = []
        for name, acct_type in expected.items():
            if name not in found:
                problems.append(f"missing: {name}")
            elif found[name] != acct_type:
                problems.append(f"{name}: type={found[name]!r}, expected {acct_type!r}")
        check("8. BigCapital accounts exist", 1, not problems,
              "; ".join(problems) if problems else "")
    except Exception as e:
        check("8. BigCapital accounts exist", 1, False, f"exception: {e}")


EXPECTED_MEMO = (
    "Performance bonus accrual -- H2 2025 Engineering Performance Review -- "
    "Vikram Singh (15000), Ananya Reddy (7500)"
)


def check_9_journal_entry() -> None:
    """Verify a single published journal dated 2025-12-31 with exactly 2 entries:
    debit Performance Bonus Expense 22500 / credit Accrued Performance Bonus
    Payable 22500, and memo matching the task text (same-journal scoping)."""
    try:
        result = bigcapital_sql(
            "SELECT j.ID, TRIM(j.DESCRIPTION) FROM MANUAL_JOURNALS j "
            "WHERE DATE(j.DATE) = '2025-12-31' "
            "AND j.PUBLISHED_AT IS NOT NULL "
            "AND (SELECT COUNT(*) FROM MANUAL_JOURNALS_ENTRIES e "
            "     WHERE e.MANUAL_JOURNAL_ID = j.ID) = 2 "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "            JOIN ACCOUNTS a ON a.ID = e.ACCOUNT_ID "
            "            WHERE e.MANUAL_JOURNAL_ID = j.ID "
            "            AND a.NAME = 'Performance Bonus Expense' "
            "            AND ABS(IFNULL(e.DEBIT, 0) - 22500) < 0.01 "
            "            AND IFNULL(e.CREDIT, 0) = 0) "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "            JOIN ACCOUNTS a ON a.ID = e.ACCOUNT_ID "
            "            WHERE e.MANUAL_JOURNAL_ID = j.ID "
            "            AND a.NAME = 'Accrued Performance Bonus Payable' "
            "            AND ABS(IFNULL(e.CREDIT, 0) - 22500) < 0.01 "
            "            AND IFNULL(e.DEBIT, 0) = 0);"
        )
        rows = result.splitlines() if result else []
        expected_norm = normalize_text(EXPECTED_MEMO)
        memos = []
        memo_ok = False
        for row in rows:
            parts = row.split("\t", 1)
            desc = parts[1].strip() if len(parts) >= 2 else ""
            memos.append(desc)
            if desc != "NULL" and normalize_text(desc) == expected_norm:
                memo_ok = True
        if not rows:
            check("9. Journal entry debit/credit 22500", 3, False,
                  "no published 2-line journal on 2025-12-31 with debit/credit 22500 on the required accounts")
        else:
            check("9. Journal entry debit/credit 22500", 3, memo_ok,
                  f"memo mismatch, found: {memos}" if not memo_ok else "")
    except Exception as e:
        check("9. Journal entry debit/credit 22500", 3, False, f"exception: {e}")


NOTE_TITLE = "Appraisal Cycle Results -- H2 2025 Engineering Performance Review"
NOTE_KEYWORDS = [
    "HR-EMP-00005", "HR-EMP-00007",
    "4.6", "3.4", "15000", "7500", "22500",
    "Technical Delivery Excellence", "Customer Satisfaction & Retention",
    "Engineering Performance Template 2025",
    "2025-12-31", "2026-01-31",
    "Performance Bonus Expense", "Accrued Performance Bonus Payable",
]


def check_10_twenty_note() -> None:
    """Verify the results note: exact title (dash-normalized), not deleted, body
    containing all required facts, cross-validated against actual HRMS scores."""
    try:
        title_esc = NOTE_TITLE.replace("'", "''")
        result = twenty_sql(
            'SELECT regexp_replace(COALESCE("bodyV2Markdown", \'\'), '
            r"'[\n\r\t]+', ' ', 'g') FROM note "
            'WHERE "deletedAt" IS NULL '
            f"AND {_pg_norm_title()} = '{title_esc}';"
        )
        rows = [r for r in result.splitlines() if r.strip()] if result.strip() else []
        if not rows:
            check("10. Twenty note with appraisal results", 2, False,
                  "note not found (exact title, not deleted)")
            return

        # Ground-truth cross-check: recompute overall scores from tabAppraisal
        # (HRMS actual state must appear in the note; typing the note without
        # doing the HRMS work must not score).
        truth_result = hrms_sql(
            "SELECT employee, total_score FROM `tabAppraisal` "
            f"WHERE appraisal_cycle = '{CYCLE}' AND docstatus = 1 "
            "AND employee IN ('HR-EMP-00005', 'HR-EMP-00007');"
        )
        truth_scores: dict[str, str] = {}
        for row in (truth_result.splitlines() if truth_result else []):
            parts = row.split("\t")
            if len(parts) >= 2 and parts[1].strip() not in ("", "NULL"):
                truth_scores[parts[0].strip()] = f"{round(float(parts[1].strip()), 1):.1f}"

        best_missing: list[str] | None = None
        ok = False
        for body_raw in rows:
            body = normalize_body(body_raw)
            missing = [kw for kw in NOTE_KEYWORDS if kw not in body]
            for emp in ("HR-EMP-00005", "HR-EMP-00007"):
                score = truth_scores.get(emp)
                if score is None:
                    missing.append(f"HRMS truth score for {emp} (appraisal not submitted)")
                elif score not in body:
                    missing.append(f"recomputed overall {score} ({emp})")
            if not missing:
                ok = True
                break
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
        check("10. Twenty note with appraisal results", 2, ok,
              f"missing in body: {best_missing}" if not ok else "")
    except Exception as e:
        check("10. Twenty note with appraisal results", 2, False, f"exception: {e}")


def _check_twenty_task(label: str, weight: int, title: str, due_date: str,
                       body_keywords: list[str]) -> None:
    """Same-row task check: exact title (dash-normalized), not deleted, dueAt
    matching due_date under timezone conversion, body containing keywords."""
    try:
        title_esc = title.replace("'", "''")
        result = twenty_sql(
            f"SELECT (CASE WHEN {_pg_due_date_ok(due_date)} THEN 't' ELSE 'f' END) "
            "|| E'\\t' || regexp_replace(COALESCE(\"bodyV2Markdown\", ''), "
            r"'[\n\r\t]+', ' ', 'g') FROM task "
            'WHERE "deletedAt" IS NULL '
            f"AND {_pg_norm_title()} = '{title_esc}';"
        )
        rows = [r for r in result.splitlines() if r.strip()] if result.strip() else []
        if not rows:
            check(label, weight, False, "task not found (exact title, not deleted)")
            return
        best_detail = ""
        ok = False
        for row in rows:
            parts = row.split("\t", 1)
            due_ok = parts[0].strip() == "t"
            body = normalize_body(parts[1] if len(parts) >= 2 else "").lower()
            missing = [kw for kw in body_keywords if kw.lower() not in body]
            if due_ok and not missing:
                ok = True
                break
            problems = []
            if not due_ok:
                problems.append(f"due date != {due_date}")
            if missing:
                problems.append(f"missing in body: {missing}")
            best_detail = "; ".join(problems)
        check(label, weight, ok, best_detail if not ok else "")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_11_twenty_task_payroll() -> None:
    """Verify task 'Process bonus payroll -- ...' due 2026-01-31 with body facts."""
    _check_twenty_task(
        "11. Twenty task: process bonus payroll", 1,
        "Process bonus payroll -- H2 2025 Engineering Performance Review",
        "2026-01-31",
        ["22500", "payroll"],
    )


def check_12_twenty_task_communicate() -> None:
    """Verify task 'Communicate appraisal results to employees' due 2026-01-15 with body facts."""
    _check_twenty_task(
        "12. Twenty task: communicate results", 1,
        "Communicate appraisal results to employees",
        "2026-01-15",
        ["4.6", "15000", "3.4", "7500"],
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_kras_exist()
    check_2_appraisal_template()
    check_3_goals_vikram()
    check_4_goals_ananya()
    check_5_appraisals_submitted()
    check_5b_appraisal_cycle()
    check_6_salary_component()
    check_7_employee_incentives()
    check_8_bigcapital_accounts()
    check_9_journal_entry()
    check_10_twenty_note()
    check_11_twenty_task_payroll()
    check_12_twenty_task_communicate()

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
