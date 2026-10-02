"""
Verifier for Business-031-I3: Offboard Ananya Reddy with HR Separation, Payroll Settlement, and CRM Task Reassignment

Checks: 8 weighted checks (total 15pt) across hrms, bigcapital, twenty.
Strategy: docker exec (DB queries) for all three sites.

Required env vars:
  SERVER_HOSTNAME, HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import os
import re
import sys
import subprocess
from datetime import datetime, timedelta

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


def _find_hrms_frappe_db() -> str:
    """Discover the Frappe bench database name in HRMS MariaDB."""
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "root", "-phrms123456",
        "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE '\\_%' AND SCHEMA_NAME != 'information_schema' "
        "ORDER BY SCHEMA_NAME;",
    )
    if rc != 0 or not out.strip():
        return "_frappe_bench"
    # Pick the DB that has tabEmployee
    for db in out.strip().split("\n"):
        db = db.strip()
        rc2, out2, _ = docker_exec(
            HRMS_DB_CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-u", "root", "-phrms123456",
            "-D", db, "-N", "-B", "-e",
            "SHOW TABLES LIKE 'tabEmployee';",
        )
        if rc2 == 0 and out2.strip():
            return db
    return "_frappe_bench"


_hrms_db_cache: str | None = None


def hrms_sql(query: str) -> str:
    """Run a MariaDB query against the Frappe HRMS database."""
    global _hrms_db_cache
    if _hrms_db_cache is None:
        _hrms_db_cache = _find_hrms_frappe_db()
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "root", "-phrms123456",
        "-D", _hrms_db_cache,
        "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"hrms mysql error: {err.strip()}")
    return out.strip()


def _find_bigcapital_tenant_db() -> str:
    """Discover the BigCapital tenant database name."""
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "root", "-proot123",
        "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE 'bigcapital_tenant_%' ORDER BY SCHEMA_NAME LIMIT 1;",
    )
    if rc != 0 or not out.strip():
        return "bigcapital"
    return out.strip().split("\n")[0]


_bc_db_cache: str | None = None


def bigcapital_sql(query: str) -> str:
    """Run a MariaDB query against the BigCapital tenant database."""
    global _bc_db_cache
    if _bc_db_cache is None:
        _bc_db_cache = _find_bigcapital_tenant_db()
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "root", "-proot123",
        "-D", _bc_db_cache,
        "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error: {err.strip()}")
    return out.strip()


def twenty_sql(query: str) -> str:
    """Run a Postgres query against Twenty database (default schema)."""
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql error: {err.strip()}")
    return out.strip()


def get_twenty_workspace_schema() -> str:
    """Find the workspace schema in Twenty's Postgres."""
    result = twenty_sql(
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';"
    )
    if not result:
        raise RuntimeError("No workspace schema found in Twenty DB")
    return result.split("\n")[0].strip()


def _norm_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
    return " ".join(s.split())


def date_matches_tz(stored: str, target: str) -> bool:
    """True if a stored date/timestamp matches target date (YYYY-MM-DD).

    Pure dates (exactly 10 chars) must match exactly. Timestamp-shaped values
    match if the UTC date OR the local-timezone-shifted date equals target
    (UI stores midnight-local as a UTC instant). No bare substring fallback.
    Local offset is computed from the verifier host's timezone, not hardcoded.
    """
    s = stored.strip()
    if not s:
        return False
    if len(s) == 10:
        return s == target
    m = re.match(
        r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?"
        r"(?:\s*([+-])(\d{2}):?(\d{2})?)?",
        s,
    )
    if not m:
        return False
    y, mo, d, hh, mi = (int(m.group(i)) for i in range(1, 6))
    ss = int(m.group(6) or 0)
    naive = datetime(y, mo, d, hh, mi, ss)
    if m.group(7):
        sign = 1 if m.group(7) == "+" else -1
        off = timedelta(hours=int(m.group(8)), minutes=int(m.group(9) or 0)) * sign
    else:
        off = timedelta(0)
    utc_dt = naive - off
    local_off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (
        utc_dt.date().isoformat() == target
        or (utc_dt + local_off).date().isoformat() == target
    )


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_employee_separation() -> None:
    """Employee Separation exists for HR-EMP-00007 with date 2026-06-30, submitted."""
    try:
        row = hrms_sql(
            "SELECT name, boarding_status, docstatus FROM `tabEmployee Separation` "
            "WHERE employee = 'HR-EMP-00007' AND boarding_begins_on = '2026-06-30' LIMIT 1;"
        )
        if not row:
            check("1. Employee Separation exists", 1, False, "no record found for HR-EMP-00007 with date 2026-06-30")
            return
        parts = row.split("\t")
        boarding_status = parts[1] if len(parts) > 1 else ""
        docstatus = int(parts[2]) if len(parts) > 2 else -1
        check("1. Employee Separation exists", 1, docstatus == 1,
              f"name={parts[0]}, boarding_status={boarding_status}, docstatus={docstatus}")
    except Exception as e:
        check("1. Employee Separation exists", 1, False, f"exception: {e}")


def check_2_exit_activities() -> None:
    """Exactly three exit activities with correct names and assignees."""
    try:
        rows = hrms_sql(
            "SELECT a.activity_name, a.user "
            "FROM `tabEmployee Boarding Activity` a "
            "JOIN `tabEmployee Separation` s ON a.parent = s.name "
            "WHERE s.employee = 'HR-EMP-00007' AND s.boarding_begins_on = '2026-06-30' "
            "ORDER BY a.activity_name;"
        )
        if not rows:
            check("2. Exit activities (exactly 3, correct assignees)", 2, False, "no activities found")
            return

        activity_rows: list[tuple[str, str]] = []
        for line in rows.split("\n"):
            parts = line.strip().split("\t")
            if parts and parts[0].strip():
                activity_rows.append((parts[0].strip(), parts[1].strip() if len(parts) > 1 else ""))

        # The 'user' field stores email addresses (e.g. rajesh.kumar@...) not full names.
        # Match by checking if a lowercase dot-separated name fragment appears in the email.
        expected = {
            "Conduct exit interview": "pooja.malhotra",
            "Return company laptop": "rajesh.kumar",
            "Revoke system access": "rajesh.kumar",
        }

        issues = []
        if len(activity_rows) != 3:
            issues.append(f"expected exactly 3 activities, found {len(activity_rows)}")
        for act_name, assignee_fragment in expected.items():
            matched = [u for (n, u) in activity_rows if n == act_name]
            if not matched:
                issues.append(f"'{act_name}' missing")
            elif not any(assignee_fragment in u.lower() for u in matched):
                issues.append(f"'{act_name}' assigned to '{matched[0]}' not matching '{assignee_fragment}'")

        check("2. Exit activities (exactly 3, correct assignees)", 2, not issues,
              "all 3 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("2. Exit activities (exactly 3, correct assignees)", 2, False, f"exception: {e}")


def check_5_bigcapital_vendor() -> str | None:
    """Vendor with email ananya.reddy@gmail.com, display name and vendor name gated.

    Returns the CONTACTS.ID of the matching vendor (for check 7), or None.
    """
    label = "5. Vendor 'Ananya Reddy - Ex Employee'"
    try:
        rows = bigcapital_sql(
            "SELECT ID, COMPANY_NAME, FIRST_NAME, LAST_NAME, DISPLAY_NAME, EMAIL "
            "FROM CONTACTS "
            "WHERE CONTACT_SERVICE = 'vendor' AND EMAIL = 'ananya.reddy@gmail.com';"
        )
        if not rows:
            check(label, 1, False, "no vendor contact with email ananya.reddy@gmail.com")
            return None

        expected_name = "Ananya Reddy - Ex Employee"
        contact_id: str | None = None
        seen = []
        for line in rows.split("\n"):
            cols = line.split("\t")
            if len(cols) < 6:
                continue
            cid, company, first, last, display, email = (c.strip() for c in cols[:6])
            # mysql -N -B renders SQL NULL as the literal string "NULL"
            company = "" if company == "NULL" else company
            first = "" if first == "NULL" else first
            last = "" if last == "NULL" else last
            display = "" if display == "NULL" else display
            seen.append(f"display={display!r}, company={company!r}")
            full_name = _norm_ws(f"{first} {last}")
            name_ok = (
                _norm_ws(company) == expected_name or full_name == expected_name
            )
            display_ok = _norm_ws(display) == "Ananya Reddy"
            if name_ok and display_ok:
                contact_id = cid
                break

        if contact_id is not None:
            check(label, 1, True, f"contact id={contact_id}")
        else:
            check(label, 1, False,
                  f"no row with display_name='Ananya Reddy' and name='{expected_name}'; got {'; '.join(seen)}")
        return contact_id
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return None


def check_6_journal_entry() -> None:
    """Published journal dated 2026-06-30, correct memo, exactly 3 lines, GL posted."""
    label = "6. Journal entry (settlement, 3 lines, published, GL posted)"
    try:
        journal_row = bigcapital_sql(
            "SELECT ID, DATE, DESCRIPTION, PUBLISHED_AT FROM MANUAL_JOURNALS "
            "WHERE DESCRIPTION LIKE '%Final settlement%Ananya Reddy%2026-06-30%' "
            "AND DATE = '2026-06-30' LIMIT 1;"
        )
        if not journal_row:
            check(label, 3, False, "journal not found with matching memo/date")
            return
        parts = journal_row.split("\t")
        journal_id = parts[0].strip()
        published = parts[3].strip() if len(parts) > 3 else ""

        entries = bigcapital_sql(
            f"SELECT a.NAME, e.CREDIT, e.DEBIT "
            f"FROM MANUAL_JOURNALS_ENTRIES e "
            f"JOIN ACCOUNTS a ON e.ACCOUNT_ID = a.ID "
            f"WHERE e.MANUAL_JOURNAL_ID = {journal_id} "
            f"ORDER BY e.DEBIT DESC;"
        )
        if not entries:
            check(label, 3, False, "no journal entries found")
            return

        entry_lines = [ln for ln in (l.strip() for l in entries.split("\n")) if ln]
        debits = {}
        credits = {}
        for line in entry_lines:
            cols = line.split("\t")
            if len(cols) < 3:
                continue
            acct = cols[0].strip()
            cr = float(cols[1]) if cols[1].strip() and cols[1].strip() != "NULL" else 0.0
            dr = float(cols[2]) if cols[2].strip() and cols[2].strip() != "NULL" else 0.0
            if dr > 0:
                debits[acct] = dr
            if cr > 0:
                credits[acct] = cr

        issues = []
        if len(entry_lines) != 3:
            issues.append(f"expected exactly 3 entry lines, got {len(entry_lines)}")
        rent_dr = debits.get("Rent", 0)
        if abs(rent_dr - 57950.0) > 0.01:
            issues.append(f"Rent debit expected 57950, got {rent_dr}")
        adv_dr = debits.get("Advertising Expense", 0)
        if abs(adv_dr - 29000.0) > 0.01:
            issues.append(f"Advertising Expense debit expected 29000, got {adv_dr}")
        obl_cr = credits.get("Opening Balance Liabilities", 0)
        if abs(obl_cr - 86950.0) > 0.01:
            issues.append(f"Opening Balance Liabilities credit expected 86950, got {obl_cr}")
        # PUBLISHED_AT is a DATE column: published <=> IS NOT NULL
        # (mysql -N -B renders SQL NULL as the literal string "NULL")
        if published in ("", "NULL"):
            issues.append(f"journal not published (PUBLISHED_AT={published or 'empty'})")

        # GL posting (task step 9): Rent debit 57950 dated 2026-06-30 for this journal
        gl_rows = bigcapital_sql(
            f"SELECT t.DEBIT, t.DATE FROM ACCOUNTS_TRANSACTIONS t "
            f"JOIN ACCOUNTS a ON t.ACCOUNT_ID = a.ID "
            f"WHERE t.REFERENCE_TYPE = 'Journal' AND t.REFERENCE_ID = {journal_id} "
            f"AND a.NAME = 'Rent';"
        )
        gl_ok = False
        gl_seen = []
        for line in (gl_rows.split("\n") if gl_rows else []):
            cols = line.split("\t")
            if len(cols) < 2:
                continue
            dr_s, dt_s = cols[0].strip(), cols[1].strip()
            gl_seen.append(f"debit={dr_s}, date={dt_s}")
            dr = float(dr_s) if dr_s and dr_s != "NULL" else 0.0
            if abs(dr - 57950.0) < 0.01 and dt_s == "2026-06-30":
                gl_ok = True
                break
        if not gl_ok:
            issues.append(
                "GL not posted: no ACCOUNTS_TRANSACTIONS row (Journal ref) with Rent debit 57950 dated 2026-06-30"
                + (f"; got {'; '.join(gl_seen)}" if gl_seen else "")
            )

        check(label, 3, not issues, "correct" if not issues else "; ".join(issues))
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_7_payment_made(vendor_contact_id: str | None) -> None:
    """Payment Made of 86950 on 2026-07-05 to the check-5 vendor, from
    'Sales of Product Income', with settlement reference."""
    label = "7. Payment Made (86950 to vendor, account & reference)"
    try:
        if not vendor_contact_id:
            check(label, 2, False, "vendor contact not resolved (check 5 failed)")
            return
        ref_like = "'%Final settlement%Ananya Reddy%2026-06-30%'"
        rows = bigcapital_sql(
            "SELECT bp.AMOUNT, bp.PAYMENT_DATE, a.NAME, "
            f"(COALESCE(bp.REFERENCE,'') LIKE {ref_like} "
            f"OR COALESCE(bp.STATEMENT,'') LIKE {ref_like}) AS REF_OK "
            "FROM BILLS_PAYMENTS bp "
            "LEFT JOIN ACCOUNTS a ON bp.PAYMENT_ACCOUNT_ID = a.ID "
            f"WHERE bp.VENDOR_ID = {vendor_contact_id};"
        )
        if not rows:
            check(label, 2, False, "no payment found for the check-5 vendor")
            return

        best_issues: list[str] | None = None
        for line in rows.split("\n"):
            cols = line.split("\t")
            if len(cols) < 4:
                continue
            amt_s, pdate, account, ref_ok_s = (c.strip() for c in cols[:4])
            amount = float(amt_s) if amt_s and amt_s != "NULL" else 0.0
            account = "" if account == "NULL" else account
            issues = []
            if abs(amount - 86950.0) > 0.01:
                issues.append(f"amount expected 86950, got {amount}")
            if pdate != "2026-07-05":
                issues.append(f"payment date expected 2026-07-05, got {pdate}")
            if account != "Sales of Product Income":
                issues.append(f"account expected 'Sales of Product Income', got '{account}'")
            if ref_ok_s != "1":
                issues.append("reference/statement missing 'Final settlement — Ananya Reddy — 2026-06-30'")
            if not issues:
                best_issues = []
                break
            if best_issues is None or len(issues) < len(best_issues):
                best_issues = issues

        ok = best_issues == []
        check(label, 2, ok, "correct" if ok else "; ".join(best_issues or ["no usable payment row"]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_twenty_tasks_titles() -> None:
    """3 tasks with correct titles, each linked to company MetricStream."""
    label = "8. Twenty tasks (3 titles, linked to MetricStream)"
    try:
        ws = get_twenty_workspace_schema()
        expected_titles = [
            "Schedule MetricStream compliance review meeting",
            "Update MetricStream primary contact details",
            "Follow up on MetricStream contract renewal",
        ]

        missing = []
        for title in expected_titles:
            safe_title = title.replace("'", "''")
            cnt = twenty_sql(
                f"SELECT count(*) FROM \"{ws}\".task t "
                f"JOIN \"{ws}\".\"taskTarget\" tt ON tt.\"taskId\" = t.id AND tt.\"deletedAt\" IS NULL "
                f"JOIN \"{ws}\".company c ON c.id = tt.\"targetCompanyId\" AND c.\"deletedAt\" IS NULL "
                f"WHERE t.\"deletedAt\" IS NULL AND c.name = 'MetricStream' "
                f"AND t.title = '{safe_title}';"
            )
            try:
                n = int(cnt.split("\n")[0].strip()) if cnt else 0
            except ValueError:
                n = 0
            if n < 1:
                missing.append(title)

        check(label, 2, not missing,
              "all 3 found and linked" if not missing else f"not linked to MetricStream or missing: {missing}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_twenty_tasks_details() -> None:
    """Tasks have due date 2026-07-20 (tz-aware) and exact body text."""
    label = "9. Twenty tasks (due date & body)"
    try:
        ws = get_twenty_workspace_schema()
        expected_body = _norm_ws(
            "Reassigned from Ananya Reddy (separated 2026-06-30). "
            "Original responsibility transferred — review and update client contacts."
        )
        expected_titles = [
            "Schedule MetricStream compliance review meeting",
            "Update MetricStream primary contact details",
            "Follow up on MetricStream contract renewal",
        ]
        issues = []
        for title in expected_titles:
            safe_title = title.replace("'", "''")
            due = twenty_sql(
                f"SELECT t.\"dueAt\"::text FROM \"{ws}\".task t "
                f"WHERE t.\"deletedAt\" IS NULL AND t.title = '{safe_title}' LIMIT 1;"
            )
            body = twenty_sql(
                f"SELECT regexp_replace(COALESCE(t.\"bodyV2Markdown\", ''), E'[\\n\\r]+', ' ', 'g') "
                f"FROM \"{ws}\".task t "
                f"WHERE t.\"deletedAt\" IS NULL AND t.title = '{safe_title}' LIMIT 1;"
            )
            if not due and not body:
                issues.append(f"'{title}' not found")
                continue
            due = due.split("\n")[0].strip() if due else ""
            if not date_matches_tz(due, "2026-07-20"):
                issues.append(f"'{title}' due date={due or 'empty'}, expected 2026-07-20 (tz-aware)")
            if expected_body not in _norm_ws(body):
                issues.append(f"'{title}' body mismatch")

        check(label, 2, not issues, "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_10_twenty_note() -> None:
    """Note with exact title and exact body."""
    label = "10. Twenty note (separation summary)"
    try:
        ws = get_twenty_workspace_schema()
        expected_title = "Employee Separation Complete — Ananya Reddy"
        expected_body = _norm_ws(
            "Separation date: 2026-06-30. Final settlement: 86,950.00 "
            "(salary: 57,950.00, leave encashment: 29,000.00). "
            "Payment processed 2026-07-05 from Sales of Product Income. "
            "3 client tasks reassigned to company MetricStream."
        )
        safe_title = expected_title.replace("'", "''")

        title_row = twenty_sql(
            f"SELECT n.title FROM \"{ws}\".note n "
            f"WHERE n.\"deletedAt\" IS NULL AND n.title = '{safe_title}' LIMIT 1;"
        )
        if not title_row:
            check(label, 2, False, f"note with exact title '{expected_title}' not found")
            return

        body = twenty_sql(
            f"SELECT regexp_replace(COALESCE(n.\"bodyV2Markdown\", ''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM \"{ws}\".note n "
            f"WHERE n.\"deletedAt\" IS NULL AND n.title = '{safe_title}' LIMIT 1;"
        )
        body_ok = expected_body in _norm_ws(body)
        check(label, 2, body_ok, "correct" if body_ok else "body mismatch")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_employee_separation()
    check_2_exit_activities()
    vendor_contact_id = check_5_bigcapital_vendor()
    check_6_journal_entry()
    check_7_payment_made(vendor_contact_id)
    check_8_twenty_tasks_titles()
    check_9_twenty_tasks_details()
    check_10_twenty_note()

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
