"""
Verifier for Business-135-I2: Brooklyn Jazz Symposium 2026

Checks: 17 weighted checks (+1 zero-weight logo precondition probe) across
pretix and twenty.
Strategy: docker exec (DB queries) for both sites.

Tightened per docs/tightening/business.md (business_135 section):
  - relation-table joins (quota_items / question_items / questionoption /
    checkinlist_limit_products / discount_condition_limit_products),
  - numeric equality instead of substring matching,
  - event-timezone date comparison for pretix timestamps,
  - "deletedAt" IS NULL + same-row scoping for all Twenty queries,
  - anchored phrases for note/task body content.
QR code (task item 18) is a pure frontend artifact and stays unscored.

Required env vars:
  SERVER_HOSTNAME, PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

PRETIX_PORT = os.getenv("PRETIX_PORT")
PRETIX_CONTAINER = os.getenv("PRETIX_CONTAINER")
PRETIX_DB_CONTAINER = os.getenv("PRETIX_DB_CONTAINER")
TWENTY_PORT = os.getenv("TWENTY_PORT")
TWENTY_CONTAINER = os.getenv("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.getenv("TWENTY_DB_CONTAINER")

for var in ("PRETIX_PORT", "PRETIX_CONTAINER", "PRETIX_DB_CONTAINER",
            "TWENTY_PORT", "TWENTY_CONTAINER", "TWENTY_DB_CONTAINER"):
    if not os.getenv(var):
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def pretix_sql(query: str, timeout: int = 15) -> str:
    """Run a SQL query against the Pretix PostgreSQL database."""
    r = subprocess.run(
        ["docker", "exec", PRETIX_DB_CONTAINER,
         "psql", "-U", "pretix", "-d", "pretix", "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"pretix psql error (rc={r.returncode}): {r.stderr.strip()[-500:]}"
        )
    return r.stdout.strip()


def twenty_sql(query: str, timeout: int = 15) -> str:
    """Run a SQL query against the Twenty PostgreSQL database."""
    r = subprocess.run(
        ["docker", "exec", TWENTY_DB_CONTAINER,
         "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if r.returncode != 0:
        # Do not swallow psql errors as "not found" — a broken query must be
        # visible as an exception in the check detail, not a silent FAIL.
        raise RuntimeError(
            f"twenty psql error (rc={r.returncode}): {r.stderr.strip()[-500:]}"
        )
    return r.stdout.strip()


def get_twenty_workspace_schema() -> str:
    """Discover the workspace schema name in Twenty's DB."""
    result = twenty_sql(
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';"
    )
    return result.strip()


_EVENT_FILTER = (
    "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
    "WHERE o.slug = 'urban-music' AND e.slug = 'bklyn-jazz-symposium-2026'"
)

_event_id: str | None = None


def get_event_id() -> str:
    """Return the pretix event id ('' if the event does not exist)."""
    global _event_id
    if _event_id is None:
        _event_id = pretix_sql(
            f"SELECT e.id FROM pretixbase_event e {_EVENT_FILTER};"
        ).strip()
    return _event_id


_event_tz: str | None = None


def get_event_tz() -> str:
    """Event timezone from settingsstore (COALESCE 'UTC'); sanitized for SQL."""
    global _event_tz
    if _event_tz is None:
        eid = get_event_id()
        tz = ""
        if eid:
            tz = pretix_sql(
                "SELECT value FROM pretixbase_event_settingsstore "
                f"WHERE object_id = {eid} AND key = 'timezone';"
            ).strip()
        if not tz or not re.fullmatch(r"[A-Za-z0-9_+\-/]+", tz):
            tz = "UTC"
        _event_tz = tz
    return _event_tz


def event_local_date(column: str) -> str:
    """SQL expression: timestamptz column -> calendar date in the event tz."""
    return f"(({column}) AT TIME ZONE '{get_event_tz()}')::date::text"


def i18n_to_text(raw: str) -> str:
    """Decode a pretix i18n JSON value ({'en': ...} with \\u escapes) to text."""
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if isinstance(obj, dict):
        return " ".join(str(v) for v in obj.values())
    return str(obj)


def norm_text(s: str) -> str:
    """Normalize dashes (em/en/double) and whitespace for text comparison."""
    s = (s or "").replace("—", "-").replace("–", "-").replace("--", "-")
    s = s.replace("&amp;", "&")
    return " ".join(s.split())


def num_eq(raw: str, expected: float, tol: float = 0.01) -> bool:
    """Numeric equality with tolerance; never substring ('30' in '130')."""
    try:
        return abs(float((raw or "").strip()) - expected) < tol
    except ValueError:
        return False


def date_matches_tz(stored: str, exp_date: str) -> bool:
    """True if a stored Twenty timestamp corresponds to calendar date exp_date.

    Twenty stores date fields as UTC timestamps; a local date D entered in the
    UI is stored shifted by the deployment's UTC offset. Pure-date values
    (exactly 10 chars) compare by equality; timestamp-shaped values are parsed
    and compared both as the UTC date and shifted by the verifier host's local
    UTC offset (computed, not hardcoded). No bare substring branch.
    """
    stored = (stored or "").strip()
    if not stored:
        return False
    if len(stored) == 10:
        return stored == exp_date
    try:
        dt = datetime.strptime(stored[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    if dt.strftime("%Y-%m-%d") == exp_date:
        return True
    offset = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (dt + offset).strftime("%Y-%m-%d") == exp_date


# ── Pretix checks ─────────────────────────────────────────────────────────────

def check_1_event_basics() -> None:
    """Event exists with correct name, slug, date, currency, and is live (EO #1, #11)."""
    try:
        if not get_event_id():
            check("1. Event basics + live", 2, False, "event not found")
            return
        row = pretix_sql(
            "SELECT e.slug, e.name::text, "
            f"{event_local_date('e.date_from')}, e.currency, e.live "
            f"FROM pretixbase_event e {_EVENT_FILTER};"
        )
        parts = row.split("|")
        slug, name_json, date_local, currency, live = (
            parts[0], parts[1], parts[2].strip(), parts[3], parts[4]
        )
        # date_from compared as a calendar date in the event's own timezone
        # (settingsstore key 'timezone', default UTC), not as a raw UTC prefix.
        ok = (
            slug == "bklyn-jazz-symposium-2026"
            and "Brooklyn Jazz Symposium 2026" in name_json
            and date_local == "2026-10-18"
            and currency == "USD"
            and live == "t"
        )
        check("1. Event basics + live", 2, ok,
              f"slug={slug}, date={date_local} (tz={get_event_tz()}), "
              f"currency={currency}, live={live}")
    except Exception as e:
        check("1. Event basics + live", 2, False, f"exception: {e}")


def check_2_categories() -> None:
    """Categories 'General Admission' and 'Experience Add-ons' exist (EO #2)."""
    try:
        rows = pretix_sql(
            "SELECT ic.name::text FROM pretixbase_itemcategory ic "
            "JOIN pretixbase_event e ON ic.event_id = e.id "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            "WHERE o.slug = 'urban-music' AND e.slug = 'bklyn-jazz-symposium-2026';"
        )
        has_general = "General Admission" in rows
        has_addons = "Experience Add-ons" in rows
        check("2. Categories", 1, has_general and has_addons,
              f"general={'found' if has_general else 'missing'}, addons={'found' if has_addons else 'missing'}")
    except Exception as e:
        check("2. Categories", 1, False, f"exception: {e}")


def check_3_products() -> None:
    """Three products exist with correct prices and categories (EO #3)."""
    try:
        rows = pretix_sql(
            "SELECT i.name::text, i.default_price, ic.name::text "
            "FROM pretixbase_item i "
            "JOIN pretixbase_event e ON i.event_id = e.id "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            "LEFT JOIN pretixbase_itemcategory ic ON i.category_id = ic.id "
            "WHERE o.slug = 'urban-music' AND e.slug = 'bklyn-jazz-symposium-2026';"
        )
        lines = [l for l in rows.split("\n") if l.strip()]
        expected = [
            ("General Admission Pass", "85.00", "General Admission"),
            ("VIP Backstage Pass", "220.00", "General Admission"),
            ("Jam Session Workshop", "60.00", "Experience Add-ons"),
        ]
        issues = []
        for name, price, cat in expected:
            matched = False
            for line in lines:
                if name in line:
                    matched = True
                    if price not in line:
                        issues.append(f"{name}: wrong price")
                    if cat not in line:
                        issues.append(f"{name}: wrong category")
                    break
            if not matched:
                issues.append(f"{name}: not found")
        check("3. Products", 2, not issues,
              "all 3 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("3. Products", 2, False, f"exception: {e}")


def check_4_quotas() -> None:
    """Quotas with correct sizes AND product bindings (EO #4).

    Shared-quota semantics are the core of this task: 'Main Venue Capacity'
    (350) must be linked to BOTH 'General Admission Pass' and 'VIP Backstage
    Pass' (lenient superset per plan — extra items tolerated); 'Jam Session
    Quota' (50) must be linked to exactly {'Jam Session Workshop'}.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("4. Quotas + item bindings", 2, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT q.name, q.size, COUNT(i.id), "
            "COALESCE(string_agg(i.name::text, ';' ORDER BY i.id), '') "
            "FROM pretixbase_quota q "
            "LEFT JOIN pretixbase_quota_items qi ON qi.quota_id = q.id "
            "LEFT JOIN pretixbase_item i ON i.id = qi.item_id "
            f"WHERE q.event_id = {eid} GROUP BY q.id;"
        )
        quotas: dict[str, tuple[int | None, int, str]] = {}
        for line in rows.split("\n"):
            parts = line.split("|")
            if len(parts) >= 4:
                size_raw = parts[1].strip()
                quotas[parts[0].strip()] = (
                    int(size_raw) if size_raw.isdigit() else None,
                    int(parts[2].strip() or 0),
                    parts[3],
                )
        issues = []
        venue = quotas.get("Main Venue Capacity")
        if not venue:
            issues.append("'Main Venue Capacity' not found")
        else:
            size, _n, items = venue
            if size != 350:
                issues.append(f"venue size={size}, expected 350")
            if "General Admission Pass" not in items:
                issues.append("venue quota not linked to General Admission Pass")
            if "VIP Backstage Pass" not in items:
                issues.append("venue quota not linked to VIP Backstage Pass")
        jam = quotas.get("Jam Session Quota")
        if not jam:
            issues.append("'Jam Session Quota' not found")
        else:
            size, n, items = jam
            if size != 50:
                issues.append(f"workshop size={size}, expected 50")
            if not (n == 1 and "Jam Session Workshop" in items):
                issues.append(
                    f"workshop quota items != exactly {{Jam Session Workshop}} (n={n})"
                )
        check("4. Quotas + item bindings", 2, not issues,
              "both quotas correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("4. Quotas + item bindings", 2, False, f"exception: {e}")


def check_5_questions() -> None:
    """Custom questions with correct types, required flags, product scoping,
    and Dietary options (EO #5).

    'Company Affiliation': type 'S' only (Text one line; 'T' = multi-line is
    wrong), required, linked to exactly {GA Pass, VIP Pass}.
    'Dietary Preference': type 'C', required, linked to exactly {VIP Pass},
    options exactly {'Vegetarian','Gluten-Free','No Preference'}.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("5. Custom questions", 2, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT q.question::text, q.type, q.required, COUNT(i.id), "
            "COALESCE(string_agg(i.name::text, ';' ORDER BY i.id), '') "
            "FROM pretixbase_question q "
            "LEFT JOIN pretixbase_question_items qi ON qi.question_id = q.id "
            "LEFT JOIN pretixbase_item i ON i.id = qi.item_id "
            f"WHERE q.event_id = {eid} GROUP BY q.id;"
        )
        issues = []
        found_affiliation = False
        found_dietary = False
        for line in rows.split("\n"):
            parts = line.split("|")
            if len(parts) < 5:
                continue
            qtext, qtype, qreq = parts[0], parts[1].strip(), parts[2].strip()
            n_items = int(parts[3].strip() or 0)
            items = parts[4]
            if "Company Affiliation" in qtext:
                found_affiliation = True
                if qtype != "S":  # S = short text (one line); T (long) rejected
                    issues.append(f"Company Affiliation type={qtype}, expected S")
                if qreq != "t":
                    issues.append("Company Affiliation not required")
                if not (n_items == 2
                        and "General Admission Pass" in items
                        and "VIP Backstage Pass" in items):
                    issues.append(
                        f"Affiliation items != {{GA Pass, VIP Pass}} (n={n_items})")
            if "Dietary Preference" in qtext:
                found_dietary = True
                if qtype != "C":  # C = choice single
                    issues.append(f"Dietary Preference type={qtype}, expected C")
                if qreq != "t":
                    issues.append("Dietary Preference not required")
                if not (n_items == 1 and "VIP Backstage Pass" in items):
                    issues.append(
                        f"Dietary items != {{VIP Pass}} only (n={n_items})")
        if not found_affiliation:
            issues.append("Company Affiliation not found")
        if not found_dietary:
            issues.append("Dietary Preference not found")
        if found_dietary:
            # Options set: answer is an i18n JSON value — compare decoded text.
            opt_rows = pretix_sql(
                "SELECT COUNT(*), COALESCE(string_agg(qo.answer, ';'), '') "
                "FROM pretixbase_questionoption qo "
                "JOIN pretixbase_question q ON qo.question_id = q.id "
                f"WHERE q.event_id = {eid} "
                "AND q.question::text ILIKE '%Dietary Preference%';"
            )
            oparts = opt_rows.split("|", 1)
            n_opts = int(oparts[0].strip() or 0)
            opts_text = i18n_to_text(oparts[1]) if len(oparts) > 1 else ""
            # i18n_to_text handles plain strings; also check raw agg since each
            # answer may itself be a JSON fragment inside the aggregate.
            blob = (oparts[1] if len(oparts) > 1 else "") + " " + opts_text
            missing = [o for o in ("Vegetarian", "Gluten-Free", "No Preference")
                       if o not in blob]
            if n_opts != 3 or missing:
                issues.append(
                    f"Dietary options != exactly 3 expected (n={n_opts}, "
                    f"missing={missing})")
        check("5. Custom questions", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("5. Custom questions", 2, False, f"exception: {e}")


def check_6_vouchers() -> None:
    """Vouchers with numeric values, validity dates, and product linkage (EO #6).

    VIPJAZZ2026: percent 30 (numeric, +-0.01 — no '30' in '130' substring),
    max 8, valid until 2026-10-18 (event-tz date), linked to VIP+Workshop.
    pretixbase_voucher has a single item_id/quota_id, so a two-product binding
    can only be expressed natively via a quota whose item set covers both
    {VIP Backstage Pass, Jam Session Workshop} — we accept either item_id in
    {VIP, Workshop} or quota_id pointing at such a (superset) quota.
    GROUPJAZZ40: subtract 40 (numeric), max 25, valid until 2026-09-30
    (event-tz date), item_id -> 'General Admission Pass'.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("6. Vouchers", 2, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT v.code, v.price_mode, v.value, v.max_usages, "
            f"COALESCE({event_local_date('v.valid_until')}, ''), "
            "COALESCE(i.name::text, ''), COALESCE(v.quota_id::text, '') "
            "FROM pretixbase_voucher v "
            "LEFT JOIN pretixbase_item i ON v.item_id = i.id "
            f"WHERE v.event_id = {eid} "
            "AND v.code IN ('VIPJAZZ2026', 'GROUPJAZZ40');"
        )
        vouchers: dict[str, dict[str, str]] = {}
        for line in rows.split("\n"):
            parts = line.split("|")
            if len(parts) >= 7:
                vouchers[parts[0].strip()] = {
                    "price_mode": parts[1].strip(),
                    "value": parts[2].strip(),
                    "max_usages": parts[3].strip(),
                    "valid_until": parts[4].strip(),
                    "item_name": parts[5],
                    "quota_id": parts[6].strip(),
                }
        issues = []
        vip = vouchers.get("VIPJAZZ2026")
        if not vip:
            issues.append("VIPJAZZ2026 not found")
        else:
            if vip["price_mode"] != "percent":
                issues.append(f"VIPJAZZ2026 mode={vip['price_mode']}")
            if not num_eq(vip["value"], 30):
                issues.append(f"VIPJAZZ2026 value={vip['value']}, expected 30")
            if vip["max_usages"] != "8":
                issues.append(f"VIPJAZZ2026 max_usages={vip['max_usages']}")
            if vip["valid_until"] != "2026-10-18":
                issues.append(
                    f"VIPJAZZ2026 valid_until={vip['valid_until']!r}, "
                    "expected 2026-10-18")
            linked = ("VIP Backstage Pass" in vip["item_name"]
                      or "Jam Session Workshop" in vip["item_name"])
            if not linked and vip["quota_id"]:
                # Native pretix solution: an auxiliary quota covering both
                # products (superset accepted).
                qitems = pretix_sql(
                    "SELECT COALESCE(string_agg(i.name::text, ';'), '') "
                    "FROM pretixbase_quota_items qi "
                    "JOIN pretixbase_item i ON i.id = qi.item_id "
                    f"WHERE qi.quota_id = {vip['quota_id']};"
                )
                linked = ("VIP Backstage Pass" in qitems
                          and "Jam Session Workshop" in qitems)
            if not linked:
                issues.append(
                    "VIPJAZZ2026 not linked to VIP/Workshop (item or quota)")
        grp = vouchers.get("GROUPJAZZ40")
        if not grp:
            issues.append("GROUPJAZZ40 not found")
        else:
            if grp["price_mode"] != "subtract":
                issues.append(f"GROUPJAZZ40 mode={grp['price_mode']}")
            if not num_eq(grp["value"], 40):
                issues.append(f"GROUPJAZZ40 value={grp['value']}, expected 40")
            if grp["max_usages"] != "25":
                issues.append(f"GROUPJAZZ40 max_usages={grp['max_usages']}")
            if grp["valid_until"] != "2026-09-30":
                issues.append(
                    f"GROUPJAZZ40 valid_until={grp['valid_until']!r}, "
                    "expected 2026-09-30")
            if "General Admission Pass" not in grp["item_name"]:
                issues.append("GROUPJAZZ40 not linked to General Admission Pass")
        check("6. Vouchers", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("6. Vouchers", 2, False, f"exception: {e}")


def check_7_discount_rule() -> None:
    """Discount rule 'Jazz Early Bird', active, 20% (numeric), scoped to GA Pass
    (EO #7).

    Same-row assertion. Product scoping (lenient per plan): all_products=true
    is accepted even though the task says "applied to General Admission Pass"
    — pretix's discount UI defaults to all products and the task intent is
    satisfied as long as GA Pass is covered; otherwise an explicit
    discount_condition_limit_products row to GA Pass is required.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("7. Discount rule", 1, False, "event not found")
            return
        row = pretix_sql(
            "SELECT d.internal_name, d.active, "
            "d.benefit_discount_matching_percent, d.condition_all_products, "
            "COALESCE((SELECT string_agg(i.name::text, ';') "
            "  FROM pretixbase_discount_condition_limit_products dl "
            "  JOIN pretixbase_item i ON i.id = dl.item_id "
            "  WHERE dl.discount_id = d.id), '') "
            "FROM pretixbase_discount d "
            f"WHERE d.event_id = {eid} "
            "AND d.internal_name ILIKE '%Jazz Early Bird%' LIMIT 1;"
        )
        if not row.strip():
            check("7. Discount rule", 1, False, "'Jazz Early Bird' not found")
            return
        parts = row.split("|")
        active = parts[1].strip() == "t"
        pct_ok = num_eq(parts[2], 20)
        all_products = parts[3].strip() == "t"
        ga_linked = "General Admission Pass" in (parts[4] if len(parts) > 4 else "")
        scope_ok = all_products or ga_linked
        check("7. Discount rule", 1, active and pct_ok and scope_ok,
              f"active={active}, pct={parts[2].strip()}, "
              f"all_products={all_products}, ga_linked={ga_linked}")
    except Exception as e:
        check("7. Discount rule", 1, False, f"exception: {e}")


def check_8_checkin_lists() -> None:
    """Check-in lists with correct product bindings (EO #8).

    'Main Venue Check-In': all_products=false, bound to exactly {GA, VIP}.
    'Jam Session Check-In': all_products=false, bound to exactly {Workshop}
    (COUNT=1 asserts the task's "only").
    """
    try:
        eid = get_event_id()
        if not eid:
            check("8. Check-in lists", 1, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT cl.name, cl.all_products, COUNT(i.id), "
            "COALESCE(string_agg(i.name::text, ';' ORDER BY i.id), '') "
            "FROM pretixbase_checkinlist cl "
            "LEFT JOIN pretixbase_checkinlist_limit_products lp "
            "  ON lp.checkinlist_id = cl.id "
            "LEFT JOIN pretixbase_item i ON i.id = lp.item_id "
            f"WHERE cl.event_id = {eid} GROUP BY cl.id;"
        )
        lists: dict[str, tuple[str, int, str]] = {}
        for line in rows.split("\n"):
            parts = line.split("|")
            if len(parts) >= 4:
                lists[parts[0].strip()] = (
                    parts[1].strip(), int(parts[2].strip() or 0), parts[3])
        issues = []
        main = lists.get("Main Venue Check-In")
        if not main:
            issues.append("'Main Venue Check-In' missing")
        else:
            ap, n, items = main
            if not (ap == "f" and n == 2
                    and "General Admission Pass" in items
                    and "VIP Backstage Pass" in items):
                issues.append(
                    f"Main Venue bindings != exactly {{GA, VIP}} "
                    f"(all_products={ap}, n={n})")
        jam = lists.get("Jam Session Check-In")
        if not jam:
            issues.append("'Jam Session Check-In' missing")
        else:
            ap, n, items = jam
            if not (ap == "f" and n == 1 and "Jam Session Workshop" in items):
                issues.append(
                    f"Jam Session bindings != exactly {{Workshop}} "
                    f"(all_products={ap}, n={n})")
        check("8. Check-in lists", 1, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("8. Check-in lists", 1, False, f"exception: {e}")


_FRONTPAGE_SENTENCE = (
    "Welcome to the Brooklyn Jazz Symposium 2026 - an unforgettable evening "
    "of world-class jazz performances and immersive workshops."
)  # em dash normalized to '-' by norm_text


def check_9_display_settings() -> None:
    """Display settings: primary_color=#7C3AED and the FULL front page sentence
    (EO #9). frontpage_text is an i18n JSON value (\\u escapes, em dash) —
    JSON-decode then dash/whitespace-normalize before substring matching.
    Missing settingsstore row = FAIL (setting was never made)."""
    try:
        eid = get_event_id()
        if not eid:
            check("9. Display settings", 1, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT s.key, s.value FROM pretixbase_event_settingsstore s "
            f"WHERE s.object_id = {eid} "
            "AND s.key IN ('primary_color', 'frontpage_text');"
        )
        color_val, text_val = "", ""
        for line in rows.split("\n"):
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            if parts[0].strip() == "primary_color":
                color_val = parts[1] or ""
            elif parts[0].strip() == "frontpage_text":
                text_val = parts[1] or ""
        has_color = "#7c3aed" in color_val.lower()
        text_norm = norm_text(i18n_to_text(text_val))
        has_text = norm_text(_FRONTPAGE_SENTENCE) in text_norm
        check("9. Display settings", 1, has_color and has_text,
              f"color={'found' if has_color else 'missing'}, "
              f"full_sentence={'found' if has_text else 'missing'}")
    except Exception as e:
        check("9. Display settings", 1, False, f"exception: {e}")


def check_9b_logo_precondition() -> None:
    """Logo upload probe (EO #9 / task item 15) — 0pt PRECONDITION ONLY.

    /tmp/jazz_event_logo.png is not declared as multimodal_input in meta.json,
    so the agent cannot receive the file yet (asset plumbing not ready). Per
    plan this check is added at weight 0 (diagnostic probe on settingsstore
    key 'logo_image' being non-empty); raise to 1pt (weight carved from ck17)
    only once the task asset supply is fixed. ck17 keeps its 3pt meanwhile.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("9b. Logo uploaded (precondition)", 0, False, "event not found")
            return
        val = pretix_sql(
            "SELECT value FROM pretixbase_event_settingsstore "
            f"WHERE object_id = {eid} AND key = 'logo_image';"
        )
        ok = bool((val or "").strip())
        check("9b. Logo uploaded (precondition)", 0, ok,
              "logo_image set" if ok else "logo_image not set (asset not "
              "deliverable to agent yet — unscored)")
    except Exception as e:
        check("9b. Logo uploaded (precondition)", 0, False, f"exception: {e}")


def check_10_invoice_settings() -> None:
    """Invoice settings: automatic generation + prefix 'BJS2026-' (EO #10).

    invoice_generate must be an AUTOMATIC mode: 'True' (all orders) or 'paid'
    (paid orders). Manual/self-service values ('admin', 'user') are rejected —
    they are not "automatic invoice generation". Prefix is matched on its own
    settingsstore row (key='invoice_numbers_prefix') by equality.
    """
    try:
        eid = get_event_id()
        if not eid:
            check("10. Invoice settings", 1, False, "event not found")
            return
        rows = pretix_sql(
            "SELECT s.key, s.value FROM pretixbase_event_settingsstore s "
            f"WHERE s.object_id = {eid} "
            "AND s.key IN ('invoice_generate', 'invoice_numbers_prefix');"
        )
        gen_val, prefix_val = None, None
        for line in rows.split("\n"):
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            if parts[0].strip() == "invoice_generate":
                gen_val = (parts[1] or "").strip().strip('"')
            elif parts[0].strip() == "invoice_numbers_prefix":
                prefix_val = (parts[1] or "").strip().strip('"')
        has_auto = (gen_val or "").lower() in ("true", "paid")
        has_prefix = prefix_val == "BJS2026-"
        check("10. Invoice settings", 1, has_auto and has_prefix,
              f"invoice_generate={gen_val!r} (need True/paid), "
              f"prefix={prefix_val!r}")
    except Exception as e:
        check("10. Invoice settings", 1, False, f"exception: {e}")


def check_11_customers() -> None:
    """Customer accounts with correct emails AND names, same-row (EO #12)."""
    try:
        rows = pretix_sql(
            "SELECT c.email, c.name_cached FROM pretixbase_customer c "
            "JOIN pretixbase_organizer o ON c.organizer_id = o.id "
            "WHERE o.slug = 'urban-music' "
            "AND c.email IN ('helena.vasquez@jazzpremier.com', "
            "'dominic.ferrara@soundwavecorp.com');"
        )
        lines = [l for l in rows.split("\n") if l.strip()]
        expected = [
            ("helena.vasquez@jazzpremier.com", "helena vasquez"),
            ("dominic.ferrara@soundwavecorp.com", "dominic ferrara"),
        ]
        issues = []
        for email, name in expected:
            line = next((l for l in lines if l.split("|")[0].strip() == email), None)
            if line is None:
                issues.append(f"{email} missing")
                continue
            parts = line.split("|", 1)
            name_cached = parts[1] if len(parts) > 1 else ""
            if name not in " ".join(name_cached.split()).lower():
                issues.append(f"{email}: name_cached={name_cached!r}")
        check("11. Customer accounts", 1, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("11. Customer accounts", 1, False, f"exception: {e}")


def check_12_membership() -> None:
    """Membership type 'Urban Music VIP Patron' exists; Helena has an active
    (non-canceled) membership 2026-03-01 to 2027-02-28 (EO #13)."""
    try:
        mt_rows = pretix_sql(
            "SELECT mt.id FROM pretixbase_membershiptype mt "
            "JOIN pretixbase_organizer o ON mt.organizer_id = o.id "
            "WHERE o.slug = 'urban-music' AND mt.name::text LIKE '%Urban Music VIP Patron%';"
        )
        if not mt_rows.strip():
            check("12. Membership", 2, False, "membership type 'Urban Music VIP Patron' not found")
            return
        m_rows = pretix_sql(
            "SELECT m.date_start::text, m.date_end::text, m.canceled "
            "FROM pretixbase_membership m "
            "JOIN pretixbase_membershiptype mt ON m.membership_type_id = mt.id "
            "JOIN pretixbase_customer c ON m.customer_id = c.id "
            "WHERE c.email = 'helena.vasquez@jazzpremier.com' "
            "AND mt.name::text LIKE '%Urban Music VIP Patron%';"
        )
        if not m_rows.strip():
            check("12. Membership", 2, False, "Helena's membership not found")
            return
        parts = m_rows.split("|")
        start_ok = parts[0].strip().startswith("2026-03-01") if len(parts) > 0 else False
        end_ok = parts[1].strip().startswith("2027-02-28") if len(parts) > 1 else False
        not_canceled = parts[2].strip() == "f" if len(parts) > 2 else False
        check("12. Membership", 2, start_ok and end_ok and not_canceled,
              f"start={parts[0].strip()}, "
              f"end={parts[1].strip() if len(parts) > 1 else '?'}, "
              f"canceled={parts[2].strip() if len(parts) > 2 else '?'}")
    except Exception as e:
        check("12. Membership", 2, False, f"exception: {e}")


# ── Twenty CRM checks ────────────────────────────────────────────────────────

_ws_schema: str = ""


def get_ws() -> str:
    global _ws_schema
    if not _ws_schema:
        _ws_schema = get_twenty_workspace_schema()
    return _ws_schema


def check_13_twenty_companies() -> None:
    """Four companies with correct domains, same-row per company (EO #14)."""
    try:
        ws = get_ws()
        if not ws:
            check("13. Twenty companies", 2, False, "workspace schema not found")
            return
        rows = twenty_sql(
            f'SELECT name, "domainNamePrimaryLinkUrl" FROM "{ws}".company '
            f'WHERE "deletedAt" IS NULL '
            f"AND name IN ('Jazz Premier Group', 'Soundwave Corporation', "
            f"'Rhythm House Productions', 'Blue Note Ventures');"
        )
        expected = {
            "Jazz Premier Group": "jazzpremier.com",
            "Soundwave Corporation": "soundwavecorp.com",
            "Rhythm House Productions": "rhythmhouseprod.com",
            "Blue Note Ventures": "bluenoteven.com",
        }
        lines = [l for l in rows.split("\n") if l.strip()]
        issues = []
        for name, domain in expected.items():
            # Same-row: the row whose name column matches must carry the domain.
            line = next(
                (l for l in lines if l.split("|")[0].strip() == name), None)
            if line is None:
                issues.append(f"{name} missing")
            elif domain not in (line.split("|", 1)[1] if "|" in line else ""):
                issues.append(f"{name} domain wrong")
        check("13. Twenty companies", 2, not issues,
              "all 4 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("13. Twenty companies", 2, False, f"exception: {e}")


def check_14_twenty_people() -> None:
    """Four people with correct emails, names, company links, and (for Helena/
    Dominic) job titles — all same-row (EO #15). Amara/Stefan have no titles
    in the task and are not checked for one."""
    try:
        ws = get_ws()
        if not ws:
            check("14. Twenty people", 2, False, "workspace schema not found")
            return
        rows = twenty_sql(
            f'SELECT p."nameFirstName", p."nameLastName", p."emailsPrimaryEmail", '
            f'p."jobTitle", c.name '
            f'FROM "{ws}".person p '
            f'LEFT JOIN "{ws}".company c ON p."companyId" = c.id '
            f'  AND c."deletedAt" IS NULL '
            f'WHERE p."deletedAt" IS NULL AND ('
            f"p.\"emailsPrimaryEmail\" LIKE '%helena.vasquez@jazzpremier.com%' "
            f"OR p.\"emailsPrimaryEmail\" LIKE '%dominic.ferrara@soundwavecorp.com%' "
            f"OR p.\"emailsPrimaryEmail\" LIKE '%amara.diallo@rhythmhouseprod.com%' "
            f"OR p.\"emailsPrimaryEmail\" LIKE '%stefan.kowalczyk@bluenoteven.com%');"
        )
        expected_people = [
            ("helena.vasquez@jazzpremier.com", "Helena Vasquez",
             "Jazz Premier Group", "Director of Cultural Programming"),
            ("dominic.ferrara@soundwavecorp.com", "Dominic Ferrara",
             "Soundwave Corporation", "Head of Artist Relations"),
            ("amara.diallo@rhythmhouseprod.com", "Amara Diallo",
             "Rhythm House Productions", None),
            ("stefan.kowalczyk@bluenoteven.com", "Stefan Kowalczyk",
             "Blue Note Ventures", None),
        ]
        lines = [l for l in rows.split("\n") if l.strip()]
        issues = []
        for email, full_name, company, title in expected_people:
            line = next((l for l in lines if email in l), None)
            if line is None:
                issues.append(f"{full_name} ({email}) missing")
                continue
            parts = line.split("|")
            # The whole name may land in nameFirstName (UI text insertion doesn't
            # trigger Twenty's first/last split) — compare the trimmed concatenation.
            stored_name = " ".join((parts[0] + " " + parts[1]).split()) if len(parts) >= 2 else ""
            if stored_name.lower() != full_name.lower():
                issues.append(f"{full_name}: name={stored_name!r}")
            if company not in line:
                issues.append(f"{full_name} not linked to {company}")
            if title is not None:
                stored_title = parts[3].strip() if len(parts) >= 4 else ""
                if title.lower() not in stored_title.lower():
                    issues.append(f"{full_name}: jobTitle={stored_title!r}")
        check("14. Twenty people", 2, not issues,
              "all 4 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("14. Twenty people", 2, False, f"exception: {e}")


def check_15_waitlist_tasks() -> None:
    """Two waitlist tasks, each asserted independently (no cross-task union):
    linked via taskTarget to its company, correct due date (tz-aware), and all
    body keywords present in THAT task's body (EO #16)."""
    try:
        ws = get_ws()
        if not ws:
            check("15. Waitlist tasks", 2, False, "workspace schema not found")
            return
        keywords = ["2026-10-18", "350", "GROUPJAZZ40", "40 USD", "max 25"]
        issues = []
        for company in ("Rhythm House Productions", "Blue Note Ventures"):
            rows = twenty_sql(
                f'SELECT t."dueAt"::text, '
                f"regexp_replace(t.\"bodyV2Markdown\", E'[\\n\\r]+', ' ', 'g') "
                f'FROM "{ws}".task t '
                f'JOIN "{ws}"."taskTarget" tt ON tt."taskId" = t.id '
                f'  AND tt."deletedAt" IS NULL '
                f'JOIN "{ws}".company c ON tt."targetCompanyId" = c.id '
                f'  AND c."deletedAt" IS NULL '
                f'WHERE t."deletedAt" IS NULL '
                f"AND c.name = '{company}' "
                f"AND t.title LIKE '%Notify when capacity opens%Brooklyn Jazz "
                f"Symposium 2026%{company}%';"
            )
            lines = [l for l in rows.split("\n") if l.strip()]
            if not lines:
                issues.append(f"{company}: task missing or not linked to company")
                continue
            best: list[str] = [f"{company}: no row satisfies all conditions"]
            for line in lines:
                parts = line.split("|", 1)
                due, body = parts[0], parts[1] if len(parts) > 1 else ""
                row_issues = []
                if not date_matches_tz(due, "2026-09-15"):
                    row_issues.append(f"dueAt={due.strip()!r} != 2026-09-15")
                missing = [k for k in keywords if k not in body]
                if missing:
                    row_issues.append(f"body missing {missing}")
                if not row_issues:
                    best = []
                    break
                best = [f"{company}: " + "; ".join(row_issues)]
            issues.extend(best)
        check("15. Waitlist tasks", 2, not issues,
              "both tasks correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("15. Waitlist tasks", 2, False, f"exception: {e}")


def check_16_vip_task() -> None:
    """VIP invitation task with correct due date (tz-aware, single column) and
    body content incl. membership/company keywords (EO #17)."""
    try:
        ws = get_ws()
        if not ws:
            check("16. VIP invite task", 2, False, "workspace schema not found")
            return
        rows = twenty_sql(
            f'SELECT t."dueAt"::text, '
            f"regexp_replace(t.\"bodyV2Markdown\", E'[\\n\\r]+', ' ', 'g') "
            f'FROM "{ws}".task t '
            f'WHERE t."deletedAt" IS NULL '
            f"AND t.title LIKE '%Send VIP invitations%Brooklyn Jazz Symposium%';"
        )
        lines = [l for l in rows.split("\n") if l.strip()]
        keywords = [
            "Helena Vasquez", "Dominic Ferrara", "VIPJAZZ2026",
            "helena.vasquez@jazzpremier.com", "dominic.ferrara@soundwavecorp.com",
            "Jazz Premier Group", "Soundwave Corporation",
            "Urban Music VIP Patron", "2027-02-28",
        ]
        issues = []
        if not lines:
            issues.append("task not found")
        else:
            best: list[str] = ["no row satisfies all conditions"]
            for line in lines:
                parts = line.split("|", 1)
                due, body = parts[0], parts[1] if len(parts) > 1 else ""
                row_issues = []
                if not date_matches_tz(due, "2026-08-20"):
                    row_issues.append(f"dueAt={due.strip()!r} != 2026-08-20")
                missing = [k for k in keywords if k not in body]
                if missing:
                    row_issues.append(f"body missing {missing}")
                if not row_issues:
                    best = []
                    break
                best = ["; ".join(row_issues)]
            issues.extend(best)
        check("16. VIP invite task", 2, not issues,
              "task correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("16. VIP invite task", 2, False, f"exception: {e}")


def check_17_note() -> None:
    """Capacity & Pricing Summary note with anchored content (EO #18).

    Title must match the task's note title (not any record mentioning the
    event); numeric facts use anchored phrases / word-boundary regexes so
    e.g. '50' can no longer ride on '350' and '85' on a phone number.
    Weight stays 3pt (the planned 1pt hand-off to the logo check is deferred
    until the logo asset plumbing exists; the logo probe runs at 0pt).
    """
    try:
        ws = get_ws()
        if not ws:
            check("17. Summary note", 3, False, "workspace schema not found")
            return
        rows = twenty_sql(
            f'SELECT n.title, '
            f"regexp_replace(n.\"bodyV2Markdown\", E'[\\n\\r]+', ' ', 'g') "
            f'FROM "{ws}".note n '
            f'WHERE n."deletedAt" IS NULL '
            f"AND (n.title ILIKE '%Capacity & Pricing Summary%' "
            f"OR n.title ILIKE '%Capacity &amp; Pricing Summary%') "
            f"AND n.title LIKE '%Brooklyn Jazz Symposium 2026%';"
        )
        lines = [l for l in rows.split("\n") if l.strip()]
        # required: (label, predicate over normalized body)
        required: list[tuple[str, object]] = [
            ("Brooklyn Jazz Symposium 2026", "Brooklyn Jazz Symposium 2026"),
            ("2026-10-18", "2026-10-18"),
            ("venue capacity 350", re.compile(r"\b350\b")),
            ("General Admission Pass", "General Admission Pass"),
            ("VIP Backstage Pass", "VIP Backstage Pass"),
            ("Jam Session Workshop", "Jam Session Workshop"),
            ("workshop capacity 50",
             re.compile(r"Workshop capacity:\s*50\b|\b50\b")),
            ("85 USD", re.compile(r"85\s*USD")),
            ("220 USD", re.compile(r"220\s*USD")),
            ("60 USD", re.compile(r"60\s*USD")),
            ("20%", "20%"),
            ("VIPJAZZ2026", "VIPJAZZ2026"),
            ("max 8 uses", re.compile(r"max\s*8\b")),
            ("GROUPJAZZ40", "GROUPJAZZ40"),
            ("max 25 uses", re.compile(r"max\s*25\b")),
            ("Helena Vasquez", "Helena Vasquez"),
            ("Urban Music VIP Patron", "Urban Music VIP Patron"),
            ("Dominic Ferrara", "Dominic Ferrara"),
            ("Waitlisted companies: 2",
             re.compile(r"Waitlisted companies:\s*2\b", re.I)),
            ("BJS2026-", "BJS2026-"),
        ]
        issues = []
        if not lines:
            issues.append("note not found (title must contain "
                          "'Capacity & Pricing Summary')")
        else:
            best: list[str] = ["no note satisfies all content requirements"]
            for line in lines:
                parts = line.split("|", 1)
                body = norm_text(parts[1] if len(parts) > 1 else "")
                missing = []
                for label, pred in required:
                    if isinstance(pred, str):
                        ok = pred in body
                    else:
                        ok = bool(pred.search(body))
                    if not ok:
                        missing.append(label)
                if not missing:
                    best = []
                    break
                best = [f"missing {missing[:6]}"]
            issues.extend(best)
        check("17. Summary note", 3, not issues,
              "note correct" if not issues else "; ".join(issues[:5]))
    except Exception as e:
        check("17. Summary note", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_event_basics()
    check_2_categories()
    check_3_products()
    check_4_quotas()
    check_5_questions()
    check_6_vouchers()
    check_7_discount_rule()
    check_8_checkin_lists()
    check_9_display_settings()
    check_9b_logo_precondition()
    check_10_invoice_settings()
    check_11_customers()
    check_12_membership()
    check_13_twenty_companies()
    check_14_twenty_people()
    check_15_waitlist_tasks()
    check_16_vip_task()
    check_17_note()

    total = sum(w for _, w, _, _ in _checks)
    earned = sum(w for _, w, p, _ in _checks if p)
    # 0-weight preconditions are diagnostic only — they never affect the score
    # and are excluded from the PASS flag.
    scored = [(w, p) for _, w, p, _ in _checks if w > 0]
    all_pass = all(p for _, p in scored) and bool(scored)
    score = (earned / total) if total else 0.0

    print(
        f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})",
        file=sys.stderr,
    )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
