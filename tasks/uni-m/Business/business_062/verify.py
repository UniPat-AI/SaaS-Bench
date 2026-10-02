"""
Verifier for Business-062-I4: Vendor Payment Reconciliation across BigCapital, Twenty CRM, and Frappe HRMS

Checks: 16 weighted checks across bigcapital, twenty, hrms.
Strategy: docker exec (DB queries) for all three sites.
  - BigCapital: MariaDB (mysql), tenant DB discovered at runtime
  - Twenty: Postgres (psql), workspace schema discovered at runtime
  - HRMS: MariaDB (mysql), site DB discovered at runtime

Required env vars:
  SERVER_HOSTNAME, BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER
"""

import os
import re
import sys
import subprocess

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

BIGCAPITAL_PORT = os.environ.get("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.environ.get("HRMS_DB_CONTAINER")

_required = [
    "BIGCAPITAL_PORT", "BIGCAPITAL_CONTAINER", "BIGCAPITAL_DB_CONTAINER",
    "TWENTY_PORT", "TWENTY_CONTAINER", "TWENTY_DB_CONTAINER",
    "HRMS_PORT", "HRMS_CONTAINER", "HRMS_DB_CONTAINER",
]
for _var in _required:
    if not os.environ.get(_var):
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


# ── BigCapital: MariaDB ───────────────────────────────────────────────────────
_bc_tenant_db = ""
_bc_db_user = ""
_bc_db_pass = ""


def discover_bc_db() -> None:
    """Discover BigCapital DB credentials and tenant DB name."""
    global _bc_tenant_db, _bc_db_user, _bc_db_pass

    # Get DB credentials from the app server container
    rc, out, _ = docker_exec(BIGCAPITAL_CONTAINER, "env")
    env_vars: dict[str, str] = {}
    for line in out.split("\n"):
        if "=" in line:
            k, _, v = line.partition("=")
            env_vars[k] = v

    _bc_db_user = env_vars.get("DB_USER", "bigcapital")
    _bc_db_pass = env_vars.get("DB_PASSWORD", "bigcapital123")
    prefix = env_vars.get("TENANT_DB_NAME_PERFIX", "bigcapital_tenant_")

    # List databases and find tenant DB
    rc, out, _ = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", f"-u{_bc_db_user}", f"-p{_bc_db_pass}",
        "-N", "-B", "-e", "SHOW DATABASES",
    )
    dbs = [d.strip() for d in out.strip().split("\n") if d.strip()]

    # Look for tenant DB by prefix
    for db in dbs:
        if db.startswith(prefix):
            _bc_tenant_db = db
            return

    # Fallback: try system DB to read tenants table
    sys_db = env_vars.get("SYSTEM_DB_NAME", "bigcapital")
    if sys_db in dbs:
        rc2, out2, _ = docker_exec(
            BIGCAPITAL_DB_CONTAINER,
            "mysql", f"-u{_bc_db_user}", f"-p{_bc_db_pass}",
            "-N", "-B", "-e", "SELECT db_name FROM tenants LIMIT 1", sys_db,
        )
        if rc2 == 0 and out2.strip():
            _bc_tenant_db = out2.strip()
            return

    # Fallback: find DB with contacts table
    for db in dbs:
        if db in ("information_schema", "mysql", "performance_schema", "sys"):
            continue
        rc3, out3, _ = docker_exec(
            BIGCAPITAL_DB_CONTAINER,
            "mysql", f"-u{_bc_db_user}", f"-p{_bc_db_pass}",
            "-N", "-B", "-e",
            "SELECT 1 FROM information_schema.TABLES "
            f"WHERE TABLE_SCHEMA='{db}' AND TABLE_NAME='CONTACTS' LIMIT 1",
        )
        if rc3 == 0 and "1" in out3:
            _bc_tenant_db = db
            return

    _bc_tenant_db = dbs[0] if dbs else "bigcapital"


def bc_sql(query: str) -> str:
    """Run SQL against BigCapital tenant DB (MariaDB). Returns tab-separated output.

    Raises on non-zero mysql exit so bad queries surface instead of reading as
    "not found".
    """
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER,
        "mysql", f"-u{_bc_db_user}", f"-p{_bc_db_pass}",
        "--default-character-set=utf8mb4",
        "-N", "-B", "-e", query, _bc_tenant_db,
    )
    if rc != 0:
        raise RuntimeError(f"bc mysql failed (rc={rc}): {err.strip()[:300]}")
    return out.strip()


def bc_parse_rows(output: str) -> list[list[str]]:
    """Parse tab-separated mysql output into rows of fields."""
    rows = []
    for line in output.split("\n"):
        line = line.strip()
        if line:
            rows.append(line.split("\t"))
    return rows


# ── Twenty: Postgres ──────────────────────────────────────────────────────────
_twenty_db = "default"
_twenty_user = "twenty"
_twenty_schema = ""


def discover_twenty_schema() -> None:
    global _twenty_db, _twenty_user, _twenty_schema
    for db, user in [("default", "twenty"), ("default", "postgres"),
                     ("twenty", "twenty"), ("twenty", "postgres")]:
        rc, out, _ = docker_exec(
            TWENTY_DB_CONTAINER,
            "psql", "-U", user, "-d", db, "-t", "-A", "-c",
            # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
            # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
            # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
            # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
            'SELECT ds.schema FROM core."dataSource" ds '
            'JOIN core.workspace w ON w.id = ds."workspaceId" '
            "WHERE w.subdomain = 'yc';",
        )
        if rc == 0 and out.strip().startswith("workspace_"):
            _twenty_db = db
            _twenty_user = user
            _twenty_schema = out.strip()
            return
    _twenty_schema = "public"


def twenty_sql(query: str) -> str:
    """Run SQL against Twenty workspace schema (Postgres). Returns pipe-separated output.

    Raises on non-zero psql exit so bad queries (e.g. wrong column names) surface
    instead of being swallowed as "not found".
    """
    full = f'SET search_path TO "{_twenty_schema}"; {query}'
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", _twenty_user, "-d", _twenty_db, "-t", "-A", "-c", full,
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql failed (rc={rc}): {err.strip()[:300]}")
    # Filter out the "SET" line that psql echoes for the SET command
    lines = [l for l in out.strip().split("\n") if l.strip() and l.strip() != "SET"]
    return "\n".join(lines)


# ── HRMS: MariaDB ────────────────────────────────────────────────────────────
_hrms_db = ""
_hrms_db_user = "root"
_hrms_db_pass = ""


def discover_hrms_db() -> None:
    """Discover HRMS MariaDB credentials and site DB name."""
    global _hrms_db, _hrms_db_user, _hrms_db_pass

    # Get root password from DB container env
    rc, out, _ = docker_exec(HRMS_DB_CONTAINER, "env")
    env_vars: dict[str, str] = {}
    for line in out.split("\n"):
        if "=" in line:
            k, _, v = line.partition("=")
            env_vars[k] = v
    _hrms_db_pass = env_vars.get("MYSQL_ROOT_PASSWORD", "")
    _hrms_db_user = "root"

    # Find DB with tabExpense Claim Type (Frappe site DB)
    rc, out, _ = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        f"-u{_hrms_db_user}", f"-p{_hrms_db_pass}" if _hrms_db_pass else "",
        "-N", "-B", "-e",
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES "
        "WHERE TABLE_NAME='tabExpense Claim Type' LIMIT 1",
    )
    if rc == 0 and out.strip():
        _hrms_db = out.strip()
        return

    # Fallback: find Frappe DB by looking for tabDocType
    rc, out, _ = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        f"-u{_hrms_db_user}", f"-p{_hrms_db_pass}" if _hrms_db_pass else "",
        "-N", "-B", "-e",
        "SELECT TABLE_SCHEMA FROM information_schema.TABLES "
        "WHERE TABLE_NAME='tabDocType' LIMIT 1",
    )
    if rc == 0 and out.strip():
        _hrms_db = out.strip()
        return

    _hrms_db = "_frappe_bench"


def hrms_sql(query: str) -> str:
    """Run SQL against HRMS Frappe DB (MariaDB). Returns tab-separated output.

    Raises on non-zero mysql exit so bad queries surface instead of reading as
    "not found".
    """
    args = [
        "mysql", "--default-character-set=utf8mb4",
        f"-u{_hrms_db_user}",
    ]
    if _hrms_db_pass:
        args.append(f"-p{_hrms_db_pass}")
    args.extend(["-N", "-B", "-e", query, _hrms_db])
    rc, out, err = docker_exec(HRMS_DB_CONTAINER, *args)
    if rc != 0:
        raise RuntimeError(f"hrms mysql failed (rc={rc}): {err.strip()[:300]}")
    return out.strip()


def _norm_text(s: str) -> str:
    """Normalize a DB text field for substring comparison: undo mysql -B escapes,
    lowercase, collapse whitespace."""
    s = s.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


# ── Cross-check bindings (bill/payment ids resolved by checks 3-6) ────────────
_bill1_id: int | None = None  # Vantage bill
_bill2_id: int | None = None  # Luminary bill
_pay1_id: int | None = None   # payment against bill 1
_pay2_id: int | None = None   # payment against bill 2


# ── BigCapital checks ─────────────────────────────────────────────────────────

def check_1_vendors() -> None:
    """Both vendors exist (as vendor contacts) with correct emails and opening balances."""
    try:
        out = bc_sql(
            "SELECT DISPLAY_NAME, EMAIL, OPENING_BALANCE, CONTACT_SERVICE "
            "FROM CONTACTS "
            "WHERE DISPLAY_NAME IN ('Vantage Systems LLC','Luminary Consulting Group') "
            "ORDER BY DISPLAY_NAME"
        )
        vendors: dict[str, dict] = {}
        for row in bc_parse_rows(out):
            if len(row) >= 4:
                vendors[row[0]] = {"email": row[1], "ob": row[2], "service": row[3]}

        v1 = vendors.get("Vantage Systems LLC", {})
        v2 = vendors.get("Luminary Consulting Group", {})
        v1_ok = (v1.get("email") == "ap@vantagesystems.com"
                 and abs(float(v1.get("ob", 0)) - 800) < 0.01
                 and v1.get("service", "").strip().lower() == "vendor")
        v2_ok = (v2.get("email") == "billing@luminarycg.com"
                 and abs(float(v2.get("ob", 0)) - 350) < 0.01
                 and v2.get("service", "").strip().lower() == "vendor")
        check("1. Vendors exist", 2, v1_ok and v2_ok,
              f"V1={'ok' if v1_ok else v1}, V2={'ok' if v2_ok else v2}")
    except Exception as e:
        check("1. Vendors exist", 2, False, f"exception: {e}")


def check_2_items() -> None:
    """Both service items exist with correct cost prices and prescribed descriptions."""
    try:
        out = bc_sql(
            "SELECT NAME, TYPE, COST_PRICE, "
            "COALESCE(NOTE,''), COALESCE(PURCHASE_DESCRIPTION,''), COALESCE(SELL_DESCRIPTION,'') "
            "FROM ITEMS "
            "WHERE NAME IN ('ERP Implementation Services','Business Process Optimization') "
            "ORDER BY NAME"
        )
        items: dict[str, dict] = {}
        for row in bc_parse_rows(out):
            if len(row) >= 6:
                items[row[0]] = {"type": row[1], "cost": row[2],
                                 "descs": [row[3], row[4], row[5]]}

        def _desc_ok(item: dict, expected: str) -> bool:
            want = _norm_text(expected)
            return any(want in _norm_text(d) for d in item.get("descs", []))

        i1 = items.get("ERP Implementation Services", {})
        i2 = items.get("Business Process Optimization", {})
        i1_ok = (i1.get("type", "").lower() in ("service", "services")
                 and abs(float(i1.get("cost", 0)) - 280) < 0.01
                 and _desc_ok(i1, "Full-cycle ERP deployment, configuration, and user training"))
        i2_ok = (i2.get("type", "").lower() in ("service", "services")
                 and abs(float(i2.get("cost", 0)) - 160) < 0.01
                 and _desc_ok(i2, "Workflow analysis and process improvement consulting services"))
        check("2. Service items", 2, i1_ok and i2_ok,
              f"ERP={'ok' if i1_ok else i1}, BPO={'ok' if i2_ok else i2}")
    except Exception as e:
        check("2. Service items", 2, False, f"exception: {e}")


def _bill_lines(bill_id: int) -> list[tuple[str, float, float]]:
    """Fetch (item name, quantity, rate) line entries for a bill."""
    out = bc_sql(
        "SELECT i.NAME, ie.QUANTITY, ie.RATE "
        "FROM ITEMS_ENTRIES ie JOIN ITEMS i ON ie.ITEM_ID = i.ID "
        "WHERE LOWER(ie.REFERENCE_TYPE) = 'bill' "
        f"AND ie.REFERENCE_ID = {bill_id}"
    )
    lines = []
    for r in bc_parse_rows(out):
        if len(r) >= 3:
            try:
                lines.append((r[0], float(r[1]), float(r[2])))
            except ValueError:
                pass
    return lines


def _has_line(lines: list[tuple[str, float, float]],
              name: str, qty: float, rate: float) -> bool:
    return any(n == name and abs(q - qty) < 0.01 and abs(rt - rate) < 0.01
               for n, q, rt in lines)


def check_3_bill_vantage() -> None:
    """Exactly 1 Vantage bill: total 1160, dated 2025-10-06, opened;
    exactly 2 lines: ERP(3x280) + BPO(2x160)."""
    global _bill1_id
    try:
        out = bc_sql(
            "SELECT b.ID, b.AMOUNT, b.BILL_DATE, b.OPENED_AT "
            "FROM BILLS b JOIN CONTACTS c ON b.VENDOR_ID = c.ID "
            "WHERE c.DISPLAY_NAME = 'Vantage Systems LLC'"
        )
        rows = bc_parse_rows(out)
        if not rows:
            check("3. Bill 1 (Vantage)", 2, False, "bill not found")
            return
        if len(rows) != 1:
            check("3. Bill 1 (Vantage)", 2, False,
                  f"expected exactly 1 bill, found {len(rows)}")
            return
        p = rows[0]
        if p[0].strip().isdigit():
            _bill1_id = int(p[0].strip())
        amount = float(p[1])
        bdate = p[2].strip()
        opened = p[3].strip() if len(p) > 3 else ""
        opened_ok = opened not in ("", "NULL")
        head_ok = (abs(amount - 1160) < 0.01
                   and bdate.startswith("2025-10-06")
                   and opened_ok)

        lines_ok = False
        lines: list[tuple[str, float, float]] = []
        if _bill1_id is not None:
            lines = _bill_lines(_bill1_id)
            lines_ok = (len(lines) == 2
                        and _has_line(lines, "ERP Implementation Services", 3, 280)
                        and _has_line(lines, "Business Process Optimization", 2, 160))
        check("3. Bill 1 (Vantage)", 2, head_ok and lines_ok,
              f"amount={amount}, date={bdate}, opened={'yes' if opened_ok else 'no'}, "
              f"lines={lines}")
    except Exception as e:
        check("3. Bill 1 (Vantage)", 2, False, f"exception: {e}")


def check_4_bill_luminary() -> None:
    """Exactly 1 Luminary bill: total 800, dated 2025-10-15, opened;
    exactly 1 line: BPO(5x160)."""
    global _bill2_id
    try:
        out = bc_sql(
            "SELECT b.ID, b.AMOUNT, b.BILL_DATE, b.OPENED_AT "
            "FROM BILLS b JOIN CONTACTS c ON b.VENDOR_ID = c.ID "
            "WHERE c.DISPLAY_NAME = 'Luminary Consulting Group'"
        )
        rows = bc_parse_rows(out)
        if not rows:
            check("4. Bill 2 (Luminary)", 2, False, "bill not found")
            return
        if len(rows) != 1:
            check("4. Bill 2 (Luminary)", 2, False,
                  f"expected exactly 1 bill, found {len(rows)}")
            return
        p = rows[0]
        if p[0].strip().isdigit():
            _bill2_id = int(p[0].strip())
        amount = float(p[1])
        bdate = p[2].strip()
        opened = p[3].strip() if len(p) > 3 else ""
        opened_ok = opened not in ("", "NULL")
        head_ok = (abs(amount - 800) < 0.01
                   and bdate.startswith("2025-10-15")
                   and opened_ok)

        lines_ok = False
        lines: list[tuple[str, float, float]] = []
        if _bill2_id is not None:
            lines = _bill_lines(_bill2_id)
            lines_ok = (len(lines) == 1
                        and _has_line(lines, "Business Process Optimization", 5, 160))
        check("4. Bill 2 (Luminary)", 2, head_ok and lines_ok,
              f"amount={amount}, date={bdate}, opened={'yes' if opened_ok else 'no'}, "
              f"lines={lines}")
    except Exception as e:
        check("4. Bill 2 (Luminary)", 2, False, f"exception: {e}")


def _check_payment(label: str, vendor: str, expect_amount: float,
                   expect_date: str, expect_bill_id: int | None) -> int | None:
    """Shared logic for checks 5/6: exactly 1 payment for the vendor, exact
    amount/date/account, entry bound to the expected bill. Returns payment id."""
    out = bc_sql(
        "SELECT bp.ID, bp.AMOUNT, bp.PAYMENT_DATE, a.NAME, "
        "bpe.BILL_ID, bpe.PAYMENT_AMOUNT "
        "FROM BILLS_PAYMENTS bp "
        "JOIN BILLS_PAYMENTS_ENTRIES bpe ON bpe.BILL_PAYMENT_ID = bp.ID "
        "JOIN ACCOUNTS a ON bp.PAYMENT_ACCOUNT_ID = a.ID "
        "JOIN CONTACTS c ON bp.VENDOR_ID = c.ID "
        f"WHERE c.DISPLAY_NAME = '{vendor}'"
    )
    rows = bc_parse_rows(out)
    if not rows:
        check(label, 2, False, "payment not found")
        return None
    if len(rows) != 1:
        check(label, 2, False,
              f"expected exactly 1 payment entry, found {len(rows)}")
        return None
    p = rows[0]
    pay_id = int(p[0].strip()) if p[0].strip().isdigit() else None
    amount = float(p[1])
    pdate = p[2].strip()
    acct = p[3].strip()
    entry_bill = p[4].strip()
    entry_amt = float(p[5]) if len(p) > 5 and p[5] not in ("", "NULL") else 0.0
    bill_ok = expect_bill_id is not None and entry_bill == str(expect_bill_id)
    ok = (abs(amount - expect_amount) < 0.01
          and pdate.startswith(expect_date)
          and acct == "Bank Account"
          and bill_ok
          and abs(entry_amt - expect_amount) < 0.01)
    check(label, 2, ok,
          f"amount={amount}, date={pdate}, account={acct}, "
          f"entry_bill={entry_bill} (expect {expect_bill_id}), entry_amount={entry_amt}")
    return pay_id


def check_5_payment_vantage() -> None:
    """Exactly 1 payment to Vantage: 950 on 2025-10-21 from 'Bank Account',
    applied to bill 1 for 950."""
    global _pay1_id
    try:
        _pay1_id = _check_payment(
            "5. Payment to Vantage", "Vantage Systems LLC", 950, "2025-10-21",
            _bill1_id)
    except Exception as e:
        check("5. Payment to Vantage", 2, False, f"exception: {e}")


def check_6_payment_luminary() -> None:
    """Exactly 1 payment to Luminary: 650 on 2025-10-27 from 'Bank Account',
    applied to bill 2 for 650."""
    global _pay2_id
    try:
        _pay2_id = _check_payment(
            "6. Payment to Luminary", "Luminary Consulting Group", 650, "2025-10-27",
            _bill2_id)
    except Exception as e:
        check("6. Payment to Luminary", 2, False, f"exception: {e}")


def check_7_purchase_totals() -> None:
    """Purchase totals scoped to the two task bills: ERP=840, BPO=1120."""
    try:
        if _bill1_id is None or _bill2_id is None:
            check("7. Purchase totals by item", 2, False,
                  f"bill ids unresolved (bill1={_bill1_id}, bill2={_bill2_id})")
            return
        out = bc_sql(
            "SELECT i.NAME, SUM(ie.QUANTITY * ie.RATE) AS total "
            "FROM ITEMS_ENTRIES ie "
            "JOIN ITEMS i ON ie.ITEM_ID = i.ID "
            "WHERE LOWER(ie.REFERENCE_TYPE) = 'bill' "
            f"AND ie.REFERENCE_ID IN ({_bill1_id},{_bill2_id}) "
            "AND i.NAME IN ('ERP Implementation Services','Business Process Optimization') "
            "GROUP BY i.NAME"
        )
        totals: dict[str, float] = {}
        for row in bc_parse_rows(out):
            if len(row) >= 2:
                totals[row[0]] = float(row[1])
        erp = totals.get("ERP Implementation Services", 0)
        bpo = totals.get("Business Process Optimization", 0)
        ok = abs(erp - 840) < 0.01 and abs(bpo - 1120) < 0.01
        check("7. Purchase totals by item", 2, ok, f"ERP={erp}, BPO={bpo}")
    except Exception as e:
        check("7. Purchase totals by item", 2, False, f"exception: {e}")


def check_8_expense() -> None:
    """Expense MISC-EXP-2025-004: 210 to 'Cost of Goods Sold' paid from
    'Petty Cash', dated 2025-10-29, published."""
    try:
        out = bc_sql(
            "SELECT et.TOTAL_AMOUNT, et.PAYMENT_DATE, pa.NAME, ea.NAME, "
            "cat.AMOUNT, et.PUBLISHED_AT "
            "FROM EXPENSES_TRANSACTIONS et "
            "JOIN ACCOUNTS pa ON et.PAYMENT_ACCOUNT_ID = pa.ID "
            "JOIN EXPENSE_TRANSACTION_CATEGORIES cat ON cat.EXPENSE_ID = et.ID "
            "JOIN ACCOUNTS ea ON cat.EXPENSE_ACCOUNT_ID = ea.ID "
            "WHERE et.REFERENCE_NO = 'MISC-EXP-2025-004'"
        )
        rows = bc_parse_rows(out)
        if not rows:
            check("8. Expense published", 1, False, "expense not found")
            return

        def _row_ok(p: list[str]) -> bool:
            if len(p) < 6:
                return False
            return (abs(float(p[0]) - 210) < 0.01
                    and p[1].strip().startswith("2025-10-29")
                    and p[2].strip() == "Petty Cash"
                    and p[3].strip() == "Cost of Goods Sold"
                    and abs(float(p[4]) - 210) < 0.01
                    and p[5].strip() not in ("", "NULL"))

        ok = any(_row_ok(p) for p in rows)
        p = rows[0]
        check("8. Expense published", 1, ok,
              f"total={p[0]}, date={p[1]}, paid_from={p[2] if len(p) > 2 else '?'}, "
              f"expense_acct={p[3] if len(p) > 3 else '?'}, "
              f"cat_amount={p[4] if len(p) > 4 else '?'}, "
              f"published={'yes' if len(p) > 5 and p[5].strip() not in ('', 'NULL') else 'no'}")
    except Exception as e:
        check("8. Expense published", 1, False, f"exception: {e}")


def check_9_bank_rule() -> None:
    """Bank rule 'Cost of Goods Auto-Categorization' with 'cogs' condition."""
    try:
        out = bc_sql(
            "SELECT br.NAME, a.NAME AS acct "
            "FROM BANK_RULES br "
            "LEFT JOIN ACCOUNTS a ON br.ASSIGN_ACCOUNT_ID = a.ID "
            "WHERE br.NAME = 'Cost of Goods Auto-Categorization'"
        )
        rows = bc_parse_rows(out)
        if not rows:
            check("9. Bank rule", 2, False, "bank rule not found")
            return
        acct = rows[0][1].strip() if len(rows[0]) > 1 else ""
        acct_ok = "cost of goods sold" in acct.lower()

        cond = bc_sql(
            "SELECT brc.FIELD, brc.COMPARATOR, brc.VALUE "
            "FROM BANK_RULE_CONDITIONS brc "
            "JOIN BANK_RULES br ON brc.RULE_ID = br.ID "
            "WHERE br.NAME = 'Cost of Goods Auto-Categorization'"
        )
        conds = [(r[0].strip().lower(), r[1].strip().lower(), r[2].strip().lower())
                 for r in bc_parse_rows(cond) if len(r) >= 3]
        cond_ok = any(f == "description" and "contain" in comp and v == "cogs"
                      for f, comp, v in conds)
        check("9. Bank rule", 2, acct_ok and cond_ok,
              f"account={acct}, conditions={conds}")
    except Exception as e:
        check("9. Bank rule", 2, False, f"exception: {e}")


def check_10_gl_credits() -> None:
    """Bank Account GL credits bound to the two bill payments:
    (2025-10-21, 950) and (2025-10-27, 650), each on the same row."""
    try:
        if _pay1_id is None or _pay2_id is None:
            check("10. GL credits Bank Account", 3, False,
                  f"payment ids unresolved (pay1={_pay1_id}, pay2={_pay2_id})")
            return
        out = bc_sql(
            "SELECT at2.DATE, at2.CREDIT "
            "FROM ACCOUNTS_TRANSACTIONS at2 "
            "JOIN ACCOUNTS a ON at2.ACCOUNT_ID = a.ID "
            "WHERE a.NAME = 'Bank Account' "
            "AND at2.REFERENCE_TYPE = 'BillPayment' "
            f"AND at2.REFERENCE_ID IN ({_pay1_id},{_pay2_id})"
        )
        pairs: list[tuple[str, float]] = []
        for row in bc_parse_rows(out):
            if len(row) >= 2 and row[1] not in ("", "NULL"):
                try:
                    pairs.append((row[0].strip(), float(row[1])))
                except ValueError:
                    pass
        has_950 = any(d.startswith("2025-10-21") and abs(c - 950) < 0.01
                      for d, c in pairs)
        has_650 = any(d.startswith("2025-10-27") and abs(c - 650) < 0.01
                      for d, c in pairs)
        check("10. GL credits Bank Account", 3, has_950 and has_650,
              f"entries={pairs}, has_950_on_10-21={has_950}, has_650_on_10-27={has_650}")
    except Exception as e:
        check("10. GL credits Bank Account", 3, False, f"exception: {e}")


# ── Twenty CRM checks ────────────────────────────────────────────────────────

def check_11_twenty_companies() -> None:
    """Both companies exist in Twenty with correct domains."""
    try:
        rows = twenty_sql(
            "SELECT name, \"domainNamePrimaryLinkUrl\" "
            "FROM company "
            "WHERE name IN ('Vantage Systems LLC','Luminary Consulting Group') "
            "AND \"deletedAt\" IS NULL"
        )
        comps: dict[str, str] = {}
        for line in rows.split("\n"):
            if "|" in line:
                p = line.split("|")
                comps[p[0]] = p[1].strip()
        v1_ok = "vantagesystems.com" in comps.get("Vantage Systems LLC", "")
        v2_ok = "luminarycg.com" in comps.get("Luminary Consulting Group", "")
        check("11. Twenty companies", 2, v1_ok and v2_ok,
              f"V1_domain={comps.get('Vantage Systems LLC', 'missing')}, "
              f"V2_domain={comps.get('Luminary Consulting Group', 'missing')}")
    except Exception as e:
        check("11. Twenty companies", 2, False, f"exception: {e}")


def check_12_twenty_persons() -> None:
    """Both persons exist with correct emails, titles, and company links."""
    try:
        rows = twenty_sql(
            "SELECT p.\"nameFirstName\", p.\"nameLastName\", "
            "p.\"emailsPrimaryEmail\", p.\"jobTitle\", c.name AS company "
            "FROM person p "
            "LEFT JOIN company c ON p.\"companyId\" = c.id "
            "AND c.\"deletedAt\" IS NULL "
            "WHERE p.\"emailsPrimaryEmail\" IN "
            "('ap@vantagesystems.com','billing@luminarycg.com') "
            "AND p.\"deletedAt\" IS NULL"
        )
        persons: dict[str, dict] = {}
        for line in rows.split("\n"):
            if "|" in line:
                p = line.split("|")
                email = p[2].strip()
                persons[email] = {
                    "name": f"{p[0].strip()} {p[1].strip()}",
                    "title": p[3].strip(),
                    "company": p[4].strip() if len(p) > 4 else "",
                }

        p1 = persons.get("ap@vantagesystems.com", {})
        p2 = persons.get("billing@luminarycg.com", {})
        p1_ok = ("harrison" in p1.get("name", "").lower()
                 and "blake" in p1.get("name", "").lower()
                 and "vendor relations" in p1.get("title", "").lower()
                 and "vantage" in p1.get("company", "").lower())
        p2_ok = ("celeste" in p2.get("name", "").lower()
                 and "moreau" in p2.get("name", "").lower()
                 and "senior consultant" in p2.get("title", "").lower()
                 and "luminary" in p2.get("company", "").lower())
        check("12. Twenty persons", 2, p1_ok and p2_ok,
              f"Harrison={'ok' if p1_ok else p1}, Celeste={'ok' if p2_ok else p2}")
    except Exception as e:
        check("12. Twenty persons", 2, False, f"exception: {e}")


def check_13_twenty_note() -> None:
    """Reconciliation note exists with correct title and all prescribed body facts."""
    try:
        # Title and body fetched in two separate queries (no fragile "|" splitting).
        note_id = twenty_sql(
            "SELECT id FROM note "
            "WHERE \"deletedAt\" IS NULL "
            "AND title LIKE '%Vendor Payment Reconciliation%2025-10-27%' "
            "LIMIT 1"
        ).strip()
        if not note_id:
            check("13. Twenty note", 2, False, "note not found")
            return
        body_raw = twenty_sql(
            "SELECT regexp_replace(COALESCE(\"bodyV2Markdown\", ''), "
            "E'[\\n\\r]+', ' ', 'g') "
            f"FROM note WHERE id = '{note_id}'"
        )
        body = re.sub(r"\s+", " ", body_raw).strip().lower()

        # Every prescribed fact must appear as a phrase (digit-bounded), which
        # kills both template-paste credit and "500 in 5000" false hits.
        required = [
            ("total 1160", r"total 1160(?!\d)"),
            ("paid 950 on 2025-10-21", r"paid 950 on 2025-10-21"),
            ("remaining 1010", r"remaining 1010(?!\d)"),
            ("total 800", r"total 800(?!\d)"),
            ("paid 650 on 2025-10-27", r"paid 650 on 2025-10-27"),
            ("remaining 500", r"remaining 500(?!\d)"),
            ("210 from Petty Cash on 2025-10-29",
             r"(?<!\d)210 from petty cash on 2025-10-29"),
            ("Cost of Goods Auto-Categorization",
             r"cost of goods auto-categorization"),
            ("purchase total 840", r"purchase total\W{0,4}840(?!\d)"),
            ("purchase total 1120", r"purchase total\W{0,4}1120(?!\d)"),
        ]
        missing = [name for name, pat in required if not re.search(pat, body)]
        body_ok = not missing
        check("13. Twenty note", 2, body_ok,
              f"title matched, body phrases {len(required) - len(missing)}/"
              f"{len(required)}" + (f", missing={missing}" if missing else ""))
    except Exception as e:
        check("13. Twenty note", 2, False, f"exception: {e}")


def check_14_twenty_favorites() -> None:
    """Vantage Systems LLC is in favorites."""
    try:
        row = twenty_sql(
            "SELECT f.id FROM favorite f "
            "JOIN company c ON f.\"companyId\" = c.id "
            "WHERE c.name = 'Vantage Systems LLC' "
            "AND f.\"deletedAt\" IS NULL AND c.\"deletedAt\" IS NULL LIMIT 1"
        )
        ok = bool(row.strip())
        check("14. Vantage in favorites", 1, ok,
              "found" if ok else "not in favorites")
    except Exception as e:
        check("14. Vantage in favorites", 1, False, f"exception: {e}")


# ── HRMS checks ───────────────────────────────────────────────────────────────

def check_15_expense_types() -> None:
    """Expense claim types 'Calls' and 'Food' with correct descriptions."""
    try:
        out = hrms_sql(
            "SELECT name, description FROM `tabExpense Claim Type` "
            "WHERE name IN ('Calls','Food')"
        )
        types: dict[str, str] = {}
        for line in out.split("\n"):
            if "\t" in line:
                p = line.split("\t", 1)
                types[p[0]] = p[1] if len(p) > 1 else ""
        calls_ok = "Linked to BigCapital account: Cost of Goods Sold" in types.get("Calls", "")
        food_ok = "Linked to BigCapital account: Advertising Expense" in types.get("Food", "")
        check("15. HRMS expense types", 2, calls_ok and food_ok,
              f"Calls={'ok' if calls_ok else types.get('Calls', 'missing')}, "
              f"Food={'ok' if food_ok else types.get('Food', 'missing')}")
    except Exception as e:
        check("15. HRMS expense types", 2, False, f"exception: {e}")


def check_16_expense_claim() -> None:
    """Submitted expense claim for HR-EMP-00010 (Deepika Joshi): claim total 210,
    exactly 1 detail line (Calls, 210, referencing MISC-EXP-2025-004)."""
    try:
        out = hrms_sql(
            "SELECT ec.name, ec.employee_name, ec.posting_date, "
            "ec.total_claimed_amount, ec.docstatus, "
            "ecd.expense_type, ecd.amount, ecd.description "
            "FROM `tabExpense Claim` ec "
            "JOIN `tabExpense Claim Detail` ecd ON ecd.parent = ec.name "
            "WHERE ec.employee = 'HR-EMP-00010' "
            "AND ec.posting_date = '2025-10-29'"
        )
        if not out:
            check("16. HRMS expense claim", 2, False,
                  "claim not found for employee HR-EMP-00010 on 2025-10-29")
            return

        # Group detail rows by claim name; pass if any claim satisfies all gates.
        claims: dict[str, dict] = {}
        for line in out.split("\n"):
            p = line.split("\t")
            if len(p) < 8:
                continue
            c = claims.setdefault(p[0], {
                "emp": p[1], "pdate": p[2],
                "total": p[3], "docstatus": p[4], "details": [],
            })
            c["details"].append({"type": p[5], "amount": p[6], "desc": p[7]})

        def _claim_ok(c: dict) -> bool:
            try:
                total = float(c["total"]) if c["total"] not in ("", "NULL") else 0.0
                docstatus = int(c["docstatus"]) if c["docstatus"] not in ("", "NULL") else 0
            except ValueError:
                return False
            if not (c["emp"] == "Deepika Joshi"
                    and "2025-10-29" in c["pdate"]
                    and abs(total - 210) < 0.01
                    and docstatus == 1
                    and len(c["details"]) == 1):
                return False
            d = c["details"][0]
            try:
                amt = float(d["amount"]) if d["amount"] not in ("", "NULL") else 0.0
            except ValueError:
                return False
            return (d["type"] == "Calls"
                    and abs(amt - 210) < 0.01
                    and "MISC-EXP-2025-004" in d["desc"])

        ok = any(_claim_ok(c) for c in claims.values())
        summary = [
            f"{name}: emp={c['emp']}, date={c['pdate']}, total={c['total']}, "
            f"docstatus={c['docstatus']}, details={len(c['details'])}"
            for name, c in claims.items()
        ]
        check("16. HRMS expense claim", 2, ok, "; ".join(summary) or "no rows")
    except Exception as e:
        check("16. HRMS expense claim", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    print("Discovering BigCapital DB...", file=sys.stderr)
    discover_bc_db()
    print(f"  -> tenant DB: {_bc_tenant_db}, user: {_bc_db_user}", file=sys.stderr)

    print("Discovering Twenty workspace schema...", file=sys.stderr)
    discover_twenty_schema()
    print(f"  -> DB={_twenty_db}, user={_twenty_user}, schema={_twenty_schema}",
          file=sys.stderr)

    print("Discovering HRMS DB...", file=sys.stderr)
    discover_hrms_db()
    print(f"  -> DB: {_hrms_db}, user: {_hrms_db_user}", file=sys.stderr)

    check_1_vendors()
    check_2_items()
    check_3_bill_vantage()
    check_4_bill_luminary()
    check_5_payment_vantage()
    check_6_payment_luminary()
    check_7_purchase_totals()
    check_8_expense()
    check_9_bank_rule()
    check_10_gl_credits()
    check_11_twenty_companies()
    check_12_twenty_persons()
    check_13_twenty_note()
    check_14_twenty_favorites()
    check_15_expense_types()
    check_16_expense_claim()

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
