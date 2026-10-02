#!/usr/bin/env python3
"""
Verifier for Business-032-I4: Milestone-Based Client Engagement — Arcturus Digital

Checks: 18 weighted checks across twenty, bigcapital, pretix.
Strategy: docker exec DB queries — Twenty (psql), BigCapital (mysql), Pretix (psql).

Required env vars:
  SERVER_HOSTNAME,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER.
"""

import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    return val


TWENTY_PORT = _require("TWENTY_PORT")
TWENTY_CONTAINER = _require("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = _require("TWENTY_DB_CONTAINER")
BIGCAPITAL_PORT = _require("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = _require("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = _require("BIGCAPITAL_DB_CONTAINER")
PRETIX_PORT = _require("PRETIX_PORT")
PRETIX_CONTAINER = _require("PRETIX_CONTAINER")
PRETIX_DB_CONTAINER = _require("PRETIX_DB_CONTAINER")


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


def twenty_sql(query: str) -> str:
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"Twenty SQL error: {err.strip()}")
    return out.strip()


# BigCapital: MySQL embedded in app container. Multi-tenant DB.
_bc_tenant_db: str = ""


def _init_bc_tenant_db() -> None:
    global _bc_tenant_db
    if _bc_tenant_db:
        return
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "-u", "root", "--password=",
        "--default-character-set=utf8mb4",
        "--batch", "--skip-column-names",
        "bigcapital", "-e", "SELECT ORGANIZATION_ID FROM TENANTS LIMIT 1;",
    )
    if rc != 0 or not out.strip():
        raise RuntimeError(f"Cannot find BC tenant DB: {err.strip()}")
    _bc_tenant_db = f"bigcapital_tenant_{out.strip()}"


def bigcapital_sql(query: str) -> str:
    _init_bc_tenant_db()
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", "-u", "root", "--password=",
        "--default-character-set=utf8mb4",
        "--batch", "--skip-column-names",
        _bc_tenant_db, "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"BigCapital SQL error: {err.strip()}")
    return out.strip()


def pretix_sql(query: str) -> str:
    rc, out, err = docker_exec(
        PRETIX_DB_CONTAINER,
        "psql", "-U", "pretix", "-d", "pretix",
        "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"Pretix SQL error: {err.strip()}")
    return out.strip()


def get_twenty_workspace_schema() -> str:
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


def _local_utc_offset() -> timedelta:
    """UTC offset of the verifier host's local timezone (deployment tz)."""
    return datetime.now().astimezone().utcoffset() or timedelta(0)


def date_matches_tz(stored: str, exp_date: str) -> bool:
    """True if a stored timestamp/date corresponds to calendar date exp_date.

    Twenty stores date fields as UTC timestamps; a local date D entered in the
    UI on a UTC+N deployment is stored as (D-1)T(24-N):00:00Z. Accept only
    stored::date == exp_date or (stored + local_utc_offset)::date == exp_date.
    A pure 10-char date must match exactly.
    """
    stored = (stored or "").strip()
    if len(stored) == 10:
        return stored == exp_date
    try:
        dt = datetime.strptime(stored[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    if dt.strftime("%Y-%m-%d") == exp_date:
        return True
    return (dt + _local_utc_offset()).strftime("%Y-%m-%d") == exp_date


# Cross-check IDs populated by earlier checks
_twenty_company_id: str = ""


# ── Twenty checks ────────────────────────────────────────────────────────────

def check_1_twenty_company() -> None:
    """Company 'Arcturus Digital' with domain arcturusdigital.com and 275 employees."""
    global _twenty_company_id
    try:
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f'SELECT id, name, "domainNamePrimaryLinkUrl", employees '
            f'FROM "{ws}".company '
            f"WHERE name = 'Arcturus Digital' AND \"deletedAt\" IS NULL "
            f"LIMIT 1;"
        )
        if not row:
            check("1. Twenty company Arcturus Digital", 2, False, "not found")
            return
        parts = row.split("|")
        cid = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        domain = parts[2].strip() if len(parts) > 2 else ""
        employees = parts[3].strip() if len(parts) > 3 else ""
        _twenty_company_id = cid
        ok = (
            name == "Arcturus Digital"
            and "arcturusdigital.com" in domain
            and str(employees) == "275"
        )
        check("1. Twenty company Arcturus Digital", 2, ok,
              f"domain={domain}, employees={employees}")
    except Exception as e:
        check("1. Twenty company Arcturus Digital", 2, False, f"exception: {e}")


def check_2_twenty_person() -> None:
    """Person 'Elena Vasquez' with email, title, phone, linked to Arcturus Digital."""
    try:
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f'SELECT p."nameFirstName", p."nameLastName", p."emailsPrimaryEmail", '
            f'p."jobTitle", p."phonesPrimaryPhoneNumber" '
            f'FROM "{ws}".person p '
            f'JOIN "{ws}".company c ON p."companyId" = c.id '
            f"AND c.name = 'Arcturus Digital' AND c.\"deletedAt\" IS NULL "
            f"WHERE p.\"emailsPrimaryEmail\" = 'elena.vasquez@arcturusdigital.com' "
            f"AND p.\"deletedAt\" IS NULL "
            f"LIMIT 1;"
        )
        if not row:
            check("2. Twenty person Elena Vasquez", 2, False,
                  "not found (or not linked to company Arcturus Digital)")
            return
        parts = row.split("|")
        first = parts[0].strip()
        last = parts[1].strip() if len(parts) > 1 else ""
        email = parts[2].strip() if len(parts) > 2 else ""
        title = parts[3].strip() if len(parts) > 3 else ""
        phone = parts[4].strip() if len(parts) > 4 else ""
        digits = re.sub(r"\D", "", phone)
        phone_ok = digits.endswith("15036214480") or digits == "5036214480"
        ok = (
            first == "Elena" and last == "Vasquez"
            and email == "elena.vasquez@arcturusdigital.com"
            and "Director of Engineering" in title
            and phone_ok
        )
        check("2. Twenty person Elena Vasquez", 2, ok,
              f"name={first} {last}, title={title}, phone={phone}, "
              f"phone_ok={phone_ok}, company_linked=yes")
    except Exception as e:
        check("2. Twenty person Elena Vasquez", 2, False, f"exception: {e}")


def check_3_twenty_opportunity() -> None:
    """Opportunity with amount 100000, stage Won, close date 2026-01-31, correct links."""
    try:
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f'SELECT o.name, o."amountAmountMicros", o.stage, o."closeDate"::text '
            f'FROM "{ws}".opportunity o '
            f'JOIN "{ws}".company c ON o."companyId" = c.id '
            f"AND c.name = 'Arcturus Digital' AND c.\"deletedAt\" IS NULL "
            f'JOIN "{ws}".person p ON o."pointOfContactId" = p.id '
            f"AND p.\"emailsPrimaryEmail\" = 'elena.vasquez@arcturusdigital.com' "
            f"AND p.\"deletedAt\" IS NULL "
            f"WHERE o.name LIKE '%Arcturus Digital%Enterprise Software Modernization%' "
            f"AND o.\"deletedAt\" IS NULL "
            f"LIMIT 1;"
        )
        if not row:
            check("3. Twenty opportunity", 2, False,
                  "not found (or not linked to company + point of contact)")
            return
        parts = row.split("|")
        name = parts[0].strip()
        amount_raw = parts[1].strip() if len(parts) > 1 else "0"
        stage = parts[2].strip() if len(parts) > 2 else ""
        close_date = parts[3].strip() if len(parts) > 3 else ""

        try:
            amount = int(amount_raw) / 1_000_000
        except (ValueError, TypeError):
            amount = float(amount_raw or 0)
        amount_ok = abs(amount - 100000.0) < 1.0
        stage_ok = stage.upper() == "WON"
        date_ok = date_matches_tz(close_date, "2026-01-31")

        ok = amount_ok and stage_ok and date_ok
        check("3. Twenty opportunity", 2, ok,
              f"amount={amount}, stage={stage}, closeDate={close_date}, "
              f"date_ok={date_ok}, links=ok")
    except Exception as e:
        check("3. Twenty opportunity", 2, False, f"exception: {e}")


def check_4_twenty_favorite() -> None:
    """Company 'Arcturus Digital' is marked as a favorite."""
    try:
        if not _twenty_company_id:
            check("4. Twenty company is favorite", 1, False, "company ID unknown from check 1")
            return
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f'SELECT id FROM "{ws}".favorite '
            f"WHERE \"companyId\" = '{_twenty_company_id}' "
            f"AND \"deletedAt\" IS NULL "
            f"LIMIT 1;"
        )
        ok = bool(row)
        check("4. Twenty company is favorite", 1, ok,
              "found" if ok else "no favorite record")
    except Exception as e:
        check("4. Twenty company is favorite", 1, False, f"exception: {e}")


def _twenty_task_on_opportunity(ws: str, title_like: str) -> str:
    """Fetch a task by title, required to be linked to the Arcturus opportunity."""
    return twenty_sql(
        f'SELECT t.title, t."dueAt"::text, '
        f"regexp_replace(t.\"bodyV2Markdown\", E'[\\n\\r]+', ' ', 'g') "
        f'FROM "{ws}".task t '
        f'JOIN "{ws}"."taskTarget" tt ON tt."taskId" = t.id '
        f"AND tt.\"deletedAt\" IS NULL "
        f'JOIN "{ws}".opportunity o ON tt."targetOpportunityId" = o.id '
        f"AND o.name LIKE '%Arcturus Digital%Enterprise Software Modernization%' "
        f"AND o.\"deletedAt\" IS NULL "
        f"WHERE t.title LIKE '{title_like}' "
        f"AND t.\"deletedAt\" IS NULL "
        f"LIMIT 1;"
    )


def check_5_twenty_task_milestone2() -> None:
    """Task 'Milestone 2 collection — Arcturus Digital' with due 2026-02-04 and body keywords."""
    try:
        ws = get_twenty_workspace_schema()
        row = _twenty_task_on_opportunity(ws, "%Milestone 2 collection%Arcturus Digital%")
        if not row:
            check("5. Twenty task Milestone 2 collection", 2, False,
                  "not found (or not linked to the Arcturus opportunity)")
            return
        parts = row.split("|", 2)
        title = parts[0].strip()
        due_at = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""

        date_ok = date_matches_tz(due_at, "2026-02-04")
        kw = {
            "55000": "55000" in body,
            "ARCTURUS100VIP": "ARCTURUS100VIP" in body,
            "elena": "elena" in body.lower(),
            "2026-02-04": "2026-02-04" in body,
            "2026-03-10": "2026-03-10" in body,
            "Phase 2": "Phase 2" in body or "Development & Go-Live" in body,
        }
        body_ok = all(kw.values())
        missing = ",".join(k for k, v in kw.items() if not v) or "none"
        ok = date_ok and body_ok
        check("5. Twenty task Milestone 2 collection", 2, ok,
              f"dueAt={'ok' if date_ok else due_at}, body_missing={missing}")
    except Exception as e:
        check("5. Twenty task Milestone 2 collection", 2, False, f"exception: {e}")


def check_6_twenty_task_celebration() -> None:
    """Task 'Send celebration invite — Arcturus Digital' with due 2026-02-25 and body keywords."""
    try:
        ws = get_twenty_workspace_schema()
        row = _twenty_task_on_opportunity(ws, "%Send celebration invite%Arcturus Digital%")
        if not row:
            check("6. Twenty task Send celebration invite", 2, False,
                  "not found (or not linked to the Arcturus opportunity)")
            return
        parts = row.split("|", 2)
        title = parts[0].strip()
        due_at = parts[1].strip() if len(parts) > 1 else ""
        body = parts[2] if len(parts) > 2 else ""

        date_ok = date_matches_tz(due_at, "2026-02-25")
        bl = body.lower()
        kw = {
            "ARCTURUS100VIP": "ARCTURUS100VIP" in body,
            "elena": "elena" in bl,
            "100%": "100%" in body or "100 %" in body,
            "event_name": "Arcturus Digital Milestone Celebration" in body,
            "max 3": "max 3" in bl or "max usages 3" in bl,
        }
        body_ok = all(kw.values())
        missing = ",".join(k for k, v in kw.items() if not v) or "none"
        ok = date_ok and body_ok
        check("6. Twenty task Send celebration invite", 2, ok,
              f"dueAt={'ok' if date_ok else due_at}, body_missing={missing}")
    except Exception as e:
        check("6. Twenty task Send celebration invite", 2, False, f"exception: {e}")


# ── BigCapital checks (MySQL, UPPERCASE tables) ──────────────────────────────

def check_7_bc_customer() -> None:
    """Customer 'Arcturus Digital' with email elena.vasquez@arcturusdigital.com."""
    try:
        row = bigcapital_sql(
            "SELECT DISPLAY_NAME, IFNULL(EMAIL, '') "
            "FROM CONTACTS "
            "WHERE DISPLAY_NAME = 'Arcturus Digital' "
            "AND CONTACT_SERVICE = 'customer' "
            "LIMIT 1;"
        )
        if not row:
            check("7. BigCapital customer Arcturus Digital", 1, False, "not found")
            return
        parts = row.split("\t")
        name = parts[0].strip()
        email = parts[1].strip() if len(parts) > 1 else ""
        ok = name == "Arcturus Digital" and "elena.vasquez@arcturusdigital.com" in email
        check("7. BigCapital customer Arcturus Digital", 1, ok,
              f"name={name}, email={email}")
    except Exception as e:
        check("7. BigCapital customer Arcturus Digital", 1, False, f"exception: {e}")


def check_8_bc_items() -> None:
    """Items 'Phase 1' (45000) and 'Phase 2' (55000) exist as service type."""
    try:
        rows = bigcapital_sql(
            "SELECT NAME, TYPE, SELL_PRICE "
            "FROM ITEMS "
            "WHERE NAME LIKE 'Phase 1%' OR NAME LIKE 'Phase 2%' "
            "ORDER BY NAME;"
        )
        if not rows:
            check("8. BigCapital items Phase 1 & 2", 2, False, "no items found")
            return
        p1_ok = False
        p2_ok = False
        for line in rows.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            type_ok = parts[1].strip().lower() == "service"
            price_raw = parts[2].strip()
            # mysql -N -B renders SQL NULL as the literal string "NULL"
            price = float(price_raw) if price_raw not in ("", "NULL") else 0.0
            if "Requirements" in name and "Solution Design" in name:
                p1_ok = type_ok and abs(price - 45000.0) < 1.0
            elif "Development" in name and "Go-Live" in name:
                p2_ok = type_ok and abs(price - 55000.0) < 1.0

        ok = p1_ok and p2_ok
        check("8. BigCapital items Phase 1 & 2", 2, ok,
              f"Phase1={'ok' if p1_ok else 'FAIL'}, Phase2={'ok' if p2_ok else 'FAIL'}")
    except Exception as e:
        check("8. BigCapital items Phase 1 & 2", 2, False, f"exception: {e}")


def check_9_bc_account() -> None:
    """Account 'Deferred Revenue — Enterprise Engagements' as Other Current Liability."""
    try:
        row = bigcapital_sql(
            "SELECT NAME, ACCOUNT_TYPE "
            "FROM ACCOUNTS "
            "WHERE NAME LIKE '%Deferred Revenue%Enterprise Engagements%' "
            "LIMIT 1;"
        )
        if not row:
            check("9. BigCapital deferred revenue account", 1, False, "not found")
            return
        parts = row.split("\t")
        name = parts[0].strip()
        acct_type = parts[1].strip() if len(parts) > 1 else ""
        # BigCapital stores account types with hyphens (e.g. 'other-current-liability')
        normalised = acct_type.lower().replace(" ", "-").replace("_", "-")
        ok = "Deferred Revenue" in name and "other-current-liability" in normalised
        check("9. BigCapital deferred revenue account", 1, ok,
              f"name={name}, type={acct_type}")
    except Exception as e:
        check("9. BigCapital deferred revenue account", 1, False, f"exception: {e}")


def _bc_milestone_invoice(label: str, weight: int, inv_date: str,
                          item_like: str, rate: int, due_date: str) -> None:
    """Invoice identified by date + customer + line item; gate DUE_DATE + DELIVERED_AT."""
    try:
        row = bigcapital_sql(
            "SELECT si.ID, si.DUE_DATE, si.DELIVERED_AT "
            "FROM SALES_INVOICES si "
            "JOIN CONTACTS c ON si.CUSTOMER_ID = c.ID "
            "AND c.DISPLAY_NAME = 'Arcturus Digital' "
            "JOIN ITEMS_ENTRIES ie ON ie.REFERENCE_TYPE = 'SaleInvoice' "
            "AND ie.REFERENCE_ID = si.ID "
            "JOIN ITEMS i ON ie.ITEM_ID = i.ID "
            f"WHERE si.INVOICE_DATE = '{inv_date}' "
            f"AND i.NAME LIKE '{item_like}' "
            f"AND ie.QUANTITY = 1 AND ABS(ie.RATE - {rate}) < 1 "
            "LIMIT 1;"
        )
        if not row:
            check(label, weight, False,
                  f"no invoice dated {inv_date} with line item {item_like} qty 1 @ {rate}")
            return
        parts = row.split("\t")
        due = parts[1].strip() if len(parts) > 1 else ""
        delivered = parts[2].strip() if len(parts) > 2 else ""

        due_ok = due_date in due
        delivered_ok = delivered not in ("", "NULL", "None", "null", "0000-00-00")

        ok = due_ok and delivered_ok
        check(label, weight, ok,
              f"line_item=ok, due={due}, delivered={'yes' if delivered_ok else 'no'}")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_10_bc_invoice1() -> None:
    """Milestone 1 invoice: 2025-10-15, Phase 1 line item qty 1 @ 45000, due 2025-11-14, delivered."""
    _bc_milestone_invoice("10. BigCapital Milestone 1 invoice", 2,
                          "2025-10-15", "Phase 1%", 45000, "2025-11-14")


def check_11_bc_invoice2() -> None:
    """Milestone 2 invoice: 2026-01-05, Phase 2 line item qty 1 @ 55000, due 2026-02-04, delivered."""
    _bc_milestone_invoice("11. BigCapital Milestone 2 invoice", 2,
                          "2026-01-05", "Phase 2%", 55000, "2026-02-04")


def check_12_bc_payment() -> None:
    """Payment of 45000 dated 2025-11-20, applied to the Milestone 1 invoice, into Petty Cash."""
    try:
        row = bigcapital_sql(
            "SELECT pr.PAYMENT_DATE, pr.AMOUNT, a.NAME "
            "FROM PAYMENT_RECEIVES pr "
            "JOIN PAYMENT_RECEIVES_ENTRIES pre ON pre.PAYMENT_RECEIVE_ID = pr.ID "
            "JOIN SALES_INVOICES si ON pre.INVOICE_ID = si.ID "
            "AND si.INVOICE_DATE = '2025-10-15' "
            "JOIN CONTACTS c ON pr.CUSTOMER_ID = c.ID "
            "AND c.DISPLAY_NAME = 'Arcturus Digital' "
            "JOIN ACCOUNTS a ON pr.DEPOSIT_ACCOUNT_ID = a.ID "
            "WHERE pr.PAYMENT_DATE = '2025-11-20' "
            "AND ABS(pr.AMOUNT - 45000) < 1 "
            "AND ABS(pre.PAYMENT_AMOUNT - 45000) < 1 "
            "AND a.NAME LIKE '%Petty Cash%' "
            "LIMIT 1;"
        )
        if not row:
            check("12. BigCapital payment received", 2, False,
                  "no 45000 payment dated 2025-11-20 into Petty Cash applied to the 2025-10-15 invoice")
            return
        parts = row.split("\t")
        pay_date = parts[0].strip()
        amount = float(parts[1].strip() or 0) if len(parts) > 1 else 0.0
        account = parts[2].strip() if len(parts) > 2 else ""

        account_ok = "Petty Cash" in account

        ok = account_ok
        check("12. BigCapital payment received", 2, ok,
              f"date={pay_date}, amount={amount}, applied_to=Milestone1, account={account}")
    except Exception as e:
        check("12. BigCapital payment received", 2, False, f"exception: {e}")


def check_13_bc_journal() -> None:
    """Published journal dated 2025-11-20: debit Deferred Revenue 45000, credit
    Uncategorized Income 45000 — all conditions scoped to the same journal."""
    try:
        row = bigcapital_sql(
            "SELECT mj.ID "
            "FROM MANUAL_JOURNALS mj "
            "WHERE mj.DATE = '2025-11-20' "
            "AND mj.DESCRIPTION LIKE '%Revenue recognition%Milestone 1%Arcturus Digital%' "
            "AND mj.PUBLISHED_AT IS NOT NULL "
            "AND EXISTS(SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "JOIN ACCOUNTS a ON e.ACCOUNT_ID = a.ID "
            "WHERE e.MANUAL_JOURNAL_ID = mj.ID "
            "AND a.NAME LIKE '%Deferred Revenue%Enterprise Engagements%' "
            "AND ABS(e.DEBIT - 45000) < 1) "
            "AND EXISTS(SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "JOIN ACCOUNTS a ON e.ACCOUNT_ID = a.ID "
            "WHERE e.MANUAL_JOURNAL_ID = mj.ID "
            "AND a.NAME LIKE '%Uncategorized Income%' "
            "AND ABS(e.CREDIT - 45000) < 1) "
            "LIMIT 1;"
        )
        ok = bool(row)
        check("13. BigCapital journal entry", 3, ok,
              f"journal_id={row}" if ok else
              "no single published journal dated 2025-11-20 with matching memo, "
              "debit and credit lines")
    except Exception as e:
        check("13. BigCapital journal entry", 3, False, f"exception: {e}")


def check_14_bc_balance() -> None:
    """Customer outstanding balance for Arcturus Digital = 55000."""
    try:
        row = bigcapital_sql(
            "SELECT IFNULL(SUM(si.BALANCE), 0) "
            "FROM SALES_INVOICES si "
            "JOIN CONTACTS c ON si.CUSTOMER_ID = c.ID "
            "WHERE c.DISPLAY_NAME = 'Arcturus Digital';"
        )
        balance = float(row.strip() or 0) if row else 0.0
        ok = abs(balance - 55000.0) < 1.0
        check("14. BigCapital customer balance 55000", 3, ok,
              f"balance={balance}, expected=55000")
    except Exception as e:
        check("14. BigCapital customer balance 55000", 3, False, f"exception: {e}")


# ── Pretix checks ────────────────────────────────────────────────────────────

def check_15_pretix_event() -> None:
    """Event 'Arcturus Digital Milestone Celebration' live, slug, dates, currency."""
    try:
        row = pretix_sql(
            "SELECT e.slug, e.name::text, "
            "(e.date_from AT TIME ZONE COALESCE("
            "(SELECT s.value FROM pretixbase_event_settingsstore s "
            "WHERE s.object_id = e.id AND s.key = 'timezone' LIMIT 1), "
            "'UTC'))::date::text, "
            "e.currency, e.live "
            "FROM pretixbase_event e "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            "WHERE o.slug = 'culinary-arts' "
            "AND e.slug = 'arcturus-milestone-celebration' "
            "LIMIT 1;"
        )
        if not row:
            check("15. Pretix event", 2, False, "not found")
            return
        parts = row.split("|")
        slug = parts[0].strip()
        name = parts[1].strip() if len(parts) > 1 else ""
        date_from = parts[2].strip() if len(parts) > 2 else ""
        currency = parts[3].strip() if len(parts) > 3 else ""
        live = parts[4].strip() if len(parts) > 4 else ""

        name_ok = "Arcturus Digital Milestone Celebration" in name
        date_ok = date_from == "2026-03-10"
        currency_ok = currency == "USD"
        live_ok = live in ("t", "true", "True", "1")

        ok = name_ok and date_ok and currency_ok and live_ok
        check("15. Pretix event", 2, ok,
              f"name_ok={name_ok}, date={date_from}, currency={currency}, live={live}")
    except Exception as e:
        check("15. Pretix event", 2, False, f"exception: {e}")


def check_16_pretix_product() -> None:
    """Product 'Celebration Gala Ticket' at price 175."""
    try:
        row = pretix_sql(
            "SELECT i.name::text, i.default_price "
            "FROM pretixbase_item i "
            "JOIN pretixbase_event e ON i.event_id = e.id "
            "WHERE e.slug = 'arcturus-milestone-celebration' "
            "AND i.name::text LIKE '%Celebration Gala Ticket%' "
            "LIMIT 1;"
        )
        if not row:
            check("16. Pretix product Celebration Gala Ticket", 1, False, "not found")
            return
        parts = row.split("|")
        name = parts[0].strip()
        price = float(parts[1].strip() or 0) if len(parts) > 1 else 0.0
        ok = "Celebration Gala Ticket" in name and abs(price - 175.0) < 0.01
        check("16. Pretix product Celebration Gala Ticket", 1, ok,
              f"price={price}")
    except Exception as e:
        check("16. Pretix product Celebration Gala Ticket", 1, False, f"exception: {e}")


def check_17_pretix_quota() -> None:
    """Quota 'Gala Ticket Quota' with size 40, linked to 'Celebration Gala Ticket'."""
    try:
        row = pretix_sql(
            "SELECT count(*) "
            "FROM pretixbase_quota q "
            "JOIN pretixbase_event e ON q.event_id = e.id "
            "AND e.slug = 'arcturus-milestone-celebration' "
            "JOIN pretixbase_quota_items qi ON qi.quota_id = q.id "
            "JOIN pretixbase_item i ON i.id = qi.item_id "
            "WHERE q.name LIKE '%Gala Ticket Quota%' "
            "AND q.size = 40 "
            "AND i.name::text LIKE '%Celebration Gala Ticket%';"
        )
        count = int(row.strip() or 0) if row else 0
        ok = count >= 1
        check("17. Pretix quota Gala Ticket Quota", 1, ok,
              f"quota(size=40)->item links={count}")
    except Exception as e:
        check("17. Pretix quota Gala Ticket Quota", 1, False, f"exception: {e}")


def check_18_pretix_voucher() -> None:
    """Voucher ARCTURUS100VIP: 100% discount, max 3, linked to 'Celebration Gala
    Ticket', valid until 2026-03-10 (event-timezone calendar date)."""
    try:
        row = pretix_sql(
            "SELECT v.code, v.price_mode, v.value, v.max_usages, "
            "(v.valid_until AT TIME ZONE COALESCE("
            "(SELECT s.value FROM pretixbase_event_settingsstore s "
            "WHERE s.object_id = e.id AND s.key = 'timezone' LIMIT 1), "
            "'UTC'))::date::text "
            "FROM pretixbase_voucher v "
            "JOIN pretixbase_event e ON v.event_id = e.id "
            "AND e.slug = 'arcturus-milestone-celebration' "
            "JOIN pretixbase_item i ON v.item_id = i.id "
            "AND i.name::text LIKE '%Celebration Gala Ticket%' "
            "WHERE v.code = 'ARCTURUS100VIP' "
            "LIMIT 1;"
        )
        if not row:
            check("18. Pretix voucher ARCTURUS100VIP", 2, False,
                  "not found (or not linked to product Celebration Gala Ticket)")
            return
        parts = row.split("|")
        code = parts[0].strip()
        price_mode = parts[1].strip() if len(parts) > 1 else ""
        value = float(parts[2].strip() or 0) if len(parts) > 2 else 0.0
        max_usages = int(parts[3].strip() or 0) if len(parts) > 3 else 0
        valid_until = parts[4].strip() if len(parts) > 4 else ""

        code_ok = code == "ARCTURUS100VIP"
        discount_ok = price_mode == "percent" and abs(value - 100.0) < 0.01
        max_ok = max_usages == 3
        valid_ok = valid_until == "2026-03-10"

        ok = code_ok and discount_ok and max_ok and valid_ok
        check("18. Pretix voucher ARCTURUS100VIP", 2, ok,
              f"mode={price_mode}, value={value}, max={max_usages}, "
              f"item_linked=yes, valid_until={valid_until}")
    except Exception as e:
        check("18. Pretix voucher ARCTURUS100VIP", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_twenty_company()
    check_2_twenty_person()
    check_3_twenty_opportunity()
    check_4_twenty_favorite()
    check_5_twenty_task_milestone2()
    check_6_twenty_task_celebration()
    check_7_bc_customer()
    check_8_bc_items()
    check_9_bc_account()
    check_10_bc_invoice1()
    check_11_bc_invoice2()
    check_12_bc_payment()
    check_13_bc_journal()
    check_14_bc_balance()
    check_15_pretix_event()
    check_16_pretix_product()
    check_17_pretix_quota()
    check_18_pretix_voucher()

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
