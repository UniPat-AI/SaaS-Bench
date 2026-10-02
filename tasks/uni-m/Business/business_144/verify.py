"""
Verifier for Business-144-I5: Shift Scheduling, Overtime Accounting, and CRM Task Management

Checks: 16 weighted checks across hrms, bigcapital, twenty (total weight 26).
Strategy: docker exec MariaDB for hrms; REST API + docker exec MySQL (tenant DB)
for bigcapital; docker exec Postgres for twenty.

Required env vars:
  SERVER_HOSTNAME, HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

HRMS_PORT = os.getenv("HRMS_PORT")
HRMS_CONTAINER = os.getenv("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.getenv("HRMS_DB_CONTAINER")
BIGCAPITAL_PORT = os.getenv("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.getenv("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.getenv("BIGCAPITAL_DB_CONTAINER")
TWENTY_PORT = os.getenv("TWENTY_PORT")
TWENTY_CONTAINER = os.getenv("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.getenv("TWENTY_DB_CONTAINER")

_required = {
    "HRMS_PORT": HRMS_PORT, "HRMS_CONTAINER": HRMS_CONTAINER,
    "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT, "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BIGCAPITAL_DB_CONTAINER,
    "TWENTY_PORT": TWENTY_PORT, "TWENTY_CONTAINER": TWENTY_CONTAINER,
    "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

BIGCAPITAL_BASE = f"http://{HOST}:{BIGCAPITAL_PORT}"

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


def _norm_text(s: str) -> str:
    """Normalize text for comparison: em/en dashes -> '--', unescape '&amp;',
    flatten escaped newlines, collapse whitespace runs."""
    s = (s or "").replace("—", "--").replace("–", "--")
    s = s.replace("&amp;", "&").replace("\\n", " ")
    return " ".join(s.split())


def _local_utc_offset() -> timedelta:
    """UTC offset of the verifier host's local timezone (no hardcoded city)."""
    off = datetime.now().astimezone().utcoffset()
    return off if off is not None else timedelta(0)


_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?"
    r"(?:([+-])(\d{2})(?::?(\d{2}))?)?$"
)


def date_matches_tz(stored: str, expected: str) -> bool:
    """True if the stored date/timestamp denotes calendar date `expected`.

    Semantics: stored::date == D  OR  (stored + local_utc_offset)::date == D.
    Pure dates (exactly 10 chars) use equality; timestamps are parsed and
    compared in UTC and in the verifier host's local timezone. No bare
    substring matching.
    """
    s = (stored or "").strip()
    if not s or s.upper() == "NULL":
        return False
    if len(s) == 10:
        return s == expected
    m = _TS_RE.match(s)
    if not m:
        return False
    y, mo, d, hh, mi, ss = (int(m.group(i)) for i in range(1, 7))
    dt = datetime(y, mo, d, hh, mi, ss)
    # Normalize to UTC using any explicit offset suffix (psql prints '+00').
    if m.group(7):
        suffix = timedelta(hours=int(m.group(8)), minutes=int(m.group(9) or 0))
        if m.group(7) == "+":
            dt = dt - suffix
        else:
            dt = dt + suffix
    if str(dt.date()) == expected:
        return True
    return str((dt + _local_utc_offset()).date()) == expected


def _discover_hrms_db() -> str:
    """Discover the Frappe bench database name in the HRMS MariaDB container."""
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mariadb", "-u", "root", "-phrms123456", "--default-character-set=utf8mb4",
        "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE '\\_%' AND SCHEMA_NAME NOT IN "
        "('information_schema','mysql','performance_schema','sys')",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot discover HRMS DB: {err.strip()}")
    # Pick the schema that has tabShift Type
    for schema in out.strip().split("\n"):
        schema = schema.strip()
        if not schema:
            continue
        rc2, out2, _ = docker_exec(
            HRMS_DB_CONTAINER,
            "mariadb", "-u", "root", "-phrms123456", "--default-character-set=utf8mb4",
            "-D", schema, "-N", "-B", "-e",
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema='{schema}' AND table_name='tabShift Type'",
        )
        if rc2 == 0 and out2.strip() == "1":
            return schema
    raise RuntimeError("No Frappe bench DB found with tabShift Type")


_hrms_db_name: str | None = None


def hrms_sql(query: str) -> str:
    """Run a MariaDB query against the HRMS Frappe database."""
    global _hrms_db_name
    if _hrms_db_name is None:
        _hrms_db_name = _discover_hrms_db()
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mariadb", "-u", "root", "-phrms123456", "--default-character-set=utf8mb4",
        "-D", _hrms_db_name, "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"mariadb error: {err.strip()}")
    return out.strip()


def _bigcapital_token_and_org() -> tuple[str, str]:
    """Authenticate to BigCapital and return (access_token, organization_id)."""
    r = requests.post(
        f"{BIGCAPITAL_BASE}/api/auth/signin",
        json={"email": "admin@bigcapital.local", "password": "admin123"},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    return data["access_token"], data["organization_id"]


_bc_auth: tuple[str, str] | None = None


def bc_api_get(path: str, params: dict | None = None) -> dict:
    """GET a BigCapital API endpoint (authenticated)."""
    global _bc_auth
    if _bc_auth is None:
        _bc_auth = _bigcapital_token_and_org()
    token, org_id = _bc_auth
    r = requests.get(
        f"{BIGCAPITAL_BASE}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "organization-id": org_id},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


_bc_db_name: str | None = None


def _find_bigcapital_db() -> str:
    """Find the BigCapital tenant database name in the MySQL container."""
    global _bc_db_name
    if _bc_db_name:
        return _bc_db_name
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "-u", "bigcapital", "-pbigcapital123", "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE 'bigcapital_tenant_%' OR SCHEMA_NAME = 'bigcapital'",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot list BigCapital databases: {err.strip()}")
    candidates = [l.strip() for l in out.strip().splitlines() if l.strip()]
    for c in candidates:
        if c.startswith("bigcapital_tenant_"):
            _bc_db_name = c
            return c
    if candidates:
        _bc_db_name = candidates[0]
        return _bc_db_name
    raise RuntimeError("No BigCapital database found")


def bigcapital_sql(query: str) -> str:
    """Run a MySQL query against the BigCapital tenant database."""
    db = _find_bigcapital_db()
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "-u", "bigcapital", "-pbigcapital123",
        "--default-character-set=utf8mb4", "-D", db, "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"BigCapital SQL error: {err.strip()}")
    return out.strip()


def _discover_twenty_workspace() -> str:
    """Discover the Twenty workspace schema name."""
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c",
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot discover Twenty workspace: {err.strip()}")
    ws = out.strip()
    if not ws:
        raise RuntimeError("No workspace schema found in Twenty DB")
    return ws


_twenty_ws: str | None = None


def twenty_sql(query: str) -> str:
    """Run a Postgres query against the Twenty workspace schema."""
    global _twenty_ws
    if _twenty_ws is None:
        _twenty_ws = _discover_twenty_workspace()
    full_query = f"SET search_path TO {_twenty_ws}; {query}"
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", full_query,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    # Remove the 'SET' line from SET search_path output
    lines = out.strip().split("\n")
    result_lines = [l for l in lines if l.strip() != "SET"]
    return "\n".join(result_lines).strip()


def _twenty_flat_body(table: str, title: str) -> str:
    """Fetch the newline-flattened markdown body of a task/note by exact title."""
    return twenty_sql(
        "SELECT regexp_replace("
        "COALESCE(\"bodyV2Markdown\", \"bodyV2Blocknote\"::text, ''), "
        "E'[\\n\\r]+', ' ', 'g') "
        f"FROM {table} "
        "WHERE \"deletedAt\" IS NULL "
        f"AND title = '{title}' "
        "LIMIT 1"
    )


# ── HRMS checks ──────────────────────────────────────────────────────────────

def check_1_gamma_shift() -> None:
    """Gamma Shift exists with start 06:30, end 14:30, grace 8min, auto-attendance."""
    try:
        row = hrms_sql(
            "SELECT start_time, end_time, enable_auto_attendance, "
            "early_exit_grace_period, late_entry_grace_period "
            "FROM `tabShift Type` WHERE name='Gamma Shift'"
        )
        if not row:
            check("1. Gamma Shift type", 1, False, "not found")
            return
        parts = row.split("\t")
        ok = (
            parts[0].startswith("06:30") and parts[1].startswith("14:30")
            and parts[2] == "1"
            and float(parts[3]) == 8 and float(parts[4]) == 8
        )
        check("1. Gamma Shift type", 1, ok,
              f"start={parts[0]}, end={parts[1]}, auto={parts[2]}, grace={parts[3]}/{parts[4]}")
    except Exception as e:
        check("1. Gamma Shift type", 1, False, f"exception: {e}")


def check_2_sigma_shift() -> None:
    """Sigma Shift exists with start 14:30, end 22:30, grace 8min, auto-attendance."""
    try:
        row = hrms_sql(
            "SELECT start_time, end_time, enable_auto_attendance, "
            "early_exit_grace_period, late_entry_grace_period "
            "FROM `tabShift Type` WHERE name='Sigma Shift'"
        )
        if not row:
            check("2. Sigma Shift type", 1, False, "not found")
            return
        parts = row.split("\t")
        ok = (
            parts[0].startswith("14:30") and parts[1].startswith("22:30")
            and parts[2] == "1"
            and float(parts[3]) == 8 and float(parts[4]) == 8
        )
        check("2. Sigma Shift type", 1, ok,
              f"start={parts[0]}, end={parts[1]}, auto={parts[2]}, grace={parts[3]}/{parts[4]}")
    except Exception as e:
        check("2. Sigma Shift type", 1, False, f"exception: {e}")


def check_3_theta_shift() -> None:
    """Theta Shift exists with start 22:30, end 06:30, grace 8min, auto-attendance."""
    try:
        row = hrms_sql(
            "SELECT start_time, end_time, enable_auto_attendance, "
            "early_exit_grace_period, late_entry_grace_period "
            "FROM `tabShift Type` WHERE name='Theta Shift'"
        )
        if not row:
            check("3. Theta Shift type", 1, False, "not found")
            return
        parts = row.split("\t")
        ok = (
            parts[0].startswith("22:30") and parts[1].startswith("06:30")
            and parts[2] == "1"
            and float(parts[3]) == 8 and float(parts[4]) == 8
        )
        check("3. Theta Shift type", 1, ok,
              f"start={parts[0]}, end={parts[1]}, auto={parts[2]}, grace={parts[3]}/{parts[4]}")
    except Exception as e:
        check("3. Theta Shift type", 1, False, f"exception: {e}")


def check_4_gamma_bulk_assignment() -> None:
    """Gamma Shift bulk assignment covers the ENTIRE 'Finance & Accounting - TVS'
    active roster (derived independently from tabEmployee), 2026-09-01 to
    2026-09-30, submitted and Active."""
    label = "4. Gamma Shift bulk assignment (Finance & Accounting - TVS)"
    try:
        roster = hrms_sql(
            "SELECT COUNT(*) FROM `tabEmployee` "
            "WHERE department='Finance & Accounting - TVS' AND status='Active'"
        )
        n_roster = int(roster or 0)
        uncovered = hrms_sql(
            "SELECT e.name FROM `tabEmployee` e "
            "WHERE e.department='Finance & Accounting - TVS' AND e.status='Active' "
            "AND NOT EXISTS (SELECT 1 FROM `tabShift Assignment` sa "
            "WHERE sa.employee=e.name AND sa.shift_type='Gamma Shift' "
            "AND sa.docstatus=1 AND sa.status='Active' "
            "AND sa.start_date='2026-09-01' AND sa.end_date='2026-09-30')"
        )
        missing = [l.strip() for l in uncovered.split("\n") if l.strip()] if uncovered else []
        ok = n_roster >= 1 and not missing
        check(label, 2, ok,
              f"dept roster={n_roster} active employees, "
              f"uncovered={missing if missing else 'none'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_sigma_individual_assignments() -> None:
    """Sigma Shift assignments for Kavitha Iyer, Arjun Nair, Ananya Reddy
    (matched by employee ID, exact date range, submitted, Active)."""
    try:
        employees = [
            ("HR-EMP-00012", "Kavitha Iyer"),
            ("HR-EMP-00011", "Arjun Nair"),
            ("HR-EMP-00007", "Ananya Reddy"),
        ]
        found = []
        missing = []
        for emp_id, emp_name in employees:
            row = hrms_sql(
                f"SELECT COUNT(*) FROM `tabShift Assignment` "
                f"WHERE shift_type='Sigma Shift' "
                f"AND employee='{emp_id}' "
                f"AND docstatus=1 AND status='Active' "
                f"AND start_date='2026-09-01' AND end_date='2026-09-30'"
            )
            if int(row or 0) > 0:
                found.append(emp_name)
            else:
                missing.append(f"{emp_name} ({emp_id})")
        ok = len(missing) == 0
        check("5. Sigma Shift individual assignments (3 employees)", 2, ok,
              f"found={found}, missing={missing}" if missing else "all 3 found")
    except Exception as e:
        check("5. Sigma Shift individual assignments (3 employees)", 2, False, f"exception: {e}")


def check_6_theta_individual_assignments() -> None:
    """Theta Shift assignments for Mohammed Farooq, Sanjay Krishnan
    (matched by employee ID, exact date range, submitted, Active)."""
    try:
        employees = [
            ("HR-EMP-00015", "Mohammed Farooq"),
            ("HR-EMP-00014", "Sanjay Krishnan"),
        ]
        found = []
        missing = []
        for emp_id, emp_name in employees:
            row = hrms_sql(
                f"SELECT COUNT(*) FROM `tabShift Assignment` "
                f"WHERE shift_type='Theta Shift' "
                f"AND employee='{emp_id}' "
                f"AND docstatus=1 AND status='Active' "
                f"AND start_date='2026-09-01' AND end_date='2026-09-30'"
            )
            if int(row or 0) > 0:
                found.append(emp_name)
            else:
                missing.append(f"{emp_name} ({emp_id})")
        ok = len(missing) == 0
        check("6. Theta Shift individual assignments (2 employees)", 2, ok,
              f"found={found}, missing={missing}" if missing else "all 2 found")
    except Exception as e:
        check("6. Theta Shift individual assignments (2 employees)", 2, False, f"exception: {e}")


def check_7_shift_request_approved() -> None:
    """Shift Request for Deepika Joshi (HR-EMP-00010) to Sigma Shift on
    2026-09-12 (from_date AND to_date), submitted and Approved."""
    try:
        row = hrms_sql(
            "SELECT status, shift_type "
            "FROM `tabShift Request` "
            "WHERE employee='HR-EMP-00010' "
            "AND from_date='2026-09-12' AND to_date='2026-09-12' "
            "AND docstatus=1 LIMIT 1"
        )
        if not row:
            check("7. Shift Request Deepika Joshi approved", 2, False,
                  "not found (employee=HR-EMP-00010, from_date=to_date=2026-09-12, submitted)")
            return
        parts = row.split("\t")
        status = parts[0] if len(parts) > 0 else ""
        to_shift = parts[1] if len(parts) > 1 else ""
        ok = status == "Approved" and "Sigma" in to_shift
        check("7. Shift Request Deepika Joshi approved", 2, ok,
              f"status={status}, shift_type={to_shift}")
    except Exception as e:
        check("7. Shift Request Deepika Joshi approved", 2, False, f"exception: {e}")


def check_8_overtime_type() -> None:
    """Overtime Type 'Night Differential Overtime' with multiplier 1.25."""
    try:
        # First check if the table exists
        exists = hrms_sql(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_name='tabOvertime Type'"
        )
        if int(exists or 0) == 0:
            check("8. Overtime Type Night Differential Overtime", 1, False,
                  "tabOvertime Type table does not exist")
            return
        row = hrms_sql(
            "SELECT name FROM `tabOvertime Type` "
            "WHERE name='Night Differential Overtime' LIMIT 1"
        )
        if not row:
            check("8. Overtime Type Night Differential Overtime", 1, False, "not found")
            return
        val = hrms_sql(
            "SELECT standard_multiplier FROM `tabOvertime Type` "
            "WHERE name='Night Differential Overtime'"
        )
        ok = abs(float(val or 0) - 1.25) < 0.01
        check("8. Overtime Type Night Differential Overtime", 1, ok,
              f"standard_multiplier={val}")
    except Exception as e:
        check("8. Overtime Type Night Differential Overtime", 1, False, f"exception: {e}")


def check_9_ot_slip_suresh() -> None:
    """Overtime Slip for Suresh Menon (HR-EMP-00009), submitted, with a detail
    row: date 2026-09-06, type 'Night Differential Overtime', 6 hours
    (parent+child same-row join)."""
    label = "9. OT Slip Suresh Menon (6h, 2026-09-06, Night Differential Overtime)"
    try:
        row = hrms_sql(
            "SELECT s.name, d.date, d.overtime_type, d.overtime_duration "
            "FROM `tabOvertime Slip` s "
            "JOIN `tabOvertime Details` d "
            "ON d.parent=s.name AND d.parenttype='Overtime Slip' "
            "WHERE s.employee='HR-EMP-00009' AND s.docstatus=1 "
            "AND d.date='2026-09-06' "
            "AND d.overtime_type='Night Differential Overtime' "
            "AND ABS(d.overtime_duration-6)<0.01 "
            "LIMIT 1"
        )
        if not row:
            check(label, 2, False,
                  "no submitted slip for HR-EMP-00009 with a matching detail row")
            return
        parts = row.split("\t")
        check(label, 2, True,
              f"slip={parts[0]}, date={parts[1] if len(parts) > 1 else ''}, "
              f"type={parts[2] if len(parts) > 2 else ''}, "
              f"duration={parts[3] if len(parts) > 3 else ''}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_10_ot_slip_rahul() -> None:
    """Overtime Slip for Rahul Verma (HR-EMP-00013), submitted, with a detail
    row: date 2026-09-13, type 'Night Differential Overtime', 4 hours
    (parent+child same-row join)."""
    label = "10. OT Slip Rahul Verma (4h, 2026-09-13, Night Differential Overtime)"
    try:
        row = hrms_sql(
            "SELECT s.name, d.date, d.overtime_type, d.overtime_duration "
            "FROM `tabOvertime Slip` s "
            "JOIN `tabOvertime Details` d "
            "ON d.parent=s.name AND d.parenttype='Overtime Slip' "
            "WHERE s.employee='HR-EMP-00013' AND s.docstatus=1 "
            "AND d.date='2026-09-13' "
            "AND d.overtime_type='Night Differential Overtime' "
            "AND ABS(d.overtime_duration-4)<0.01 "
            "LIMIT 1"
        )
        if not row:
            check(label, 2, False,
                  "no submitted slip for HR-EMP-00013 with a matching detail row")
            return
        parts = row.split("\t")
        check(label, 2, True,
              f"slip={parts[0]}, date={parts[1] if len(parts) > 1 else ''}, "
              f"type={parts[2] if len(parts) > 2 else ''}, "
              f"duration={parts[3] if len(parts) > 3 else ''}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── BigCapital checks ────────────────────────────────────────────────────────

def check_11_expense_account() -> None:
    """Overtime Shift Differential Expense account exists as expense type."""
    try:
        data = bc_api_get("/api/accounts")
        accounts = data.get("accounts", [])
        match = [a for a in accounts if a.get("name") == "Overtime Shift Differential Expense"]
        if not match:
            check("11. Expense account (Overtime Shift Differential Expense)", 1, False, "not found")
            return
        atype = match[0].get("account_type", "")
        ok = "expense" in atype.lower()
        check("11. Expense account (Overtime Shift Differential Expense)", 1, ok,
              f"account_type={atype}")
    except Exception as e:
        check("11. Expense account (Overtime Shift Differential Expense)", 1, False, f"exception: {e}")


def check_12_ap_account() -> None:
    """[precondition, 0pt] Accounts Payable (A/P) exists as liability type.

    Seed-provided account ('verify or create' in the task): pristine
    environments already pass, so this carries no score weight and exists
    only for diagnostics (check 13 depends on it)."""
    label = "12. Accounts Payable (A/P) account [precondition]"
    try:
        data = bc_api_get("/api/accounts")
        accounts = data.get("accounts", [])
        match = [a for a in accounts if a.get("name") == "Accounts Payable (A/P)"]
        if not match:
            check(label, 0, False,
                  "FATAL: seed account 'Accounts Payable (A/P)' missing -- "
                  "journal check 13 cannot pass")
            return
        atype = match[0].get("account_type", "")
        ok = "liabilit" in atype.lower() or "payable" in atype.lower() or "current_liability" in atype.lower()
        check(label, 0, ok,
              f"account_type={atype}" if ok
              else f"FATAL: unexpected account_type={atype}")
    except Exception as e:
        check(label, 0, False, f"FATAL: exception: {e}")


JOURNAL_MEMO = ("Overtime accrual -- Suresh Menon (6h) + Rahul Verma (4h) "
                "-- rate 50 x 1.25 multiplier")


def check_13_journal_entry() -> None:
    """A SINGLE published manual journal dated 2026-09-30 with exactly 2 entries:
    debit 'Overtime Shift Differential Expense' 625.00, credit
    'Accounts Payable (A/P)' 625.00, and the exact memo (dash-normalized).
    All conditions scoped to the same journal (DB query)."""
    label = "13. Journal entry (single journal: 625.00 debit/credit, memo, 2026-09-30)"
    try:
        out = bigcapital_sql(
            "SELECT j.ID, j.DESCRIPTION FROM MANUAL_JOURNALS j "
            "WHERE DATE(j.DATE)='2026-09-30' AND j.PUBLISHED_AT IS NOT NULL "
            "AND (SELECT COUNT(*) FROM MANUAL_JOURNALS_ENTRIES e "
            "     WHERE e.MANUAL_JOURNAL_ID=j.ID)=2 "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "            JOIN ACCOUNTS a ON a.ID=e.ACCOUNT_ID "
            "            WHERE e.MANUAL_JOURNAL_ID=j.ID "
            "            AND a.NAME='Overtime Shift Differential Expense' "
            "            AND ABS(e.DEBIT-625)<0.01 AND COALESCE(e.CREDIT,0)=0) "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "            JOIN ACCOUNTS a ON a.ID=e.ACCOUNT_ID "
            "            WHERE e.MANUAL_JOURNAL_ID=j.ID "
            "            AND a.NAME='Accounts Payable (A/P)' "
            "            AND ABS(e.CREDIT-625)<0.01 AND COALESCE(e.DEBIT,0)=0)"
        )
        rows = [r for r in out.split("\n") if r.strip()] if out else []
        if not rows:
            check(label, 3, False,
                  "no published journal on 2026-09-30 with exactly 2 entries, "
                  "debit(Overtime Shift Differential Expense)=625.00 and "
                  "credit(Accounts Payable (A/P))=625.00")
            return
        expected_memo = _norm_text(JOURNAL_MEMO)
        ok = False
        details = []
        for line in rows:
            parts = line.split("\t", 1)
            jid = parts[0].strip()
            memo = parts[1].strip() if len(parts) > 1 else ""
            details.append(f"id={jid}, memo={memo[:100]}")
            if _norm_text(memo) == expected_memo:
                ok = True
        check(label, 3, ok,
              "; ".join(details)[:220] if ok
              else "amounts/date/entries matched but memo mismatch: " + "; ".join(details)[:180])
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── Twenty checks (DB) ──────────────────────────────────────────────────────

def check_14_review_task() -> None:
    """Task 'Review shift schedule compliance -- 2026-09-01 to 2026-09-30' with
    due 2026-10-07 (timezone-aware) and body covering the shift deployment."""
    label = "14. Twenty task: Review shift schedule compliance"
    title = "Review shift schedule compliance -- 2026-09-01 to 2026-09-30"
    try:
        due = twenty_sql(
            "SELECT COALESCE(\"dueAt\"::text, 'NULL') FROM task "
            "WHERE \"deletedAt\" IS NULL "
            f"AND title = '{title}' "
            "LIMIT 1"
        ).strip()
        if not due:
            check(label, 2, False, "task not found (exact title)")
            return
        due_ok = date_matches_tz(due, "2026-10-07")
        body = _norm_text(_twenty_flat_body("task", title))
        keywords = [
            "Gamma Shift", "Sigma Shift", "Theta Shift",
            "Finance & Accounting - TVS", "Deepika Joshi", "2026-09-12",
        ]
        missing = [k for k in keywords if k not in body]
        ok = due_ok and not missing
        check(label, 2, ok,
              f"dueAt={due} (match={due_ok}), "
              f"body missing={missing if missing else 'none'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15_payments_task() -> None:
    """Task 'Process overtime payments -- 2026-09-30' with due 2026-10-14
    (timezone-aware) and body covering the overtime cost breakdown."""
    label = "15. Twenty task: Process overtime payments"
    title = "Process overtime payments -- 2026-09-30"
    try:
        due = twenty_sql(
            "SELECT COALESCE(\"dueAt\"::text, 'NULL') FROM task "
            "WHERE \"deletedAt\" IS NULL "
            f"AND title = '{title}' "
            "LIMIT 1"
        ).strip()
        if not due:
            check(label, 2, False, "task not found (exact title)")
            return
        due_ok = date_matches_tz(due, "2026-10-14")
        body = _norm_text(_twenty_flat_body("task", title))
        keywords = [
            "Suresh Menon", "6 hours", "2026-09-06", "375.00",
            "Rahul Verma", "4 hours", "2026-09-13", "250.00",
            "625.00", "2026-09-30",
        ]
        missing = [k for k in keywords if k not in body]
        ok = due_ok and not missing
        check(label, 2, ok,
              f"dueAt={due} (match={due_ok}), "
              f"body missing={missing if missing else 'none'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_16_summary_note() -> None:
    """Note 'Shift & Overtime Summary -- 2026-09-01 to 2026-09-30' exists with a
    body covering all three sections (shift config, assignments, overtime)."""
    label = "16. Twenty note: Shift & Overtime Summary"
    title = "Shift & Overtime Summary -- 2026-09-01 to 2026-09-30"
    try:
        row = twenty_sql(
            "SELECT id FROM note "
            "WHERE \"deletedAt\" IS NULL "
            f"AND title = '{title}' "
            "LIMIT 1"
        )
        if not row:
            check(label, 2, False, "note not found (exact title)")
            return
        body = _norm_text(_twenty_flat_body("note", title))
        keywords = [
            # SHIFT CONFIGURATION section
            "06:30", "14:30", "22:30", "grace 8 min",
            # ASSIGNMENTS section
            "Kavitha Iyer", "Arjun Nair", "Ananya Reddy",
            "Mohammed Farooq", "Sanjay Krishnan",
            "Deepika Joshi", "Sigma Shift", "2026-09-12",
            # OVERTIME section
            "375.00", "250.00", "625.00",
            "Overtime Shift Differential Expense", "Accounts Payable (A/P)",
        ]
        missing = [k for k in keywords if k not in body]
        ok = not missing
        check(label, 2, ok,
              "all body keywords present" if ok
              else f"body missing={missing}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_gamma_shift()
    check_2_sigma_shift()
    check_3_theta_shift()
    check_4_gamma_bulk_assignment()
    check_5_sigma_individual_assignments()
    check_6_theta_individual_assignments()
    check_7_shift_request_approved()
    check_8_overtime_type()
    check_9_ot_slip_suresh()
    check_10_ot_slip_rahul()
    check_11_expense_account()
    check_12_ap_account()
    check_13_journal_entry()
    check_14_review_task()
    check_15_payments_task()
    check_16_summary_note()

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
