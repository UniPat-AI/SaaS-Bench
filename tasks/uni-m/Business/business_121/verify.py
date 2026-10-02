"""
Verifier for Business-121-I3: Q3 2026 Quarter-End Operations Review Across Four Apps

Checks: 14 weighted checks across twenty, bigcapital, hrms, pretix.
Strategy: docker exec (DB queries) for DB-backed truths; BigCapital report API
(authenticated, organization-id header) for financial truths and the
OVERDUE_CLIENTS list. Checks 10-14 are 0-weight sanity preconditions.

Required env vars:
  SERVER_HOSTNAME,
  TWENTY_PORT, TWENTY_CONTAINER, TWENTY_DB_CONTAINER,
  BIGCAPITAL_PORT, BIGCAPITAL_CONTAINER,
  HRMS_PORT, HRMS_CONTAINER, HRMS_DB_CONTAINER,
  PRETIX_PORT, PRETIX_CONTAINER, PRETIX_DB_CONTAINER
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

TWENTY_PORT = os.getenv("TWENTY_PORT")
TWENTY_CONTAINER = os.getenv("TWENTY_CONTAINER")
TWENTY_DB_CONTAINER = os.getenv("TWENTY_DB_CONTAINER")
BIGCAPITAL_PORT = os.getenv("BIGCAPITAL_PORT")
BIGCAPITAL_CONTAINER = os.getenv("BIGCAPITAL_CONTAINER")
HRMS_PORT = os.getenv("HRMS_PORT")
HRMS_CONTAINER = os.getenv("HRMS_CONTAINER")
HRMS_DB_CONTAINER = os.getenv("HRMS_DB_CONTAINER")
PRETIX_PORT = os.getenv("PRETIX_PORT")
PRETIX_CONTAINER = os.getenv("PRETIX_CONTAINER")
PRETIX_DB_CONTAINER = os.getenv("PRETIX_DB_CONTAINER")

_required = {
    "TWENTY_PORT": TWENTY_PORT, "TWENTY_CONTAINER": TWENTY_CONTAINER,
    "TWENTY_DB_CONTAINER": TWENTY_DB_CONTAINER,
    "BIGCAPITAL_PORT": BIGCAPITAL_PORT, "BIGCAPITAL_CONTAINER": BIGCAPITAL_CONTAINER,
    "HRMS_PORT": HRMS_PORT, "HRMS_CONTAINER": HRMS_CONTAINER,
    "HRMS_DB_CONTAINER": HRMS_DB_CONTAINER,
    "PRETIX_PORT": PRETIX_PORT, "PRETIX_CONTAINER": PRETIX_CONTAINER,
    "PRETIX_DB_CONTAINER": PRETIX_DB_CONTAINER,
}
for var_name, var_val in _required.items():
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

BC_BASE = f"http://{HOST}:{BIGCAPITAL_PORT}"

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


def twenty_psql(query: str, timeout: int = 15, field_sep: str | None = None) -> str:
    """Run a psql query against Twenty's Postgres DB (user=postgres, db=default)."""
    args = ["psql", "-U", "postgres", "-d", "default", "-t", "-A"]
    if field_sep is not None:
        args += ["-F", field_sep]
    args += ["-c", query]
    rc, out, err = docker_exec(TWENTY_DB_CONTAINER, *args, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"twenty psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def bigcapital_mysql(query: str, db: str = "", timeout: int = 15) -> str:
    """Run a mysql query against BigCapital's embedded MariaDB (user=root, in BIGCAPITAL_CONTAINER)."""
    cmd = ["mysql", "-u", "root", "--default-character-set=utf8mb4", "-N", "-B"]
    if db:
        cmd += ["-D", db]
    cmd += ["-e", query]
    rc, out, err = docker_exec(BIGCAPITAL_CONTAINER, *cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"bigcapital mysql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def pretix_psql(query: str, timeout: int = 15) -> str:
    """Run a psql query against Pretix Postgres (-U pretix
    can be rejected when the connection pool is saturated)."""
    rc, out, err = docker_exec(
        PRETIX_DB_CONTAINER,
        "psql", "-U", "pretix", "-d", "pretix",
        "-t", "-A", "-c", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"pretix psql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def hrms_mysql(query: str, db: str = "", timeout: int = 15) -> str:
    """Run a mysql query against HRMS MariaDB. Discovers the frappe bench DB dynamically."""
    cmd = [
        "mysql", "-u", "root", "-phrms123456",
        "--default-character-set=utf8mb4", "-N", "-B",
    ]
    if db:
        cmd += ["-D", db]
    cmd += ["-e", query]
    rc, out, err = docker_exec(HRMS_DB_CONTAINER, *cmd, timeout=timeout)
    if rc != 0:
        raise RuntimeError(f"hrms mysql error: {err.strip()}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


# ── Twenty workspace schema discovery ─────────────────────────────────────────
_ws_schema: str | None = None


def get_twenty_schema() -> str:
    global _ws_schema
    if _ws_schema is None:
        raw = twenty_psql(
            # The image seeds TWO workspaces (Apple/apple, YCombinator/yc). The app serves yc on the
            # benchmark's subdomain-less localhost URL, so that is where the agent's writes land; an
            # unordered LIMIT 1 only happened to agree, and ORDER BY schema_name picks Apple and
            # silently finds nothing. core."dataSource" carries the authoritative schema mapping.
            'SELECT ds.schema FROM core."dataSource" ds '
            'JOIN core.workspace w ON w.id = ds."workspaceId" '
            "WHERE w.subdomain = 'yc';"
        )
        if not raw:
            raise RuntimeError("No Twenty workspace schema found")
        _ws_schema = raw.split("\n")[0].strip()
    return _ws_schema


def twenty_ws_query(query: str) -> str:
    """Run a query against the Twenty workspace schema.
    Replaces unqualified table names 'note' and 'task' with schema-qualified versions.
    """
    schema = get_twenty_schema()
    q = query.replace(" note ", f' "{schema}".note ')
    q = q.replace(" note\n", f' "{schema}".note\n')
    q = q.replace("FROM note", f'FROM "{schema}".note')
    q = q.replace(" task ", f' "{schema}".task ')
    q = q.replace(" task\n", f' "{schema}".task\n')
    q = q.replace("FROM task", f'FROM "{schema}".task')
    return twenty_psql(q)


# ── BigCapital tenant DB discovery ────────────────────────────────────────────
_bc_tenant_db: str | None = None


def get_bigcapital_tenant_db() -> str:
    global _bc_tenant_db
    if _bc_tenant_db is None:
        raw = bigcapital_mysql(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME LIKE 'bigcapital_tenant_%' LIMIT 1;"
        )
        if not raw:
            raise RuntimeError("No BigCapital tenant DB found")
        _bc_tenant_db = raw.split("\n")[0].strip()
    return _bc_tenant_db


def bigcapital_tenant_query(query: str) -> str:
    db = get_bigcapital_tenant_db()
    return bigcapital_mysql(query, db=db)


# ── HRMS frappe bench DB discovery ────────────────────────────────────────────
_hrms_bench_db: str | None = None


def get_hrms_bench_db() -> str:
    global _hrms_bench_db
    if _hrms_bench_db is None:
        dbs = hrms_mysql(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
            "WHERE SCHEMA_NAME NOT IN ('information_schema','mysql','performance_schema','sys','hrms') "
            "AND SCHEMA_NAME LIKE '\\_%';"
        )
        for db_name in dbs.strip().split("\n"):
            db_name = db_name.strip()
            if not db_name:
                continue
            try:
                result = hrms_mysql(
                    "SELECT COUNT(*) FROM `tabCompany`;", db=db_name
                )
                if result.strip().isdigit():
                    _hrms_bench_db = db_name
                    break
            except RuntimeError:
                continue
        if _hrms_bench_db is None:
            raise RuntimeError("No HRMS frappe bench DB found")
    return _hrms_bench_db


def hrms_bench_query(query: str) -> str:
    db = get_hrms_bench_db()
    return hrms_mysql(query, db=db)


# ── BigCapital report API (authenticated; organization-id tenant header) ──────
_bc_token: str | None = None
_bc_org_id: str = ""


def bc_login() -> str:
    global _bc_token, _bc_org_id
    if _bc_token:
        return _bc_token

    def _post(path: str) -> dict:
        req = urllib.request.Request(
            f"{BC_BASE}{path}",
            data=json.dumps({"email": "admin@bigcapital.local",
                             "password": "admin123"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    try:
        data = _post("/api/auth/signin")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            data = _post("/api/auth/login")
        else:
            raise
    _bc_token = (data.get("access_token") or data.get("token")
                 or data.get("data", {}).get("token", ""))
    _bc_org_id = str(data.get("organization_id")
                     or data.get("tenant", {}).get("organization_id", "") or "")
    if not _bc_token:
        raise RuntimeError(f"no token in BigCapital login response: {json.dumps(data)[:200]}")
    return _bc_token


def bc_api_get(path_and_query: str) -> dict:
    token = bc_login()
    req = urllib.request.Request(
        f"{BC_BASE}/api/{path_and_query}",
        headers={"Authorization": f"Bearer {token}", "x-access-token": token,
                 "organization-id": _bc_org_id},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ── OVERDUE_CLIENTS truth (AR aging summary API, buckets index>=2) ────────────
_overdue_clients: list[str] | None = None


def get_overdue_clients() -> list[str]:
    """Independently derive OVERDUE_CLIENTS: customers with a non-zero balance in
    the 61-90 or 90+ day buckets of the A/R Aging Summary as of 2026-09-30."""
    global _overdue_clients
    if _overdue_clients is None:
        d = bc_api_get(
            "reports/receivable-aging-summary"
            "?as_date=2026-09-30&aging_days_before=30&aging_periods=4"
        )
        out: list[str] = []
        for c in (d.get("data") or {}).get("customers") or []:
            aging = c.get("aging") or []
            overdue = any(
                abs(float((b.get("total") or {}).get("amount") or 0)) > 1e-9
                for b in aging[2:]
            )
            if overdue:
                nm = str(c.get("customer_name") or "").strip()
                if nm:
                    out.append(nm)
        if not out:
            raise RuntimeError("AR aging summary returned no overdue customers")
        _overdue_clients = out
    return _overdue_clients


# ── Financial truths (BigCapital report APIs; constants fallback) ─────────────
_FIN_FALLBACK = {
    "pl_revenue": 0.0, "pl_expenses": 0.0, "pl_net": 0.0,
    "bs_assets": 1798226.74, "bs_liabilities": 441310.03, "bs_equity": 1356916.71,
    "cf_ops": 0.0, "ar_total": 2444475.39, "ap_total": 1526452.44,
}
_fin_truth: tuple[dict, list[str]] | None = None


def get_financial_truth() -> tuple[dict, list[str]]:
    """Compute P&L/BS/CF/AR/AP truths from the report APIs; per-report fallback to
    the audited constants (noted in the returned fallback list)."""
    global _fin_truth
    if _fin_truth is not None:
        return _fin_truth
    vals = dict(_FIN_FALLBACK)
    fallbacks: list[str] = []

    # P&L Q3 (accrual)
    try:
        d = bc_api_get("reports/profit-loss-sheet"
                       "?from_date=2026-07-01&to_date=2026-09-30&basis=accrual")
        data = d.get("data") or []
        if not data:
            vals["pl_revenue"] = vals["pl_expenses"] = vals["pl_net"] = 0.0
        else:
            got: dict[str, float] = {}

            def _walk(nodes: list) -> None:
                for n in nodes:
                    nid = str(n.get("id", ""))
                    if nid in ("INCOME", "EXPENSES", "NET_INCOME"):
                        got[nid] = float((n.get("total") or {}).get("amount") or 0)
                    _walk(n.get("children") or [])

            _walk(data)
            vals["pl_revenue"] = got["INCOME"]
            vals["pl_expenses"] = got["EXPENSES"]
            vals["pl_net"] = got["NET_INCOME"]
    except Exception:
        fallbacks.append("P&L")

    # Balance sheet as of 2026-09-30 (accrual)
    try:
        d = bc_api_get("reports/balance-sheet?to_date=2026-09-30&basis=accrual")
        assets = liab = equity = None
        for n in d.get("data") or []:
            nid = str(n.get("id", ""))
            if nid == "ASSETS":
                assets = float((n.get("total") or {}).get("amount") or 0)
            elif nid == "LIABILITY_EQUITY":
                for c in n.get("children") or []:
                    cid = str(c.get("id", ""))
                    if cid == "LIABILITY":
                        liab = float((c.get("total") or {}).get("amount") or 0)
                    elif cid == "EQUITY":
                        equity = float((c.get("total") or {}).get("amount") or 0)
        if assets is None or liab is None or equity is None:
            raise RuntimeError("balance sheet aggregates not found")
        vals["bs_assets"], vals["bs_liabilities"], vals["bs_equity"] = assets, liab, equity
    except Exception:
        fallbacks.append("BS")

    # Cash flow from operating activities, Q3
    try:
        d = bc_api_get("reports/cashflow-statement"
                       "?from_date=2026-07-01&to_date=2026-09-30")
        ops = None
        for n in d.get("data") or []:
            if str(n.get("id", "")) == "OPERATING":
                ops = float((n.get("total") or {}).get("amount") or 0)
        if ops is None:
            raise RuntimeError("OPERATING section not found")
        vals["cf_ops"] = ops
    except Exception:
        fallbacks.append("CF")

    # AR / AP totals as of 2026-09-30
    try:
        d = bc_api_get("reports/receivable-aging-summary"
                       "?as_date=2026-09-30&aging_days_before=30&aging_periods=4")
        vals["ar_total"] = float(
            (((d.get("data") or {}).get("total") or {}).get("total") or {}).get("amount"))
    except Exception:
        fallbacks.append("AR")
    try:
        d = bc_api_get("reports/payable-aging-summary"
                       "?as_date=2026-09-30&aging_days_before=30&aging_periods=4")
        vals["ap_total"] = float(
            (((d.get("data") or {}).get("total") or {}).get("total") or {}).get("amount"))
    except Exception:
        fallbacks.append("AP")

    _fin_truth = (vals, fallbacks)
    return _fin_truth


# ── Text normalization & regex helpers ────────────────────────────────────────
def norm(s: str) -> str:
    """lowercase, unify dashes, decode &amp;, unify curly quotes, collapse whitespace."""
    s = (s or "").lower()
    s = s.replace("&amp;", "&")
    s = s.replace("—", "-").replace("–", "-").replace("‑", "-")
    s = re.sub(r"-{2,}", "-", s)
    s = s.replace("’", "'").replace("‘", "'")
    s = re.sub(r"\s+", " ", s)
    return s.strip()


# Zero must be label-anchored by the caller; this pattern refuses to match a bare
# '0' inside a longer number (e.g. '20', '30.00', '2026-10-15').
ZERO_RE = r"(?<![\d.])(?:\$\s*)?0(?:\.0{1,2})?(?!\.?\d)"


def num_re(value: float) -> str:
    """Regex accepting the amount with optional $, optional thousand separators,
    and optional .00 for integral amounts."""
    v = round(float(value), 2)
    if abs(v) < 0.005:
        return ZERO_RE
    sign = r"-?\s*" if v < 0 else ""
    a = abs(v)
    ip = int(a)
    cents = int(round((a - ip) * 100))
    int_pat = f"{ip:,}".replace(",", ",?")
    tail = r"(?:\.0{1,2})?" if cents == 0 else r"\." + f"{cents:02d}"
    return r"(?<![\d.])" + r"(?:\$\s*)?" + sign + int_pat + tail + r"(?!\.?\d)"


def cnt_re(n: int) -> str:
    if int(n) == 0:
        return ZERO_RE
    return r"(?<![\d.])" + str(int(n)) + r"(?!\.?\d)"


# Open pipeline amount: accept $ / thousand separators / compact 6.87M form.
def pipeline_amt_re(amount: float) -> str:
    compact = amount / 1_000_000.0
    compact_s = f"{compact:.2f}".rstrip("0").rstrip(".")
    compact_pat = re.escape(compact_s) + r"\s*m\b"
    return r"(?:" + num_re(amount) + r"|(?<![\d.])(?:\$\s*)?" + compact_pat + r")"


# ── Placeholder (unreplaced template token) detection ─────────────────────────
PLACEHOLDER_TOKENS = [
    "WON_COUNT", "WON_REVENUE", "LOST_COUNT", "OPEN_COUNT", "OPEN_PIPELINE",
    "OVERDUE_CLIENTS", "PL_REVENUE", "PL_EXPENSES", "PL_NET_INCOME",
    "BS_ASSETS", "BS_LIABILITIES", "BS_EQUITY", "CASH_OPS", "AR_TOTAL",
    "AP_TOTAL", "TOP_ITEM_REVENUE", "TOP_ITEM", "ATTENDANCE_HEADCOUNT",
    "HIGH_ABSENCE_EMPLOYEES", "UNPAID_CLAIMS_COUNT", "UNPAID_CLAIMS_TOTAL",
    "LEAVE_LIABILITY_DAYS", "OPEN_ADVANCES_COUNT", "OPEN_ADVANCES_TOTAL",
    "EVENT_ORDERS", "EVENT_REVENUE", "PROD1_REVENUE", "PROD2_REVENUE",
    "PAID_ORDERS", "PENDING_ORDERS", "CANCELLED_ORDERS",
]
_PLACEHOLDER_GENERIC = re.compile(r"\[(?:value|count|computed)[^\]]*\]", re.IGNORECASE)


def find_placeholders(raw_body: str) -> list[str]:
    hits = [t for t in PLACEHOLDER_TOKENS if t in raw_body]
    m = _PLACEHOLDER_GENERIC.search(raw_body)
    if m:
        hits.append(m.group(0))
    return hits


# ── Note body fetch (flattened markdown; cached) ──────────────────────────────
_note_cache: dict[str, str] = {}

_FLATTEN_BODY_SQL = (
    "regexp_replace(COALESCE(NULLIF(\"bodyV2Markdown\", ''), "
    "\"bodyV2Blocknote\"::text, ''), E'[\\n\\r]+', ' ', 'g')"
)


def get_note_body(title_like: str) -> str:
    if title_like not in _note_cache:
        schema = get_twenty_schema()
        _note_cache[title_like] = twenty_psql(
            f'SELECT {_FLATTEN_BODY_SQL} FROM "{schema}".note '
            f"WHERE title LIKE '{title_like}' AND \"deletedAt\" IS NULL LIMIT 1;"
        )
    return _note_cache[title_like]


PIPELINE_TITLE_LIKE = "%Q3 Pipeline Summary%2026-09-30%"
OPS_TITLE_LIKE = "%Q3 Operations Review%Complete%2026-09-30%"


def section(body_norm: str, start_label: str, end_labels: list[str]) -> str:
    """Slice out one section of the ops note (falls back to the full body if
    the section header is missing — the numeric gates still apply)."""
    i = body_norm.find(start_label)
    if i < 0:
        return body_norm
    j = len(body_norm)
    for e in end_labels:
        k = body_norm.find(e, i + len(start_label))
        if 0 <= k < j:
            j = k
    return body_norm[i:j]


def gate_all(text: str, gates: list[tuple[str, str]]) -> list[str]:
    """Return names of gates whose regex does not match `text`."""
    return [name for name, pat in gates if not re.search(pat, text)]


# ── Due-date matching (date_matches_tz semantics) ─────────────────────────────
def date_matches_tz(stored: str, expected: str) -> bool:
    """Pure date (exactly 10 chars): equality. Timestamp: date-part equals, in
    UTC or shifted by the verifier host's local UTC offset. No bare substring."""
    stored = (stored or "").strip()
    if not stored:
        return False
    if len(stored) == 10:
        return stored == expected
    ts = stored.replace(" ", "T", 1)
    if re.search(r"[+-]\d{2}$", ts):
        ts += ":00"
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return False
    if dt.date().isoformat() == expected:
        return True
    off = datetime.now().astimezone().utcoffset() or timedelta(0)
    return (dt + off).date().isoformat() == expected


# ── CRM pipeline gate constants (audited truth; see docs/tightening) ──────────
CRM_OPEN_COUNT = 9
CRM_OPEN_PIPELINE = 6870000.0


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_pipeline_summary_note_exists() -> None:
    """Q3 Pipeline Summary note exists; body has no unreplaced template tokens."""
    label = "1. Twenty: Q3 Pipeline Summary note exists (no placeholders)"
    try:
        rows = twenty_ws_query(
            "SELECT id, title FROM note "
            f"WHERE title LIKE '{PIPELINE_TITLE_LIKE}' "
            "AND \"deletedAt\" IS NULL;"
        )
        if not rows:
            check(label, 2, False, "note not found")
            return
        body = get_note_body(PIPELINE_TITLE_LIKE)
        ph = find_placeholders(body)
        ok = not ph
        check(label, 2, ok,
              f"found={rows[:100]}" if ok else f"unreplaced placeholder(s): {ph[:4]}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_pipeline_summary_content() -> None:
    """Pipeline Summary numbers gate + full OVERDUE_CLIENTS list (API truth)."""
    label = "2. Twenty: Pipeline Summary numbers & overdue client list"
    try:
        body_raw = get_note_body(PIPELINE_TITLE_LIKE)
        if not body_raw:
            check(label, 3, False, "note/body not found")
            return
        b = norm(body_raw)
        gates = [
            ("Won deals: 0", r"won\s*deals\s*:\s*" + ZERO_RE),
            ("total revenue: 0", r"revenue\s*:?\s*" + ZERO_RE),
            ("Lost deals: 0", r"lost\s*deals\s*:\s*" + ZERO_RE),
            ("Open pipeline: 9 deals",
             r"open\s*pipeline[^:]{0,40}:\s*" + cnt_re(CRM_OPEN_COUNT) + r"\s*deals"),
            ("Open pipeline amount 6,870,000", pipeline_amt_re(CRM_OPEN_PIPELINE)),
            ("Win rate: N/A", r"win\s*rate\s*:\s*n/a"),
        ]
        failed = gate_all(b, gates)
        clients = get_overdue_clients()
        missing = [c for c in clients if norm(c) not in b]
        ok = not failed and not missing
        detail = (f"all {len(gates)} numeric gates + {len(clients)} overdue clients present"
                  if ok else
                  f"failed gates: {failed[:3]}; missing clients "
                  f"{len(missing)}/{len(clients)}: {missing[:3]}")
        check(label, 3, ok, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_3_ops_review_note_exists() -> None:
    """Q3 Operations Review note exists; body has no unreplaced template tokens."""
    label = "3. Twenty: Q3 Ops Review note exists (no placeholders)"
    try:
        rows = twenty_ws_query(
            "SELECT id, title FROM note "
            f"WHERE title LIKE '{OPS_TITLE_LIKE}' "
            "AND \"deletedAt\" IS NULL;"
        )
        if not rows:
            check(label, 2, False, "note not found")
            return
        body = get_note_body(OPS_TITLE_LIKE)
        ph = find_placeholders(body)
        ok = not ph
        check(label, 2, ok,
              f"found={rows[:100]}" if ok else f"unreplaced placeholder(s): {ph[:4]}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_ops_review_financial() -> None:
    """FINANCIAL SUMMARY: all figures anchored to API-computed truths."""
    label = "4. Twenty: Ops Review financial summary figures"
    try:
        body_raw = get_note_body(OPS_TITLE_LIKE)
        if not body_raw:
            check(label, 4, False, "note/body not found")
            return
        b = norm(body_raw)
        sec = section(b, "financial summary",
                      ["crm pipeline", "hr metrics", "event performance"])
        t, fallbacks = get_financial_truth()
        gates = [
            ("Revenue 0", r"revenue\s*:?\s*" + num_re(t["pl_revenue"])),
            ("Expenses 0", r"expenses?\s*:?\s*" + num_re(t["pl_expenses"])),
            ("Net Income 0", r"net\s*income\s*:?\s*" + num_re(t["pl_net"])),
            ("Assets", r"assets\s*:?\s*" + num_re(t["bs_assets"])),
            ("Liabilities", r"liabilit(?:y|ies)\s*:?\s*" + num_re(t["bs_liabilities"])),
            ("Equity", r"equity\s*:?\s*" + num_re(t["bs_equity"])),
            ("Cash Flow from Ops 0", r"cash[^:]{0,40}:\s*" + num_re(t["cf_ops"])),
            ("A/R total", r"(?:a\s*/\s*r|receivables?)[^:]{0,30}:\s*" + num_re(t["ar_total"])),
            ("A/P total", r"(?:a\s*/\s*p|payables?)[^:]{0,30}:\s*" + num_re(t["ap_total"])),
            ("Top item N/A",
             r"top\s*item[^:]{0,30}:.{0,80}?(?:n/a|none\b|no\s+sales|not\s+available)"),
        ]
        failed = gate_all(sec, gates)
        ok = not failed
        detail = "all financial figures match API truths" if ok else f"failed gates: {failed[:5]}"
        if fallbacks:
            detail += f"; fallback constants used for: {fallbacks}"
        check(label, 4, ok, detail)
    except Exception as e:
        check(label, 4, False, f"exception: {e}")


def check_5_ops_review_crm() -> None:
    """CRM PIPELINE section: five values (0 / 0 / 9 / 6,870,000 / N/A)."""
    label = "5. Twenty: Ops Review CRM pipeline figures"
    try:
        body_raw = get_note_body(OPS_TITLE_LIKE)
        if not body_raw:
            check(label, 2, False, "note/body not found")
            return
        b = norm(body_raw)
        sec = section(b, "crm pipeline", ["hr metrics", "event performance"])
        gates = [
            ("Won: 0 deals", r"won\s*:\s*" + ZERO_RE + r"\s*deals"),
            ("Lost: 0 deals", r"lost\s*:\s*" + ZERO_RE + r"\s*deals"),
            ("Open (SCREENING): 9 deals",
             r"open[^:]{0,40}:\s*" + cnt_re(CRM_OPEN_COUNT) + r"\s*deals"),
            ("Open pipeline amount 6,870,000", pipeline_amt_re(CRM_OPEN_PIPELINE)),
            ("Win rate: N/A", r"win\s*rate\s*:\s*n/a"),
        ]
        failed = gate_all(sec, gates)
        ok = not failed
        check(label, 2, ok,
              "all 5 CRM figures present" if ok else f"failed gates: {failed}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_ops_review_hr() -> None:
    """HR METRICS section: figures anchored to HRMS-DB-computed truths."""
    label = "6. Twenty: Ops Review HR metrics figures"
    try:
        body_raw = get_note_body(OPS_TITLE_LIKE)
        if not body_raw:
            check(label, 3, False, "note/body not found")
            return
        b = norm(body_raw)
        sec = section(b, "hr metrics", ["event performance"])

        fallback_note = ""
        try:
            att_cnt = int(hrms_bench_query(
                "SELECT COUNT(DISTINCT employee) FROM `tabAttendance` "
                "WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' "
                "AND docstatus=1;"))
            high_names_raw = hrms_bench_query(
                "SELECT COALESCE(GROUP_CONCAT(employee_name SEPARATOR '||'),'') FROM ("
                "SELECT employee, employee_name FROM `tabAttendance` "
                "WHERE attendance_date BETWEEN '2026-09-01' AND '2026-09-30' "
                "AND docstatus=1 AND status='Absent' "
                "GROUP BY employee, employee_name HAVING COUNT(*) > 5) x;")
            high_names = [n for n in high_names_raw.split("||")
                          if n.strip() and n.strip() != "NULL"]
            claims = hrms_bench_query(
                "SELECT COUNT(*), COALESCE(SUM(grand_total - total_amount_reimbursed),0) "
                "FROM `tabExpense Claim` WHERE docstatus=1 AND status='Unpaid';"
            ).split("\t")
            claims_cnt, claims_total = int(claims[0]), float(claims[1])
            sick = float(hrms_bench_query(
                "SELECT COALESCE(SUM(leaves),0) FROM `tabLeave Ledger Entry` "
                "WHERE leave_type='Sick Leave' AND docstatus=1;"))
            adv = hrms_bench_query(
                "SELECT COUNT(*), COALESCE(SUM(paid_amount - claimed_amount - return_amount),0) "
                "FROM `tabEmployee Advance` WHERE docstatus=1 AND "
                "(status='Unpaid' OR (paid_amount - claimed_amount - return_amount) > 0);"
            ).split("\t")
            adv_cnt, adv_total = int(adv[0]), float(adv[1])
        except Exception as sql_e:
            # Audited seed constants (docs/tightening/business.md, business_121)
            att_cnt, high_names = 0, []
            claims_cnt, claims_total = 5, 32400.0
            sick = 98.0
            adv_cnt, adv_total = 0, 0.0
            fallback_note = f"; fallback constants (HRMS SQL failed: {str(sql_e)[:60]})"

        gates = [
            ("Headcount", r"headcount[^:]{0,40}:\s*" + cnt_re(att_cnt)),
            ("Unpaid claims count+total",
             r"unpaid\s*expense\s*claims?[^:]{0,20}:\s*" + cnt_re(claims_cnt)
             + r"\s*totaling\s*" + num_re(claims_total)),
            ("Leave liability days",
             r"leave\s*liability[^:]{0,40}:\s*" + num_re(sick)),
            ("Open advances count+total",
             r"open\s*advances?[^:]{0,20}:\s*" + cnt_re(adv_cnt)
             + r"\s*totaling\s*" + num_re(adv_total)),
        ]
        if high_names:
            for n in high_names:
                gates.append((f"High absence name {n}",
                              re.escape(norm(n))))
        else:
            gates.append(("High absence: None",
                          r"high\s*absence[^:]{0,40}:\s*none\b"))
        failed = gate_all(sec, gates)
        ok = not failed
        detail = ("all HR figures match DB truths" if ok
                  else f"failed gates: {failed[:4]}") + fallback_note
        check(label, 3, ok, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_7_ops_review_event() -> None:
    """EVENT PERFORMANCE section: figures anchored to Pretix-DB-computed truths."""
    label = "7. Twenty: Ops Review event performance figures"
    try:
        body_raw = get_note_body(OPS_TITLE_LIKE)
        if not body_raw:
            check(label, 3, False, "note/body not found")
            return
        b = norm(body_raw)
        sec = section(b, "event performance", [])

        fallback_note = ""
        try:
            rows = pretix_psql(
                "SELECT ord.status, COUNT(*), COALESCE(SUM(ord.total),0) "
                "FROM pretixbase_order ord "
                "JOIN pretixbase_event e ON ord.event_id = e.id "
                "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
                "WHERE o.slug = 'broadway-group' AND e.slug = 'hamilton' "
                "GROUP BY ord.status;")
            status: dict[str, tuple[int, float]] = {}
            for line in rows.splitlines():
                parts = line.split("|")
                if len(parts) >= 3:
                    status[parts[0].strip()] = (int(parts[1]), float(parts[2]))
            total_orders = sum(c for c, _ in status.values())
            paid_cnt, paid_rev = status.get("p", (0, 0.0))
            pend_cnt = status.get("n", (0, 0.0))[0]
            canc_cnt = status.get("c", (0, 0.0))[0]
            prows = pretix_psql(
                "SELECT i.name::text, ord.status, COALESCE(SUM(op.price),0) "
                "FROM pretixbase_orderposition op "
                "JOIN pretixbase_order ord ON op.order_id = ord.id "
                "JOIN pretixbase_item i ON op.item_id = i.id "
                "JOIN pretixbase_event e ON ord.event_id = e.id "
                "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
                "WHERE o.slug = 'broadway-group' AND e.slug = 'hamilton' "
                "GROUP BY i.name::text, ord.status;")
            bal_paid = bal_all = pb_paid = pb_all = 0.0
            for line in prows.splitlines():
                parts = line.split("|")
                if len(parts) < 3:
                    continue
                name, st, amt = parts[0].lower(), parts[1].strip(), float(parts[2])
                if "balcony" in name:
                    bal_all += amt
                    if st == "p":
                        bal_paid += amt
                elif "playbill" in name:
                    pb_all += amt
                    if st == "p":
                        pb_paid += amt
        except Exception as sql_e:
            # Audited seed constants (docs/tightening/business.md, business_121)
            total_orders, paid_rev = 20, 7178.96
            paid_cnt, pend_cnt, canc_cnt = 8, 9, 1
            bal_paid, bal_all, pb_paid, pb_all = 262.77, 1576.62, 30.00, 30.00
            fallback_note = f"; fallback constants (pretix SQL failed: {str(sql_e)[:60]})"

        # Balcony/Playbill: orders overview exposes both paid-only and all-status
        # column readings; accept either.
        gates = [
            ("Total orders: 20",
             r"(?:total\s*)?orders\s*:\s*" + cnt_re(total_orders)),
            ("Revenue 7,178.96", r"revenue\s*:\s*" + num_re(paid_rev)),
            ("Balcony revenue",
             r"balcony[^:]{0,20}:\s*(?:" + num_re(bal_paid) + r"|" + num_re(bal_all) + r")"),
            ("Playbill revenue",
             r"playbill[^:]{0,30}:\s*(?:" + num_re(pb_paid) + r"|" + num_re(pb_all) + r")"),
            ("Paid: 8", r"\bpaid\s*:\s*" + cnt_re(paid_cnt)),
            ("Pending: 9", r"\bpending\s*:\s*" + cnt_re(pend_cnt)),
            ("Cancelled: 1", r"\bcancel+ed\s*:\s*" + cnt_re(canc_cnt)),
        ]
        failed = gate_all(sec, gates)
        ok = not failed
        detail = ("all event figures match DB truths" if ok
                  else f"failed gates: {failed[:4]}") + fallback_note
        check(label, 3, ok, detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_8_presentation_task() -> None:
    """Presentation task: due 2026-10-22, body references review note + key sentence."""
    label = "8. Twenty: Presentation task (due date + body)"
    try:
        schema = get_twenty_schema()
        body_expr = _FLATTEN_BODY_SQL.replace('"bodyV2', 't."bodyV2')
        raw = twenty_psql(
            f'SELECT t.title, t."dueAt"::text, {body_expr} '
            f'FROM "{schema}".task t '
            "WHERE t.title LIKE '%Present Q3 operations review to leadership%' "
            "AND t.\"deletedAt\" IS NULL;",
            field_sep="\x1f",
        )
        if not raw:
            check(label, 2, False, "task not found")
            return
        best_fail = "no candidate row"
        ok = False
        for line in raw.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3:
                continue
            _, due, body = parts[0], parts[1], "\x1f".join(parts[2:])
            due_ok = date_matches_tz(due, "2026-10-22")
            bn = norm(body)
            ref_ok = "q3 operations review - complete - 2026-09-30" in bn
            key_ok = "overdue collection tasks assigned" in bn
            if due_ok and ref_ok and key_ok:
                ok = True
                break
            best_fail = f"due_ok={due_ok} note_ref_ok={ref_ok} key_sentence_ok={key_ok}"
        check(label, 2, ok,
              "due 2026-10-22, body references review note + key sentence" if ok
              else best_fail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_followup_tasks() -> None:
    """Set-equality: exactly one correct follow-up task per API-derived overdue client."""
    label = "9. Twenty: Follow-up tasks == overdue client set"
    try:
        clients = get_overdue_clients()
        schema = get_twenty_schema()
        body_expr = _FLATTEN_BODY_SQL.replace('"bodyV2', 't."bodyV2')
        raw = twenty_psql(
            f'SELECT t.id, t.title, t."dueAt"::text, {body_expr}, '
            f'(SELECT COUNT(*) FROM "{schema}"."taskTarget" tt '
            ' WHERE tt."taskId" = t.id AND tt."deletedAt" IS NULL '
            ' AND tt."targetCompanyId" IS NOT NULL) '
            f'FROM "{schema}".task t '
            "WHERE t.title LIKE '%Follow up on overdue receivable%' "
            "AND t.\"deletedAt\" IS NULL;",
            field_sep="\x1f", timeout=30,
        )
        tasks: list[tuple[str, str, str, str, str]] = []
        for line in raw.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 5:
                # body may itself contain the separator only if agent typed \x1f — join middle back
                tasks.append((parts[0], parts[1], parts[2],
                              "\x1f".join(parts[3:-1]), parts[-1]))

        # Companies with an exact (normalized) name match decide linked/unlinked.
        comp_raw = twenty_psql(
            f'SELECT name FROM "{schema}".company WHERE "deletedAt" IS NULL;',
            timeout=30,
        )
        company_names = {norm(c) for c in comp_raw.splitlines() if c.strip()}

        prefix = "follow up on overdue receivable - "
        norm_to_client = {prefix + norm(c): c for c in clients}
        by_client: dict[str, list[tuple[str, str, str, str, str]]] = {}
        extras: list[str] = []
        for row in tasks:
            cl = norm_to_client.get(norm(row[1]))
            if cl is None:
                extras.append(row[1])
            else:
                by_client.setdefault(cl, []).append(row)

        problems: list[str] = []
        if extras:
            problems.append(f"{len(extras)} extra task(s) outside overdue set: {extras[:2]}")
        missing = [c for c in clients if c not in by_client]
        if missing:
            problems.append(f"missing {len(missing)}/{len(clients)} clients: {missing[:3]}")
        dups = [c for c, v in by_client.items() if len(v) > 1]
        if dups:
            problems.append(f"duplicate tasks for: {dups[:3]}")
        bad_due, bad_body, bad_link = [], [], []
        for c, v in by_client.items():
            if len(v) != 1:
                continue
            _, _, due, body, linked = v[0]
            if not date_matches_tz(due, "2026-10-15"):
                bad_due.append(c)
            bn = norm(body)
            if "q3 aged receivables review" not in bn or "2026-10-15" not in bn:
                bad_body.append(c)
            should_link = norm(c) in company_names
            is_linked = linked.strip() not in ("", "0")
            if is_linked != should_link:
                bad_link.append(c)
        if bad_due:
            problems.append(f"wrong due date for: {bad_due[:3]}")
        if bad_body:
            problems.append(f"body missing review ref/deadline for: {bad_body[:3]}")
        if bad_link:
            problems.append(f"wrong company-link state for: {bad_link[:3]}")

        ok = not problems
        check(label, 3, ok,
              f"exactly one correct unlinked/linked task per {len(clients)} overdue clients"
              if ok else "; ".join(problems)[:400])
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── 0-weight sanity preconditions (seed-state existence; diagnostics only) ────

def check_10_pretix_hamilton_event() -> None:
    """[sanity] Hamilton event exists under broadway-group in Pretix."""
    try:
        rows = pretix_psql(
            "SELECT e.slug, e.live FROM pretixbase_event e "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            "WHERE o.slug = 'broadway-group' "
            "AND (e.slug ILIKE '%hamilton%' OR e.name::text ILIKE '%Hamilton%');"
        )
        found = bool(rows)
        check("10. [sanity] Pretix: Hamilton event exists", 0, found,
              f"found={rows[:120]}" if found else "event not found")
    except Exception as e:
        check("10. [sanity] Pretix: Hamilton event exists", 0, False, f"exception: {e}")


def check_11_pretix_products() -> None:
    """[sanity] Balcony and Playbill Program products exist for Hamilton event."""
    try:
        rows = pretix_psql(
            "SELECT i.name::text FROM pretixbase_item i "
            "JOIN pretixbase_event e ON i.event_id = e.id "
            "JOIN pretixbase_organizer o ON e.organizer_id = o.id "
            "WHERE o.slug = 'broadway-group' "
            "AND (e.slug ILIKE '%hamilton%' OR e.name::text ILIKE '%Hamilton%');"
        )
        rows_lower = rows.lower()
        has_balcony = "balcony" in rows_lower
        has_playbill = "playbill" in rows_lower
        ok = has_balcony and has_playbill
        missing = []
        if not has_balcony: missing.append("Balcony")
        if not has_playbill: missing.append("Playbill Program")
        check("11. [sanity] Pretix: Balcony & Playbill products exist", 0, ok,
              "both found" if ok else f"missing: {missing}, items: {rows[:200]}")
    except Exception as e:
        check("11. [sanity] Pretix: Balcony & Playbill products exist", 0, False, f"exception: {e}")


def check_12_hrms_company() -> None:
    """[sanity] TechVista Solutions Pvt. Ltd. company exists in HRMS."""
    try:
        rows = hrms_bench_query(
            "SELECT name FROM `tabCompany` "
            "WHERE name LIKE '%TechVista%' LIMIT 1;"
        )
        found = bool(rows)
        check("12. [sanity] HRMS: TechVista company exists", 0, found,
              f"found={rows[:80]}" if found else "company not found")
    except Exception as e:
        check("12. [sanity] HRMS: TechVista company exists", 0, False, f"exception: {e}")


def check_13_hrms_employees() -> None:
    """[sanity] Active employees exist in HRMS for attendance verification."""
    try:
        rows = hrms_bench_query(
            "SELECT COUNT(*) FROM `tabEmployee` WHERE status = 'Active';"
        )
        count = int(rows.strip()) if rows.strip().isdigit() else 0
        ok = count > 0
        check("13. [sanity] HRMS: Active employees exist", 0, ok,
              f"{count} active employees" if ok else "no active employees found")
    except Exception as e:
        check("13. [sanity] HRMS: Active employees exist", 0, False, f"exception: {e}")


def check_14_bigcapital_customers() -> None:
    """[sanity] Customers exist in BigCapital for A/R aging data."""
    try:
        rows = bigcapital_tenant_query(
            "SELECT COUNT(*) FROM CONTACTS WHERE CONTACT_SERVICE = 'customer';"
        )
        count = int(rows.strip()) if rows.strip().isdigit() else 0
        ok = count > 0
        check("14. [sanity] BigCapital: Customers exist", 0, ok,
              f"{count} customers" if ok else "no customers found")
    except Exception as e:
        check("14. [sanity] BigCapital: Customers exist", 0, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # 0-weight sanity preconditions first (diagnostics for the scored checks)
    check_10_pretix_hamilton_event()
    check_11_pretix_products()
    check_12_hrms_company()
    check_13_hrms_employees()
    check_14_bigcapital_customers()

    check_1_pipeline_summary_note_exists()
    check_2_pipeline_summary_content()
    check_3_ops_review_note_exists()
    check_4_ops_review_financial()
    check_5_ops_review_crm()
    check_6_ops_review_hr()
    check_7_ops_review_event()
    check_8_presentation_task()
    check_9_followup_tasks()

    total = sum(w for _, w, _, _ in _checks)
    earned = sum(w for _, w, p, _ in _checks if p)
    weighted = [(w, p) for _, w, p, _ in _checks if w > 0]
    all_pass = all(p for _, p in weighted) and bool(weighted)
    score = (earned / total) if total else 0.0

    print(
        f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})",
        file=sys.stderr,
    )
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
