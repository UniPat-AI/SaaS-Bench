#!/usr/bin/env python3
"""
Verifier for Business-023-I1: Process Expense Reimbursement for Mohammed Farooq
Across HRMS, BigCapital, and Twenty CRM.

Checks: 13 checks (20 points total; check 2 is a 0pt seed-state precondition)
across hrms, bigcapital, twenty.
Strategy: docker exec MariaDB for HRMS and BigCapital, docker exec Postgres for Twenty.

Required env vars:
  SERVER_HOSTNAME, HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import os
import re
import sys
import subprocess
import json
from datetime import datetime, timedelta


# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.environ.get("HRMS_DB_CONTAINER")

BC_PORT = os.environ.get("BIGCAPITAL_PORT")
BC_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BC_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")

_required = {
    "HRMS_PORT": HRMS_PORT, "HRMS_CONTAINER": HRMS_CONTAINER,
    "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "BIGCAPITAL_PORT": BC_PORT, "BIGCAPITAL_CONTAINER": BC_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BC_DB_CONTAINER,
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


# NOTE: mysql -N -B prints SQL NULL as the literal string "NULL".
def _is_null(field: str) -> bool:
    return field is None or field.strip() == "" or field.strip() == "NULL"


def _norm_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
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

    Semantics: dueAt::date == D  OR  (dueAt + local_utc_offset)::date == D.
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


# ── HRMS DB helpers ───────────────────────────────────────────────────────────
_hrms_db_name: str = ""
_hrms_db_password: str = ""


def _discover_hrms_db() -> None:
    """Read Frappe site_config.json to get DB name and password."""
    global _hrms_db_name, _hrms_db_password
    # Get default site name
    rc, out, err = docker_exec(
        HRMS_CONTAINER, "cat",
        "/home/frappe/frappe-bench/sites/common_site_config.json",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot read common_site_config: {err.strip()}")
    common = json.loads(out)
    site = common.get("default_site", "hrms.localhost")
    # Get site-specific config
    rc, out, err = docker_exec(
        HRMS_CONTAINER, "cat",
        f"/home/frappe/frappe-bench/sites/{site}/site_config.json",
    )
    if rc != 0:
        raise RuntimeError(f"Cannot read site_config for {site}: {err.strip()}")
    site_cfg = json.loads(out)
    _hrms_db_name = site_cfg["db_name"]
    _hrms_db_password = site_cfg.get("db_password", "")


def hrms_query(sql: str) -> str:
    """Run a MariaDB query on the HRMS database. Raises on mysql error."""
    if not _hrms_db_name:
        _discover_hrms_db()
    # Use the site-specific DB user (same as db_name in Frappe) with its password
    args = [
        "mysql", "--default-character-set=utf8mb4",
        "-u", _hrms_db_name,
    ]
    if _hrms_db_password:
        args.append(f"-p{_hrms_db_password}")
    args += ["-D", _hrms_db_name, "-N", "-B", "-e", sql]
    rc, out, err = docker_exec(HRMS_DB_CONTAINER, *args)
    if rc != 0:
        raise RuntimeError(f"mysql error: {err.strip()}")
    return out.strip()


# ── BigCapital DB helpers ─────────────────────────────────────────────────────
_bc_tenant_db: str = ""


def _discover_bc_tenant_db() -> None:
    """Find BigCapital's tenant database name."""
    global _bc_tenant_db
    rc, out, err = docker_exec(
        BC_CONTAINER, "mysql",
        "-u", "bigcapital", "-pbigcapital123",
        "-N", "-B", "-e", "SHOW DATABASES LIKE 'bigcapital_tenant_%';",
    )
    if rc != 0:
        # Fall back: try the separate DB container
        rc, out, err = docker_exec(
            BC_DB_CONTAINER, "mysql",
            "-u", "bigcapital", "-pbigcapital123",
            "-N", "-B", "-e", "SHOW DATABASES LIKE 'bigcapital_tenant_%';",
        )
        if rc != 0:
            raise RuntimeError(f"Cannot list BigCapital DBs: {err.strip()}")
    dbs = [d.strip() for d in out.strip().split("\n") if d.strip()]
    if not dbs:
        raise RuntimeError("No BigCapital tenant database found")
    _bc_tenant_db = dbs[0]


def bc_query(sql: str) -> str:
    """Run a MariaDB query on BigCapital's tenant database. Raises on error."""
    if not _bc_tenant_db:
        _discover_bc_tenant_db()
    # Try embedded DB first (BC_CONTAINER), then separate (BC_DB_CONTAINER)
    err = ""
    for container in (BC_CONTAINER, BC_DB_CONTAINER):
        rc, out, err = docker_exec(
            container, "mysql",
            "-u", "bigcapital", "-pbigcapital123",
            "-D", _bc_tenant_db,
            "-N", "-B", "-e", sql,
        )
        if rc == 0:
            return out.strip()
    raise RuntimeError(f"BigCapital mysql error: {err.strip()}")


# ── Twenty DB helpers ─────────────────────────────────────────────────────────
_twenty_schema: str = ""


def twenty_psql(sql: str) -> str:
    """Run a Postgres query on the Twenty database. Raises on psql error."""
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    return out.strip()


def get_twenty_schema() -> str:
    global _twenty_schema
    if _twenty_schema:
        return _twenty_schema
    result = twenty_psql(
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
    _twenty_schema = result.split("\n")[0].strip()
    return _twenty_schema


def twenty_ws(sql: str) -> str:
    """Run a query in the Twenty workspace schema. Raises on psql error."""
    schema = get_twenty_schema()
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default",
        "-t", "-A",
        "-c", f'SET search_path TO "{schema}";',
        "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"psql error: {err.strip()}")
    # Filter out the "SET" acknowledgment line from the first -c command
    lines = out.strip().split("\n")
    filtered = [ln for ln in lines if ln.strip() != "SET"]
    return "\n".join(filtered).strip()


# ── Expected values (from task description) ──────────────────────────────────
CLAIM_NAME = "HR-EXP-2026-00006"
CLAIM_TOTAL = 10350.0
EXPECTED_LINES = {"Travel": 8500.0, "Food": 1500.0, "Calls": 350.0}
VENDOR_NAME = "Mohammed Farooq Reimbursement"
VENDOR_EMAIL = "mohammed.farooq@gmail.com"
BILL_DATE = "2026-03-20"
PAYMENT_DATE = "2026-04-05"
PAYMENT_ACCOUNT = "Bank Account"
TASK_TITLE = "Expense reimbursement processed — Mohammed Farooq"
TASK_DUE_DATE = "2026-04-05"
TASK_BODY_SENTENCE = (
    "Expense claim HR-EXP-2026-00006 approved and paid. "
    "Total: ₹10,350.00. "
    "Items: Travel (₹8,500.00), Food (₹1,500.00), Calls (₹350.00). "
    "Payment made from Bank Account on 2026-04-05."
)


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_hrms_claim_approved() -> None:
    """Claim is Approved AND submitted (docstatus=1) with correct total 10350."""
    try:
        result = hrms_query(
            "SELECT approval_status, docstatus, total_claimed_amount "
            f"FROM `tabExpense Claim` WHERE name='{CLAIM_NAME}';"
        )
        if not result:
            check("1. HRMS claim approved", 2, False, "claim not found")
            return
        parts = result.split("\t")
        status = parts[0].strip()
        docstatus = parts[1].strip() if len(parts) > 1 else ""
        amount = float(parts[2]) if len(parts) > 2 and not _is_null(parts[2]) else 0.0
        ok = (
            status == "Approved"
            and docstatus == "1"
            and abs(amount - CLAIM_TOTAL) < 0.01
        )
        check("1. HRMS claim approved", 2, ok,
              f"status={status}, docstatus={docstatus}, total={amount}")
    except Exception as e:
        check("1. HRMS claim approved", 2, False, f"exception: {e}")


def check_2_hrms_claim_line_items() -> None:
    """[0pt precondition] 3 seeded line items: Travel 8500, Food 1500, Calls 350.

    Pristine environments already satisfy this (seed data), so it carries no
    weight; it is kept only as a diagnostic for downstream failures.
    """
    try:
        result = hrms_query(
            "SELECT expense_type, amount FROM `tabExpense Claim Detail` "
            f"WHERE parent='{CLAIM_NAME}' ORDER BY idx;"
        )
        if not result:
            check("2. HRMS claim line items (precondition)", 0, False,
                  "no line items")
            return
        found: dict[str, float] = {}
        for line in result.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2 and not _is_null(parts[1]):
                found[parts[0].strip()] = float(parts[1])
        ok = len(found) == 3 and all(
            abs(found.get(k, -1) - v) < 0.01 for k, v in EXPECTED_LINES.items()
        )
        check("2. HRMS claim line items (precondition)", 0, ok, f"found={found}")
    except Exception as e:
        check("2. HRMS claim line items (precondition)", 0, False, f"exception: {e}")


def check_2b_hrms_claim_unpaid_report_state() -> None:
    """Claim is in the Unpaid Expense Claim report state.

    Mirrors hrms unpaid_expense_claim report logic: docstatus=1 AND is_paid=0,
    with sanctioned amount 10350 and nothing reimbursed.
    """
    try:
        result = hrms_query(
            "SELECT docstatus, is_paid, total_sanctioned_amount, "
            "total_amount_reimbursed "
            f"FROM `tabExpense Claim` WHERE name='{CLAIM_NAME}';"
        )
        if not result:
            check("2b. HRMS claim unpaid report state", 1, False, "claim not found")
            return
        parts = result.split("\t")
        docstatus = parts[0].strip()
        is_paid = parts[1].strip() if len(parts) > 1 else ""
        sanctioned = (
            float(parts[2]) if len(parts) > 2 and not _is_null(parts[2]) else -1.0
        )
        reimbursed = (
            float(parts[3]) if len(parts) > 3 and not _is_null(parts[3]) else -1.0
        )
        ok = (
            docstatus == "1"
            and is_paid == "0"
            and abs(sanctioned - CLAIM_TOTAL) < 0.01
            and abs(reimbursed) < 0.01
        )
        check("2b. HRMS claim unpaid report state", 1, ok,
              f"docstatus={docstatus}, is_paid={is_paid}, "
              f"sanctioned={sanctioned}, reimbursed={reimbursed}")
    except Exception as e:
        check("2b. HRMS claim unpaid report state", 1, False, f"exception: {e}")


def check_3_bc_vendor_exists() -> None:
    """Exactly one vendor 'Mohammed Farooq Reimbursement' with the exact email."""
    try:
        result = bc_query(
            "SELECT EMAIL FROM CONTACTS "
            f"WHERE DISPLAY_NAME = '{VENDOR_NAME}' "
            "AND CONTACT_SERVICE = 'vendor';"
        )
        rows = [r for r in result.split("\n") if r.strip()] if result else []
        if len(rows) != 1:
            check("3. BC vendor exists", 1, False,
                  f"expected exactly 1 vendor row, got {len(rows)}")
            return
        email = rows[0].strip()
        ok = email == VENDOR_EMAIL
        check("3. BC vendor exists", 1, ok, f"email={email}")
    except Exception as e:
        check("3. BC vendor exists", 1, False, f"exception: {e}")


def check_4_bc_items_exist() -> None:
    """Active items 'Travel', 'Food', 'Calls' exist."""
    try:
        result = bc_query(
            "SELECT NAME FROM ITEMS "
            "WHERE NAME IN ('Travel', 'Food', 'Calls') AND ACTIVE = 1;"
        )
        found = {r.strip() for r in result.split("\n") if r.strip()} if result else set()
        required = {"Travel", "Food", "Calls"}
        missing = required - found
        check("4. BC items exist", 1, not missing,
              f"found={found}" if not missing else f"missing={missing}")
    except Exception as e:
        check("4. BC items exist", 1, False, f"exception: {e}")


def check_5_bc_bill_exists() -> tuple[str | None, str | None]:
    """Exactly one bill for the vendor, dated 2026-03-20, amount 10350.

    Returns (bill_id, opened_at) for reuse by checks 5b/6/7/8; bill_id is None
    when the vendor has no unique bill (downstream checks then FAIL cleanly).
    """
    label = "5. BC bill exists"
    try:
        result = bc_query(
            "SELECT b.ID, b.BILL_DATE, b.AMOUNT, b.OPENED_AT "
            "FROM BILLS b "
            "JOIN CONTACTS c ON b.VENDOR_ID = c.ID "
            f"WHERE c.DISPLAY_NAME = '{VENDOR_NAME}' "
            "AND c.CONTACT_SERVICE = 'vendor';"
        )
        rows = [r for r in result.split("\n") if r.strip()] if result else []
        if len(rows) != 1:
            check(label, 2, False,
                  f"expected exactly 1 bill for vendor, got {len(rows)}")
            return None, None
        parts = rows[0].split("\t")
        bill_id = parts[0].strip()
        bill_date = parts[1].strip().split(" ")[0] if len(parts) > 1 else ""
        amount = float(parts[2]) if len(parts) > 2 and not _is_null(parts[2]) else 0.0
        opened_at = parts[3].strip() if len(parts) > 3 else ""
        ok = bill_date == BILL_DATE and abs(amount - CLAIM_TOTAL) < 0.01
        check(label, 2, ok,
              f"id={bill_id}, date={bill_date}, amount={amount}, "
              f"opened_at={opened_at}")
        return bill_id, opened_at
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return None, None


def check_5b_bc_bill_opened(bill_id: str | None, opened_at: str | None) -> None:
    """Bill has been approved (opened): OPENED_AT is set."""
    label = "5b. BC bill opened"
    if bill_id is None:
        check(label, 1, False, "bill not found (see check 5)")
        return
    ok = not _is_null(opened_at or "")
    check(label, 1, ok, f"opened_at={opened_at}")


def check_6_bc_bill_line_items(bill_id: str | None) -> None:
    """Bill has exactly 3 entries: Travel 8500, Food 1500, Calls 350."""
    label = "6. BC bill line items"
    if bill_id is None:
        check(label, 2, False, "bill not found (see check 5)")
        return
    try:
        result = bc_query(
            "SELECT i.NAME, ie.RATE, ie.QUANTITY "
            "FROM ITEMS_ENTRIES ie "
            "JOIN ITEMS i ON ie.ITEM_ID = i.ID "
            f"WHERE ie.REFERENCE_TYPE = 'Bill' AND ie.REFERENCE_ID = {bill_id};"
        )
        rows = [r for r in result.split("\n") if r.strip()] if result else []
        if not rows:
            check(label, 2, False, "no line items found")
            return
        found: dict[str, float] = {}
        for line in rows:
            parts = line.split("\t")
            if len(parts) >= 3 and not _is_null(parts[1]) and not _is_null(parts[2]):
                found[parts[0].strip()] = float(parts[1]) * float(parts[2])
        ok = (
            len(rows) == 3
            and len(found) == 3
            and all(
                abs(found.get(k, -1) - v) < 0.01 for k, v in EXPECTED_LINES.items()
            )
        )
        check(label, 2, ok, f"rows={len(rows)}, found={found}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_bc_payment_recorded(bill_id: str | None) -> None:
    """Payment of 10350 from 'Bank Account' dated 2026-04-05, bound to the bill
    via BILLS_PAYMENTS_ENTRIES (all conditions on the same row)."""
    label = "7. BC payment recorded"
    if bill_id is None:
        check(label, 2, False, "bill not found (see check 5)")
        return
    try:
        result = bc_query(
            "SELECT bp.AMOUNT, bp.PAYMENT_DATE, a.NAME, bpe.PAYMENT_AMOUNT "
            "FROM BILLS_PAYMENTS bp "
            "JOIN BILLS_PAYMENTS_ENTRIES bpe ON bpe.BILL_PAYMENT_ID = bp.ID "
            "JOIN ACCOUNTS a ON bp.PAYMENT_ACCOUNT_ID = a.ID "
            f"WHERE bpe.BILL_ID = {bill_id};"
        )
        rows = [r for r in result.split("\n") if r.strip()] if result else []
        if not rows:
            check(label, 2, False, "no payment bound to the bill")
            return
        ok = False
        details = []
        for line in rows:
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            amount = float(parts[0]) if not _is_null(parts[0]) else -1.0
            pay_date = parts[1].strip().split(" ")[0]
            acct = parts[2].strip()
            entry_amount = float(parts[3]) if not _is_null(parts[3]) else -1.0
            details.append(
                f"amount={amount}, date={pay_date}, account={acct}, "
                f"entry_amount={entry_amount}"
            )
            if (
                abs(amount - CLAIM_TOTAL) < 0.01
                and pay_date == PAYMENT_DATE
                and acct == PAYMENT_ACCOUNT
                and abs(entry_amount - CLAIM_TOTAL) < 0.01
            ):
                ok = True
        check(label, 2, ok, "; ".join(details)[:200])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_bc_bill_fully_paid(bill_id: str | None) -> None:
    """The target bill is fully paid: PAYMENT_AMOUNT equals AMOUNT (zero due).

    DB proxy for the A/P Aging zero-balance verification (task step 8).
    """
    label = "8. BC bill fully paid"
    if bill_id is None:
        check(label, 3, False, "bill not found (see check 5)")
        return
    try:
        result = bc_query(
            f"SELECT AMOUNT, PAYMENT_AMOUNT FROM BILLS WHERE ID = {bill_id};"
        )
        if not result:
            check(label, 3, False, "bill row not found")
            return
        parts = result.split("\t")
        if len(parts) < 2 or _is_null(parts[0]) or _is_null(parts[1]):
            check(label, 3, False, f"amount/payment_amount NULL: {result[:80]}")
            return
        amount = float(parts[0])
        paid = float(parts[1])
        due = amount - paid
        ok = abs(due) < 0.01
        check(label, 3, ok, f"amount={amount}, paid={paid}, due={due:.2f}")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_9_twenty_task_exists() -> None:
    """Task with the exact title 'Expense reimbursement processed — Mohammed
    Farooq' exists (not deleted)."""
    try:
        result = twenty_ws(
            "SELECT id FROM task "
            'WHERE "deletedAt" IS NULL '
            f"AND title = '{TASK_TITLE}' "
            "LIMIT 1;"
        )
        ok = bool(result.strip())
        check("9. Twenty task exists", 1, ok,
              f"id={result.strip()[:60]}" if ok else "task not found")
    except Exception as e:
        check("9. Twenty task exists", 1, False, f"exception: {e}")


def check_10_twenty_task_completed() -> None:
    """Task is completed (DONE) with due date 2026-04-05 (timezone-aware)."""
    label = "10. Twenty task completed + due date"
    try:
        status = twenty_ws(
            "SELECT status FROM task "
            'WHERE "deletedAt" IS NULL '
            f"AND title = '{TASK_TITLE}' "
            "LIMIT 1;"
        ).strip()
        if not status:
            check(label, 2, False, "task not found")
            return
        due_at = twenty_ws(
            'SELECT "dueAt"::text FROM task '
            'WHERE "deletedAt" IS NULL '
            f"AND title = '{TASK_TITLE}' "
            "LIMIT 1;"
        ).strip()
        ok = status == "DONE" and date_matches_tz(due_at, TASK_DUE_DATE)
        check(label, 2, ok, f"status={status}, dueAt={due_at}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_twenty_task_body() -> None:
    """Task body contains the full required sentence from the task description."""
    label = "11. Twenty task body"
    try:
        result = twenty_ws(
            "SELECT regexp_replace("
            "COALESCE(\"bodyV2Markdown\", \"bodyV2Blocknote\"::text, ''), "
            "E'[\\n\\r]+', ' ', 'g') FROM task "
            'WHERE "deletedAt" IS NULL '
            f"AND title = '{TASK_TITLE}' "
            "LIMIT 1;"
        )
        body = _norm_ws(result)
        if not body:
            check(label, 2, False, "task not found or body empty")
            return
        expected = _norm_ws(TASK_BODY_SENTENCE)
        ok = expected in body
        check(label, 2, ok,
              "full sentence present" if ok
              else f"required sentence not found in body: {body[:160]}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_hrms_claim_approved()
    check_2_hrms_claim_line_items()
    check_2b_hrms_claim_unpaid_report_state()
    check_3_bc_vendor_exists()
    check_4_bc_items_exist()
    bill_id, opened_at = check_5_bc_bill_exists()
    check_5b_bc_bill_opened(bill_id, opened_at)
    check_6_bc_bill_line_items(bill_id)
    check_7_bc_payment_recorded(bill_id)
    check_8_bc_bill_fully_paid(bill_id)
    check_9_twenty_task_exists()
    check_10_twenty_task_completed()
    check_11_twenty_task_body()

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
