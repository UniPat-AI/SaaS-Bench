#!/usr/bin/env python3
"""
Verifier for Business-051-I2: Fundraising Gala Setup with Sponsorship, Accounting, and CRM

Checks: 19 weighted checks across pretix, bigcapital, twenty (total weight 30).
Strategy: docker exec DB queries for all three sites.

Required env vars:
  SERVER_HOSTNAME, PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import json
import os
import sys
import subprocess
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

PRETIX_PORT = os.getenv("PRETIX_PORT")
PRETIX_CONTAINER = os.getenv("PRETIX_CONTAINER")
PRETIX_DB = os.getenv("PRETIX_DB_CONTAINER")

BC_PORT = os.getenv("BIGCAPITAL_PORT")
BC_CONTAINER = os.getenv("BIGCAPITAL_CONTAINER")
BC_DB = os.getenv("BIGCAPITAL_DB_CONTAINER")

TWENTY_PORT = os.getenv("TWENTY_PORT")
TWENTY_CONTAINER = os.getenv("TWENTY_CONTAINER")
TWENTY_DB = os.getenv("TWENTY_DB_CONTAINER")

for _var in [
    "PRETIX_PORT", "PRETIX_CONTAINER", "PRETIX_DB_CONTAINER",
    "BIGCAPITAL_PORT", "BIGCAPITAL_CONTAINER", "BIGCAPITAL_DB_CONTAINER",
    "TWENTY_PORT", "TWENTY_CONTAINER", "TWENTY_DB_CONTAINER",
]:
    if not os.getenv(_var):
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

EVENT_SLUG = "stars-stripes-gala-2025"

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


def pretix_q(sql: str) -> str:
    """Run SQL on Pretix Postgres DB."""
    rc, out, err = docker_exec(
        PRETIX_DB, "psql", "-U", "pretix", "-d", "pretix", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"pretix psql error (rc={rc}): {err.strip()[-500:]}")
    return out.strip()


_bc_tenant_db: str = ""


def _discover_bc_tenant_db() -> None:
    global _bc_tenant_db
    rc, out, err = docker_exec(
        BC_DB, "mysql",
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


def bc_q(sql: str) -> str:
    """Run SQL on BigCapital MariaDB (tenant DB, auto-discovered)."""
    if not _bc_tenant_db:
        _discover_bc_tenant_db()
    rc, out, err = docker_exec(
        BC_DB, "mysql",
        "--default-character-set=utf8mb4",
        "-u", "bigcapital", "-pbigcapital123",
        "-D", _bc_tenant_db,
        "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error: {err.strip()}")
    return out.strip()


def twenty_q(sql: str) -> str:
    """Run SQL on Twenty Postgres DB."""
    rc, out, err = docker_exec(
        TWENTY_DB, "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"twenty psql error (rc={rc}): {err.strip()[-500:]}")
    return out.strip()


_ws_schema: str | None = None


def ws() -> str:
    """Get (cached) Twenty workspace schema name."""
    global _ws_schema
    if _ws_schema is None:
        r = twenty_q(
            # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
            # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
            # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
            # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
            'SELECT ds.schema FROM core."dataSource" ds '
            'JOIN core.workspace w ON w.id = ds."workspaceId" '
            "WHERE w.subdomain = 'yc';"
        )
        if not r:
            raise RuntimeError("No workspace schema found in Twenty DB")
        _ws_schema = r.split("\n")[0].strip()
    return _ws_schema


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


def norm(s: str) -> str:
    """Normalize dashes (em/en/double) to '-' and collapse whitespace."""
    s = s or ""
    for d in ("—", "–", "--"):
        s = s.replace(d, "-")
    return " ".join(s.split())


def fnum(s: str) -> float | None:
    """Parse a numeric field from mysql -N -B output (SQL NULL prints as 'NULL')."""
    s = (s or "").strip()
    if not s or s.upper() == "NULL":
        return None
    try:
        return float(s)
    except ValueError:
        return None


_LOCAL_UTC_OFFSET = datetime.now().astimezone().utcoffset() or timedelta(0)


def date_matches_tz(stored: str, exp_date: str) -> bool:
    """True if a stored timestamp corresponds to local calendar date exp_date.

    Twenty stores date fields as UTC timestamps; a local date D entered in the UI
    is stored shifted back by the host UTC offset (e.g. UTC+8 -> (D-1)T16:00:00Z).
    Pure-date values (exactly 10 chars) compare by equality; timestamp-shaped
    values compare the UTC date or the local-offset-shifted date — no bare
    substring matching.
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
    return (dt + _LOCAL_UTC_OFFSET).strftime("%Y-%m-%d") == exp_date


# ── Shared pretix lookups ─────────────────────────────────────────────────────
_gala_items: dict[str, int] | None = None


def gala_item_ids() -> dict[str, int]:
    """Map of product name -> item id for the gala event (cached)."""
    global _gala_items
    if _gala_items is None:
        r = pretix_q(
            "SELECT i.id, i.name::text FROM pretixbase_item i "
            "JOIN pretixbase_event e ON i.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        items: dict[str, int] = {}
        for line in r.splitlines():
            if "|" not in line:
                continue
            iid, raw_name = line.split("|", 1)
            try:
                items[i18n_en(raw_name)] = int(iid.strip())
            except ValueError:
                continue
        _gala_items = items
    return _gala_items


def quota_item_ids(quota_id: int) -> set[int]:
    r = pretix_q(
        f"SELECT DISTINCT item_id FROM pretixbase_quota_items WHERE quota_id = {quota_id};"
    )
    return {int(x) for x in r.split() if x.strip().isdigit()}


# Invoice IDs located by check 11, consumed by checks 12 and 13b.
_located: dict[str, int] = {}


# ── Pretix checks (1-7) ──────────────────────────────────────────────────────

def check_1_pretix_event() -> None:
    """Event exists under organizer nyc-cultural with correct name, slug, date, currency, live."""
    try:
        r = pretix_q(
            "SELECT e.slug, e.date_from, e.currency, e.live, o.slug, e.name::text "
            "FROM pretixbase_event e "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        if not r:
            check("1. Pretix event + live", 2, False, "event not found")
            return
        p = r.split("|")
        slug_ok = p[0] == EVENT_SLUG
        date_ok = p[1].startswith("2025-12-06")
        curr_ok = p[2] == "USD"
        live_ok = p[3].lower() in ("t", "true", "1")
        org_ok = p[4].strip() == "nyc-cultural" if len(p) > 4 else False
        name_text = i18n_en("|".join(p[5:])) if len(p) > 5 else ""
        name_ok = "Stars & Stripes Charity Gala 2025" in name_text
        ok = slug_ok and date_ok and curr_ok and live_ok and org_ok and name_ok
        check("1. Pretix event + live", 2, ok,
              f"slug={p[0]}, date={p[1]}, curr={p[2]}, live={p[3]}, "
              f"organizer_ok={org_ok}, name_ok={name_ok}")
    except Exception as e:
        check("1. Pretix event + live", 2, False, f"exception: {e}")


def check_2_pretix_categories() -> None:
    """Three categories: Platinum Benefactors, Gold Benefactors, Silver Supporters Circle."""
    try:
        r = pretix_q(
            "SELECT c.name::text FROM pretixbase_itemcategory c "
            "JOIN pretixbase_event e ON c.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        expected = ["Platinum Benefactors", "Gold Benefactors", "Silver Supporters Circle"]
        found = [c for c in expected if c in r]
        check("2. Pretix categories", 1, len(found) == 3,
              f"found {len(found)}/3: {found}")
    except Exception as e:
        check("2. Pretix categories", 1, False, f"exception: {e}")


def check_3_pretix_products() -> None:
    """Three products with correct prices and category assignments."""
    try:
        r = pretix_q(
            "SELECT i.name::text, i.default_price, c.name::text "
            "FROM pretixbase_item i "
            "JOIN pretixbase_event e ON i.event_id = e.id "
            "LEFT JOIN pretixbase_itemcategory c ON i.category_id = c.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        lines = [l for l in r.split("\n") if l.strip()]
        expected = [
            ("Platinum Gala Table", 15000.0, "Platinum Benefactors"),
            ("Gold Gala Table", 7500.0, "Gold Benefactors"),
            ("Silver Gala Seat", 750.0, "Silver Supporters Circle"),
        ]
        issues = []
        for name, price, cat in expected:
            matched = [l for l in lines if name in l]
            if not matched:
                issues.append(f"{name}: not found")
                continue
            parts = matched[0].split("|")
            if len(parts) >= 2:
                try:
                    if abs(float(parts[1]) - price) > 0.01:
                        issues.append(f"{name}: price={parts[1]}, expected={price}")
                except ValueError:
                    issues.append(f"{name}: price parse error ({parts[1]})")
            if len(parts) >= 3 and cat not in parts[2]:
                issues.append(f"{name}: wrong category ({parts[2]})")
        ok = not issues
        check("3. Pretix products", 2, ok,
              "all 3 correct" if ok else str(issues))
    except Exception as e:
        check("3. Pretix products", 2, False, f"exception: {e}")


def check_4_pretix_quotas() -> None:
    """Three quotas with correct sizes, each linked to exactly its own product."""
    try:
        items = gala_item_ids()
        id_to_name = {v: k for k, v in items.items()}
        r = pretix_q(
            "SELECT q.id, q.size, q.name FROM pretixbase_quota q "
            "JOIN pretixbase_event e ON q.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        expected = {
            "Platinum Benefactors Quota": (4, {"Platinum Gala Table"}),
            "Gold Benefactors Quota": (8, {"Gold Gala Table"}),
            "Silver Supporters Quota": (60, {"Silver Gala Seat"}),
        }
        found: dict[str, tuple[int, int]] = {}
        for line in r.split("\n"):
            if "|" not in line:
                continue
            qid, size, name = line.split("|", 2)
            try:
                found[name.strip()] = (int(qid.strip()), int(size.strip()))
            except ValueError:
                continue
        issues = []
        for name, (size, exp_items) in expected.items():
            if name not in found:
                issues.append(f"{name}: not found")
                continue
            qid, got_size = found[name]
            if got_size != size:
                issues.append(f"{name}: got size {got_size}, expected {size}")
            linked = {id_to_name.get(i, f"item#{i}") for i in quota_item_ids(qid)}
            if linked != exp_items:
                issues.append(f"{name}: items={sorted(linked)}, expected {sorted(exp_items)}")
        check("4. Pretix quotas", 2, not issues,
              "all 3 correct + linked" if not issues else str(issues))
    except Exception as e:
        check("4. Pretix quotas", 2, False, f"exception: {e}")


def check_5_pretix_question() -> None:
    """Required 'Company Name' one-line text question (type S) covering all 3 products."""
    try:
        items = gala_item_ids()
        r = pretix_q(
            "SELECT q.id, q.type, q.required, q.question::text "
            "FROM pretixbase_question q "
            "JOIN pretixbase_event e ON q.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        found = False
        detail = "not found or misconfigured"
        for line in r.split("\n"):
            if "|" not in line:
                continue
            qid, qtype, req, raw_q = line.split("|", 3)
            if "Company Name" not in i18n_en(raw_q):
                continue
            # Pretix type 'S' = String (one line) — the task asks for Text (one line)
            type_ok = qtype.strip() == "S"
            req_ok = req.strip().lower() in ("t", "true", "1")
            linked = pretix_q(
                "SELECT DISTINCT item_id FROM pretixbase_question_items "
                f"WHERE question_id = {int(qid.strip())};"
            )
            linked_ids = {int(x) for x in linked.split() if x.strip().isdigit()}
            cover_ok = bool(items) and set(items.values()) <= linked_ids
            detail = (f"type={qtype.strip()}, required={req.strip()}, "
                      f"items_covered={len(linked_ids & set(items.values()))}/{len(items)}")
            if type_ok and req_ok and cover_ok:
                found = True
                break
        check("5. Pretix Company Name question", 1, found, detail)
    except Exception as e:
        check("5. Pretix Company Name question", 1, False, f"exception: {e}")


def check_6_pretix_voucher() -> None:
    """Voucher GALASPONSOR2025: 20%, max 15, valid until 2025-12-06, scoped to Platinum+Gold."""
    try:
        items = gala_item_ids()
        plat_id = items.get("Platinum Gala Table")
        gold_id = items.get("Gold Gala Table")
        silver_id = items.get("Silver Gala Seat")
        r = pretix_q(
            "SELECT v.price_mode, v.value, v.max_usages, v.valid_until, "
            "v.item_id, v.quota_id "
            "FROM pretixbase_voucher v "
            "JOIN pretixbase_event e ON v.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}' AND v.code = 'GALASPONSOR2025';"
        )
        if not r:
            check("6. Pretix voucher GALASPONSOR2025", 2, False, "not found")
            return
        p = r.split("|")
        mode_ok = p[0].strip() == "percent"
        val_ok = abs(float(p[1].strip()) - 20.0) < 0.01
        max_ok = int(p[2].strip()) == 15
        valid_ok = "2025-12-06" in (p[3].strip() if len(p) > 3 else "")
        item_id = int(p[4].strip()) if len(p) > 4 and p[4].strip().isdigit() else None
        quota_id = int(p[5].strip()) if len(p) > 5 and p[5].strip().isdigit() else None
        # Scope: not unrestricted, and restricted to {Platinum, Gold} — either
        # directly via item_id, or via a quota covering both and not Silver.
        scope_ok = False
        scope_detail = "unrestricted"
        if item_id is not None:
            scope_ok = item_id in {plat_id, gold_id}
            scope_detail = f"item_id={item_id}"
        elif quota_id is not None:
            q_items = quota_item_ids(quota_id)
            scope_ok = (
                plat_id in q_items and gold_id in q_items
                and (silver_id is None or silver_id not in q_items)
            )
            scope_detail = f"quota_id={quota_id}, quota_items={sorted(q_items)}"
        ok = mode_ok and val_ok and max_ok and valid_ok and scope_ok
        check("6. Pretix voucher GALASPONSOR2025", 2, ok,
              f"mode={p[0].strip()}, value={p[1].strip()}, max={p[2].strip()}, "
              f"valid_until={p[3].strip() if len(p) > 3 else 'N/A'}, "
              f"scope_ok={scope_ok} ({scope_detail})")
    except Exception as e:
        check("6. Pretix voucher GALASPONSOR2025", 2, False, f"exception: {e}")


def check_7_pretix_checkin() -> None:
    """Check-in list 'Stars & Stripes Gala Check-In List' covering all three products."""
    try:
        items = gala_item_ids()
        r = pretix_q(
            "SELECT cl.id, cl.all_products, cl.name FROM pretixbase_checkinlist cl "
            "JOIN pretixbase_event e ON cl.event_id = e.id "
            f"WHERE e.slug = '{EVENT_SLUG}';"
        )
        target: tuple[int, bool] | None = None
        for line in r.split("\n"):
            if "|" not in line:
                continue
            cid, all_p, name = line.split("|", 2)
            if name.strip() == "Stars & Stripes Gala Check-In List":
                target = (int(cid.strip()), all_p.strip().lower() in ("t", "true", "1"))
                break
        if target is None:
            check("7. Pretix check-in list", 1, False,
                  f"not found, got: {r[:200]}")
            return
        cid, all_products = target
        if all_products:
            covered = True
            detail = "found, all_products=true"
        else:
            linked = pretix_q(
                "SELECT DISTINCT item_id FROM pretixbase_checkinlist_limit_products "
                f"WHERE checkinlist_id = {cid};"
            )
            linked_ids = {int(x) for x in linked.split() if x.strip().isdigit()}
            covered = bool(items) and set(items.values()) <= linked_ids
            detail = (f"found, all_products=false, "
                      f"items_covered={len(linked_ids & set(items.values()))}/{len(items)}")
        check("7. Pretix check-in list", 1, covered, detail)
    except Exception as e:
        check("7. Pretix check-in list", 1, False, f"exception: {e}")


# ── BigCapital checks (8-13b) ────────────────────────────────────────────────

def check_8_bc_accounts() -> None:
    """Income account 'Stars & Stripes Gala Revenue' and liability account 'Restricted Gala Sponsorship Fund'."""
    try:
        r = bc_q(
            "SELECT NAME, ACCOUNT_TYPE FROM ACCOUNTS "
            "WHERE NAME IN ('Stars & Stripes Gala Revenue', "
            "'Restricted Gala Sponsorship Fund');"
        )
        types: dict[str, str] = {}
        for line in r.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                types[parts[0].strip()] = parts[1].strip()
        rev_ok = types.get("Stars & Stripes Gala Revenue") == "income"
        fund_ok = types.get("Restricted Gala Sponsorship Fund") == "other-current-liability"
        check("8. BigCapital accounts", 1, rev_ok and fund_ok,
              f"revenue_type={types.get('Stars & Stripes Gala Revenue')!r}, "
              f"fund_type={types.get('Restricted Gala Sponsorship Fund')!r}")
    except Exception as e:
        check("8. BigCapital accounts", 1, False, f"exception: {e}")


def check_9_bc_customers() -> None:
    """Customers 'Pinnacle Ventures Corp' and 'Horizon Media Group' with correct emails."""
    try:
        r = bc_q(
            "SELECT DISPLAY_NAME, EMAIL FROM CONTACTS "
            "WHERE DISPLAY_NAME IN ('Pinnacle Ventures Corp', 'Horizon Media Group') "
            "AND CONTACT_SERVICE = 'customer';"
        )
        emails: dict[str, str] = {}
        for line in r.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                emails[parts[0].strip()] = parts[1].strip().lower()
        pin_ok = emails.get("Pinnacle Ventures Corp") == "contact@pinnacleventures.com"
        hor_ok = emails.get("Horizon Media Group") == "info@horizonmediagroup.com"
        check("9. BigCapital customers", 1, pin_ok and hor_ok,
              f"pinnacle_email={emails.get('Pinnacle Ventures Corp')!r}, "
              f"horizon_email={emails.get('Horizon Media Group')!r}")
    except Exception as e:
        check("9. BigCapital customers", 1, False, f"exception: {e}")


def check_10_bc_items() -> None:
    """Service items with correct type, sell prices, sell account, and descriptions."""
    try:
        r = bc_q(
            "SELECT i.NAME, i.TYPE, i.SELL_PRICE, a.NAME, i.SELL_DESCRIPTION "
            "FROM ITEMS i "
            "LEFT JOIN ACCOUNTS a ON a.ID = i.SELL_ACCOUNT_ID "
            "WHERE i.NAME IN ('Platinum Gala Sponsorship Service', "
            "'Gold Gala Sponsorship Service');"
        )
        expected = {
            "Platinum Gala Sponsorship Service": (15000.0, "Platinum table sponsorship"),
            "Gold Gala Sponsorship Service": (7500.0, "Gold table sponsorship"),
        }
        rows: dict[str, tuple[str, float | None, str, str]] = {}
        for line in r.split("\n"):
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            rows[parts[0].strip()] = (
                parts[1].strip().lower(),          # TYPE
                fnum(parts[2]),                    # SELL_PRICE
                parts[3].strip(),                  # sell account name
                norm(parts[4]),                    # SELL_DESCRIPTION (dash-normalized)
            )
        issues = []
        for name, (exp_price, key_phrase) in expected.items():
            row = rows.get(name)
            if row is None:
                issues.append(f"{name}: not found")
                continue
            typ, price, acct, desc = row
            if typ != "service":
                issues.append(f"{name}: type={typ!r}")
            if price is None or abs(price - exp_price) > 0.01:
                issues.append(f"{name}: sell_price={price}")
            if acct != "Stars & Stripes Gala Revenue":
                issues.append(f"{name}: sell_account={acct!r}")
            if norm(key_phrase) not in desc:
                issues.append(f"{name}: description missing {key_phrase!r}")
        check("10. BigCapital service items", 1, not issues,
              "both items correct" if not issues else str(issues))
    except Exception as e:
        check("10. BigCapital service items", 1, False, f"exception: {e}")


def check_11_bc_invoices() -> None:
    """Two delivered invoices (dates, due dates, line items, totals) for Pinnacle and Horizon."""
    try:
        r = bc_q(
            "SELECT c.DISPLAY_NAME, si.ID, si.INVOICE_DATE, si.DUE_DATE, si.BALANCE, "
            "si.DELIVERED_AT, ie.QUANTITY, ie.RATE, i.NAME "
            "FROM SALES_INVOICES si "
            "JOIN CONTACTS c ON si.CUSTOMER_ID = c.ID "
            "JOIN ITEMS_ENTRIES ie ON ie.REFERENCE_TYPE = 'SaleInvoice' "
            "AND ie.REFERENCE_ID = si.ID "
            "JOIN ITEMS i ON i.ID = ie.ITEM_ID "
            "WHERE c.DISPLAY_NAME IN ('Pinnacle Ventures Corp', 'Horizon Media Group');"
        )
        if not r:
            check("11. BigCapital invoices", 2, False, "no invoice line items found")
            return
        # BALANCE is the invoice total in BigCapital (not the open balance).
        expected = {
            "Pinnacle Ventures Corp": ("pinnacle", 1.0, 15000.0,
                                       "Platinum Gala Sponsorship Service"),
            "Horizon Media Group": ("horizon", 2.0, 7500.0,
                                    "Gold Gala Sponsorship Service"),
        }
        found: dict[str, bool] = {"pinnacle": False, "horizon": False}
        for line in r.split("\n"):
            parts = line.split("\t")
            if len(parts) < 9:
                continue
            cust = parts[0].strip()
            if cust not in expected:
                continue
            key, exp_qty, exp_rate, exp_item = expected[cust]
            inv_date_ok = parts[2].strip().startswith("2025-10-01")
            due_ok = parts[3].strip().startswith("2025-11-15")
            balance = fnum(parts[4])
            total_ok = balance is not None and abs(balance - 15000.0) < 0.01
            delivered = parts[5].strip()
            delivered_ok = delivered not in ("", "\\N", "NULL", "None")
            qty = fnum(parts[6])
            rate = fnum(parts[7])
            qty_ok = qty is not None and abs(qty - exp_qty) < 0.001
            rate_ok = rate is not None and abs(rate - exp_rate) < 0.01
            item_ok = parts[8].strip() == exp_item
            if (inv_date_ok and due_ok and total_ok and delivered_ok
                    and qty_ok and rate_ok and item_ok):
                found[key] = True
                try:
                    _located[key] = int(parts[1].strip())
                except ValueError:
                    pass
        ok = found["pinnacle"] and found["horizon"]
        check("11. BigCapital invoices", 2, ok,
              f"pinnacle_ok={found['pinnacle']}, horizon_ok={found['horizon']}")
    except Exception as e:
        check("11. BigCapital invoices", 2, False, f"exception: {e}")


def check_12_bc_payment() -> None:
    """Payment of 15000.00 from 'Bank Account' applied to the Pinnacle invoice."""
    try:
        r = bc_q(
            "SELECT pr.AMOUNT, pr.PAYMENT_DATE, da.NAME, pre.INVOICE_ID, "
            "pre.PAYMENT_AMOUNT "
            "FROM PAYMENT_RECEIVES pr "
            "JOIN CONTACTS c ON pr.CUSTOMER_ID = c.ID "
            "AND c.DISPLAY_NAME = 'Pinnacle Ventures Corp' "
            "JOIN ACCOUNTS da ON da.ID = pr.DEPOSIT_ACCOUNT_ID "
            "JOIN PAYMENT_RECEIVES_ENTRIES pre ON pre.PAYMENT_RECEIVE_ID = pr.ID "
            "JOIN SALES_INVOICES si ON si.ID = pre.INVOICE_ID "
            "AND si.CUSTOMER_ID = c.ID;"
        )
        if not r:
            check("12. BigCapital payment", 2, False, "no applied payment found")
            return
        pin_inv = _located.get("pinnacle")
        ok = False
        detail = "no row satisfied all conditions"
        for line in r.split("\n"):
            parts = line.split("\t")
            if len(parts) < 5:
                continue
            amount = fnum(parts[0])
            amt_ok = amount is not None and abs(amount - 15000.0) < 0.01
            # BigCapital stores a local date D as a UTC timestamp that can render as
            # D-1 — accept either the requested date or the day before.
            pay_date = parts[1].strip()
            date_ok = pay_date.startswith("2025-10-22") or pay_date.startswith("2025-10-21")
            acct_ok = parts[2].strip() == "Bank Account"
            entry_amt = fnum(parts[4])
            entry_ok = entry_amt is not None and abs(entry_amt - 15000.0) < 0.01
            inv_ok = True
            if pin_inv is not None:
                try:
                    inv_ok = int(parts[3].strip()) == pin_inv
                except ValueError:
                    inv_ok = False
            detail = (f"amount={parts[0].strip()}, date={pay_date}, "
                      f"account={parts[2].strip()!r}, invoice_bound={inv_ok}")
            if amt_ok and date_ok and acct_ok and entry_ok and inv_ok:
                ok = True
                break
        check("12. BigCapital payment", 2, ok, detail)
    except Exception as e:
        check("12. BigCapital payment", 2, False, f"exception: {e}")


def check_13_bc_journal() -> None:
    """One published journal (2025-10-22, memo) with debit Revenue 15000 and credit Fund 15000."""
    try:
        r = bc_q(
            "SELECT mj.ID, mj.DESCRIPTION FROM MANUAL_JOURNALS mj "
            "WHERE DATE(mj.DATE) = '2025-10-22' AND mj.PUBLISHED_AT IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "JOIN ACCOUNTS a ON a.ID = e.ACCOUNT_ID "
            "WHERE e.MANUAL_JOURNAL_ID = mj.ID "
            "AND a.NAME = 'Stars & Stripes Gala Revenue' "
            "AND ABS(e.DEBIT - 15000) < 0.01) "
            "AND EXISTS (SELECT 1 FROM MANUAL_JOURNALS_ENTRIES e "
            "JOIN ACCOUNTS a ON a.ID = e.ACCOUNT_ID "
            "WHERE e.MANUAL_JOURNAL_ID = mj.ID "
            "AND a.NAME = 'Restricted Gala Sponsorship Fund' "
            "AND ABS(e.CREDIT - 15000) < 0.01);"
        )
        if not r:
            check("13. BigCapital journal entry", 2, False,
                  "no published 2025-10-22 journal with matching debit+credit")
            return
        phrase1 = norm("Reclassify Platinum sponsorship to restricted fund")
        phrase2 = "Pinnacle Ventures Corp"
        ok = False
        detail = "candidate journal found but memo mismatched"
        for line in r.split("\n"):
            parts = line.split("\t", 1)
            desc = norm(parts[1]) if len(parts) > 1 else ""
            detail = f"journal_id={parts[0].strip()}, memo_ok={phrase1 in desc and phrase2 in desc}"
            if phrase1 in desc and phrase2 in desc:
                ok = True
                break
        check("13. BigCapital journal entry", 2, ok, detail)
    except Exception as e:
        check("13. BigCapital journal entry", 2, False, f"exception: {e}")


def check_13b_bc_customer_balances() -> None:
    """Customers Balance Summary truth: Pinnacle balance 0, Horizon balance 15000."""
    try:
        if "pinnacle" not in _located or "horizon" not in _located:
            check("13b. BigCapital customer balances", 1, False,
                  "prerequisite invoices not located by check 11")
            return
        r = bc_q(
            "SELECT c.DISPLAY_NAME, "
            "COALESCE(SUM(si.BALANCE - COALESCE(si.PAYMENT_AMOUNT, 0) "
            "- COALESCE(si.CREDITED_AMOUNT, 0)), 0) "
            "FROM CONTACTS c "
            "LEFT JOIN SALES_INVOICES si ON si.CUSTOMER_ID = c.ID "
            "WHERE c.DISPLAY_NAME IN ('Pinnacle Ventures Corp', 'Horizon Media Group') "
            "AND c.CONTACT_SERVICE = 'customer' "
            "GROUP BY c.ID, c.DISPLAY_NAME;"
        )
        balances: dict[str, float | None] = {}
        for line in r.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                balances[parts[0].strip()] = fnum(parts[1])
        pin = balances.get("Pinnacle Ventures Corp")
        hor = balances.get("Horizon Media Group")
        pin_ok = pin is not None and abs(pin) < 0.01
        hor_ok = hor is not None and abs(hor - 15000.0) < 0.01
        check("13b. BigCapital customer balances", 1, pin_ok and hor_ok,
              f"pinnacle_balance={pin}, horizon_balance={hor}")
    except Exception as e:
        check("13b. BigCapital customer balances", 1, False, f"exception: {e}")


# ── Twenty CRM checks (14-18) ────────────────────────────────────────────────

def check_14_twenty_companies() -> None:
    """Companies 'Pinnacle Ventures Corp' and 'Horizon Media Group' with correct domains."""
    try:
        s = ws()
        r = twenty_q(
            'SELECT "name", "domainNamePrimaryLinkUrl" '
            f"FROM {s}.company "
            'WHERE "deletedAt" IS NULL '
            "AND \"name\" IN ('Pinnacle Ventures Corp', 'Horizon Media Group');"
        )
        found: dict[str, str] = {}
        for line in r.split("\n"):
            if "|" in line:
                n, d = line.split("|", 1)
                found[n.strip()] = d.strip()
        pin_ok = "pinnacleventures.com" in found.get("Pinnacle Ventures Corp", "")
        hor_ok = "horizonmediagroup.com" in found.get("Horizon Media Group", "")
        check("14. Twenty companies", 1, pin_ok and hor_ok,
              f"pinnacle_domain={found.get('Pinnacle Ventures Corp')!r}, "
              f"horizon_domain={found.get('Horizon Media Group')!r}")
    except Exception as e:
        check("14. Twenty companies", 1, False, f"exception: {e}")


def check_15_twenty_people() -> None:
    """Margaret Holloway and Thomas Beaumont with correct titles and company links (same-row)."""
    try:
        s = ws()
        expected = [
            ("contact@pinnacleventures.com", "Margaret Holloway",
             "Chief Executive Officer", "Pinnacle Ventures Corp"),
            ("info@horizonmediagroup.com", "Thomas Beaumont",
             "Director of Corporate Partnerships", "Horizon Media Group"),
        ]
        issues = []
        for email, full_name, title, company in expected:
            r = twenty_q(
                'SELECT p."nameFirstName", p."nameLastName", p."jobTitle", c."name" '
                f"FROM {s}.person p "
                f"LEFT JOIN {s}.company c ON p.\"companyId\" = c.id "
                'WHERE p."deletedAt" IS NULL '
                f"AND p.\"emailsPrimaryEmail\" = '{email}';"
            )
            if not r:
                issues.append(f"{full_name}: no person with email {email}")
                continue
            row_ok = False
            first_issue = ""
            for line in r.split("\n"):
                parts = line.split("|")
                if len(parts) < 4:
                    continue
                # The whole name may land in nameFirstName (UI text insertion doesn't
                # trigger Twenty's first/last split) — compare the trimmed concatenation.
                stored_full = " ".join(f"{parts[0]} {parts[1]}".split()).lower()
                name_ok = stored_full == full_name.lower()
                title_ok = norm(parts[2]).lower() == title.lower()
                company_ok = norm(parts[3]).lower() == company.lower()
                if name_ok and title_ok and company_ok:
                    row_ok = True
                    break
                if not first_issue:
                    first_issue = (f"{full_name}: name={parts[0]} {parts[1]!r}, "
                                   f"title={parts[2]!r}, company={parts[3]!r}")
            if not row_ok:
                issues.append(first_issue or f"{full_name}: no matching row")
        check("15. Twenty people", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("15. Twenty people", 2, False, f"exception: {e}")


def check_16_twenty_opportunities() -> None:
    """Two opportunities with correct titles, amounts, stages, close dates, company links."""
    try:
        s = ws()
        r = twenty_q(
            'SELECT o."name", o."amountAmountMicros", o."stage", o."closeDate", c."name" '
            f"FROM {s}.opportunity o "
            f"LEFT JOIN {s}.company c ON o.\"companyId\" = c.id "
            'WHERE o."deletedAt" IS NULL;'
        )
        found: dict[str, dict] = {}
        for line in r.split("\n"):
            parts = line.split("|")
            if len(parts) < 5:
                continue
            try:
                micros = int(parts[1]) if parts[1].strip() else 0
            except ValueError:
                micros = 0
            found[norm(parts[0]).lower()] = {
                "amount": micros / 1_000_000,
                "stage": parts[2].strip().upper(),
                "closeDate": parts[3].strip(),
                "company": norm(parts[4]),
            }
        expected = [
            ("Pinnacle Ventures — Platinum Sponsorship 2025", 15000.0, "WON",
             "2025-10-22", "Pinnacle Ventures Corp"),
            ("Horizon Media — Gold Sponsorship 2025", 15000.0, "QUALIFICATION",
             "2025-11-15", "Horizon Media Group"),
        ]
        issues = []
        for title, exp_amt, exp_stage, exp_date, exp_company in expected:
            opp = found.get(norm(title).lower())
            if not opp:
                issues.append(f"{title}: not found")
                continue
            if abs(opp["amount"] - exp_amt) > 0.01:
                issues.append(f"{title}: amount={opp['amount']}")
            if opp["stage"] != exp_stage:
                issues.append(f"{title}: stage={opp['stage']!r}")
            if not date_matches_tz(opp["closeDate"], exp_date):
                issues.append(f"{title}: closeDate={opp['closeDate']!r}")
            if opp["company"].lower() != exp_company.lower():
                issues.append(f"{title}: company={opp['company']!r}")
        check("16. Twenty opportunities", 2, not issues,
              "both correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("16. Twenty opportunities", 2, False, f"exception: {e}")


def check_17_twenty_task() -> None:
    """Task 'Collect sponsorship payment — Horizon Media Group': due date, company link, body."""
    try:
        s = ws()
        r = twenty_q(
            'SELECT t.id, t."dueAt", t."title", '
            "regexp_replace(coalesce(t.\"bodyV2Markdown\", ''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM {s}.task t "
            'WHERE t."deletedAt" IS NULL '
            "AND t.\"title\" LIKE '%Collect sponsorship payment%';"
        )
        if not r:
            check("17. Twenty collection task", 2, False, "task not found")
            return
        ok = False
        detail = "no matching row"
        for line in r.split("\n"):
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            tid, due_at, _title, body = parts
            body_n = norm(body).replace(",", "")
            has_amt = "15000" in body_n
            has_name = "Thomas Beaumont" in body_n
            has_email = "info@horizonmediagroup.com" in body_n
            has_voucher = "GALASPONSOR2025" in body_n
            due_ok = date_matches_tz(due_at, "2025-11-15")
            tt = twenty_q(
                'SELECT c."name" '
                f"FROM {s}.\"taskTarget\" tt "
                f"JOIN {s}.company c ON c.id = tt.\"targetCompanyId\" "
                'WHERE tt."deletedAt" IS NULL '
                f"AND tt.\"taskId\" = '{tid.strip()}';"
            )
            company_ok = any(
                norm(l).lower() == "horizon media group" for l in tt.split("\n") if l.strip()
            )
            detail = (f"due_ok={due_ok}, company_linked={company_ok}, amount={has_amt}, "
                      f"contact={has_name and has_email}, voucher={has_voucher}")
            if due_ok and company_ok and has_amt and has_name and has_email and has_voucher:
                ok = True
                break
        check("17. Twenty collection task", 2, ok, detail)
    except Exception as e:
        check("17. Twenty collection task", 2, False, f"exception: {e}")


def check_18_twenty_note() -> None:
    """Note 'Stars & Stripes Charity Gala 2025 — Sponsorship Tracker' with full tracker content."""
    try:
        s = ws()
        r = twenty_q(
            'SELECT "title", '
            "regexp_replace(coalesce(\"bodyV2Markdown\", ''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM {s}.note "
            'WHERE "deletedAt" IS NULL '
            "AND \"title\" LIKE '%Sponsorship Tracker%';"
        )
        if not r:
            check("18. Twenty sponsorship note", 2, False, "note not found")
            return
        fragments = [
            "Pinnacle Ventures Corp",
            "Horizon Media Group",
            "PAID",
            "PENDING",
            "2025-10-22",
            "2025-11-15",
            "30000",
        ]
        ok = False
        detail = "no matching row"
        for line in r.split("\n"):
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            body = norm(parts[1]).replace(",", "")
            missing = [f for f in fragments if f not in body]
            n15000 = body.count("15000")
            detail = f"missing={missing}, count_15000={n15000}"
            if not missing and n15000 >= 3:
                ok = True
                break
        check("18. Twenty sponsorship note", 2, ok, detail)
    except Exception as e:
        check("18. Twenty sponsorship note", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_pretix_event()
    check_2_pretix_categories()
    check_3_pretix_products()
    check_4_pretix_quotas()
    check_5_pretix_question()
    check_6_pretix_voucher()
    check_7_pretix_checkin()
    check_8_bc_accounts()
    check_9_bc_customers()
    check_10_bc_items()
    check_11_bc_invoices()
    check_12_bc_payment()
    check_13_bc_journal()
    check_13b_bc_customer_balances()
    check_14_twenty_companies()
    check_15_twenty_people()
    check_16_twenty_opportunities()
    check_17_twenty_task()
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
