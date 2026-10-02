"""
Verifier for Business-084-I5: AI Horizons 2026 Conference Sponsorship & Lead Pipeline

Checks: 18 weighted checks across pretix, bigcapital, twenty, hrms.
Strategy: docker exec DB (pretix, bigcapital, twenty) + REST API (hrms)

Required env vars:
  SERVER_HOSTNAME, PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER
"""

import os
import sys
import subprocess
import json
import requests
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

PRETIX_PORT = os.environ.get("PRETIX_PORT")
PRETIX_CONTAINER = os.environ.get("PRETIX_CONTAINER")
PRETIX_DB = os.environ.get("PRETIX_DB_CONTAINER")

BC_PORT = os.environ.get("BIGCAPITAL_PORT")
BC_CONTAINER = os.environ.get("BIGCAPITAL_CONTAINER")
BC_DB = os.environ.get("BIGCAPITAL_DB_CONTAINER")

TW_PORT = os.environ.get("TWENTY_PORT")
TW_CONTAINER = os.environ.get("TWENTY_CONTAINER")
TW_DB = os.environ.get("TWENTY_DB_CONTAINER")

HRMS_PORT = os.environ.get("HRMS_PORT")
HRMS_CONTAINER = os.environ.get("HRMS_CONTAINER")
HRMS_DB = os.environ.get("HRMS_DB_CONTAINER")

for var in ("PRETIX_PORT", "PRETIX_CONTAINER", "PRETIX_DB_CONTAINER",
            "BIGCAPITAL_PORT", "BIGCAPITAL_CONTAINER", "BIGCAPITAL_DB_CONTAINER",
            "TWENTY_PORT", "TWENTY_CONTAINER", "TWENTY_DB_CONTAINER",
            "HRMS_PORT", "HRMS_CONTAINER", "HRMS_DB_CONTAINER"):
    if not os.environ.get(var):
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
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def pq(query: str, timeout: int = 15) -> str:
    """Query Pretix Postgres DB."""
    rc, out, err = docker_exec(
        PRETIX_DB, "psql", "-U", "pretix", "-d", "pretix",
        "-t", "-A", "-c", query, timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"pretix psql error (rc={rc}): {err.strip()[-500:]}")
    return out.strip()


def i18n_en(raw: str) -> str:
    """Extract the English value from a pretix i18n column.

    The column type is text: it holds either a JSON dict like {"en": "..."} or a
    plain string (a `->>'en'` in SQL fails with `operator does not exist`).
    """
    raw = (raw or "").strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return str(data.get("en") or next(iter(data.values()), ""))
        except Exception:
            pass
    return raw


def norm_text(s: str) -> str:
    """Normalize em/en dashes and '--' to '-', fold whitespace."""
    s = (s or "").replace("—", "-").replace("–", "-").replace("--", "-")
    return " ".join(s.split())


# Local UTC offset of the verifier host (deployment runs UTC+8; do not hardcode).
_LOCAL_UTC_OFFSET = datetime.now().astimezone().utcoffset() or timedelta(0)


def date_matches_tz(stored: str, exp_date: str) -> bool:
    """True if a stored value corresponds to local calendar date exp_date.

    Twenty stores date fields as UTC timestamps; a local date D entered in the UI
    is stored shifted back by the local UTC offset (e.g. (D-1)T16:00:00+00 under
    UTC+8). For timestamp-shaped values (longer than 10 chars) only two
    interpretations are accepted: stored::date == exp_date, or
    (stored + local_utc_offset)::date == exp_date. A pure date (exactly 10 chars)
    must match exactly. No raw-substring shortcut.
    """
    stored = (stored or "").strip()
    if len(stored) == 10:
        return stored == exp_date
    raw = stored[:19].replace("T", " ")
    try:
        dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    if dt.strftime("%Y-%m-%d") == exp_date:
        return True
    return (dt + _LOCAL_UTC_OFFSET).strftime("%Y-%m-%d") == exp_date


# ── BigCapital DB helpers ─────────────────────────────────────────────────────
_bc_tenant_db: str | None = None


def bc_tenant_db() -> str:
    """Discover BigCapital tenant DB name."""
    global _bc_tenant_db
    if _bc_tenant_db:
        return _bc_tenant_db
    rc, out, err = docker_exec(
        BC_DB, "mysql", "-u", "bigcapital", "-pbigcapital123",
        "-N", "-B", "-e", "SHOW DATABASES LIKE 'bigcapital_tenant_%'",
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error (rc={rc}): {err.strip()[-500:]}")
    dbs = [line.strip() for line in out.strip().splitlines() if line.strip()]
    if not dbs:
        raise RuntimeError("No BigCapital tenant DB found")
    _bc_tenant_db = dbs[0]
    return _bc_tenant_db


def bcq(query: str, timeout: int = 15) -> str:
    """Query BigCapital tenant MariaDB."""
    db = bc_tenant_db()
    rc, out, err = docker_exec(
        BC_DB, "mysql", "-u", "bigcapital", "-pbigcapital123",
        "--default-character-set=utf8mb4", db, "-N", "-B", "-e", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error (rc={rc}): {err.strip()[-500:]}")
    return out.strip()


# ── Twenty DB helpers ─────────────────────────────────────────────────────────
_tw_schema: str | None = None


def tw_schema() -> str:
    """Discover Twenty workspace schema."""
    global _tw_schema
    if _tw_schema:
        return _tw_schema
    rc, out, err = docker_exec(
        TW_DB, "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c",
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';",
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql error (rc={rc}): {err.strip()[-500:]}")
    schema = out.strip().splitlines()[0].strip() if out.strip() else ""
    if not schema:
        raise RuntimeError("No Twenty workspace schema found")
    _tw_schema = schema
    return _tw_schema


def twq(query: str, timeout: int = 15) -> str:
    """Query Twenty Postgres DB."""
    rc, out, err = docker_exec(
        TW_DB, "psql", "-U", "postgres", "-d", "default",
        "-t", "-A", "-c", query, timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql error (rc={rc}): {err.strip()[-500:]}")
    return out.strip()


_tt_company_col: str | None = None


def tt_company_col() -> str:
    """Probe the taskTarget→company FK column name (varies across Twenty versions)."""
    global _tt_company_col
    if _tt_company_col is None:
        ws = tw_schema()
        out = twq(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='{ws}' AND table_name='taskTarget' "
            f"AND column_name IN ('targetCompanyId','companyId')"
        )
        cols = [l.strip() for l in out.splitlines() if l.strip()]
        if "targetCompanyId" in cols:
            _tt_company_col = "targetCompanyId"
        elif "companyId" in cols:
            _tt_company_col = "companyId"
        else:
            raise RuntimeError("taskTarget company FK column not found")
    return _tt_company_col


# ── HRMS (Frappe) API ─────────────────────────────────────────────────────────
_hrms_session: requests.Session | None = None


def hrms_api() -> requests.Session:
    global _hrms_session
    if _hrms_session:
        return _hrms_session
    s = requests.Session()
    r = s.post(
        f"http://{HOST}:{HRMS_PORT}/api/method/login",
        data={"usr": "Administrator", "pwd": "admin"},
        timeout=10,
    )
    r.raise_for_status()
    _hrms_session = s
    return s


def hrms_get(doctype: str, filters: list | None = None, fields: list | None = None) -> list:
    s = hrms_api()
    params: dict = {}
    if filters:
        params["filters"] = json.dumps(filters)
    if fields:
        params["fields"] = json.dumps(fields)
    r = s.get(
        f"http://{HOST}:{HRMS_PORT}/api/resource/{doctype}",
        params=params, timeout=10,
    )
    r.raise_for_status()
    return r.json().get("data", [])


# ── Pretix event ID cache ────────────────────────────────────────────────────
_pretix_eid: int | None = None


def pretix_eid() -> int:
    global _pretix_eid
    if _pretix_eid is None:
        row = pq("SELECT id FROM pretixbase_event WHERE slug='ai-horizons-2026'")
        _pretix_eid = int(row) if row else 0
    return _pretix_eid


def pretix_quota_items(quota_id: int) -> set[str]:
    """Product names linked to a quota (i18n-decoded)."""
    rows = pq(
        f"SELECT i.name FROM pretixbase_quota_items qi "
        f"JOIN pretixbase_item i ON i.id=qi.item_id WHERE qi.quota_id={quota_id}"
    )
    return {i18n_en(l) for l in rows.splitlines() if l.strip()}


# ══════════════════════════════════════════════════════════════════════════════
# CHECKS
# ══════════════════════════════════════════════════════════════════════════════

# ── Pretix (7 checks) ────────────────────────────────────────────────────────

def check_1_pretix_event():
    """Pretix event exists under organizer 'culinary-arts', live, start date, currency."""
    try:
        eid = pretix_eid()
        if not eid:
            check("1. Pretix event basics", 2, False, "event 'ai-horizons-2026' not found")
            return
        # `name` is a text column holding i18n JSON — select raw (last) and decode
        row = pq(
            f"SELECT o.slug, e.date_from, e.currency, e.live, e.name "
            f"FROM pretixbase_event e "
            f"JOIN pretixbase_organizer o ON e.organizer_id=o.id "
            f"WHERE e.id={eid}"
        )
        parts = row.split("|", 4)
        if len(parts) < 5:
            check("1. Pretix event basics", 2, False, f"unexpected row format: {row!r}")
            return
        org, date_from, currency, live = parts[0], parts[1], parts[2], parts[3]
        name = i18n_en(parts[4])
        ok = (
            org == "culinary-arts"
            and "AI Horizons 2026" in name
            and date_from.startswith("2026-12-03")
            and currency == "USD"
            and live.lower() in ("t", "true")
        )
        check("1. Pretix event basics", 2, ok,
              f"organizer={org}, name={name!r}, date={date_from}, curr={currency}, live={live}")
    except Exception as e:
        check("1. Pretix event basics", 2, False, f"exception: {e}")


def check_2_pretix_products():
    """4 products with correct prices and category assignments."""
    try:
        eid = pretix_eid()
        if not eid:
            check("2. Pretix products & categories", 2, False, "no event")
            return
        cats_raw = pq(f"SELECT id, name FROM pretixbase_itemcategory WHERE event_id={eid}")
        cats = {}
        for line in cats_raw.splitlines():
            if "|" in line:
                cid, cname = line.split("|", 1)
                cats[int(cid)] = i18n_en(cname)

        items_raw = pq(
            f"SELECT name, default_price, category_id "
            f"FROM pretixbase_item WHERE event_id={eid}"
        )
        items: dict[str, dict] = {}
        for line in items_raw.splitlines():
            if "|" in line:
                p = line.split("|")
                items[i18n_en(p[0])] = {"price": float(p[1]), "cat_id": int(p[2]) if p[2] else None}

        expected = {
            "Platinum Exhibit Booth": (11000.0, "Sponsor Exhibits"),
            "Gold Exhibit Booth": (5500.0, "Sponsor Exhibits"),
            "General Entry Ticket": (329.0, "Attendee Tickets"),
            "Workshop Session Pass": (169.0, "Workshop Sessions"),
        }
        issues = []
        for name, (price, cat_name) in expected.items():
            item = items.get(name)
            if not item:
                issues.append(f"{name}: missing")
            elif abs(item["price"] - price) > 0.01:
                issues.append(f"{name}: price={item['price']} expected {price}")
            elif cats.get(item["cat_id"]) != cat_name:
                issues.append(f"{name}: cat={cats.get(item['cat_id'])!r} expected {cat_name!r}")

        check("2. Pretix products & categories", 2, not issues,
              "all 4 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("2. Pretix products & categories", 2, False, f"exception: {e}")


def check_3_pretix_quotas():
    """4 quotas with correct sizes, each linked to exactly its one product."""
    try:
        eid = pretix_eid()
        if not eid:
            check("3. Pretix quotas", 2, False, "no event")
            return
        rows = pq(f"SELECT id, size, name FROM pretixbase_quota WHERE event_id={eid}")
        quotas: dict[str, tuple[int, int]] = {}
        for line in rows.splitlines():
            if "|" in line:
                p = line.split("|", 2)
                if len(p) < 3:
                    continue
                quotas[p[2]] = (int(p[0]), int(p[1]))

        expected = {
            "Platinum Exhibit Quota": (4, "Platinum Exhibit Booth"),
            "Gold Exhibit Quota": (7, "Gold Exhibit Booth"),
            "General Entry Quota": (550, "General Entry Ticket"),
            "Workshop Session Quota": (130, "Workshop Session Pass"),
        }
        issues = []
        for name, (size, product) in expected.items():
            if name not in quotas:
                issues.append(f"{name}: missing")
                continue
            qid, qsize = quotas[name]
            if qsize != size:
                issues.append(f"{name}: size={qsize} expected {size}")
            linked = pretix_quota_items(qid)
            if linked != {product}:
                issues.append(f"{name}: items={sorted(linked)} expected [{product!r}]")

        check("3. Pretix quotas", 2, not issues,
              "all 4 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("3. Pretix quotas", 2, False, f"exception: {e}")


def check_4_pretix_vouchers():
    """2 vouchers: codes, tag, max_usages, 100% discount, valid_until, linked to General Entry Ticket."""
    try:
        eid = pretix_eid()
        if not eid:
            check("4. Pretix vouchers", 2, False, "no event")
            return
        rows = pq(
            f"SELECT v.code, v.max_usages, v.value, v.price_mode, v.valid_until, "
            f"v.quota_id, v.variation_id, COALESCE(i.name,'') "
            f"FROM pretixbase_voucher v "
            f"LEFT JOIN pretixbase_item i ON v.item_id=i.id "
            f"WHERE v.event_id={eid} AND v.tag='AIHORIZONS-SPONSOR-COMP'"
        )
        vouchers: dict[str, dict] = {}
        for line in rows.splitlines():
            if "|" in line:
                p = line.split("|", 7)
                if len(p) < 8:
                    continue
                vouchers[p[0]] = {
                    "max_usages": int(p[1]),
                    "value": float(p[2]) if p[2] else 0.0,
                    "price_mode": p[3], "valid_until": p[4],
                    "quota_id": int(p[5]) if p[5] else None,
                    "variation_id": int(p[6]) if p[6] else None,
                    "item": i18n_en(p[7]),
                }

        issues = []
        for code in ("PLAT-AIHORIZONS-001", "GOLD-AIHORIZONS-001"):
            v = vouchers.get(code)
            if not v:
                issues.append(f"{code}: missing")
                continue
            if v["max_usages"] != 5:
                issues.append(f"{code}: max_usages={v['max_usages']}")
            is_100pct = (
                (v["price_mode"] == "percent" and abs(v["value"] - 100.0) < 0.01)
                or (v["price_mode"] == "set" and abs(v["value"]) < 0.01)
            )
            if not is_100pct:
                issues.append(f"{code}: mode={v['price_mode']}, val={v['value']}")
            if not v["valid_until"].startswith("2026-12-03"):
                issues.append(f"{code}: valid_until={v['valid_until']}")
            # Product linkage: item must be 'General Entry Ticket' (directly, or via a
            # quota that contains only that product); no variation restriction.
            quota_items = (
                pretix_quota_items(v["quota_id"]) if v["quota_id"] else None
            )
            if v["item"] == "General Entry Ticket":
                if quota_items is not None and quota_items != {"General Entry Ticket"}:
                    issues.append(f"{code}: quota items={sorted(quota_items)}")
            elif not v["item"] and quota_items == {"General Entry Ticket"}:
                pass
            else:
                issues.append(
                    f"{code}: linked item={v['item']!r} "
                    f"(quota items={sorted(quota_items) if quota_items is not None else None}), "
                    f"expected 'General Entry Ticket'"
                )
            if v["variation_id"] is not None:
                issues.append(f"{code}: variation_id={v['variation_id']} expected NULL")

        total_tagged = len(vouchers)
        if total_tagged != 2:
            issues.append(f"expected 2 tagged vouchers, found {total_tagged}")

        check("4. Pretix vouchers", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("4. Pretix vouchers", 2, False, f"exception: {e}")


def check_5_pretix_questions():
    """Custom questions: type 'S', required, and linked to the correct products."""
    try:
        eid = pretix_eid()
        if not eid:
            check("5. Pretix custom questions", 1, False, "no event")
            return
        rows = pq(
            f"SELECT id, required, type, question "
            f"FROM pretixbase_question WHERE event_id={eid}"
        )
        questions: dict[str, dict] = {}
        for line in rows.splitlines():
            if "|" in line:
                p = line.split("|", 3)
                if len(p) < 4:
                    continue
                questions[i18n_en(p[3])] = {
                    "id": int(p[0]),
                    "required": p[1].lower() in ("t", "true"),
                    "type": p[2],
                }

        expected_items = {
            "Company Name": {"Platinum Exhibit Booth", "Gold Exhibit Booth"},
            "Job Title": {"General Entry Ticket"},
        }
        issues = []
        for qname, exp_items in expected_items.items():
            q = questions.get(qname)
            if not q:
                issues.append(f"{qname!r} missing")
                continue
            if not q["required"]:
                issues.append(f"{qname!r} not required")
            if q["type"] != "S":
                issues.append(f"{qname!r} type={q['type']!r} expected 'S'")
            linked = pq(
                f"SELECT i.name FROM pretixbase_question_items qi "
                f"JOIN pretixbase_item i ON i.id=qi.item_id "
                f"WHERE qi.question_id={q['id']}"
            )
            names = {i18n_en(l) for l in linked.splitlines() if l.strip()}
            if names != exp_items:
                issues.append(f"{qname!r} items={sorted(names)} expected {sorted(exp_items)}")

        check("5. Pretix custom questions", 1, not issues,
              "both correct (type S, required, right products)" if not issues else "; ".join(issues))
    except Exception as e:
        check("5. Pretix custom questions", 1, False, f"exception: {e}")


def check_6_pretix_checkin():
    """Two check-in lists with correct product linkage."""
    try:
        eid = pretix_eid()
        if not eid:
            check("6. Pretix check-in lists", 2, False, "no event")
            return
        rows = pq(f"SELECT id, all_products, name FROM pretixbase_checkinlist WHERE event_id={eid}")
        lists: dict[str, tuple[int, bool]] = {}
        for line in rows.splitlines():
            if "|" in line:
                p = line.split("|", 2)
                if len(p) < 3:
                    continue
                lists[p[2]] = (int(p[0]), p[1].lower() in ("t", "true"))

        def linked_items(cid: int) -> set[str]:
            linked = pq(
                f"SELECT i.name FROM pretixbase_checkinlist_limit_products cl "
                f"JOIN pretixbase_item i ON cl.item_id=i.id WHERE cl.checkinlist_id={cid}"
            )
            return {i18n_en(l) for l in linked.splitlines() if l.strip()}

        all_4 = {"Platinum Exhibit Booth", "Gold Exhibit Booth",
                 "General Entry Ticket", "Workshop Session Pass"}
        sponsor_items = {"Platinum Exhibit Booth", "Gold Exhibit Booth"}

        issues = []
        if "AI Horizons Main Check-In" not in lists:
            issues.append("'AI Horizons Main Check-In' missing")
        else:
            cid, all_products = lists["AI Horizons Main Check-In"]
            linked_names = linked_items(cid)
            # Must cover all 4 products: either the all-products flag or explicit links.
            if not (all_products or all_4 <= linked_names):
                issues.append(
                    f"Main check-in: all_products={all_products}, "
                    f"linked={sorted(linked_names)} (need all 4 products)"
                )

        if "Sponsor Exhibit Check-In" not in lists:
            issues.append("'Sponsor Exhibit Check-In' missing")
        else:
            cid, all_products = lists["Sponsor Exhibit Check-In"]
            linked_names = linked_items(cid)
            if all_products:
                issues.append("Sponsor check-in: all_products=true (must be booth products only)")
            if not (sponsor_items <= linked_names):
                issues.append(f"Sponsor check-in linked to: {sorted(linked_names)}")
            if linked_names - sponsor_items:
                issues.append(f"Sponsor check-in extra: {sorted(linked_names - sponsor_items)}")

        check("6. Pretix check-in lists", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("6. Pretix check-in lists", 2, False, f"exception: {e}")


def check_7_pretix_tax_discount():
    """Default tax rule 'Conference Tax Rule' 8% + active discount 'Early Bird 18% Off' on the right products."""
    try:
        eid = pretix_eid()
        if not eid:
            check("7. Pretix tax & discount rules", 1, False, "no event")
            return
        issues = []

        # Tax rule: name + rate 8 + set as default
        tax_raw = pq(f'SELECT id, rate, "default", name FROM pretixbase_taxrule WHERE event_id={eid}')
        tax_found = False
        tax_detail = "no tax rules"
        for line in tax_raw.splitlines():
            p = line.split("|", 3)
            if len(p) < 4:
                continue
            rate = float(p[1])
            is_default = p[2].lower() in ("t", "true")
            nm = i18n_en(p[3])
            if "Conference Tax Rule" in nm:
                tax_detail = f"rate={rate}, default={is_default}"
                if abs(rate - 8.0) < 0.01 and is_default:
                    tax_found = True
        if not tax_found:
            issues.append(f"'Conference Tax Rule' 8% default not found ({tax_detail})")

        # Discount: 18%, active, limited to General Entry Ticket + Workshop Session Pass.
        # No exception swallowing: a failed query or an empty discount table must fail.
        disc_raw = pq(
            f"SELECT id, benefit_discount_matching_percent, active, "
            f"condition_all_products, internal_name "
            f"FROM pretixbase_discount WHERE event_id={eid}"
        )
        exp_products = {"General Entry Ticket", "Workshop Session Pass"}
        disc_found = False
        disc_detail = "no discount rows"
        for line in (disc_raw or "").splitlines():
            p = line.split("|", 4)
            if len(p) < 5:
                continue
            did = int(p[0])
            pct = float(p[1]) if p[1] else 0.0
            active = p[2].lower() in ("t", "true")
            all_prods = p[3].lower() in ("t", "true")
            nm = p[4]
            if "Early Bird" in nm and abs(pct - 18.0) < 0.01:
                linked = pq(
                    f"SELECT i.name FROM pretixbase_discount_condition_limit_products dcp "
                    f"JOIN pretixbase_item i ON i.id=dcp.item_id WHERE dcp.discount_id={did}"
                )
                names = {i18n_en(l) for l in linked.splitlines() if l.strip()}
                disc_detail = f"active={active}, all_products={all_prods}, products={sorted(names)}"
                if active and not all_prods and names == exp_products:
                    disc_found = True
        if not disc_found:
            issues.append(
                f"'Early Bird 18% Off' (18%, active, limited to General Entry Ticket + "
                f"Workshop Session Pass) not found ({disc_detail})"
            )

        check("7. Pretix tax & discount rules", 1, not issues,
              "tax rule + discount correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("7. Pretix tax & discount rules", 1, False, f"exception: {e}")


# ── BigCapital (5 checks) ────────────────────────────────────────────────────

def check_8_bc_accounts_items():
    """BigCapital: 2 accounts (typed), 2 customers (email), 2 service items (sell account)."""
    try:
        issues = []

        # Accounts with type gate
        accts = bcq("SELECT NAME, ACCOUNT_TYPE FROM ACCOUNTS WHERE NAME IN "
                    "('AI Horizons Sponsorship Revenue','Deferred AI Horizons Sponsorship')")
        acct_map: dict[str, str] = {}
        for line in accts.splitlines():
            if "\t" in line:
                n, t = line.split("\t", 1)
                acct_map[n] = t.strip()
        for name, exp_type in (("AI Horizons Sponsorship Revenue", "income"),
                               ("Deferred AI Horizons Sponsorship", "other-current-liability")):
            if name not in acct_map:
                issues.append(f"account '{name}' missing")
            elif acct_map[name] != exp_type:
                issues.append(f"account '{name}': type={acct_map[name]!r} expected {exp_type!r}")

        # Customers: exact display name + email gate
        custs = bcq("SELECT DISPLAY_NAME, EMAIL FROM CONTACTS WHERE CONTACT_SERVICE='customer' "
                    "AND DISPLAY_NAME IN ('Zenith Cloud Corp','Prism Digital Solutions')")
        cust_map: dict[str, str] = {}
        for line in custs.splitlines():
            if "\t" in line:
                n, e = line.split("\t", 1)
                cust_map[n] = "" if e.strip() == "NULL" else e.strip()
        for exp_name, exp_email in (("Zenith Cloud Corp", "sponsor@zenithcloud.com"),
                                    ("Prism Digital Solutions", "events@prismdigital.com")):
            if exp_name not in cust_map:
                issues.append(f"customer '{exp_name}' missing (exact name required)")
            elif cust_map[exp_name].lower() != exp_email:
                issues.append(f"{exp_name}: email={cust_map[exp_name]!r} expected {exp_email!r}")

        # Items: price + TYPE='service' + sell account gate
        items = bcq("SELECT i.NAME, i.SELL_PRICE, i.TYPE, COALESCE(a.NAME,'') "
                    "FROM ITEMS i LEFT JOIN ACCOUNTS a ON a.ID=i.SELL_ACCOUNT_ID "
                    "WHERE i.NAME IN "
                    "('Platinum Exhibit Sponsorship Service','Gold Exhibit Sponsorship Service')")
        item_map: dict[str, tuple[float, str, str]] = {}
        for line in items.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                item_map[parts[0]] = (float(parts[1]), parts[2].strip(), parts[3].strip())
        for exp_name, exp_price in (("Platinum Exhibit Sponsorship Service", 11000),
                                    ("Gold Exhibit Sponsorship Service", 5500)):
            if exp_name not in item_map:
                issues.append(f"item '{exp_name}' missing")
                continue
            price, itype, sell_acct = item_map[exp_name]
            if abs(price - exp_price) > 0.01:
                issues.append(f"{exp_name}: price={price}")
            if itype.lower() != "service":
                issues.append(f"{exp_name}: type={itype!r} expected 'service'")
            if sell_acct != "AI Horizons Sponsorship Revenue":
                issues.append(f"{exp_name}: sell account={sell_acct!r}")

        check("8. BigCapital accounts & items", 2, not issues,
              "all present and correctly typed" if not issues else "; ".join(issues))
    except Exception as e:
        check("8. BigCapital accounts & items", 2, False, f"exception: {e}")


# Invoice IDs of the verified Zenith invoice (used by the payment-received check).
_zenith_invoice_ids: set[int] = set()


def _bc_invoice_rows(customer: str) -> list[dict]:
    """Invoice + line-item rows for an exact customer display name."""
    rows = bcq(
        "SELECT si.ID, si.INVOICE_DATE, si.DUE_DATE, si.BALANCE, si.PAYMENT_AMOUNT, "
        "si.DELIVERED_AT, ie.QUANTITY, ie.RATE, it.NAME "
        "FROM SALES_INVOICES si "
        "JOIN CONTACTS c ON si.CUSTOMER_ID = c.ID "
        "JOIN ITEMS_ENTRIES ie ON ie.REFERENCE_TYPE='SaleInvoice' AND ie.REFERENCE_ID=si.ID "
        "JOIN ITEMS it ON it.ID=ie.ITEM_ID "
        f"WHERE c.DISPLAY_NAME='{customer}'"
    )
    out: list[dict] = []
    for line in rows.splitlines():
        p = line.split("\t")
        if len(p) < 9:
            continue

        def num(x: str) -> float:
            return float(x) if x and x != "NULL" else 0.0

        out.append({
            "id": int(p[0]),
            "invoice_date": p[1][:10],
            "due_date": p[2][:10],
            "balance": num(p[3]),
            "payment": num(p[4]),
            "delivered": p[5] not in ("", "NULL"),
            "qty": num(p[6]),
            "rate": num(p[7]),
            "item": p[8],
        })
    return out


def check_9_bc_invoice_zenith():
    """BigCapital: Zenith invoice — Platinum service x1 @11000, dated/due, delivered, fully paid."""
    try:
        rows = _bc_invoice_rows("Zenith Cloud Corp")
        if not rows:
            check("9. BigCapital invoice Zenith (paid)", 2, False,
                  "no invoice with line items found for customer 'Zenith Cloud Corp'")
            return
        best: list[str] | None = None
        for r in rows:
            issues = []
            if r["item"] != "Platinum Exhibit Sponsorship Service":
                issues.append(f"item={r['item']!r}")
            if abs(r["qty"] - 1) > 0.001:
                issues.append(f"qty={r['qty']}")
            if abs(r["rate"] - 11000) > 0.01:
                issues.append(f"rate={r['rate']}")
            if r["invoice_date"] != "2026-10-01":
                issues.append(f"invoice_date={r['invoice_date']}")
            if r["due_date"] != "2026-11-01":
                issues.append(f"due_date={r['due_date']}")
            if not r["delivered"]:
                issues.append("not delivered")
            # BALANCE stores the invoice TOTAL; payments accumulate in PAYMENT_AMOUNT.
            if r["balance"] <= 0.01 or r["payment"] < r["balance"] - 0.01:
                issues.append(f"payment={r['payment']} of total={r['balance']}, expected fully paid")
            if not issues:
                _zenith_invoice_ids.add(r["id"])
            if best is None or len(issues) < len(best):
                best = issues
        check("9. BigCapital invoice Zenith (paid)", 2, bool(_zenith_invoice_ids),
              "delivered + paid, all fields correct" if _zenith_invoice_ids
              else "closest invoice line: " + "; ".join(best or ["none"]))
    except Exception as e:
        check("9. BigCapital invoice Zenith (paid)", 2, False, f"exception: {e}")


def check_10_bc_invoice_prism():
    """BigCapital: Prism invoice — Gold service x2 @5500, dated/due, delivered, 11000 outstanding."""
    try:
        rows = _bc_invoice_rows("Prism Digital Solutions")
        if not rows:
            check("10. BigCapital invoice Prism (outstanding)", 2, False,
                  "no invoice with line items found for customer 'Prism Digital Solutions'")
            return
        best: list[str] | None = None
        found = False
        for r in rows:
            issues = []
            if r["item"] != "Gold Exhibit Sponsorship Service":
                issues.append(f"item={r['item']!r}")
            if abs(r["qty"] - 2) > 0.001:
                issues.append(f"qty={r['qty']}")
            if abs(r["rate"] - 5500) > 0.01:
                issues.append(f"rate={r['rate']}")
            if r["invoice_date"] != "2026-10-01":
                issues.append(f"invoice_date={r['invoice_date']}")
            if r["due_date"] != "2026-11-01":
                issues.append(f"due_date={r['due_date']}")
            if not r["delivered"]:
                issues.append("not delivered")
            if abs(r["balance"] - 11000) > 0.01:
                issues.append(f"total={r['balance']}, expected 11000")
            # Truly outstanding: no payment applied.
            if abs(r["payment"]) > 0.01:
                issues.append(f"payment={r['payment']}, expected 0 (outstanding)")
            if not issues:
                found = True
            if best is None or len(issues) < len(best):
                best = issues
        check("10. BigCapital invoice Prism (outstanding)", 2, found,
              "delivered, 11000 outstanding, all fields correct" if found
              else "closest invoice line: " + "; ".join(best or ["none"]))
    except Exception as e:
        check("10. BigCapital invoice Prism (outstanding)", 2, False, f"exception: {e}")


def check_11_bc_journal():
    """BigCapital: one published journal (2026-10-18, deferral memo) with both legs of 11000."""
    try:
        rows = bcq(
            "SELECT mj.ID, mj.PUBLISHED_AT, mje.DEBIT, mje.CREDIT, a.NAME, mj.DESCRIPTION "
            "FROM MANUAL_JOURNALS mj "
            "JOIN MANUAL_JOURNALS_ENTRIES mje ON mj.ID = mje.MANUAL_JOURNAL_ID "
            "JOIN ACCOUNTS a ON mje.ACCOUNT_ID = a.ID "
            "WHERE mj.DATE = '2026-10-18'"
        )
        if not rows:
            check("11. BigCapital deferral journal entry", 2, False,
                  "no manual journal dated 2026-10-18")
            return
        journals: dict[int, dict] = {}
        for line in rows.splitlines():
            parts = line.split("\t", 5)
            if len(parts) < 6:
                continue
            jid = int(parts[0])
            published = parts[1] not in ("", "NULL")
            debit = float(parts[2]) if parts[2] and parts[2] != "NULL" else 0.0
            credit = float(parts[3]) if parts[3] and parts[3] != "NULL" else 0.0
            acct = parts[4]
            desc = parts[5]
            j = journals.setdefault(jid, {
                "published": published, "desc": desc,
                "debit_ok": False, "credit_ok": False,
            })
            if acct == "AI Horizons Sponsorship Revenue" and abs(debit - 11000) < 0.01:
                j["debit_ok"] = True
            if acct == "Deferred AI Horizons Sponsorship" and abs(credit - 11000) < 0.01:
                j["credit_ok"] = True

        # A single journal must satisfy all conditions (no cross-journal union).
        best: list[str] | None = None
        found = False
        for j in journals.values():
            # mysql -B escapes embedded newlines/tabs as \n/\t literals
            desc_n = norm_text(j["desc"].replace("\\n", " ").replace("\\t", " "))
            issues = []
            if not j["published"]:
                issues.append("not published")
            if "Defer Platinum sponsorship revenue" not in desc_n:
                issues.append("memo missing 'Defer Platinum sponsorship revenue'")
            if "Zenith Cloud Corp" not in desc_n:
                issues.append("memo missing 'Zenith Cloud Corp'")
            if not j["debit_ok"]:
                issues.append("debit 'AI Horizons Sponsorship Revenue' 11000 not found")
            if not j["credit_ok"]:
                issues.append("credit 'Deferred AI Horizons Sponsorship' 11000 not found")
            if not issues:
                found = True
            if best is None or len(issues) < len(best):
                best = issues

        check("11. BigCapital deferral journal entry", 2, found,
              "published, memo + both legs in one journal" if found
              else "closest journal: " + "; ".join(best or ["none"]))
    except Exception as e:
        check("11. BigCapital deferral journal entry", 2, False, f"exception: {e}")


def check_11b_bc_payment_received():
    """BigCapital: Payment Received 11000 on 2026-10-18 from 'Saving Bank Account' against the Zenith invoice."""
    try:
        rows = bcq(
            "SELECT pr.AMOUNT, pr.PAYMENT_DATE, da.NAME, pre.INVOICE_ID, pre.PAYMENT_AMOUNT "
            "FROM PAYMENT_RECEIVES pr "
            "JOIN ACCOUNTS da ON da.ID = pr.DEPOSIT_ACCOUNT_ID "
            "JOIN PAYMENT_RECEIVES_ENTRIES pre ON pre.PAYMENT_RECEIVE_ID = pr.ID "
            "JOIN CONTACTS c ON c.ID = pr.CUSTOMER_ID "
            "WHERE c.DISPLAY_NAME='Zenith Cloud Corp'"
        )
        if not rows:
            check("11b. BigCapital payment received (Zenith)", 1, False,
                  "no Payment Received found for 'Zenith Cloud Corp'")
            return
        best: list[str] | None = None
        found = False
        for line in rows.splitlines():
            p = line.split("\t")
            if len(p) < 5:
                continue
            amount = float(p[0]) if p[0] and p[0] != "NULL" else 0.0
            pay_date = p[1][:10]
            acct = p[2]
            inv_id = int(p[3]) if p[3] and p[3] != "NULL" else 0
            issues = []
            if abs(amount - 11000) > 0.01:
                issues.append(f"amount={amount}")
            if pay_date != "2026-10-18":
                issues.append(f"payment_date={pay_date}")
            if acct != "Saving Bank Account":
                issues.append(f"deposit account={acct!r}")
            if not _zenith_invoice_ids:
                issues.append("no verified Zenith invoice to apply against (see check 9)")
            elif inv_id not in _zenith_invoice_ids:
                issues.append(f"applied to invoice id={inv_id}, not the verified Zenith invoice")
            if not issues:
                found = True
                break
            if best is None or len(issues) < len(best):
                best = issues
        check("11b. BigCapital payment received (Zenith)", 1, found,
              "11000 on 2026-10-18 from Saving Bank Account, applied to the Zenith invoice"
              if found else "closest payment: " + "; ".join(best or ["none"]))
    except Exception as e:
        check("11b. BigCapital payment received (Zenith)", 1, False, f"exception: {e}")


# ── Twenty CRM (5 checks) ────────────────────────────────────────────────────

def check_12_twenty_companies():
    """6 companies exist in Twenty with correct domains."""
    try:
        ws = tw_schema()
        expected = {
            "Zenith Cloud Corp": "zenithcloud.com",
            "Prism Digital Solutions": "prismdigital.com",
            "Ironclad Analytics": None,
            "Mosaic Data Labs": None,
            "Parallax Systems": None,
            "Cipher Tech Inc": None,
        }
        names_sql = ", ".join(f"'{n}'" for n in expected)
        rows = twq(
            f'SELECT "name", "domainNamePrimaryLinkUrl" '
            f'FROM {ws}.company WHERE "deletedAt" IS NULL '
            f'AND "name" IN ({names_sql})'
        )
        found: dict[str, str] = {}
        for line in rows.splitlines():
            if "|" in line:
                n, d = line.split("|", 1)
                found[n] = d

        issues = []
        for name, exp_domain in expected.items():
            if name not in found:
                issues.append(f"{name}: missing")
            elif exp_domain and exp_domain not in found[name]:
                issues.append(f"{name}: domain={found[name]!r}")

        check("12. Twenty companies", 2, not issues,
              f"all 6 present" if not issues else "; ".join(issues))
    except Exception as e:
        check("12. Twenty companies", 2, False, f"exception: {e}")


def check_13_twenty_people():
    """6 people with correct emails, titles, and company links."""
    try:
        ws = tw_schema()
        expected = [
            ("sponsor@zenithcloud.com", "Howard", "Lim", "Chief Technology Officer", "Zenith Cloud Corp"),
            ("events@prismdigital.com", "Beatrice", "Fontaine", "Head of Partnerships", "Prism Digital Solutions"),
            ("owen.stafford@ironclad-analytics.com", "Owen", "Stafford", None, "Ironclad Analytics"),
            ("yuki.tanaka@mosaicdatalabs.io", "Yuki", "Tanaka", None, "Mosaic Data Labs"),
            ("renee.holloway@parallaxsystems.com", "Renee", "Holloway", None, "Parallax Systems"),
            ("andre.dubois@ciphertech.com", "Andre", "Dubois", None, "Cipher Tech Inc"),
        ]
        emails_sql = ", ".join(f"'{e}'" for e, *_ in expected)
        rows = twq(
            f'SELECT p."emailsPrimaryEmail", p."nameFirstName", p."nameLastName", '
            f'p."jobTitle", c."name" AS company_name '
            f'FROM {ws}.person p '
            f'LEFT JOIN {ws}.company c ON p."companyId" = c.id '
            f'WHERE p."deletedAt" IS NULL '
            f'AND p."emailsPrimaryEmail" IN ({emails_sql})'
        )
        found: dict[str, dict] = {}
        for line in rows.splitlines():
            if "|" in line:
                parts = line.split("|")
                found[parts[0]] = {
                    "first": parts[1], "last": parts[2],
                    "title": parts[3] if len(parts) > 3 else "",
                    "company": parts[4] if len(parts) > 4 else "",
                }

        issues = []
        for email, first, last, title, company in expected:
            p = found.get(email)
            if not p:
                issues.append(f"{first} {last}: missing")
            else:
                # The whole name may land in nameFirstName (UI text insertion doesn't
                # trigger Twenty's first/last split) — compare the trimmed concatenation.
                stored_full = " ".join(f"{p['first']} {p['last']}".split()).lower()
                expected_full = " ".join(f"{first} {last}".split()).lower()
                if stored_full != expected_full:
                    issues.append(f"{email}: name={p['first']} {p['last']}")
                if title and title.lower() not in (p.get("title") or "").lower():
                    issues.append(f"{first} {last}: title={p['title']!r}")
                if company.lower() not in (p.get("company") or "").lower():
                    issues.append(f"{first} {last}: company={p['company']!r}")

        check("13. Twenty people", 2, not issues,
              f"all 6 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("13. Twenty people", 2, False, f"exception: {e}")


def check_14_twenty_opportunities():
    """6 opportunities with exact titles, amounts, stages, close dates, company links."""
    try:
        ws = tw_schema()
        expected = [
            ("Platinum Sponsor — Zenith Cloud Corp", 11000, "WON", "2026-10-18", "Zenith Cloud Corp"),
            ("Gold Sponsor — Prism Digital Solutions", 11000, "PROPOSAL", "2026-11-01", "Prism Digital Solutions"),
            ("Conference Lead — Ironclad Analytics", 14000, "QUALIFICATION", "2027-02-28", "Ironclad Analytics"),
            ("Conference Lead — Mosaic Data Labs", 14000, "QUALIFICATION", "2027-02-28", "Mosaic Data Labs"),
            ("Conference Lead — Parallax Systems", 14000, "QUALIFICATION", "2027-02-28", "Parallax Systems"),
            ("Conference Lead — Cipher Tech Inc", 14000, "QUALIFICATION", "2027-02-28", "Cipher Tech Inc"),
        ]

        rows = twq(
            f'SELECT o."name", o."amountAmountMicros", o."stage", '
            f'o."closeDate", c."name" AS company_name '
            f'FROM {ws}.opportunity o '
            f'LEFT JOIN {ws}.company c ON o."companyId" = c.id '
            f'WHERE o."deletedAt" IS NULL'
        )
        # Titles are compared for equality after dash normalization (em/en dash vs '-').
        found: dict[str, dict] = {}
        for line in rows.splitlines():
            if "|" in line:
                parts = line.split("|")
                if len(parts) < 5:
                    continue
                micros = int(parts[1]) if parts[1] else 0
                found[norm_text(parts[0])] = {
                    "amount": micros / 1_000_000,
                    "stage": parts[2].upper() if parts[2] else "",
                    "closeDate": parts[3],
                    "company": parts[4],
                }

        issues = []
        for name, exp_amt, exp_stage, exp_date, exp_company in expected:
            opp = found.get(norm_text(name))
            if not opp:
                issues.append(f"{name}: missing (exact title required)")
                continue
            if abs(opp["amount"] - exp_amt) > 1:
                issues.append(f"{name}: amount={opp['amount']}")
            if opp["stage"] != exp_stage:
                issues.append(f"{name}: stage={opp['stage']!r} expected {exp_stage!r}")
            if not date_matches_tz(opp["closeDate"], exp_date):
                issues.append(f"{name}: closeDate={opp['closeDate']!r}")
            if exp_company.lower() not in opp["company"].lower():
                issues.append(f"{name}: company={opp['company']!r}")

        check("14. Twenty opportunities", 2, not issues,
              f"all 6 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("14. Twenty opportunities", 2, False, f"exception: {e}")


def check_15_twenty_tasks():
    """4 lead follow-up tasks: due date, full body, linked to the lead company."""
    try:
        ws = tw_schema()
        col = tt_company_col()
        leads = ["Ironclad Analytics", "Mosaic Data Labs", "Parallax Systems", "Cipher Tech Inc"]
        body_fragments = (
            "AI Horizons 2026 on 2026-12-03",
            "14000",
            "discovery call within 5 business days",
        )
        issues = []
        for company in leads:
            rows = twq(
                f"SELECT t.id, t.\"title\", t.\"dueAt\", "
                f"regexp_replace(COALESCE(t.\"bodyV2Markdown\",''), E'[\\n\\r]+', ' ', 'g') "
                f"FROM {ws}.task t "
                f"WHERE t.\"deletedAt\" IS NULL "
                f"AND t.\"title\" LIKE '%{company}%' "
                f"AND t.\"title\" LIKE '%follow%'"
            )
            if not rows:
                issues.append(f"task for {company}: missing")
                continue
            best: list[str] | None = None
            matched = False
            for line in rows.splitlines():
                parts = line.split("|", 3)
                if len(parts) < 4:
                    continue
                tid, _title, due, body = parts
                row_issues = []
                if not date_matches_tz(due, "2026-12-10"):
                    row_issues.append(f"dueAt={due!r}")
                body_n = " ".join(body.split())
                for frag in body_fragments:
                    if frag not in body_n:
                        row_issues.append(f"body missing {frag!r}")
                linked = twq(
                    f"SELECT c.\"name\" FROM {ws}.\"taskTarget\" tt "
                    f"JOIN {ws}.company c ON c.id = tt.\"{col}\" "
                    f"WHERE tt.\"taskId\"='{tid}' "
                    f"AND tt.\"deletedAt\" IS NULL AND c.\"deletedAt\" IS NULL"
                )
                names = {l.strip() for l in linked.splitlines() if l.strip()}
                if company not in names:
                    row_issues.append(f"not linked to company (targets={sorted(names)})")
                if not row_issues:
                    matched = True
                    break
                if best is None or len(row_issues) < len(best):
                    best = row_issues
            if not matched:
                issues.append(f"{company}: " + "; ".join(best or ["no parseable task row"]))

        check("15. Twenty lead tasks", 1, not issues,
              "all 4 correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("15. Twenty lead tasks", 1, False, f"exception: {e}")


def check_16_twenty_note():
    """Pipeline summary note: every numeric/status fact of the specified body present."""
    try:
        ws = tw_schema()
        rows = twq(
            f"SELECT \"title\", "
            f"regexp_replace(COALESCE(\"bodyV2Markdown\",''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM {ws}.note "
            f"WHERE \"deletedAt\" IS NULL "
            f"AND \"title\" LIKE '%Sponsorship%Lead Pipeline%'"
        )
        if not rows:
            check("16. Twenty pipeline note", 2, False, "note not found")
            return

        # The task body is given verbatim; require every factual fragment (numbers,
        # statuses, codes) after whitespace/dash normalization.
        fragments = [
            "Conference: AI Horizons 2026",
            "2026-12-03",
            "Zenith Cloud Corp (Platinum): 11000 USD",
            "PAID 2026-10-18",
            "revenue deferred",
            "Prism Digital Solutions (Gold",
            "PENDING due 2026-11-01",
            "PLAT-AIHORIZONS-001",
            "GOLD-AIHORIZONS-001",
            "5 uses each",
            "Leads captured: 4 companies",
            "56000",
            "2026-12-10",
        ]
        best_missing: list[str] | None = None
        for line in rows.splitlines():
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            body_n = norm_text(parts[1])
            missing = [f for f in fragments if norm_text(f) not in body_n]
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
            if not missing:
                break
        ok = best_missing == []
        check("16. Twenty pipeline note", 2, ok,
              "body matches all required facts" if ok
              else f"missing: {best_missing}")
    except Exception as e:
        check("16. Twenty pipeline note", 2, False, f"exception: {e}")


# ── HRMS (1 check) ───────────────────────────────────────────────────────────

def check_17_hrms_job_opening():
    """HRMS: Job Opening 'Booth Staff — AI Horizons 2026' with correct details."""
    try:
        results = hrms_get(
            "Job Opening",
            filters=[["job_title", "like", "%Booth Staff%"]],
            fields=["job_title", "department", "designation", "status", "description"],
        )
        if not results:
            check("17. HRMS job opening", 1, False, "job opening not found")
            return
        exp_title = norm_text("Booth Staff — AI Horizons 2026")
        # Prefer the record whose (dash-normalized) title matches exactly.
        jo = next((r for r in results
                   if norm_text(r.get("job_title") or "") == exp_title), results[0])
        issues = []
        if norm_text(jo.get("job_title") or "") != exp_title:
            issues.append(f"title={jo.get('job_title')!r} (exact match required)")
        dept = norm_text((jo.get("department") or "").replace("&amp;", "&"))
        if dept != "Research & Development - TVS":
            issues.append(f"dept={jo.get('department')!r}")
        if (jo.get("designation") or "").lower() != "secretary":
            issues.append(f"designation={jo.get('designation')!r}")
        # NOTE: no vacancies assertion — the deployed HRMS Job Opening form does not
        # expose a vacancies field (it is staffing-plan gated), so it cannot be set via UI.
        if (jo.get("status") or "").lower() != "open":
            issues.append(f"status={jo.get('status')!r}")
        desc = " ".join((jo.get("description") or "").lower().split())
        for phrase in ("badge scanning", "lead capture", "sponsor coordination",
                       "ai horizons 2026 on 2026-12-03"):
            if phrase not in desc:
                issues.append(f"description missing {phrase!r}")

        check("17. HRMS job opening", 1, not issues,
              "all fields correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("17. HRMS job opening", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_pretix_event()
    check_2_pretix_products()
    check_3_pretix_quotas()
    check_4_pretix_vouchers()
    check_5_pretix_questions()
    check_6_pretix_checkin()
    check_7_pretix_tax_discount()
    check_8_bc_accounts_items()
    check_9_bc_invoice_zenith()
    check_10_bc_invoice_prism()
    check_11_bc_journal()
    check_11b_bc_payment_received()
    check_12_twenty_companies()
    check_13_twenty_people()
    check_14_twenty_opportunities()
    check_15_twenty_tasks()
    check_16_twenty_note()
    check_17_hrms_job_opening()

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
