"""
Verifier for Business-052-I5: Leadership Development Training Series
across Pretix, Twenty CRM, and BigCapital.

Checks: 15 weighted checks (25 total points).
Strategy: docker exec (Pretix Postgres, Twenty Postgres, BigCapital MariaDB).

Required env vars:
  SERVER_HOSTNAME,
  PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

PRETIX_PORT = os.environ.get("PRETIX_PORT")
PRETIX_CONTAINER = os.environ.get("PRETIX_CONTAINER")
PRETIX_DB_CONTAINER = os.environ.get("PRETIX_DB_CONTAINER")
TWENTY_PORT = os.environ.get("TWENTY_PORT")
TWENTY_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.environ.get("TWENTY_DB_CONTAINER")
BIGCAPITAL_PORT = os.environ.get("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BIGCAPITAL_DB_CONTAINER = os.environ.get("BIGCAPITAL_DB_CONTAINER")

_required = {
    "PRETIX_PORT": PRETIX_PORT,
    "PRETIX_CONTAINER": PRETIX_CONTAINER,
    "PRETIX_DB_CONTAINER": PRETIX_DB_CONTAINER,
    "TWENTY_PORT": TWENTY_PORT,
    "TWENTY_CONTAINER": TWENTY_CONTAINER,
    "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT,
    "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "BIGCAPITAL_DB_CONTAINER": BIGCAPITAL_DB_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
ORGANIZER = "nyc-cultural"
EVENT_CURRENCY = "USD"

EVENT1_SLUG = "leadership-comm-skills"
EVENT2_SLUG = "leadership-negotiation-influence"
EVENT3_SLUG = "leadership-strategy-exec"

EVENT1_DATE = "2026-08-12"
EVENT2_DATE = "2026-09-09"
EVENT3_DATE = "2026-10-07"

EVENT1_TICKET = "Leadership Communication Skills Ticket"
EVENT1_PRICE = 239.00
EVENT1_QUOTA_NAME = "Leadership Communication Skills Quota"
EVENT1_QUOTA_SIZE = 48
EVENT1_DISCOUNT_NAME = "Early Enrollment Discount"
EVENT1_DISCOUNT_PERCENT = 24.0  # task text: "24% percentage discount"

EVENT2_TICKET = "Leadership Negotiation Workshop Ticket"
EVENT2_PRICE = 289.00
EVENT2_QUOTA_NAME = "Leadership Negotiation Workshop Quota"
EVENT2_QUOTA_SIZE = 38
EVENT2_VOUCHER_CODE = "LDRNEGO17"
EVENT2_VOUCHER_PCT = 17
EVENT2_VOUCHER_MAX = 14
EVENT2_VOUCHER_VALID_UNTIL = "2026-09-09"

EVENT3_TICKET = "Leadership Strategy Masterclass Ticket"
EVENT3_PRICE = 369.00
EVENT3_QUOTA_NAME = "Leadership Strategy Masterclass Quota"
EVENT3_QUOTA_SIZE = 28
EVENT3_QUESTION = "Please describe your current leadership role and the size of the team you manage."

SERIES_TOTAL = (11 * 239.00) + (9 * 289.00) + (5 * 369.00)  # 7075.00

COMPANY_NAME = "Mediaocean"
OPP_TITLE = "Mediaocean Leadership Training Series 2026"
OPP_CLOSE_DATE = "2026-11-12"
CONTACT_FIRST = "Victoria"
CONTACT_LAST = "Lam"
CONTACT_EMAIL = "victoria.lam@mediaocean-training.com"
CONTACT_TITLE = "Head of People Development"

# Per-task specs: title ILIKE key (avoids '&' vs '&amp;' storage ambiguity),
# due date (date_matches_tz), and body substrings required (case-insensitive).
TASK_SPECS = [
    {
        "name": "Task1 Communication",
        "title_ilike": "%Communication Skills registration link%",
        "due": "2026-07-29",
        "body": ["2026-08-12", "239.00", "24%", CONTACT_EMAIL],
    },
    {
        "name": "Task2 Negotiation",
        "title_ilike": "%Negotiation%registration link%",
        "due": "2026-08-26",
        "body": ["2026-09-09", "289.00", "LDRNEGO17", "17%", CONTACT_EMAIL],
    },
    {
        "name": "Task3 Strategy",
        "title_ilike": "%Strategy%registration link%",
        "due": "2026-09-23",
        "body": ["2026-10-07", "369.00", CONTACT_EMAIL],
    },
]

BC_ITEM_NAME = "Leadership Development Training Series Package"
BC_CUSTOMER = "Mediaocean Training Account"
BC_ESTIMATE_DATE = "2026-07-25"
BC_ITEM_DESC_EVENTS = [
    "Leadership Communication Skills",
    "Leadership Negotiation & Influence Workshop",
    "Leadership Strategy & Executive Presence Masterclass",
]
# (quantity, rate, description keyword) per estimate line
BC_EXPECTED_LINES = [
    (11, 239.00, "Communication Skills"),
    (9, 289.00, "Negotiation"),
    (5, 369.00, "Strategy"),
]

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


def pretix_sql(query: str) -> str:
    """Run a SQL query against the Pretix Postgres DB and return stdout."""
    rc, out, err = docker_exec(
        PRETIX_DB_CONTAINER,
        "psql", "-U", "pretix", "-d", "pretix", "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"pretix psql error: {err.strip()}")
    return out.strip()


_twenty_conn: tuple[str, str] | None = None


def twenty_sql(query: str) -> str:
    """Run a SQL query against the Twenty Postgres DB and return stdout.

    Discovers a working (user, db) combo once via SELECT 1, then raises on any
    rc != 0 so real query errors are never swallowed as "not found".
    """
    global _twenty_conn
    if _twenty_conn is None:
        last_err = ""
        for user, db in [("twenty", "default"), ("twenty", "twenty"),
                         ("postgres", "default"), ("postgres", "twenty")]:
            rc, _, err = docker_exec(
                TWENTY_DB_CONTAINER,
                "psql", "-U", user, "-d", db, "-t", "-A", "-c", "SELECT 1;",
            )
            if rc == 0:
                _twenty_conn = (user, db)
                break
            last_err = err.strip()
        if _twenty_conn is None:
            raise RuntimeError(f"twenty psql: no working user/db combo: {last_err}")
    user, db = _twenty_conn
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER,
        "psql", "-U", user, "-d", db, "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql error: {err.strip()}")
    return out.strip()


_bc_tenant_db: str = ""


def _discover_bc_tenant_db() -> None:
    global _bc_tenant_db
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER, "mysql",
        "--default-character-set=utf8mb4",
        "-u", "bigcapital", "-pbigcapital123",
        "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE 'bigcapital_tenant_%' ORDER BY SCHEMA_NAME LIMIT 1;",
    )
    if rc == 0 and out.strip():
        _bc_tenant_db = out.strip().split("\n")[0]
    else:
        _bc_tenant_db = "bigcapital"


def bigcapital_sql(query: str) -> str:
    """Run a SQL query against the BigCapital MariaDB (auto-detects tenant DB)."""
    if not _bc_tenant_db:
        _discover_bc_tenant_db()
    rc, out, err = docker_exec(
        BIGCAPITAL_DB_CONTAINER, "mysql",
        "--default-character-set=utf8mb4",
        "-u", "bigcapital", "-pbigcapital123",
        "-D", _bc_tenant_db,
        "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error: {err.strip()}")
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


def name_str(val: str) -> str:
    """Extract text from a Pretix i18n JSON name field."""
    try:
        d = json.loads(val)
        if isinstance(d, dict):
            return d.get("en") or next(iter(d.values()), "")
    except (json.JSONDecodeError, TypeError):
        pass
    return val


def norm_text(s: str) -> str:
    """Normalize free text for comparison: HTML-escaped '&', em/en dashes,
    mysql -B escape sequences, whitespace folding, lowercase."""
    s = s or ""
    s = s.replace("&amp;", "&")
    s = s.replace("—", "-").replace("–", "-").replace("--", "-")
    s = s.replace("\\n", " ").replace("\\t", " ")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def _local_utc_offset() -> timedelta:
    """UTC offset of the verifier host's local timezone (no hardcoded city)."""
    off = datetime.now().astimezone().utcoffset()
    return off if off is not None else timedelta(0)


def date_matches_tz(stored: str, expected: str) -> bool:
    """True if a stored date/timestamp denotes calendar day `expected` (YYYY-MM-DD).

    - Pure date (exactly 10 chars): strict equality.
    - Timestamp shape: parsed, then the date is accepted either as UTC or after
      shifting by the verifier host's local UTC offset. No bare substring match.
    """
    stored = (stored or "").strip()
    if not stored or stored.upper() == "NULL":
        return False
    if len(stored) == 10:
        return stored == expected
    iso = stored.replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", iso):
        iso += ":00"  # '+00' -> '+00:00' for pre-3.11 fromisoformat
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    utc_dt = dt.astimezone(timezone.utc)
    if utc_dt.date().isoformat() == expected:
        return True
    return (utc_dt + _local_utc_offset()).date().isoformat() == expected


# ── Pretix: event lookups ─────────────────────────────────────────────────────
def get_pretix_event_id(slug: str) -> tuple[int, bool]:
    """Return (event_id, is_live) for a given event slug under ORGANIZER."""
    row = pretix_sql(
        f"SELECT e.id, e.live FROM pretixbase_event e "
        f"JOIN pretixbase_organizer o ON e.organizer_id = o.id "
        f"WHERE e.slug = '{slug}' AND o.slug = '{ORGANIZER}';"
    )
    if not row:
        return -1, False
    parts = row.split("|")
    return int(parts[0]), parts[1].strip().lower() in ("t", "true")


def get_pretix_event_info(slug: str) -> tuple[int, bool, str, str]:
    """Return (event_id, is_live, currency, date_from-as-event-tz-date)."""
    row = pretix_sql(
        f"SELECT e.id, e.live, e.currency, "
        f"(e.date_from AT TIME ZONE COALESCE("
        f"(SELECT s.value FROM pretixbase_event_settingsstore s "
        f"WHERE s.object_id = e.id AND s.key = 'timezone'), 'UTC'))::date "
        f"FROM pretixbase_event e "
        f"JOIN pretixbase_organizer o ON e.organizer_id = o.id "
        f"WHERE e.slug = '{slug}' AND o.slug = '{ORGANIZER}';"
    )
    if not row:
        return -1, False, "", ""
    parts = row.split("\n")[0].split("|")
    eid = int(parts[0])
    live = parts[1].strip().lower() in ("t", "true")
    currency = parts[2].strip() if len(parts) > 2 else ""
    date_str = parts[3].strip() if len(parts) > 3 else ""
    return eid, live, currency, date_str


def _check_event_exists_live(label: str, weight: int, slug: str, exp_date: str) -> None:
    """Event exists under ORGANIZER, is live, currency USD, date_from == exp_date
    (in the event's own timezone)."""
    try:
        eid, live, currency, dstr = get_pretix_event_info(slug)
        ok = eid > 0 and live and currency == EVENT_CURRENCY and dstr == exp_date
        check(label, weight, ok,
              f"event_id={eid}, live={live}, currency={currency}, date_from={dstr}")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def _check_product_quota(label: str, weight: int, slug: str, ticket: str,
                         price: float, quota_name: str, quota_size: int) -> None:
    """Product at `price`; quota with `quota_name`+`quota_size` AND bound to
    `ticket` via pretixbase_quota_items (same-row assertion)."""
    try:
        eid, _ = get_pretix_event_id(slug)
        if eid < 0:
            check(label, weight, False, "event not found")
            return

        # Product: name + price
        item_row = pretix_sql(
            f"SELECT id, name, default_price FROM pretixbase_item "
            f"WHERE event_id = {eid};"
        )
        item_ok = False
        for line in item_row.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 3 and parts[2].strip():
                iname = name_str(parts[1].strip())
                iprice = float(parts[2].strip())
                if ticket.lower() in iname.lower() and abs(iprice - price) < 0.01:
                    item_ok = True

        # Quota: name + size + item binding, all asserted on the same row
        quota_row = pretix_sql(
            f"SELECT q.name, q.size FROM pretixbase_quota q "
            f"JOIN pretixbase_quota_items qi ON qi.quota_id = q.id "
            f"JOIN pretixbase_item i ON i.id = qi.item_id "
            f"WHERE q.event_id = {eid} AND i.name::text ILIKE '%{ticket}%';"
        )
        quota_ok = False
        for line in quota_row.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 2 and parts[1].strip():
                qname = parts[0].strip()
                qsize = int(parts[1].strip())
                if quota_name.lower() in qname.lower() and qsize == quota_size:
                    quota_ok = True

        ok = item_ok and quota_ok
        check(label, weight, ok,
              f"item_ok={item_ok}, quota_ok(linked)={quota_ok}")
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


# ── Check 1: Pretix Event 1 exists, live, USD, correct start date ─────────────
def check_1_event1_exists_live() -> None:
    _check_event_exists_live("1. Pretix Event 1 exists & live", 1,
                             EVENT1_SLUG, EVENT1_DATE)


# ── Check 2: Event 1 product + quota (quota linked to ticket) ─────────────────
def check_2_event1_product_quota() -> None:
    _check_product_quota("2. Event 1 product & quota", 2, EVENT1_SLUG,
                         EVENT1_TICKET, EVENT1_PRICE,
                         EVENT1_QUOTA_NAME, EVENT1_QUOTA_SIZE)


# ── Check 3: Event 1 discount rule (24%, scoped to the ticket) ────────────────
def check_3_event1_discount() -> None:
    """Discount 'Early Enrollment Discount': active, exactly 24%, applied to
    the Event 1 ticket (all-products or explicit limit-products binding)."""
    try:
        eid, _ = get_pretix_event_id(EVENT1_SLUG)
        if eid < 0:
            check("3. Event 1 discount rule", 2, False, "event not found")
            return

        row = pretix_sql(
            f"SELECT d.id, d.internal_name, d.active, "
            f"d.benefit_discount_matching_percent, "
            f"(d.condition_all_products OR EXISTS("
            f"SELECT 1 FROM pretixbase_discount_condition_limit_products dc "
            f"JOIN pretixbase_item i ON dc.item_id = i.id "
            f"WHERE dc.discount_id = d.id "
            f"AND i.name::text ILIKE '%{EVENT1_TICKET}%')) "
            f"FROM pretixbase_discount d WHERE d.event_id = {eid};"
        )
        found = False
        detail_parts = []
        for line in row.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 5:
                dname = parts[1].strip()
                active = parts[2].strip().lower() in ("t", "true")
                pct = float(parts[3].strip()) if parts[3].strip() else 0.0
                scope_ok = parts[4].strip().lower() in ("t", "true")
                if EVENT1_DISCOUNT_NAME.lower() in dname.lower():
                    value_ok = abs(pct - EVENT1_DISCOUNT_PERCENT) < 0.01
                    if active and value_ok and scope_ok:
                        found = True
                    detail_parts.append(
                        f"name={dname}, active={active}, pct={pct}, scope_ok={scope_ok}")

        if not detail_parts:
            detail_parts.append("discount not found")

        check("3. Event 1 discount rule", 2, found, "; ".join(detail_parts))
    except Exception as e:
        check("3. Event 1 discount rule", 2, False, f"exception: {e}")


# ── Check 4: Pretix Event 2 exists, live, USD, correct start date ─────────────
def check_4_event2_exists_live() -> None:
    _check_event_exists_live("4. Pretix Event 2 exists & live", 1,
                             EVENT2_SLUG, EVENT2_DATE)


# ── Check 5: Event 2 product + quota (quota linked to ticket) ─────────────────
def check_5_event2_product_quota() -> None:
    _check_product_quota("5. Event 2 product & quota", 2, EVENT2_SLUG,
                         EVENT2_TICKET, EVENT2_PRICE,
                         EVENT2_QUOTA_NAME, EVENT2_QUOTA_SIZE)


# ── Check 6: Event 2 voucher ─────────────────────────────────────────────────
def check_6_event2_voucher() -> None:
    """Voucher LDRNEGO17: 17% discount, max 14 usages, valid until 2026-09-09
    (event tz), linked to the Event 2 ticket (item_id or single-item quota)."""
    try:
        eid, _ = get_pretix_event_id(EVENT2_SLUG)
        if eid < 0:
            check("6. Event 2 voucher LDRNEGO17", 2, False, "event not found")
            return

        row = pretix_sql(
            f"SELECT v.code, v.price_mode, v.value, v.max_usages, "
            f"COALESCE((v.valid_until AT TIME ZONE COALESCE("
            f"(SELECT s.value FROM pretixbase_event_settingsstore s "
            f"WHERE s.object_id = v.event_id AND s.key = 'timezone'), 'UTC'"
            f"))::date::text, ''), "
            f"COALESCE((SELECT i.name::text FROM pretixbase_item i "
            f"WHERE i.id = v.item_id), ''), "
            f"COALESCE((SELECT string_agg(i2.name::text, ';') "
            f"FROM pretixbase_quota_items qi "
            f"JOIN pretixbase_item i2 ON i2.id = qi.item_id "
            f"WHERE qi.quota_id = v.quota_id), ''), "
            f"COALESCE((SELECT count(*) FROM pretixbase_quota_items qi "
            f"WHERE qi.quota_id = v.quota_id), 0) "
            f"FROM pretixbase_voucher v "
            f"WHERE v.event_id = {eid} AND v.code = '{EVENT2_VOUCHER_CODE}';"
        )
        if not row:
            check("6. Event 2 voucher LDRNEGO17", 2, False, "voucher not found")
            return

        parts = row.split("\n")[0].split("|")
        price_mode = parts[1].strip() if len(parts) > 1 else ""
        value = float(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 0.0
        max_usages = int(parts[3].strip()) if len(parts) > 3 and parts[3].strip() else 0
        valid_date = parts[4].strip() if len(parts) > 4 else ""
        item_name = parts[5].strip() if len(parts) > 5 else ""
        quota_names = parts[6].strip() if len(parts) > 6 else ""
        quota_item_count = int(parts[7].strip()) if len(parts) > 7 and parts[7].strip() else 0

        mode_ok = price_mode == "percent"
        val_ok = abs(value - EVENT2_VOUCHER_PCT) < 0.01
        max_ok = max_usages == EVENT2_VOUCHER_MAX
        valid_ok = valid_date == EVENT2_VOUCHER_VALID_UNTIL
        link_ok = (EVENT2_TICKET.lower() in item_name.lower()) or (
            quota_item_count == 1 and EVENT2_TICKET.lower() in quota_names.lower())
        ok = mode_ok and val_ok and max_ok and valid_ok and link_ok

        check("6. Event 2 voucher LDRNEGO17", 2, ok,
              f"mode={price_mode}, value={value}, max={max_usages}, "
              f"valid_until={valid_date}, link_ok={link_ok}")
    except Exception as e:
        check("6. Event 2 voucher LDRNEGO17", 2, False, f"exception: {e}")


# ── Check 7: Pretix Event 3 exists, live, USD, correct start date ─────────────
def check_7_event3_exists_live() -> None:
    _check_event_exists_live("7. Pretix Event 3 exists & live", 1,
                             EVENT3_SLUG, EVENT3_DATE)


# ── Check 8: Event 3 product + quota (quota linked to ticket) ─────────────────
def check_8_event3_product_quota() -> None:
    _check_product_quota("8. Event 3 product & quota", 2, EVENT3_SLUG,
                         EVENT3_TICKET, EVENT3_PRICE,
                         EVENT3_QUOTA_NAME, EVENT3_QUOTA_SIZE)


# ── Check 9: Event 3 custom question (bound to the ticket) ────────────────────
def check_9_event3_question() -> None:
    """Required one-line-text question exists on Event 3 and is linked to the
    Masterclass ticket via pretixbase_question_items."""
    try:
        eid, _ = get_pretix_event_id(EVENT3_SLUG)
        if eid < 0:
            check("9. Event 3 custom question", 1, False, "event not found")
            return

        row = pretix_sql(
            f"SELECT q.question, q.type, q.required "
            f"FROM pretixbase_question q "
            f"JOIN pretixbase_question_items qi ON qi.question_id = q.id "
            f"JOIN pretixbase_item i ON i.id = qi.item_id "
            f"WHERE q.event_id = {eid} "
            f"AND i.name::text ILIKE '%{EVENT3_TICKET}%';"
        )
        found = False
        for line in row.split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                qtext = name_str(parts[0].strip())
                qtype = parts[1].strip()
                qreq = parts[2].strip().lower() in ("t", "true")
                if EVENT3_QUESTION.lower() in qtext.lower() and qtype == "S" and qreq:
                    found = True

        check("9. Event 3 custom question", 1, found,
              "question found & linked to ticket" if found
              else "question not found, not linked, or wrong type/required")
    except Exception as e:
        check("9. Event 3 custom question", 1, False, f"exception: {e}")


# ── Check 10: Twenty opportunity ──────────────────────────────────────────────
def check_10_twenty_opportunity() -> None:
    """Opportunity: title, amount (~7075), stage PROPOSAL, close date
    2026-11-12, linked to company Mediaocean, not soft-deleted."""
    try:
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f"SELECT o.name, o.\"amountAmountMicros\", o.stage, "
            f"o.\"closeDate\"::text, c.name "
            f"FROM \"{ws}\".opportunity o "
            f"JOIN \"{ws}\".company c "
            f"ON o.\"companyId\" = c.id AND c.\"deletedAt\" IS NULL "
            f"WHERE o.name = '{OPP_TITLE}' AND o.\"deletedAt\" IS NULL;"
        )
        if not row:
            check("10. Twenty opportunity", 2, False, "opportunity not found")
            return

        parts = row.split("\n")[0].split("|")
        micros = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else 0
        stage = parts[2].strip().upper() if len(parts) > 2 else ""
        close_date = parts[3].strip() if len(parts) > 3 else ""
        company = parts[4].strip() if len(parts) > 4 else ""
        amount = micros / 1_000_000.0

        amount_ok = abs(amount - SERIES_TOTAL) < 1.0
        stage_ok = stage == "PROPOSAL"
        close_ok = date_matches_tz(close_date, OPP_CLOSE_DATE)
        company_ok = COMPANY_NAME.lower() in company.lower()
        ok = amount_ok and stage_ok and close_ok and company_ok

        check("10. Twenty opportunity", 2, ok,
              f"amount={amount}, stage={stage}, closeDate={close_date}, "
              f"company={company}")
    except Exception as e:
        check("10. Twenty opportunity", 2, False, f"exception: {e}")


# ── Check 11: Twenty person Victoria Lam ──────────────────────────────────────
def check_11_twenty_person() -> None:
    """Person Victoria Lam with correct email, job title, linked to Mediaocean,
    not soft-deleted."""
    try:
        ws = get_twenty_workspace_schema()
        row = twenty_sql(
            f"SELECT p.\"nameFirstName\", p.\"nameLastName\", "
            f"p.\"emailsPrimaryEmail\", p.\"jobTitle\", c.name "
            f"FROM \"{ws}\".person p "
            f"LEFT JOIN \"{ws}\".company c ON p.\"companyId\" = c.id "
            f"WHERE p.\"nameFirstName\" = '{CONTACT_FIRST}' "
            f"AND p.\"nameLastName\" = '{CONTACT_LAST}' "
            f"AND p.\"deletedAt\" IS NULL;"
        )
        if not row:
            check("11. Twenty person Victoria Lam", 2, False, "person not found")
            return

        parts = row.split("\n")[0].split("|")
        email = parts[2].strip() if len(parts) > 2 else ""
        title = parts[3].strip() if len(parts) > 3 else ""
        company = parts[4].strip() if len(parts) > 4 else ""

        email_ok = email.lower() == CONTACT_EMAIL.lower()
        title_ok = CONTACT_TITLE.lower() in title.lower()
        company_ok = COMPANY_NAME.lower() in company.lower()
        ok = email_ok and title_ok and company_ok

        check("11. Twenty person Victoria Lam", 2, ok,
              f"email={email}, title={title}, company={company}")
    except Exception as e:
        check("11. Twenty person Victoria Lam", 2, False, f"exception: {e}")


# ── Check 12: Twenty tasks ───────────────────────────────────────────────────
def check_12_twenty_tasks() -> None:
    """Three registration tasks linked to Mediaocean (per-task same-row
    assertion): title, due date (date_matches_tz), and body content
    (event date, price, discount/voucher info, contact email)."""
    try:
        ws = get_twenty_workspace_schema()

        cid = twenty_sql(
            f"SELECT id FROM \"{ws}\".company "
            f"WHERE name = '{COMPANY_NAME}' AND \"deletedAt\" IS NULL LIMIT 1;"
        )
        if not cid:
            check("12. Twenty 3 registration tasks", 2, False,
                  "company Mediaocean not found")
            return
        cid = cid.strip()

        failures = []
        for spec in TASK_SPECS:
            rows = twenty_sql(
                f"SELECT t.title, t.\"dueAt\"::text, "
                f"regexp_replace(COALESCE(t.\"bodyV2Markdown\", ''), "
                f"E'[\\n\\r]+', ' ', 'g') "
                f"FROM \"{ws}\".task t "
                f"JOIN \"{ws}\".\"taskTarget\" tt "
                f"ON t.id = tt.\"taskId\" AND tt.\"deletedAt\" IS NULL "
                f"WHERE tt.\"targetCompanyId\" = '{cid}' "
                f"AND t.\"deletedAt\" IS NULL "
                f"AND t.title ILIKE '{spec['title_ilike']}';"
            )
            task_ok = False
            reason = "task not found"
            for line in rows.split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|", 2)
                due_at = parts[1].strip() if len(parts) > 1 else ""
                body = norm_text(parts[2]) if len(parts) > 2 else ""
                due_ok = date_matches_tz(due_at, spec["due"])
                missing = [s for s in spec["body"] if norm_text(s) not in body]
                if due_ok and not missing:
                    task_ok = True
                    break
                reason = f"dueAt={due_at}(ok={due_ok}), body_missing={missing}"
            if not task_ok:
                failures.append(f"{spec['name']}: {reason}")

        ok = len(failures) == 0
        check("12. Twenty 3 registration tasks", 2, ok,
              "; ".join(failures) if failures else "all 3 tasks verified")
    except Exception as e:
        check("12. Twenty 3 registration tasks", 2, False, f"exception: {e}")


# ── Check 13: BigCapital item ─────────────────────────────────────────────────
def check_13_bc_item() -> None:
    """Item 'Leadership Development Training Series Package': type Service,
    sell price 7075, description mentions the training series and all 3 events."""
    try:
        row = bigcapital_sql(
            f"SELECT NAME, TYPE, SELL_PRICE, SELL_DESCRIPTION FROM ITEMS "
            f"WHERE NAME = '{BC_ITEM_NAME}';"
        )
        if not row:
            check("13. BigCapital service item", 1, False, "item not found")
            return

        parts = row.split("\n")[0].split("\t")
        itype = parts[1].strip() if len(parts) > 1 else ""
        sprice = float(parts[2].strip()) if len(parts) > 2 and parts[2].strip() else 0.0
        desc = norm_text("\t".join(parts[3:])) if len(parts) > 3 else ""

        type_ok = itype.lower() == "service"
        price_ok = abs(sprice - SERIES_TOTAL) < 1.0
        desc_ok = norm_text("Training series") in desc and all(
            norm_text(ev) in desc for ev in BC_ITEM_DESC_EVENTS)
        ok = type_ok and price_ok and desc_ok

        check("13. BigCapital service item", 1, ok,
              f"type={itype}, sell_price={sprice}, desc_ok={desc_ok}")
    except Exception as e:
        check("13. BigCapital service item", 1, False, f"exception: {e}")


# ── Check 14: BigCapital customer ─────────────────────────────────────────────
def check_14_bc_customer() -> None:
    """Customer 'Mediaocean Training Account' exists with the contact email."""
    try:
        row = bigcapital_sql(
            f"SELECT DISPLAY_NAME, EMAIL FROM CONTACTS "
            f"WHERE CONTACT_SERVICE = 'customer' AND DISPLAY_NAME = '{BC_CUSTOMER}';"
        )
        if not row:
            check("14. BigCapital customer", 1, False, "customer not found")
            return

        parts = row.split("\n")[0].split("\t")
        email = parts[1].strip() if len(parts) > 1 else ""
        ok = email.lower() == CONTACT_EMAIL.lower()
        check("14. BigCapital customer", 1, ok,
              f"email={email}")
    except Exception as e:
        check("14. BigCapital customer", 1, False, f"exception: {e}")


# ── Check 15: BigCapital estimate delivered with correct total & lines ────────
def check_15_bc_estimate() -> None:
    """Sales estimate for Mediaocean Training Account dated 2026-07-25,
    delivered, total 7075, with the three expected (quantity, rate) lines."""
    try:
        # Find customer ID
        cid_row = bigcapital_sql(
            f"SELECT ID FROM CONTACTS "
            f"WHERE CONTACT_SERVICE = 'customer' AND DISPLAY_NAME = '{BC_CUSTOMER}';"
        )
        if not cid_row:
            check("15. BigCapital estimate delivered", 3, False, "customer not found")
            return
        cid = cid_row.strip().split("\n")[0].strip()

        # Find the estimate (correct table name is SALES_ESTIMATES)
        est_row = bigcapital_sql(
            f"SELECT ID, AMOUNT, DELIVERED_AT FROM SALES_ESTIMATES "
            f"WHERE CUSTOMER_ID = {cid} AND ESTIMATE_DATE = '{BC_ESTIMATE_DATE}' "
            f"ORDER BY ID DESC LIMIT 1;"
        )
        if not est_row:
            check("15. BigCapital estimate delivered", 3, False,
                  f"estimate dated {BC_ESTIMATE_DATE} not found")
            return

        parts = est_row.split("\n")[0].split("\t")
        est_id = parts[0].strip()
        amount = float(parts[1].strip()) if len(parts) > 1 and parts[1].strip() else 0.0
        delivered_at = parts[2].strip() if len(parts) > 2 else ""

        delivered_ok = bool(delivered_at) and delivered_at.upper() != "NULL"
        amount_ok = abs(amount - SERIES_TOTAL) < 1.0

        # Verify line items: each expected (quantity, rate) must match a
        # distinct line. Description keywords are auxiliary (reported in
        # detail, not gated) since UI free text may use item names instead.
        lines_row = bigcapital_sql(
            f"SELECT QUANTITY, RATE, DESCRIPTION FROM ITEMS_ENTRIES "
            f"WHERE REFERENCE_TYPE = 'SaleEstimate' AND REFERENCE_ID = '{est_id}' "
            f"ORDER BY `INDEX`;"
        )
        lines = []
        for line in lines_row.split("\n"):
            if not line.strip():
                continue
            lparts = line.split("\t")
            try:
                qty = float(lparts[0].strip())
                rate = float(lparts[1].strip())
            except (ValueError, IndexError):
                continue
            desc = norm_text("\t".join(lparts[2:])) if len(lparts) > 2 else ""
            lines.append((qty, rate, desc))

        used = [False] * len(lines)
        missing_lines = []
        desc_warnings = []
        for exp_qty, exp_rate, kw in BC_EXPECTED_LINES:
            matched = False
            for i, (qty, rate, desc) in enumerate(lines):
                if used[i]:
                    continue
                if abs(qty - exp_qty) < 0.01 and abs(rate - exp_rate) < 0.01:
                    used[i] = True
                    matched = True
                    if norm_text(kw) not in desc:
                        desc_warnings.append(f"line qty={exp_qty} lacks '{kw}'")
                    break
            if not matched:
                missing_lines.append(f"({exp_qty} x {exp_rate})")

        lines_ok = len(missing_lines) == 0
        ok = delivered_ok and amount_ok and lines_ok

        detail = (f"amount={amount}, delivered={delivered_ok}, "
                  f"lines_found={len(lines)}, missing={missing_lines or 'none'}")
        if desc_warnings:
            detail += f", desc_warnings={desc_warnings}"
        check("15. BigCapital estimate delivered", 3, ok, detail)
    except Exception as e:
        check("15. BigCapital estimate delivered", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_event1_exists_live()
    check_2_event1_product_quota()
    check_3_event1_discount()
    check_4_event2_exists_live()
    check_5_event2_product_quota()
    check_6_event2_voucher()
    check_7_event3_exists_live()
    check_8_event3_product_quota()
    check_9_event3_question()
    check_10_twenty_opportunity()
    check_11_twenty_person()
    check_12_twenty_tasks()
    check_13_bc_item()
    check_14_bc_customer()
    check_15_bc_estimate()

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
