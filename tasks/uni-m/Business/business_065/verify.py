"""
Verifier for Business-065-I3: FY2026 Financial Audit Preparation Across BigCapital, HRMS, and Twenty CRM

Checks: 15 weighted checks across bigcapital, hrms, twenty (total weight 25).
Strategy: ground truth is recomputed by the verifier itself — BigCapital report
REST APIs (`/api/reports/*`, same source as the UI), docker-exec MariaDB
aggregation for HRMS, docker-exec Postgres for Twenty. Agent-recorded numbers
in the note are matched with label-anchored regexes against those truths.
Checks 2 and 3 are 0-weight environment-sanity preconditions (pristine-seed
values); their weight moved to the note numeric checks.

Required env vars:
  SERVER_HOSTNAME, BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER, BIGCAPITAL_DB_CONTAINER,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER
"""

import os
import sys
import subprocess
import json
import re

try:
    import requests

    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"FATAL: {name} not set", file=sys.stderr)
        sys.exit(1)
    return val


BC_PORT = _require("BIGCAPITAL_PORT")
BC_CONTAINER = _require("BIGCAPITAL_CONTAINER")
BC_DB_CONTAINER = _require("BIGCAPITAL_DB_CONTAINER")
HRMS_PORT = _require("HRMS_PORT")
HRMS_CONTAINER = _require("HRMS_CONTAINER")
HRMS_DB_CONTAINER = _require("HRMS_DB_CONTAINER")
TWENTY_PORT = _require("TWENTY_PORT")
TWENTY_CONTAINER = _require("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = _require("TWENTY_DB_CONTAINER")

BC_BASE = f"http://{HOST}:{BC_PORT}"

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


# -- BigCapital API helpers --
_bc_token: str | None = None
_bc_org_id: str = ""


def bc_login() -> str:
    global _bc_token, _bc_org_id
    if _bc_token:
        return _bc_token
    if not HAS_REQUESTS:
        raise RuntimeError("requests module not available")
    # signin is the endpoint the running BigCapital build exposes; it also
    # returns the organization id required as a tenant-routing header on
    # every subsequent API call (without it, tenant-scoped endpoints such
    # as transactions-locking return nothing).
    r = requests.post(
        f"{BC_BASE}/api/auth/signin",
        json={"email": "admin@bigcapital.local", "password": "admin123"},
        timeout=15,
    )
    if r.status_code == 404:
        r = requests.post(
            f"{BC_BASE}/api/auth/login",
            json={"email": "admin@bigcapital.local", "password": "admin123"},
            timeout=15,
        )
    r.raise_for_status()
    data = r.json()
    _bc_token = (data.get("access_token") or data.get("token")
                 or data.get("data", {}).get("token", ""))
    _bc_org_id = str(data.get("organization_id")
                     or data.get("tenant", {}).get("organization_id", "") or "")
    if not _bc_token:
        raise RuntimeError(f"no token in login response: {json.dumps(data)[:200]}")
    return _bc_token


def bc_get(path: str, params: dict | None = None) -> "requests.Response":
    token = bc_login()
    return requests.get(
        f"{BC_BASE}/api/{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}", "x-access-token": token,
                 "organization-id": _bc_org_id},
        timeout=30,
    )


def bc_report(path: str, params: dict | None = None) -> dict:
    """GET a BigCapital endpoint and return parsed JSON; raise on non-200."""
    resp = bc_get(path, params)
    if resp.status_code != 200:
        raise RuntimeError(f"GET /api/{path} -> HTTP {resp.status_code}")
    return resp.json()


def _find_node(nodes, node_id: str):
    """Recursively find a report node by its `id` in a nested children tree."""
    for n in nodes or []:
        if isinstance(n, dict):
            if n.get("id") == node_id:
                return n
            got = _find_node(n.get("children"), node_id)
            if got is not None:
                return got
    return None


def _node_amount(nodes, node_id: str) -> float:
    node = _find_node(nodes, node_id)
    if node is None:
        raise RuntimeError(f"report node '{node_id}' not found")
    return float((node.get("total") or {}).get("amount", 0.0))


def _close(a: float, b: float, tol: float = 0.5) -> bool:
    return abs(float(a) - float(b)) <= tol


# -- BigCapital DB helper (MariaDB; raises when the query errors everywhere) --
def bc_db(sql: str) -> str:
    """Query BigCapital tenant MariaDB. Raises if the query fails on every DB."""
    rc, out, err = docker_exec(
        BC_DB_CONTAINER, "mysql", "-u", "bigcapital", "-pbigcapital123", "-N", "-B", "-e",
        "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
        "WHERE SCHEMA_NAME LIKE 'bigcapital%' OR SCHEMA_NAME NOT IN "
        "('information_schema','performance_schema','mysql','sys','RECOVER_YOUR_DATA') LIMIT 10",
    )
    if rc != 0:
        raise RuntimeError(f"bigcapital schema listing failed: {err.strip()[:200]}")
    dbs = [d.strip() for d in out.strip().split("\n") if d.strip()]
    tenant_dbs = [d for d in dbs if "tenant" in d]
    search_order = tenant_dbs + [d for d in dbs if d not in tenant_dbs]
    errors: list[str] = []
    for dbname in search_order:
        rc2, out2, err2 = docker_exec(
            BC_DB_CONTAINER, "mysql", "-u", "bigcapital", "-pbigcapital123",
            "-D", dbname, "-N", "-B", "-e", sql,
        )
        if rc2 != 0:
            errors.append(f"{dbname}: {err2.strip()[:120]}")
            continue
        if out2.strip():
            return out2.strip()
    if search_order and len(errors) == len(search_order):
        raise RuntimeError("bigcapital db query failed on all DBs: " + "; ".join(errors[:3]))
    return ""


# -- Twenty DB helpers (raise on psql error instead of swallowing it) --
_twenty_schema: str | None = None


def twenty_db(sql: str) -> str:
    rc, out, err = docker_exec(
        TWENTY_DB_CONTAINER, "psql", "-U", "postgres", "-d", "default", "-t", "-A", "-c", sql
    )
    if rc != 0:
        rc2, out2, err2 = docker_exec(
            TWENTY_DB_CONTAINER, "psql", "-U", "postgres", "-d", "twenty", "-t", "-A", "-c", sql
        )
        if rc2 != 0:
            raise RuntimeError(
                f"twenty psql failed: {err.strip()[:200]} / {err2.strip()[:200]}"
            )
        return out2.strip()
    return out.strip()


def twenty_schema() -> str:
    global _twenty_schema
    if _twenty_schema is not None:
        return _twenty_schema
    result = twenty_db(
        # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
        # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
        # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
        # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
        'SELECT ds.schema FROM core."dataSource" ds '
        'JOIN core.workspace w ON w.id = ds."workspaceId" '
        "WHERE w.subdomain = 'yc';"
    )
    _twenty_schema = result.split("\n")[0].strip() if result.strip() else ""
    return _twenty_schema


# -- HRMS DB helper --
_hrms_db_creds: dict | None = None


def _hrms_creds() -> tuple[str, str, str]:
    """Read HRMS DB credentials from site_config.json inside the app container."""
    global _hrms_db_creds
    if _hrms_db_creds is not None:
        return _hrms_db_creds["user"], _hrms_db_creds["pass"], _hrms_db_creds["db"]
    # Try to read from the app container's site config
    rc, out, err = docker_exec(
        HRMS_CONTAINER, "bash", "-c",
        "cat /home/frappe/frappe-bench/sites/*/site_config.json 2>/dev/null | head -20",
    )
    if rc == 0 and out.strip():
        try:
            cfg = json.loads(out.strip().split("\n{")[0] if "\n{" in out else out.strip())
            _hrms_db_creds = {
                "user": cfg.get("db_user", "root"),
                "pass": cfg.get("db_password", ""),
                "db": cfg.get("db_name", "_frappe_bench"),
            }
            return _hrms_db_creds["user"], _hrms_db_creds["pass"], _hrms_db_creds["db"]
        except json.JSONDecodeError:
            pass
    # Fallback defaults
    _hrms_db_creds = {"user": "root", "pass": "", "db": "_frappe_bench"}
    return "root", "", "_frappe_bench"


def hrms_db(sql: str) -> str:
    user, password, db = _hrms_creds()
    args = ["mysql", "-u", user, "--default-character-set=utf8mb4", "-D", db, "-N", "-B"]
    if password:
        args.insert(3, f"-p{password}")
    args.extend(["-e", sql])
    rc, out, err = docker_exec(HRMS_DB_CONTAINER, *args)
    if rc != 0:
        raise RuntimeError(f"hrms mysql failed: {err.strip()[:200]}")
    return out.strip()


def _num(raw: str) -> float:
    """Parse a mysql -N -B scalar; NULL/empty count as 0."""
    raw = (raw or "").strip()
    if not raw or raw.upper() == "NULL":
        return 0.0
    return float(raw)


# ── Note body cache (fetched once, used by many checks) ──────────────────────
# Twenty's `note`/`task` tables have NO `body` column — only `bodyV2Markdown` /
# `bodyV2Blocknote`. Newlines are flattened SQL-side so regexes see one line.
_BODY_EXPR = (
    "regexp_replace(COALESCE(\"bodyV2Markdown\", \"bodyV2Blocknote\"::text, ''), "
    "E'[\\n\\r]+', ' ', 'g')"
)

# Unreplaced template placeholders such as "[value from step 1]", "[count from
# step 11]", "[exceptions from step 16 ...]" => instant fail.
PLACEHOLDER_RE = re.compile(r"\[(value|count|exceptions)[^\]]*\]", re.IGNORECASE)

_note_body: str | None = None
_note_found: bool = False


def _fetch_note() -> None:
    global _note_body, _note_found
    if _note_body is not None:
        return
    schema = twenty_schema()
    if not schema:
        _note_body = ""
        return
    conds = (
        "title LIKE '%Audit Preparation Package%FY 2026%'",   # exact-ish (em dash safe)
        "title ILIKE '%audit%preparation%package%'",           # broader
    )
    for cond in conds:
        cnt = twenty_db(
            f'SELECT COUNT(*) FROM "{schema}".note '
            f'WHERE "deletedAt" IS NULL AND {cond}'
        )
        if cnt and cnt.split("\n")[0].strip().isdigit() and int(cnt.split("\n")[0]) > 0:
            _note_found = True
            _note_body = twenty_db(
                f'SELECT {_BODY_EXPR} FROM "{schema}".note '
                f'WHERE "deletedAt" IS NULL AND {cond} LIMIT 1'
            )
            return
    _note_body = ""


def _note_gate() -> tuple[bool, str]:
    """Common gate for checks 5-14: note exists and has no template placeholder."""
    _fetch_note()
    if not _note_found:
        return False, "note not found"
    if PLACEHOLDER_RE.search(_note_body or ""):
        return False, "unreplaced template placeholder in note body"
    return True, ""


def _norm_body() -> str:
    """Note body with thousands separators stripped (handles 1,076,950 and 10,76,950)."""
    _fetch_note()
    return re.sub(r"(?<=\d),(?=\d)", "", _note_body or "")


def _near(label_pat: str, value_pats: list[str], window: int = 500) -> tuple[bool, str]:
    """All value_pats must appear within +/-window chars of label_pat (comma-stripped body)."""
    body = _norm_body()
    m = re.search(label_pat, body, re.IGNORECASE)
    if not m:
        return False, f"label /{label_pat}/ not found in note body"
    lo = max(0, m.start() - window)
    seg = body[lo:m.end() + window]
    missing = [p for p in value_pats if not re.search(p, seg, re.IGNORECASE)]
    if missing:
        return False, "missing near label: " + "; ".join(f"/{p}/" for p in missing)
    return True, ""


def _anchored(*pats: str) -> tuple[bool, str]:
    """All label-anchored patterns must match the comma-stripped body."""
    body = _norm_body()
    missing = [p for p in pats if not re.search(p, body, re.IGNORECASE)]
    if missing:
        return False, "missing: " + "; ".join(f"/{p}/" for p in missing)
    return True, ""


# Value patterns (applied to the comma-stripped body; $ optional, .00 optional)
_ZERO = r"[-(]?\s*\$?\s*0(\.0{1,2})?(?!\.?\d)"


# ── Individual checks ─────────────────────────────────────────────────────────


def check_1_transaction_lock() -> None:
    """BigCapital: all transactions locked before 2027-01-01 (structured check)."""
    label = "1. Transaction lock"
    accept = ("2026-12-31", "2027-01-01")

    def _date_ok(v) -> bool:
        return bool(v) and str(v)[:10] in accept

    try:
        found = False
        detail = ""
        # Strategy A: structured JSON from the transactions-locking API
        if HAS_REQUESTS:
            try:
                j = bc_report("transactions-locking")
                allm = j.get("all") or {}
                if allm.get("is_enabled") and _date_ok(allm.get("lock_to_date")):
                    found = True
                    detail = f"all-modules lock_to_date={str(allm.get('lock_to_date'))[:10]}"
                if not found:
                    # Per-module locking of every module is an accepted equivalent.
                    mods = {m.get("module"): m for m in (j.get("modules") or [])
                            if isinstance(m, dict)}
                    need = ("sales", "purchases", "financial")
                    if all(mods.get(k, {}).get("is_enabled")
                           and _date_ok(mods.get(k, {}).get("lock_to_date"))
                           for k in need):
                        found = True
                        detail = "per-module lock (sales+purchases+financial)"
                if not found:
                    detail = (f"API: all.is_enabled={allm.get('is_enabled')}, "
                              f"all.lock_to_date={allm.get('lock_to_date')}")
            except Exception as e:
                detail = f"API error: {e}"
        # Strategy B (fallback): settings table, exact group/key
        if not found:
            try:
                val = bc_db(
                    "SELECT `VALUE` FROM `SETTINGS` "
                    "WHERE `GROUP`='transactions-locking' AND `KEY`='all.lock_to_date' "
                    "LIMIT 1"
                )
                if _date_ok(val):
                    found = True
                    detail = f"DB all.lock_to_date={val[:10]}"
                else:
                    mvals = [
                        bc_db(
                            "SELECT `VALUE` FROM `SETTINGS` "
                            "WHERE `GROUP`='transactions-locking' "
                            f"AND `KEY`='{m}.lock_to_date' LIMIT 1"
                        )
                        for m in ("sales", "purchases", "financial")
                    ]
                    if all(_date_ok(v) for v in mvals):
                        found = True
                        detail = "DB per-module lock dates"
            except Exception as e:
                detail = (detail + "; " if detail else "") + f"DB error: {e}"
        check(label, 4, found, detail or "no lock-to-date found via API or DB")
    except Exception as e:
        check(label, 4, False, f"exception: {e}")


def check_2_bank_balance() -> None:
    """PRECONDITION (0pt): Bank Account GL balance recomputes to -$215,382.44.

    Pristine-seed value — passing proves nothing about the agent, so weight is 0;
    failing means the environment is anomalous and downstream numbers are suspect.
    """
    label = "2. [precondition] Bank Account GL balance sanity"
    try:
        acct_row = bc_db(
            "SELECT ID, ACCOUNT_TYPE FROM ACCOUNTS "
            "WHERE NAME='Bank Account' LIMIT 1"
        )
        if not acct_row:
            check(label, 0, False, "ENVIRONMENT ANOMALY: 'Bank Account' not found in ACCOUNTS")
            return
        parts = acct_row.split("\t")
        acct_id = parts[0].strip()
        acct_type = (parts[1].strip() if len(parts) > 1 else "").lower()
        debit_normal = any(k in acct_type for k in (
            "asset", "bank", "cash", "receivable", "expense", "cost", "fixed"))
        sums = bc_db(
            f"SELECT COALESCE(SUM(DEBIT),0), COALESCE(SUM(CREDIT),0) "
            f"FROM ACCOUNTS_TRANSACTIONS "
            f"WHERE ACCOUNT_ID={acct_id} AND DATE <= '2026-12-31'"
        )
        vals = sums.split("\t") if sums else []
        total_debit = _num(vals[0]) if vals else 0.0
        total_credit = _num(vals[1]) if len(vals) > 1 else 0.0
        balance = (total_debit - total_credit) if debit_normal else (total_credit - total_debit)
        ok = _close(balance, -215382.44)
        check(label, 0, ok,
              f"recomputed={balance:.2f}, expected=-215382.44"
              + ("" if ok else "  ENVIRONMENT ANOMALY: downstream numeric checks unreliable"))
    except Exception as e:
        check(label, 0, False, f"ENVIRONMENT ANOMALY: recompute failed: {e}")


def check_3_hrms_employees() -> None:
    """PRECONDITION (0pt): seed has exactly 15 active employees (pristine value)."""
    label = "3. [precondition] HRMS active employee count sanity"
    try:
        result = hrms_db(
            "SELECT COUNT(*) FROM `tabEmployee` "
            "WHERE `company`='TechVista Solutions Pvt. Ltd.' AND `status`='Active'"
        )
        count = int(result.strip()) if result.strip().isdigit() else -1
        ok = count == 15
        check(label, 0, ok,
              f"count={count}, expected=15"
              + ("" if ok else "  ENVIRONMENT ANOMALY: HRMS seed differs"))
    except Exception as e:
        check(label, 0, False, f"ENVIRONMENT ANOMALY: query failed: {e}")


def check_4_note_exists() -> None:
    """Twenty: audit note exists, not deleted, and has no unreplaced placeholder."""
    try:
        ok, why = _note_gate()
        check("4. Audit note exists (no template placeholders)", 2, ok,
              "found, placeholders replaced" if ok else why)
    except Exception as e:
        check("4. Audit note exists (no template placeholders)", 2, False, f"exception: {e}")


def check_5_trial_balance() -> None:
    """Note: Trial Balance total debits/credits = 6,008,325.46 (truth: report API)."""
    label = "5. Note: Trial Balance total 6,008,325.46"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        j = bc_report("reports/trial-balance-sheet",
                      {"from_date": "2026-01-01", "to_date": "2026-12-31",
                       "basis": "accrual"})
        tot = j["data"]["total"]
        debit, credit = float(tot["debit"]), float(tot["credit"])
        if not (_close(debit, credit) and _close(debit, 6008325.46)):
            check(label, 2, False,
                  f"truth recomputation mismatch: API debit={debit:.2f}, credit={credit:.2f},"
                  f" expected 6008325.46 (environment anomaly)")
            return
        ok, d = _near(r"trial\s*balance", [r"\$?\s*6008325\.46"])
        check(label, 2, ok, d or "6,008,325.46 present near 'Trial Balance'")
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_6_balance_sheet() -> None:
    """Note: Balance Sheet assets/liabilities/equity values (truth: report API)."""
    label = "6. Note: Balance Sheet 1,798,226.74 / 441,310.03 / 1,356,916.71"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        j = bc_report("reports/balance-sheet",
                      {"to_date": "2026-12-31", "basis": "accrual"})
        data = j["data"]
        assets = _node_amount(data, "ASSETS")
        liab = _node_amount(data, "LIABILITY")
        equity = _node_amount(data, "EQUITY")
        if not (_close(assets, 1798226.74) and _close(liab, 441310.03)
                and _close(equity, 1356916.71)):
            check(label, 2, False,
                  f"truth recomputation mismatch: API assets={assets:.2f}, "
                  f"liab={liab:.2f}, equity={equity:.2f} (environment anomaly)")
            return
        ok, d = _near(r"balance\s*sheet",
                      [r"\$?\s*1798226\.74", r"\$?\s*441310\.03", r"\$?\s*1356916\.71"])
        check(label, 2, ok, d or "all three totals present near 'Balance Sheet'")
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_7_pnl() -> None:
    """Note: P&L revenue/expenses/net income all zero, label-anchored (truth: API)."""
    label = "7. Note: P&L Total Revenue/Expenses/Net Income = 0"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        j = bc_report("reports/profit-loss-sheet",
                      {"from_date": "2026-01-01", "to_date": "2026-12-31",
                       "basis": "accrual"})
        data = j["data"]
        income = _node_amount(data, "INCOME")
        expenses = _node_amount(data, "EXPENSES")
        net = _node_amount(data, "NET_INCOME")
        if not (_close(income, 0, 0.005) and _close(expenses, 0, 0.005)
                and _close(net, 0, 0.005)):
            check(label, 2, False,
                  f"truth recomputation mismatch: API income={income}, expenses={expenses},"
                  f" net={net}, expected all 0 (environment anomaly)")
            return
        ok, d = _anchored(
            r"total\s*revenue\s*[=:]?\s*" + _ZERO,
            r"total\s*expenses\s*[=:]?\s*" + _ZERO,
            r"net\s*income\s*[=:]?\s*" + _ZERO,
        )
        check(label, 2, ok, d or "all three P&L values anchored at 0")
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_8_cash_flow() -> None:
    """Note: net cash from operating activities = 0, label-anchored (truth: API)."""
    label = "8. Note: Cash Flow operating activities = 0"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        j = bc_report("reports/cashflow-statement",
                      {"from_date": "2026-01-01", "to_date": "2026-12-31"})
        operating = _node_amount(j["data"], "OPERATING")
        if not _close(operating, 0, 0.005):
            check(label, 2, False,
                  f"truth recomputation mismatch: API OPERATING.total={operating},"
                  f" expected 0 (environment anomaly)")
            return
        ok, d = _anchored(r"operating\s*activities\s*[=:]?\s*" + _ZERO)
        check(label, 2, ok, d or "operating activities anchored at 0")
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_9_gl_closing_balance() -> None:
    """Note: GL closing balance -$215,382.44 with the sign (minus or accounting parens)."""
    label = "9. Note: GL closing balance -$215,382.44"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        ok, d = _anchored(
            r"(general\s*ledger|bank\s*account)",
            r"[-(−–]\s*\$?\s*215382\.44",
        )
        check(label, 2, ok, d or "negative GL balance present")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_10_ar_ap() -> None:
    """Note: A/R = 2,444,475.39 and A/P = 1,526,452.44 (truth: aging summary APIs)."""
    label = "10. Note: A/R 2,444,475.39 and A/P 1,526,452.44"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        ar = float(bc_report("reports/receivable-aging-summary",
                             {"as_date": "2026-12-31"})["data"]["total"]["total"]["amount"])
        ap = float(bc_report("reports/payable-aging-summary",
                             {"as_date": "2026-12-31"})["data"]["total"]["total"]["amount"])
        if not (_close(ar, 2444475.39) and _close(ap, 1526452.44)):
            check(label, 2, False,
                  f"truth recomputation mismatch: API AR={ar:.2f}, AP={ap:.2f}"
                  f" (environment anomaly)")
            return
        ok_ar, d_ar = _near(r"receivable", [r"\$?\s*2444475\.39"])
        ok_ap, d_ap = _near(r"payable", [r"\$?\s*1526452\.44"])
        ok = ok_ar and ok_ap
        check(label, 2, ok,
              "both aging totals present" if ok
              else "; ".join(x for x in (("AR: " + d_ar) if not ok_ar else "",
                                         ("AP: " + d_ap) if not ok_ap else "") if x))
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_11_hr_payroll() -> None:
    """Note: 15 active employees; March payroll 1,076,950 / 73,680 / 1,003,270 (truth: HRMS DB)."""
    label = "11. Note: HR headcount 15 and March payroll totals"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 2, False, why)
            return
        cnt = hrms_db(
            "SELECT COUNT(*) FROM `tabEmployee` "
            "WHERE `company`='TechVista Solutions Pvt. Ltd.' AND `status`='Active'"
        )
        sums = hrms_db(
            "SELECT COALESCE(SUM(gross_pay),0), COALESCE(SUM(total_deduction),0), "
            "COALESCE(SUM(net_pay),0) FROM `tabSalary Slip` "
            "WHERE docstatus=1 AND start_date='2026-03-01'"
        )
        vals = sums.split("\t") if sums else []
        gross = _num(vals[0]) if vals else 0.0
        ded = _num(vals[1]) if len(vals) > 1 else 0.0
        net = _num(vals[2]) if len(vals) > 2 else 0.0
        emp = int(cnt.strip()) if cnt.strip().isdigit() else -1
        if not (emp == 15 and _close(gross, 1076950) and _close(ded, 73680)
                and _close(net, 1003270)):
            check(label, 2, False,
                  f"truth recomputation mismatch: DB employees={emp}, gross={gross:.0f},"
                  f" deductions={ded:.0f}, net={net:.0f} (environment anomaly)")
            return
        ok, d = _anchored(
            r"(active\s*employees\s*[=:]?\s*15(?!\.?\d)|15\s*active\s*employees)",
            r"gross\s*(earnings?|pay)?\s*[=:]?\s*[\$₹]?\s*1076950(\.0{1,2})?(?!\.?\d)",
            r"(total\s*)?deductions?\s*[=:]?\s*[\$₹]?\s*73680(\.0{1,2})?(?!\.?\d)",
            r"net\s*pay\s*[=:]?\s*[\$₹]?\s*1003270(\.0{1,2})?(?!\.?\d)",
        )
        check(label, 2, ok, d or "headcount and all three payroll totals anchored")
    except Exception as e:
        check(label, 2, False, f"truth fetch failed: {e}")


def check_12_tax_pf() -> None:
    """Note: income tax in {0, 0.00, N/A}; PF deductions = 70,680 (truth: HRMS DB)."""
    label = "12. Note: income tax 0/N/A and PF 70,680"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 1, False, why)
            return
        pf = _num(hrms_db(
            "SELECT COALESCE(SUM(sd.amount),0) FROM `tabSalary Detail` sd "
            "JOIN `tabSalary Slip` ss ON sd.parent=ss.name "
            "WHERE sd.salary_component='Provident Fund' "
            "AND sd.parentfield='deductions' AND ss.docstatus=1"
        ))
        tax = _num(hrms_db(
            "SELECT COALESCE(SUM(sd.amount),0) FROM `tabSalary Detail` sd "
            "JOIN `tabSalary Slip` ss ON sd.parent=ss.name "
            "WHERE sd.salary_component='Income Tax' "
            "AND sd.parentfield='deductions' AND ss.docstatus=1"
        ))
        if not (_close(pf, 70680) and _close(tax, 0, 0.005)):
            check(label, 1, False,
                  f"truth recomputation mismatch: DB PF={pf:.0f} (expected 70680),"
                  f" income tax={tax:.0f} (expected 0) (environment anomaly)")
            return
        ok, d = _anchored(
            r"income\s*tax[^:=]{0,30}[=:]\s*[\$₹]?\s*(0(\.0{1,2})?(?!\.?\d)|n/?a)",
            r"(p\.?f\.?|provident\s*fund)\s*deductions?\s*(\(?\s*2026\s*\)?)?\s*[=:]?\s*"
            r"[\$₹]?\s*70680(\.0{1,2})?(?!\.?\d)",
        )
        check(label, 1, ok, d or "income tax 0/N/A and PF 70,680 anchored")
    except Exception as e:
        check(label, 1, False, f"truth fetch failed: {e}")


def check_13_leave_advance() -> None:
    """Note: Sick Leave balance = 98; no unclaimed advances above $750 (truth: HRMS DB)."""
    label = "13. Note: Sick Leave 98 and no advances above $750"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 1, False, why)
            return
        sick = _num(hrms_db(
            "SELECT COALESCE(SUM(leaves),0) FROM `tabLeave Ledger Entry` "
            "WHERE leave_type='Sick Leave' AND docstatus=1"
        ))
        adv_cnt_raw = hrms_db("SELECT COUNT(*) FROM `tabEmployee Advance`")
        adv_cnt = int(adv_cnt_raw.strip()) if adv_cnt_raw.strip().isdigit() else -1
        if not (_close(sick, 98) and adv_cnt == 0):
            check(label, 1, False,
                  f"truth recomputation mismatch: DB sick leave={sick:.0f} (expected 98),"
                  f" advances rows={adv_cnt} (expected 0) (environment anomaly)")
            return
        ok_sick, d_sick = _anchored(
            r"sick\s*leave\s*\)?\s*[=:]?\s*[\$₹]?\s*98(\.0{1,2})?(?!\.?\d)")
        adv_pats = (
            r"no\s+unclaimed\s+advances?\s+(above|over|exceeding)\s*\$?\s*750",
            r"no\s+(outstanding|unpaid|unclaimed)\s+(employee\s+)?advances?",
            r"(employee\s+)?advances?\s*[=:]\s*(none|0(?!\.?\d)|no\s+exceptions?)",
        )
        body = _norm_body()
        ok_adv = any(re.search(p, body, re.IGNORECASE) for p in adv_pats)
        ok = ok_sick and ok_adv
        check(label, 1, ok,
              "sick leave 98 and no-advances statement present" if ok
              else "; ".join(x for x in (
                  ("sick: " + d_sick) if not ok_sick else "",
                  "no-advances statement missing" if not ok_adv else "") if x))
    except Exception as e:
        check(label, 1, False, f"truth fetch failed: {e}")


def check_14_won_deals() -> None:
    """Note: Won deals FY2026 Count = 0 / Revenue = 0 (truth: Twenty DB, no Won stage)."""
    label = "14. Note: Won deals Count = 0 / Revenue = 0"
    try:
        gate, why = _note_gate()
        if not gate:
            check(label, 1, False, why)
            return
        schema = twenty_schema()
        won_raw = twenty_db(
            f'SELECT COUNT(*) FROM "{schema}".opportunity '
            f'WHERE "deletedAt" IS NULL AND stage::text ILIKE \'won\''
        )
        won = int(won_raw.strip()) if won_raw.strip().isdigit() else -1
        if won != 0:
            check(label, 1, False,
                  f"truth recomputation mismatch: DB Won opportunities={won},"
                  f" expected 0 (environment anomaly)")
            return
        ok, d = _near(r"won\s*deals?",
                      [r"count\s*[=:]\s*0(?!\.?\d)",
                       r"(total\s*)?revenue\s*[=:]\s*\$?\s*0(\.0{1,2})?(?!\.?\d)"])
        check(label, 1, ok, d or "Count = 0 and Revenue = 0 anchored")
    except Exception as e:
        check(label, 1, False, f"truth fetch failed: {e}")


def check_15_task() -> None:
    """Twenty: submission task with due 2027-03-15 and the prescribed body."""
    label = "15. Audit submission task (due 2027-03-15, body)"
    try:
        schema = twenty_schema()
        if not schema:
            check(label, 2, False, "workspace schema not found")
            return
        conds = (
            "title LIKE '%Submit audit package%external auditor%FY 2026%'",
            "title ILIKE '%audit package%auditor%'",
        )
        due_ok_raw = ""
        body = ""
        found = False
        for cond in conds:
            cnt = twenty_db(
                f'SELECT COUNT(*) FROM "{schema}".task '
                f'WHERE "deletedAt" IS NULL AND {cond}'
            )
            if cnt and cnt.split("\n")[0].strip().isdigit() and int(cnt.split("\n")[0]) > 0:
                found = True
                # Local-midnight timestamps can be stored shifted to UTC; accept
                # either the exact date or the +8h-shifted date.
                due_ok_raw = twenty_db(
                    f'SELECT ("dueAt"::date = DATE \'2027-03-15\' '
                    f'OR ("dueAt" + interval \'8 hours\')::date = DATE \'2027-03-15\') '
                    f'FROM "{schema}".task WHERE "deletedAt" IS NULL AND {cond} LIMIT 1'
                )
                body = twenty_db(
                    f'SELECT {_BODY_EXPR} FROM "{schema}".task '
                    f'WHERE "deletedAt" IS NULL AND {cond} LIMIT 1'
                )
                break
        if not found:
            check(label, 2, False, "task not found")
            return
        due_ok = due_ok_raw.strip().startswith("t")
        has_lock = bool(re.search(r"transaction\s*lock\s*applied", body, re.IGNORECASE))
        has_auditor = bool(re.search(r"external\s*auditor", body, re.IGNORECASE))
        no_placeholder = not PLACEHOLDER_RE.search(body)
        ok = due_ok and has_lock and has_auditor and no_placeholder
        check(label, 2, ok,
              f"due_ok={due_ok}, lock_sentence={has_lock}, auditor_sentence={has_auditor},"
              f" placeholders_absent={no_placeholder}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_transaction_lock()
    check_2_bank_balance()
    check_3_hrms_employees()
    check_4_note_exists()
    check_5_trial_balance()
    check_6_balance_sheet()
    check_7_pnl()
    check_8_cash_flow()
    check_9_gl_closing_balance()
    check_10_ar_ap()
    check_11_hr_payroll()
    check_12_tax_pf()
    check_13_leave_advance()
    check_14_won_deals()
    check_15_task()

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
