"""
Verifier for Business-155-I1: Employee Grievance Handling Workflow

Checks: 17 weighted checks across hrms, bigcapital, pretix, twenty (total weight 28).
Strategy: docker exec DB queries for all sites.

Required env vars:
  SERVER_HOSTNAME,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import os
import sys
import subprocess
import json
import re
from datetime import datetime, timedelta, timezone

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.environ.get("HRMS_DB_CONTAINER")

BIGCAPITAL_PORT = os.environ.get("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

PRETIX_PORT = os.environ.get("PRETIX_PORT")
PRETIX_CONTAINER = os.environ.get("PRETIX_CONTAINER")
PRETIX_DB_CONTAINER = os.environ.get("PRETIX_DB_CONTAINER")

TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")

_required = {
    "HRMS_PORT": HRMS_PORT, "HRMS_CONTAINER": HRMS_CONTAINER, "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT, "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BIGCAPITAL_DB_CONTAINER,
    "PRETIX_PORT": PRETIX_PORT, "PRETIX_CONTAINER": PRETIX_CONTAINER, "PRETIX_DB_CONTAINER": PRETIX_DB_CONTAINER,
    "TWENTY_PORT": TWENTY_PORT, "TWENTY_CONTAINER": TWENTY_CONTAINER, "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
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


_hrms_db_cache: str | None = None


def _hrms_find_db() -> str:
    """Find the Frappe site DB — the underscore-prefixed DB that contains tabDocType."""
    global _hrms_db_cache
    if _hrms_db_cache:
        return _hrms_db_cache
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "-u", "root", "-phrms123456",
        "--default-character-set=utf8mb4", "-N", "-e",
        "SHOW DATABASES LIKE '\\_%'"
    )
    dbs = [line.strip() for line in out.strip().splitlines() if line.strip()]
    for db in dbs:
        rc2, out2, _ = docker_exec(
            HRMS_DB_CONTAINER,
            "mysql", "-u", "root", "-phrms123456",
            "--default-character-set=utf8mb4", db, "-N", "-e",
            "SELECT COUNT(*) FROM information_schema.tables "
            f"WHERE table_schema='{db}' AND table_name='tabEmployee'"
        )
        if out2.strip() == "1":
            _hrms_db_cache = db
            return db
    _hrms_db_cache = dbs[0] if dbs else "_frappe_bench"
    return _hrms_db_cache


def hrms_sql(query: str) -> str:
    """Run SQL on the HRMS MariaDB. Auto-discovers the Frappe site DB."""
    db_name = _hrms_find_db()
    rc, out, err = docker_exec(
        HRMS_DB_CONTAINER,
        "mysql", "-u", "root", "-phrms123456",
        "--default-character-set=utf8mb4", db_name, "-N", "-e", query
    )
    if rc != 0:
        raise RuntimeError(f"HRMS SQL error: {err.strip()}")
    return out.strip()


_bc_db_cache: str | None = None


def _bc_find_tenant_db() -> str:
    """Find the BigCapital tenant DB name."""
    global _bc_db_cache
    if _bc_db_cache:
        return _bc_db_cache
    # BigCapital embeds MariaDB in the app container OR uses a separate DB container.
    # Try app container first, then DB container.
    for container in (BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER):
        rc, out, err = docker_exec(
            container,
            "mysql", "-u", "root", "-N", "-e",
            "SHOW DATABASES LIKE 'bigcapital_tenant_%'"
        )
        dbs = [line.strip() for line in out.strip().splitlines() if line.strip()]
        if dbs:
            _bc_db_cache = dbs[0]
            return _bc_db_cache
    _bc_db_cache = "bigcapital"
    return _bc_db_cache


def _bc_container() -> str:
    """Return the container that has a working mysql client with the tenant DB."""
    db = _bc_find_tenant_db()
    for container in (BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER):
        rc, out, err = docker_exec(
            container,
            "mysql", "-u", "root", "-N", db, "-e", "SELECT 1"
        )
        if rc == 0:
            return container
    return BIGCAPITAL_CONTAINER


def bigcapital_sql(query: str) -> str:
    """Run SQL on BigCapital MariaDB. Auto-discovers tenant DB and container."""
    db_name = _bc_find_tenant_db()
    container = _bc_container()
    rc, out, err = docker_exec(
        container,
        "mysql", "-u", "root", "-N", db_name, "-e", query
    )
    if rc != 0:
        raise RuntimeError(f"BigCapital SQL error: {err.strip()}")
    return out.strip()


def pretix_sql(query: str) -> str:
    rc, out, err = docker_exec(
        PRETIX_DB_CONTAINER,
        "psql", "-U", "pretix", "-d", "pretix", "-t", "-A", "-c", query
    )
    if rc != 0:
        raise RuntimeError(f"Pretix SQL error: {err.strip()}")
    return out.strip()


def twenty_sql(query: str) -> str:
    """Run SQL on Twenty Postgres. Auto-discovers workspace schema."""
    find_schema = (
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';"
    )
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", find_schema
    )
    schema = out.strip().split("\n")[0].strip() if out.strip() else "public"
    full_query = f'SET search_path TO "{schema}"; {query}'
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", full_query
    )
    if rc != 0:
        raise RuntimeError(f"Twenty SQL error: {err.strip()}")
    # Strip the "SET" line from SET search_path output
    lines = out.strip().splitlines()
    result_lines = [l for l in lines if l.strip() and l.strip() != "SET"]
    return "\n".join(result_lines)


def _norm(s: str) -> str:
    """Normalize text for comparison: em/en/figure dashes -> '-', HTML '&amp;' -> '&',
    collapse whitespace."""
    s = (s or "")
    for dash in ("—", "–", "‒", "‐", "‑", "−"):
        s = s.replace(dash, "-")
    s = s.replace("&amp;", "&")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _i18n(val: str) -> str:
    """Decode a pretix i18n value ('{"en": "X"}' or plain string) to plain text."""
    val = (val or "").strip()
    try:
        obj = json.loads(val)
        if isinstance(obj, dict):
            for v in obj.values():
                if v:
                    return str(v).strip()
            return ""
        if isinstance(obj, str):
            return obj.strip()
    except (ValueError, TypeError):
        pass
    return val


def date_matches_tz(stored: str, exp_date: str) -> bool:
    """True if a stored value corresponds to local calendar date exp_date.

    Pure date values (exactly 10 chars) must equal exp_date. Timestamp-shaped
    values (longer than 10 chars, e.g. Twenty's UTC '2025-07-11 16:00:00+00')
    only pass if stored::date == exp_date, or (stored + local UTC offset)::date
    == exp_date, where the offset is the verifier host's local timezone offset.
    No raw-substring branch for timestamps.
    """
    stored = (stored or "").strip()
    if len(stored) == 10:
        return stored == exp_date
    if len(stored) < 10:
        return False
    raw = stored[:19].replace("T", " ")
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.strptime(stored[:10], "%Y-%m-%d")
        except ValueError:
            return False
    if dt.strftime("%Y-%m-%d") == exp_date:
        return True
    offset = datetime.now(timezone.utc).astimezone().utcoffset() or timedelta(0)
    return (dt + offset).strftime("%Y-%m-%d") == exp_date


# ── HRMS Checks ───────────────────────────────────────────────────────────────
def check_1_grievance_types() -> None:
    """Grievance types 'Workplace Harassment' and 'Retaliation' exist."""
    try:
        out = hrms_sql(
            "SELECT name FROM `tabGrievance Type` "
            "WHERE name IN ('Workplace Harassment', 'Retaliation')"
        )
        found = set(line.strip() for line in out.splitlines() if line.strip())
        has_wh = "Workplace Harassment" in found
        has_ret = "Retaliation" in found
        check("1. Grievance types exist", 1, has_wh and has_ret,
              f"found={found}")
    except Exception as e:
        check("1. Grievance types exist", 1, False, f"exception: {e}")


def check_2_employee_grievance() -> None:
    """Employee Grievance: Pooja vs Arjun, exact subject/type/status/party + description phrases."""
    try:
        out = hrms_sql(
            "SELECT grievance_type, subject, status, grievance_against_party, grievance_against, "
            "REGEXP_REPLACE(COALESCE(description,''), '[[:space:]]+', ' ') "
            "FROM `tabEmployee Grievance` "
            "WHERE raised_by='HR-EMP-00008' "
            "LIMIT 5"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        found_match = False
        detail = f"rows={len(lines)}"
        for line in lines:
            cols = line.split("\t")
            if len(cols) >= 6:
                g_type, subject, status, party_type, against, desc = [c.strip() for c in cols[:6]]
                desc_n = _norm(desc)
                if (g_type == "Workplace Harassment"
                        and _norm(subject) == "Repeated hostile behavior in team meetings"
                        and status == "Open"
                        and party_type == "Employee"
                        and against == "HR-EMP-00011"
                        and "hostile and intimidating behavior" in desc_n
                        and "unsafe work environment" in desc_n):
                    found_match = True
                    detail = f"type={g_type}, status={status}, party={party_type}, against={against}"
                    break
        check("2. Employee Grievance record", 2, found_match, detail)
    except Exception as e:
        check("2. Employee Grievance record", 2, False, f"exception: {e}")


def check_3_employee_transfer() -> None:
    """Submitted Employee Transfer for Arjun Nair: Department S&M -> Customer Service,
    date 2025-07-01, plus recomputed truth: tabEmployee.department actually changed."""
    try:
        out = hrms_sql(
            "SELECT t.name, t.docstatus, t.transfer_date, d.property, d.current, d.new "
            "FROM `tabEmployee Transfer` t "
            "LEFT JOIN `tabEmployee Property History` d ON d.parent = t.name AND d.parenttype = 'Employee Transfer' "
            "WHERE t.employee = 'HR-EMP-00011' "
            "ORDER BY t.creation DESC LIMIT 10"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        found_ok = False
        detail = f"rows={len(lines)}"
        for line in lines:
            cols = line.split("\t")
            if len(cols) >= 6:
                name, docstatus, tdate, prop, current_val, new_val = [c.strip() for c in cols[:6]]
                if (docstatus == "1"
                        and prop == "Department"
                        and "Sales & Marketing - TVS" in _norm(current_val)
                        and _norm(new_val) == "Customer Service - TVS"
                        and tdate[:10] == "2025-07-01"):
                    found_ok = True
                    detail = f"docstatus={docstatus}, date={tdate}, {current_val} -> {new_val}"
                    break
        dept = hrms_sql("SELECT department FROM `tabEmployee` WHERE name='HR-EMP-00011'").strip()
        dept_ok = _norm(dept) == "Customer Service - TVS"
        detail += f", employee_dept={dept}"
        check("3. Employee Transfer submitted", 2, found_ok and dept_ok, detail)
    except Exception as e:
        check("3. Employee Transfer submitted", 2, False, f"exception: {e}")


def check_4_training_program() -> None:
    """Training Program 'Workplace Policy Compliance 2025' with the required description."""
    try:
        out = hrms_sql(
            "SELECT name, REGEXP_REPLACE(COALESCE(description,''), '[[:space:]]+', ' ') "
            "FROM `tabTraining Program` "
            "WHERE name = 'Workplace Policy Compliance 2025'"
        )
        line = out.splitlines()[0] if out.strip() else ""
        cols = line.split("\t")
        found = bool(line.strip())
        desc = _norm(cols[1]) if len(cols) >= 2 else ""
        desc_ok = "triggered by grievance investigation" in desc
        check("4. Training Program exists", 1, found and desc_ok,
              f"found={'yes' if found else 'no'}, desc_ok={desc_ok}")
    except Exception as e:
        check("4. Training Program exists", 1, False, f"exception: {e}")


def check_5_training_event() -> None:
    """Training Event: Workshop, program, start date 2025-07-15, exactly the 3 named participants."""
    exp_ids = {"HR-EMP-00001", "HR-EMP-00008", "HR-EMP-00011"}
    exp_names = {"Rajesh Kumar", "Pooja Malhotra", "Arjun Nair"}
    try:
        out = hrms_sql(
            "SELECT te.event_name, te.type, te.training_program, DATE(te.start_time), "
            "GROUP_CONCAT(tee.employee SEPARATOR ',') as employees, "
            "GROUP_CONCAT(tee.employee_name SEPARATOR '|') as employee_names "
            "FROM `tabTraining Event` te "
            "LEFT JOIN `tabTraining Event Employee` tee ON tee.parent = te.name "
            "WHERE te.event_name LIKE '%Policy Awareness Workshop%' "
            "GROUP BY te.name LIMIT 5"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        found_ok = False
        detail = f"rows={len(lines)}"
        for line in lines:
            cols = line.split("\t")
            if len(cols) >= 6:
                ename, etype, prog, sdate, emps, enames = [c.strip() for c in cols[:6]]
                id_set = set(e.strip() for e in emps.split(",") if e.strip()) if emps not in ("", "NULL") else set()
                name_set = set(_norm(n) for n in enames.split("|") if n.strip()) if enames not in ("", "NULL") else set()
                is_workshop = etype == "Workshop"
                prog_ok = prog == "Workplace Policy Compliance 2025"
                date_ok = sdate[:10] == "2025-07-15"
                emp_ok = (id_set == exp_ids) or (name_set == exp_names)
                detail = (f"type={etype}, program={prog}, start={sdate}, "
                          f"participants={sorted(id_set) or sorted(name_set)}")
                if is_workshop and prog_ok and date_ok and emp_ok:
                    found_ok = True
                    break
        check("5. Training Event with 3 participants", 2, found_ok, detail)
    except Exception as e:
        check("5. Training Event with 3 participants", 2, False, f"exception: {e}")


# ── BigCapital Checks ─────────────────────────────────────────────────────────
def check_6_expense_account() -> None:
    """Expense account 'Legal and Advisory Fees' exists with type 'expense'."""
    try:
        out = bigcapital_sql(
            "SELECT ID, NAME, ACCOUNT_TYPE FROM ACCOUNTS "
            "WHERE NAME = 'Legal and Advisory Fees' LIMIT 1"
        )
        cols = out.splitlines()[0].split("\t") if out.strip() else []
        found = len(cols) >= 3
        acct_type = cols[2].strip() if found else ""
        type_ok = acct_type.lower() == "expense"
        check("6. Expense account exists", 1, found and type_ok,
              f"found={'yes' if found else 'no'}, type={acct_type or 'n/a'}")
    except Exception as e:
        check("6. Expense account exists", 1, False, f"exception: {e}")


def _check_expense(label: str, weight: int, pay_date: str, exp_amount: float,
                   ref_phrase: str) -> None:
    """Shared shape for ck7/ck8: amount + payment account + published + reference, same row."""
    try:
        out = bigcapital_sql(
            "SELECT e.TOTAL_AMOUNT, e.PAYMENT_DATE, e.REFERENCE_NO, e.PUBLISHED_AT, pa.NAME "
            "FROM EXPENSES_TRANSACTIONS e "
            "JOIN EXPENSE_TRANSACTION_CATEGORIES c ON c.EXPENSE_ID = e.ID "
            "JOIN ACCOUNTS a ON a.ID = c.EXPENSE_ACCOUNT_ID "
            "JOIN ACCOUNTS pa ON pa.ID = e.PAYMENT_ACCOUNT_ID "
            "WHERE a.NAME = 'Legal and Advisory Fees' "
            f"AND e.PAYMENT_DATE = '{pay_date}' "
            "LIMIT 5"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        found_ok = False
        detail = f"rows={len(lines)}"
        for line in lines:
            cols = line.split("\t")
            if len(cols) >= 5:
                amount_str, date, ref, published, pay_acct = [c.strip() for c in cols[:5]]
                try:
                    amount = float(amount_str)
                except ValueError:
                    continue
                amount_ok = abs(amount - exp_amount) < 1
                acct_ok = pay_acct == "Bank Account"
                published_ok = bool(published) and published.upper() != "NULL"
                ref_ok = ref_phrase in _norm(ref)
                detail = (f"amount={amount}, date={date}, pay_acct={pay_acct}, "
                          f"published={published_ok}, ref={ref[:50]}")
                if amount_ok and acct_ok and published_ok and ref_ok:
                    found_ok = True
                    break
        check(label, weight, found_ok, detail)
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_7_investigation_expense() -> None:
    """Expense: 2800 on 2025-07-05, Bank Account, published, investigation-advisory reference."""
    _check_expense("7. Investigation expense (2800)", 2, "2025-07-05", 2800,
                   "External investigation advisory")


def check_8_mediation_expense() -> None:
    """Expense: 1700 on 2025-07-20, Bank Account, published, mediation-services reference."""
    _check_expense("8. Mediation expense (1700)", 2, "2025-07-20", 1700,
                   "Mediation services")


def check_gl_recompute() -> None:
    """GL/P&L truth recomputation: exactly 2 published entries totaling 4500 in range."""
    try:
        out = bigcapital_sql(
            "SELECT COUNT(*), COALESCE(SUM(c.AMOUNT), 0) "
            "FROM EXPENSES_TRANSACTIONS e "
            "JOIN EXPENSE_TRANSACTION_CATEGORIES c ON c.EXPENSE_ID = e.ID "
            "JOIN ACCOUNTS a ON a.ID = c.EXPENSE_ACCOUNT_ID "
            "WHERE a.NAME = 'Legal and Advisory Fees' "
            "AND e.PAYMENT_DATE BETWEEN '2025-07-05' AND '2025-07-20' "
            "AND e.PUBLISHED_AT IS NOT NULL"
        )
        cols = out.splitlines()[0].split("\t") if out.strip() else []
        cnt = int(cols[0]) if len(cols) >= 1 and cols[0].strip().isdigit() else -1
        try:
            total = float(cols[1]) if len(cols) >= 2 else -1.0
        except ValueError:
            total = -1.0
        ok = cnt == 2 and abs(total - 4500) < 0.01
        check("9. GL/P&L recompute (2 entries, 4500 total)", 1, ok,
              f"count={cnt}, total={total}")
    except Exception as e:
        check("9. GL/P&L recompute (2 entries, 4500 total)", 1, False, f"exception: {e}")


# ── Pretix Checks ─────────────────────────────────────────────────────────────
def check_9_10_pretix_event_live() -> None:
    """Event 'policy-compliance-workshop' exists, live, start date 2025-07-15, currency USD."""
    try:
        out = pretix_sql(
            "SELECT e.live, e.currency, e.date_from::text, "
            "COALESCE((SELECT s.value FROM pretixbase_event_settingsstore s "
            "          WHERE s.object_id = e.id AND s.key = 'timezone' LIMIT 1), 'UTC') "
            "FROM pretixbase_event e "
            "WHERE e.slug = 'policy-compliance-workshop' LIMIT 1"
        )
        if not out.strip():
            check("10. Pretix event exists, live, date, currency", 1, False, "event not found")
            return
        parts = [p.strip() for p in out.splitlines()[0].split("|")]
        live = parts[0] if len(parts) > 0 else ""
        currency = parts[1] if len(parts) > 1 else ""
        date_from = parts[2] if len(parts) > 2 else ""
        tz_name = parts[3] if len(parts) > 3 else "UTC"
        is_live = live.lower() in ("t", "true", "1")
        currency_ok = currency == "USD"
        date_ok = date_from.startswith("2025-07-15")
        if not date_ok and date_from:
            # Stored UTC; convert to the event's configured timezone.
            try:
                from zoneinfo import ZoneInfo
                dt = datetime.strptime(
                    date_from[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
                date_ok = dt.astimezone(ZoneInfo(tz_name)).strftime("%Y-%m-%d") == "2025-07-15"
            except Exception:
                date_ok = False
        check("10. Pretix event exists, live, date, currency", 1,
              is_live and currency_ok and date_ok,
              f"live={live}, currency={currency}, date_from={date_from}, tz={tz_name}")
    except Exception as e:
        check("10. Pretix event exists, live, date, currency", 1, False, f"exception: {e}")


def check_11_pretix_product() -> None:
    """Product 'Policy Training Admission' priced at 0."""
    try:
        out = pretix_sql(
            "SELECT i.default_price FROM pretixbase_item i "
            "JOIN pretixbase_event e ON e.id = i.event_id "
            "WHERE e.slug = 'policy-compliance-workshop' "
            "AND i.name::text LIKE '%Policy Training Admission%' "
            "LIMIT 1"
        )
        price_str = out.strip()
        try:
            price = float(price_str)
            is_free = abs(price) < 0.01
        except (ValueError, TypeError):
            is_free = False
        check("11. Product priced at 0", 1, is_free, f"price={price_str}")
    except Exception as e:
        check("11. Product priced at 0", 1, False, f"exception: {e}")


def check_12_pretix_quota() -> None:
    """Quota 'Training Capacity' size 50, linked exactly to 'Policy Training Admission'."""
    try:
        out = pretix_sql(
            "SELECT q.size, COALESCE(i.name::text, '') "
            "FROM pretixbase_quota q "
            "JOIN pretixbase_event e ON e.id = q.event_id "
            "LEFT JOIN pretixbase_quota_items qi ON qi.quota_id = q.id "
            "LEFT JOIN pretixbase_item i ON i.id = qi.item_id "
            "WHERE e.slug = 'policy-compliance-workshop' "
            "AND q.name LIKE '%Training Capacity%'"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        size_ok = False
        items: set[str] = set()
        for line in lines:
            parts = line.split("|")
            if len(parts) >= 2:
                try:
                    size_ok = int(parts[0].strip()) == 50
                except ValueError:
                    size_ok = False
                item_name = _i18n(parts[1])
                if item_name:
                    items.add(item_name)
        items_ok = len(items) == 1 and any("Policy Training Admission" in i for i in items)
        check("12. Quota size 50 linked to product", 1, size_ok and items_ok,
              f"rows={len(lines)}, size_ok={size_ok}, items={sorted(items)}")
    except Exception as e:
        check("12. Quota size 50 linked to product", 1, False, f"exception: {e}")


def check_13_pretix_questions() -> None:
    """Questions: 'Employee ID' (text S, required) + 'Department' (choice C, required,
    exact option set), both linked to 'Policy Training Admission'."""
    try:
        out = pretix_sql(
            "SELECT q.id, q.type, q.required, q.question::text "
            "FROM pretixbase_question q "
            "JOIN pretixbase_event e ON e.id = q.event_id "
            "WHERE e.slug = 'policy-compliance-workshop'"
        )
        lines = [l for l in out.splitlines() if l.strip()]
        empid_qid = None
        dept_qid = None
        for line in lines:
            cols = line.split("|", 3)
            if len(cols) >= 4:
                qid, qtype, required, qtext_raw = [c.strip() for c in cols[:4]]
                qtext = _i18n(qtext_raw)
                req_ok = required in ("t", "True", "1")
                if "Employee ID" in qtext and qtype == "S" and req_ok:
                    empid_qid = qid
                if "Department" in qtext and qtype == "C" and req_ok:
                    dept_qid = qid

        def _linked_to_product(qid: str) -> bool:
            out2 = pretix_sql(
                "SELECT i.name::text FROM pretixbase_question_items qi "
                "JOIN pretixbase_item i ON i.id = qi.item_id "
                f"WHERE qi.question_id = {qid}"
            )
            return any("Policy Training Admission" in _i18n(l)
                       for l in out2.splitlines() if l.strip())

        opts_ok = False
        if dept_qid:
            out3 = pretix_sql(
                "SELECT answer FROM pretixbase_questionoption "
                f"WHERE question_id = {dept_qid}"
            )
            opts = set(_i18n(l) for l in out3.splitlines() if l.strip())
            opts_ok = opts == {"Human Resources", "Sales & Marketing", "Customer Service"}

        empid_ok = bool(empid_qid) and _linked_to_product(empid_qid)
        dept_ok = bool(dept_qid) and opts_ok and _linked_to_product(dept_qid)
        check("13. Custom questions (Employee ID + Department)", 2,
              empid_ok and dept_ok,
              f"employee_id={'yes' if empid_ok else 'no'}, department={'yes' if dept_ok else 'no'}, "
              f"options_ok={opts_ok}")
    except Exception as e:
        check("13. Custom questions (Employee ID + Department)", 2, False, f"exception: {e}")


def check_14_pretix_checkin() -> None:
    """Check-in list 'Training Attendance Check-in' covering 'Policy Training Admission'."""
    try:
        out = pretix_sql(
            "SELECT cl.id, cl.all_products FROM pretixbase_checkinlist cl "
            "JOIN pretixbase_event e ON e.id = cl.event_id "
            "WHERE e.slug = 'policy-compliance-workshop' "
            "AND cl.name LIKE '%Training Attendance Check-in%' LIMIT 1"
        )
        if not out.strip():
            check("14. Check-in list exists", 1, False, "check-in list not found")
            return
        parts = [p.strip() for p in out.splitlines()[0].split("|")]
        cl_id = parts[0] if parts else ""
        all_products = parts[1] if len(parts) > 1 else ""
        covers = all_products.lower() in ("t", "true", "1")
        if not covers and cl_id:
            out2 = pretix_sql(
                "SELECT i.name::text FROM pretixbase_checkinlist_limit_products lp "
                "JOIN pretixbase_item i ON i.id = lp.item_id "
                f"WHERE lp.checkinlist_id = {cl_id}"
            )
            covers = any("Policy Training Admission" in _i18n(l)
                         for l in out2.splitlines() if l.strip())
        check("14. Check-in list exists", 1, covers,
              f"all_products={all_products}, covers_product={covers}")
    except Exception as e:
        check("14. Check-in list exists", 1, False, f"exception: {e}")


# ── Twenty CRM Checks ────────────────────────────────────────────────────────
def _twenty_task(label: str, weight: int, like: str, exp_title: str,
                 exp_due: str, body_phrases: list[str]) -> None:
    """Shared shape for ck15-17: exact title + tz-safe due date + AND'd body phrases."""
    try:
        out = twenty_sql(
            f"SELECT id, \"dueAt\", title FROM task "
            f"WHERE title LIKE '{like}' AND \"deletedAt\" IS NULL LIMIT 1"
        )
        if not out.strip():
            check(label, weight, False, "task not found")
            return
        parts = out.splitlines()[0].split("|", 2)
        task_id = parts[0].strip() if parts else ""
        due = parts[1].strip() if len(parts) > 1 else ""
        title = parts[2].strip() if len(parts) > 2 else ""
        title_ok = _norm(title) == _norm(exp_title)
        due_ok = date_matches_tz(due, exp_due)
        body = twenty_sql(
            f"SELECT \"bodyV2Markdown\" FROM task WHERE id = '{task_id}'"
        )
        body_n = _norm(body)
        missing = [p for p in body_phrases if p not in body_n]
        body_ok = not missing
        check(label, weight, title_ok and due_ok and body_ok,
              f"title_ok={title_ok}, due={due}, due_ok={due_ok}, "
              f"body_missing={missing if missing else 'none'}")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_15_twenty_task_investigation() -> None:
    """Task: 'CONFIDENTIAL: Investigate grievance - ...' due 2025-07-12 with full body."""
    _twenty_task(
        "15. Twenty task: investigation", 2,
        "%Investigate grievance%hostile behavior%",
        "CONFIDENTIAL: Investigate grievance - Repeated hostile behavior in team meetings",
        "2025-07-12",
        ["Pooja Malhotra (HR-EMP-00008)", "Arjun Nair (HR-EMP-00011)",
         "Workplace Harassment", "2800", "2025-07-12"],
    )


def check_16_twenty_task_mediation() -> None:
    """Task: 'CONFIDENTIAL: Mediation session - ...' due 2025-07-25 with full body."""
    _twenty_task(
        "16. Twenty task: mediation", 2,
        "%Mediation session%Pooja Malhotra%",
        "CONFIDENTIAL: Mediation session - Pooja Malhotra and Arjun Nair",
        "2025-07-25",
        ["1700", "Customer Service - TVS", "2025-07-01",
         "Pooja Malhotra", "Arjun Nair"],
    )


def check_17_twenty_task_training() -> None:
    """Task: 'Mandatory compliance training - ...' due 2025-07-15 with full body."""
    _twenty_task(
        "17. Twenty task: compliance training", 2,
        "%Mandatory compliance training%",
        "Mandatory compliance training - Workplace Policy Compliance Workshop",
        "2025-07-15",
        ["2025-07-15", "3 employees", "Training Attendance Check-in",
         "100% attendance"],
    )


def check_18_twenty_note() -> None:
    """Note: 'Grievance Resolution Log - ... - 2025-07-12' with all facts in the body."""
    exp_title = ("Grievance Resolution Log - Repeated hostile behavior in team meetings "
                 "- 2025-07-12")
    body_phrases = [
        "HR-EMP-00008", "HR-EMP-00011", "Human Resources - TVS", "HR Executive",
        "Sales & Marketing - TVS", "Customer Service - TVS", "2025-07-01",
        "2800", "1700", "4500", "Legal and Advisory Fees",
        "Workplace Policy Compliance Workshop", "2025-07-15", "3 employees",
    ]
    try:
        out = twenty_sql(
            "SELECT id, title FROM note "
            "WHERE title LIKE '%Grievance Resolution Log%hostile behavior%' "
            "AND \"deletedAt\" IS NULL LIMIT 1"
        )
        if not out.strip():
            check("18. Twenty note: grievance resolution log", 2, False, "note not found")
            return
        parts = out.splitlines()[0].split("|", 1)
        note_id = parts[0].strip() if parts else ""
        title = parts[1].strip() if len(parts) > 1 else ""
        title_ok = _norm(title) == _norm(exp_title)
        body = twenty_sql(
            f"SELECT \"bodyV2Markdown\" FROM note WHERE id = '{note_id}'"
        )
        body_n = _norm(body)
        missing = [p for p in body_phrases if p not in body_n]
        check("18. Twenty note: grievance resolution log", 2,
              title_ok and not missing,
              f"title_ok={title_ok}, body_len={len(body)}, "
              f"body_missing={missing if missing else 'none'}")
    except Exception as e:
        check("18. Twenty note: grievance resolution log", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_grievance_types()
    check_2_employee_grievance()
    check_3_employee_transfer()
    check_4_training_program()
    check_5_training_event()
    check_6_expense_account()
    check_7_investigation_expense()
    check_8_mediation_expense()
    check_gl_recompute()
    check_9_10_pretix_event_live()
    check_11_pretix_product()
    check_12_pretix_quota()
    check_13_pretix_questions()
    check_14_pretix_checkin()
    check_15_twenty_task_investigation()
    check_16_twenty_task_mediation()
    check_17_twenty_task_training()
    check_18_twenty_note()

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
