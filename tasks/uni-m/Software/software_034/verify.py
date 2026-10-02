"""
Verifier for Software-034-I2: Multi-Project Coverage Audit across code-server, Baserow, OpenProject.

Checks: 15 checks (ck0 is a 0pt truth-recompute front check; 14 weighted, total 26)
across code-server, baserow, openproject.
Strategy: verifier recomputes coverage ground truth by re-running the pinned
`npm run coverage:audit` fixture in a throwaway container from the live
code-server container's own PRISTINE image (immune to agent edits; the task's
original `npx vitest run --coverage` path is broken in the pristine image;
the fixture script is the oracle). All truth-dependent checks FAIL when the
truth recompute fails — never fall back to agent-filled data.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER.
"""

import html
import os
import re
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

for var_name, var_val in [
    ("CODE_SERVER_PORT", CODE_SERVER_PORT),
    ("CODE_SERVER_CONTAINER", CODE_SERVER_CONTAINER),
    ("BASEROW_PORT", BASEROW_PORT),
    ("BASEROW_CONTAINER", BASEROW_CONTAINER),
    ("BASEROW_DB_CONTAINER", BASEROW_DB_CONTAINER),
    ("OPENPROJECT_PORT", OPENPROJECT_PORT),
    ("OPENPROJECT_CONTAINER", OPENPROJECT_CONTAINER),
]:
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# ── Expected data ─────────────────────────────────────────────────────────────
MODULES_BY_PROJECT = {
    "blog-engine": ["src/middleware/logger.js", "src/services/markdownRenderer.js"],
    "tabler": ["core/js/tabler.ts", "core/scss/"],
    "weather-dashboard": ["src/services/geocoding.ts", "src/utils/constants.ts"],
}

# Rows ordered by project alpha then module alpha
EXPECTED_ROWS = []
_idx = 1
for proj in sorted(MODULES_BY_PROJECT.keys()):
    for mod in sorted(MODULES_BY_PROJECT[proj]):
        EXPECTED_ROWS.append({
            "entry_id": f"CV-{_idx:03d}",
            "project": proj,
            "module_path": mod,
        })
        _idx += 1

PROJECTS = sorted(MODULES_BY_PROJECT.keys())
DB_NAME = "Coverage Audit Sprint 14 2026"
TABLE1_NAME = "Coverage By Module"
TABLE2_NAME = "Project Coverage Summary"
VIEW_NAME = "Remediation Queue"
AUDIT_DATE = "2026-05-20"
REPORT_PATH = "devops-configs/docs/coverage-audit-2026-05-20.md"
OP_PROJECT_NAME = "API Gateway"
QA_OWNER = "Bob Martinez"
THRESHOLD = 70

EXPECTED_T1_FIELDS = {
    "Entry ID", "Project", "Module Path", "Coverage Pct", "Captured At", "Below Threshold",
}

# ── Coverage ground truth (verifier-side recompute) ──────────────────────────
# The task statement's `npx vitest run --coverage` is broken in the pristine
# image (npx pulls a vitest incompatible with node18); the pre-baked fixture
# `npm run coverage:audit` (pinned vitest 1.6.1 / jest 29.7.0 under
# /opt/software034) is the oracle and produces the same numbers.
#
# NOTE (image-rebuild fragility): for tabler the coverage report rows
# (data.ts / manipulator.ts) do NOT share names with the spec module keys
# (core/js/tabler.ts / core/scss/); the row->module mapping below is
# hardcoded per the tightening plan and must be revisited whenever
# code-server-bundle:latest is regenerated.
ROW_TO_MODULE = {
    "weather-dashboard": {
        "geocoding.ts": "src/services/geocoding.ts",
        "constants.ts": "src/utils/constants.ts",
    },
    "blog-engine": {
        "markdownRenderer.js": "src/services/markdownRenderer.js",
        "logger.js": "src/middleware/logger.js",
    },
    "tabler": {
        "data.ts": "core/js/tabler.ts",
        "manipulator.ts": "core/scss/",
    },
}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# `% Stmts` column of the v8/istanbul text reporter:  name | stmts | ...
COVERAGE_ROW_RE = re.compile(r"^\s*(\S+\.(?:ts|js))\s*\|\s*([\d.]+)\s*\|", re.M)

PCT_TOL = 0.005     # per-module pct reconciliation tolerance
AVG_TOL = 0.011     # avg tolerance (±0.01 plus float slack)

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
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


_pristine_image_cache: str | None = None


def _pristine_image() -> str:
    """The code-server container's own image ref — truth reads go here, immune to agent edits."""
    global _pristine_image_cache
    if _pristine_image_cache is None:
        r = subprocess.run(["docker", "inspect", CODE_SERVER_CONTAINER, "--format", "{{.Image}}"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f"docker inspect failed: {r.stderr.strip()[:200]}")
        _pristine_image_cache = r.stdout.strip()
    return _pristine_image_cache


def image_exec(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a command in a throwaway container from the PRISTINE image (not the live one)."""
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", args[0], _pristine_image(), *args[1:]],
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def baserow_auth() -> str:
    """Get Baserow JWT token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["token"]


def baserow_get(token: str, path: str, params: dict | None = None) -> dict:
    resp = requests.get(
        f"{BASEROW_URL}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


# ── Shared state across checks ───────────────────────────────────────────────
_bstate: dict = {}


def _init_baserow():
    """Auth and find the database + tables. Populates _bstate."""
    if _bstate.get("baserow_init"):
        return
    _bstate["baserow_init"] = True
    token = baserow_auth()
    _bstate["token"] = token

    # List all applications (databases)
    apps_resp = requests.get(
        f"{BASEROW_URL}/api/applications/",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    apps_resp.raise_for_status()
    apps = apps_resp.json()
    db = None
    for a in apps:
        if a.get("name") == DB_NAME and a.get("type") == "database":
            db = a
            break
    _bstate["db"] = db
    if not db:
        return

    # List tables
    tables_resp = baserow_get(token, f"database/tables/database/{db['id']}/")
    _bstate["tables"] = tables_resp
    for t in tables_resp:
        if t["name"] == TABLE1_NAME:
            _bstate["table1"] = t
        elif t["name"] == TABLE2_NAME:
            _bstate["table2"] = t


def _get_table1_fields() -> list | None:
    """Fetch (once) the field list of table 1."""
    if "table1_fields" in _bstate:
        return _bstate["table1_fields"]
    _init_baserow()
    t1 = _bstate.get("table1")
    if not t1:
        _bstate["table1_fields"] = None
        return None
    fields = baserow_get(_bstate["token"], f"database/fields/table/{t1['id']}/")
    _bstate["table1_fields"] = fields
    return fields


# ── Truth recompute helpers ───────────────────────────────────────────────────
def _compute_coverage_truth() -> tuple[dict, list[str]]:
    """Re-run the three coverage fixtures and parse per-module % Stmts.

    Each fixture runs in a throwaway container from the PRISTINE image (not the
    live, agent-touched container). Returns ({(project, module_path): pct},
    [error strings]).
    """
    pcts: dict = {}
    errors: list[str] = []
    for proj in PROJECTS:
        mapping = ROW_TO_MODULE[proj]
        try:
            rc, out, err = image_exec(
                "bash", "-c",
                f"cd /home/coder/workspace/{proj} && npm run coverage:audit",
                timeout=170,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"{proj}: coverage run timed out (170s)")
            continue
        except Exception as e:  # docker itself failed
            errors.append(f"{proj}: pristine-image exec failed: {e}")
            continue
        text = _ANSI_RE.sub("", (out or "") + "\n" + (err or ""))
        found: dict = {}
        for m in COVERAGE_ROW_RE.finditer(text):
            name, pct = m.group(1), m.group(2)
            if name in mapping and name not in found:
                found[name] = float(pct)
        missing = [n for n in mapping if n not in found]
        for n in missing:
            errors.append(
                f"{proj}: no coverage row for {n}"
                + (f" (rc={rc})" if rc != 0 else "")
            )
        for name, val in found.items():
            pcts[(proj, mapping[name])] = val
    return pcts, errors


def _truth_pcts() -> dict | None:
    """Complete truth dict {(project, module): pct} or None if recompute failed."""
    return _bstate.get("truth_pcts")


def _avg_candidates(vals: list[float]) -> set[float]:
    """Both rounding interpretations of the mean: round-half-even (Python
    round on the float mean, e.g. 82.60) and half-up on the exact decimal
    mean (e.g. 82.605 -> 82.61). Mean computed in Decimal so the .5 case
    is not lost to binary floating point."""
    mean_dec = sum(Decimal(repr(v)) for v in vals) / Decimal(len(vals))
    c1 = round(sum(vals) / len(vals), 2)
    c2 = float(mean_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    c3 = float(mean_dec.quantize(Decimal("0.01")))  # half-even on exact decimal
    return {c1, c2, c3}


def _project_truth_summary() -> dict | None:
    """Per-project derived truth: {proj: {'avgs': set, 'count': n, 'below': k}}."""
    truth = _truth_pcts()
    if truth is None:
        return None
    out = {}
    for proj in PROJECTS:
        vals = [truth[(proj, m)] for m in MODULES_BY_PROJECT[proj]]
        out[proj] = {
            "avgs": _avg_candidates(vals),
            "count": len(vals),
            "below": sum(1 for v in vals if v < THRESHOLD),
        }
    return out


def _five_lowest() -> list | None:
    """5 lowest-coverage modules from truth: [((proj, module), pct)] sorted by
    (pct asc, project alpha, module alpha)."""
    truth = _truth_pcts()
    if truth is None:
        return None
    ranked = sorted(truth.items(), key=lambda kv: (kv[1], kv[0][0], kv[0][1]))
    return ranked[:5]


def _num_eq(a: float, b: float, tol: float = PCT_TOL) -> bool:
    return abs(a - b) <= tol


# ── Individual checks ─────────────────────────────────────────────────────────

def check_0_coverage_truth() -> None:
    """ck0 (0pt, front check): re-run the three coverage:audit fixtures and
    extract the 6 ground-truth per-module pcts. On failure, every
    truth-dependent check (6/8/11/12/14) fails — no fallback to agent data."""
    try:
        pcts, errors = _compute_coverage_truth()
        complete = len(pcts) == 6 and not errors
        _bstate["truth_pcts"] = pcts if complete else None
        if complete:
            summary = "; ".join(
                f"{p}/{m}={v}" for (p, m), v in sorted(pcts.items())
            )
            check("0. Coverage truth recompute (fixture re-run)", 0, True, summary)
        else:
            check("0. Coverage truth recompute (fixture re-run)", 0, False,
                  f"truth recompute failed: {'; '.join(errors) or 'incomplete rows'}")
    except Exception as e:
        _bstate["truth_pcts"] = None
        check("0. Coverage truth recompute (fixture re-run)", 0, False,
              f"truth recompute failed: {e}")


def check_1_baserow_db_exists() -> None:
    """Baserow database 'Coverage Audit Sprint 14 2026' exists."""
    try:
        _init_baserow()
        db = _bstate.get("db")
        check("1. Baserow database exists", 2, db is not None,
              f"expected '{DB_NAME}'" if not db else f"found id={db['id']}")
    except Exception as e:
        check("1. Baserow database exists", 2, False, f"exception: {e}")


def check_2_table1_fields() -> None:
    """Table 'Coverage By Module' has exactly the 6 fields with exact schema."""
    try:
        fields = _get_table1_fields()
        if fields is None:
            check("2. Coverage By Module table + field schema", 2, False, "table not found")
            return

        bad = []
        field_names = {f["name"] for f in fields}
        if field_names != EXPECTED_T1_FIELDS:
            extra = field_names - EXPECTED_T1_FIELDS
            missing = EXPECTED_T1_FIELDS - field_names
            bad.append(f"field name set mismatch: missing={sorted(missing)}, extra={sorted(extra)}")

        by_name = {f["name"]: f for f in fields}

        def _f(name):
            return by_name.get(name, {})

        # Entry ID: primary text
        f = _f("Entry ID")
        if not f.get("primary"):
            bad.append("Entry ID not primary")
        if f.get("type") != "text":
            bad.append(f"Entry ID type={f.get('type')} expected text")
        # Project: single_select with exactly the 3 project options
        f = _f("Project")
        if f.get("type") != "single_select":
            bad.append(f"Project type={f.get('type')} expected single_select")
        else:
            opts = {o.get("value") for o in f.get("select_options", [])}
            if opts != set(PROJECTS):
                bad.append(f"Project options={sorted(opts)} expected {PROJECTS}")
        # Module Path: text
        f = _f("Module Path")
        if f.get("type") != "text":
            bad.append(f"Module Path type={f.get('type')} expected text")
        # Coverage Pct: number with 2 decimals
        f = _f("Coverage Pct")
        if f.get("type") != "number":
            bad.append(f"Coverage Pct type={f.get('type')} expected number")
        elif int(f.get("number_decimal_places") or 0) != 2:
            bad.append(f"Coverage Pct decimal_places={f.get('number_decimal_places')} expected 2")
        # Captured At: date
        f = _f("Captured At")
        if f.get("type") != "date":
            bad.append(f"Captured At type={f.get('type')} expected date")
        # Below Threshold: boolean
        f = _f("Below Threshold")
        if f.get("type") != "boolean":
            bad.append(f"Below Threshold type={f.get('type')} expected boolean")

        ok = len(bad) == 0
        check("2. Coverage By Module table + field schema", 2, ok,
              "; ".join(bad[:4]) if bad else "6 fields, exact schema (types/options/primary)")
    except Exception as e:
        check("2. Coverage By Module table + field schema", 2, False, f"exception: {e}")


def check_3_table1_row_count_and_ids() -> None:
    """Coverage By Module has exactly 6 rows with correct Entry IDs and ordering."""
    try:
        _init_baserow()
        t1 = _bstate.get("table1")
        if not t1:
            check("3. Coverage By Module rows + IDs", 2, False, "table not found")
            return
        token = _bstate["token"]
        rows_resp = baserow_get(token, f"database/rows/table/{t1['id']}/",
                                params={"size": 200, "user_field_names": "true"})
        rows = rows_resp.get("results", [])
        _bstate["table1_rows"] = rows

        if len(rows) != 6:
            check("3. Coverage By Module rows + IDs", 2, False,
                  f"expected 6 rows, got {len(rows)}")
            return

        actual_ids = [r.get("Entry ID", "") for r in rows]
        expected_ids = [er["entry_id"] for er in EXPECTED_ROWS]
        ok = actual_ids == expected_ids
        check("3. Coverage By Module rows + IDs", 2, ok,
              f"IDs: {actual_ids}" if not ok else "6 rows, IDs CV-001..CV-006 in order")
    except Exception as e:
        check("3. Coverage By Module rows + IDs", 2, False, f"exception: {e}")


def check_4_table1_project_module() -> None:
    """Coverage By Module rows have correct Project and Module Path values."""
    try:
        rows = _bstate.get("table1_rows", [])
        if not rows:
            check("4. Coverage By Module Project+Module values", 2, False, "no rows loaded")
            return
        mismatches = []
        for i, row in enumerate(rows):
            exp = EXPECTED_ROWS[i] if i < len(EXPECTED_ROWS) else None
            if not exp:
                mismatches.append(f"row {i}: unexpected extra row")
                continue
            # Project may be a dict (single-select) or string
            proj_val = row.get("Project", "")
            if isinstance(proj_val, dict):
                proj_val = proj_val.get("value", "")
            mod_val = row.get("Module Path", "")
            if proj_val != exp["project"]:
                mismatches.append(f"row {i}: project expected '{exp['project']}', got '{proj_val}'")
            if mod_val != exp["module_path"]:
                mismatches.append(f"row {i}: module expected '{exp['module_path']}', got '{mod_val}'")
        ok = len(mismatches) == 0
        check("4. Coverage By Module Project+Module values", 2, ok,
              "; ".join(mismatches[:3]) if mismatches else "all 6 rows match")
    except Exception as e:
        check("4. Coverage By Module Project+Module values", 2, False, f"exception: {e}")


def check_5_table1_captured_at() -> None:
    """Coverage By Module rows have Captured At = 2026-05-20."""
    try:
        rows = _bstate.get("table1_rows", [])
        if not rows:
            check("5. Coverage By Module Captured At", 1, False, "no rows loaded")
            return
        bad = []
        for i, row in enumerate(rows):
            val = str(row.get("Captured At", ""))
            if not val.startswith(AUDIT_DATE):
                bad.append(f"row {i}: '{val}'")
        ok = len(bad) == 0
        check("5. Coverage By Module Captured At", 1, ok,
              f"wrong dates: {'; '.join(bad[:3])}" if bad else f"all rows have {AUDIT_DATE}")
    except Exception as e:
        check("5. Coverage By Module Captured At", 1, False, f"exception: {e}")


def check_6_table1_coverage_truth() -> None:
    """Coverage Pct matches the verifier-recomputed truth (tol 0.005) AND
    Below Threshold == (truth pct < 70) per row. Truth-gated: pct
    reconciliation kept inside this check (weight discipline, total stays 26)."""
    try:
        truth = _truth_pcts()
        if truth is None:
            check("6. Coverage Pct + Below Threshold vs truth", 2, False,
                  "coverage truth recompute failed (see check 0) — no fallback to agent data")
            return
        rows = _bstate.get("table1_rows", [])
        if not rows:
            check("6. Coverage Pct + Below Threshold vs truth", 2, False, "no rows loaded")
            return
        bad = []
        for i, row in enumerate(rows):
            proj_val = row.get("Project", "")
            if isinstance(proj_val, dict):
                proj_val = proj_val.get("value", "")
            mod_val = row.get("Module Path", "")
            key = (proj_val, mod_val)
            if key not in truth:
                bad.append(f"row {i}: ({proj_val}, {mod_val}) not a known (project, module) pair")
                continue
            truth_pct = truth[key]
            cov = row.get("Coverage Pct")
            try:
                cov_f = float(cov)
            except (TypeError, ValueError):
                bad.append(f"row {i}: Coverage Pct not numeric: {cov}")
                continue
            if not _num_eq(cov_f, truth_pct):
                bad.append(f"row {i} {proj_val}/{mod_val}: pct={cov_f} truth={truth_pct}")
            expected_bt = truth_pct < THRESHOLD
            bt = row.get("Below Threshold")
            actual_bt = bt if isinstance(bt, bool) else bt in (True, "true", "True", 1)
            if actual_bt != expected_bt:
                bad.append(f"row {i} {proj_val}/{mod_val}: BT={actual_bt} expected {expected_bt} (truth pct {truth_pct})")
        ok = len(bad) == 0
        check("6. Coverage Pct + Below Threshold vs truth", 2, ok,
              "; ".join(bad[:3]) if bad else "all 6 rows match recomputed truth")
    except Exception as e:
        check("6. Coverage Pct + Below Threshold vs truth", 2, False, f"exception: {e}")


def check_7_table2_exists_and_rows() -> None:
    """Project Coverage Summary exists with 3 rows, correct Module Count."""
    try:
        _init_baserow()
        t2 = _bstate.get("table2")
        if not t2:
            check("7. Project Coverage Summary table + rows", 2, False, "table not found")
            return
        token = _bstate["token"]
        rows_resp = baserow_get(token, f"database/rows/table/{t2['id']}/",
                                params={"size": 200, "user_field_names": "true"})
        rows = rows_resp.get("results", [])
        _bstate["table2_rows"] = rows

        if len(rows) != 3:
            check("7. Project Coverage Summary table + rows", 2, False,
                  f"expected 3 rows, got {len(rows)}")
            return

        bad = []
        for row in rows:
            proj_val = row.get("Project", "")
            if isinstance(proj_val, dict):
                proj_val = proj_val.get("value", "")
            mc = row.get("Module Count")
            expected_mc = len(MODULES_BY_PROJECT.get(proj_val, []))
            if expected_mc == 0:
                bad.append(f"unknown project '{proj_val}'")
            elif mc is not None and int(mc) != expected_mc:
                bad.append(f"{proj_val}: Module Count expected {expected_mc}, got {mc}")

        ok = len(bad) == 0
        check("7. Project Coverage Summary table + rows", 2, ok,
              "; ".join(bad) if bad else "3 rows, Module Count correct")
    except Exception as e:
        check("7. Project Coverage Summary table + rows", 2, False, f"exception: {e}")


def check_8_table2_avg_coverage() -> None:
    """Project Coverage Summary gated on truth: Avg Coverage Pct (double
    rounding tolerance ±0.01), Module Count, and Below Threshold Count all
    recomputed from truth pcts."""
    try:
        summary = _project_truth_summary()
        if summary is None:
            check("8. Project Coverage Summary vs truth", 2, False,
                  "coverage truth recompute failed (see check 0) — no fallback to agent data")
            return
        t2_rows = _bstate.get("table2_rows", [])
        if not t2_rows:
            check("8. Project Coverage Summary vs truth", 2, False, "rows not loaded")
            return

        bad = []
        seen = set()
        for row in t2_rows:
            proj_val = row.get("Project", "")
            if isinstance(proj_val, dict):
                proj_val = proj_val.get("value", "")
            if proj_val not in summary:
                bad.append(f"unknown project '{proj_val}'")
                continue
            seen.add(proj_val)
            exp = summary[proj_val]
            # Avg Coverage Pct: accept both round-half-even and half-up, tol ±0.01
            avg = row.get("Avg Coverage Pct")
            try:
                avg_f = float(avg)
            except (TypeError, ValueError):
                bad.append(f"{proj_val}: Avg Coverage Pct not numeric: {avg}")
                avg_f = None
            if avg_f is not None and not any(abs(avg_f - c) <= AVG_TOL for c in exp["avgs"]):
                bad.append(f"{proj_val}: avg={avg_f} expected one of {sorted(exp['avgs'])}")
            # Module Count gated from truth
            mc = row.get("Module Count")
            try:
                mc_i = int(float(mc))
            except (TypeError, ValueError):
                mc_i = None
            if mc_i != exp["count"]:
                bad.append(f"{proj_val}: Module Count={mc} expected {exp['count']}")
            # Below Threshold Count gated from truth
            btc = row.get("Below Threshold Count")
            try:
                btc_i = int(float(btc))
            except (TypeError, ValueError):
                btc_i = None
            if btc_i != exp["below"]:
                bad.append(f"{proj_val}: Below Threshold Count={btc} expected {exp['below']}")
        if seen != set(PROJECTS):
            bad.append(f"projects covered {sorted(seen)} expected {PROJECTS}")

        ok = len(bad) == 0
        check("8. Project Coverage Summary vs truth", 2, ok,
              "; ".join(bad[:4]) if bad else "avg/module-count/below-count all match truth")
    except Exception as e:
        check("8. Project Coverage Summary vs truth", 2, False, f"exception: {e}")


def check_9_remediation_queue_view() -> None:
    """Remediation Queue view: type grid; exactly one filter
    (Below Threshold, boolean, "1"); exactly one sorting (Coverage Pct, ASC)."""
    try:
        _init_baserow()
        t1 = _bstate.get("table1")
        if not t1:
            check("9. Remediation Queue view (grid + filter + sort)", 2, False, "table1 not found")
            return
        token = _bstate["token"]
        views_resp = baserow_get(token, f"database/views/table/{t1['id']}/")
        view = None
        for v in views_resp:
            if v.get("name") == VIEW_NAME:
                view = v
                break
        if not view:
            check("9. Remediation Queue view (grid + filter + sort)", 2, False,
                  f"view '{VIEW_NAME}' not found; views: {[v.get('name') for v in views_resp]}")
            return

        bad = []
        if view.get("type") != "grid":
            bad.append(f"view type={view.get('type')} expected grid")

        fields = _get_table1_fields() or []
        fid_by_name = {f["name"]: f["id"] for f in fields}
        bt_id = fid_by_name.get("Below Threshold")
        cov_id = fid_by_name.get("Coverage Pct")

        filters = baserow_get(token, f"database/views/{view['id']}/filters/")
        if len(filters) != 1:
            bad.append(f"expected exactly 1 filter, got {len(filters)}")
        else:
            flt = filters[0]
            if flt.get("field") != bt_id:
                bad.append(f"filter field id={flt.get('field')} expected Below Threshold ({bt_id})")
            if flt.get("type") != "boolean":
                bad.append(f"filter type={flt.get('type')} expected boolean")
            if str(flt.get("value")) != "1":
                bad.append(f"filter value={flt.get('value')!r} expected '1'")

        sortings = baserow_get(token, f"database/views/{view['id']}/sortings/")
        if len(sortings) != 1:
            bad.append(f"expected exactly 1 sorting, got {len(sortings)}")
        else:
            srt = sortings[0]
            if srt.get("field") != cov_id:
                bad.append(f"sort field id={srt.get('field')} expected Coverage Pct ({cov_id})")
            if str(srt.get("order", "")).upper() != "ASC":
                bad.append(f"sort order={srt.get('order')} expected ASC")

        ok = len(bad) == 0
        check("9. Remediation Queue view (grid + filter + sort)", 2, ok,
              "; ".join(bad[:4]) if bad else "grid, 1 filter (Below Threshold=1), 1 sort (Coverage Pct ASC)")
    except Exception as e:
        check("9. Remediation Queue view (grid + filter + sort)", 2, False, f"exception: {e}")


def check_10_report_file_exists() -> None:
    """File devops-configs/docs/coverage-audit-2026-05-20.md exists in code-server."""
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER,
                                   "test", "-f", f"/home/coder/workspace/{REPORT_PATH}")
        found = rc == 0
        if not found:
            # Try alternate paths
            rc2, out2, _ = docker_exec(CODE_SERVER_CONTAINER,
                                       "find", "/home/coder", "-path",
                                       f"*/{REPORT_PATH}", "-type", "f")
            if rc2 == 0 and out2.strip():
                found = True
                _bstate["report_real_path"] = out2.strip().split("\n")[0]
        check("10. Report file exists in code-server", 1, found,
              "" if found else f"file not found at {REPORT_PATH}")
    except Exception as e:
        check("10. Report file exists in code-server", 1, False, f"exception: {e}")


def check_11_report_file_content() -> None:
    """Report is line-exact against truth: L1 em-dash heading; L2 projects
    line; L3-5 per-project lines assembled from truth (avg double rounding
    tolerance); exactly 5 lines after stripping trailing blanks."""
    try:
        summary = _project_truth_summary()
        if summary is None:
            check("11. Report file content vs truth", 2, False,
                  "coverage truth recompute failed (see check 0) — no fallback to agent data")
            return
        rpath = _bstate.get("report_real_path", f"/home/coder/workspace/{REPORT_PATH}")
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", rpath)
        if rc != 0:
            check("11. Report file content vs truth", 2, False, f"cannot read file: {err.strip()}")
            return

        lines = [l.rstrip() for l in out.replace("\r\n", "\n").split("\n")]
        while lines and lines[-1] == "":
            lines.pop()

        bad = []
        if len(lines) != 5:
            bad.append(f"expected exactly 5 lines (after trailing-blank strip), got {len(lines)}")

        if len(lines) >= 1 and lines[0] != f"# Coverage Audit — {AUDIT_DATE}":
            bad.append(f"line 1 != '# Coverage Audit — {AUDIT_DATE}': '{lines[0]}'")

        if len(lines) >= 2:
            if not re.fullmatch(
                r"Projects:\s*blog-engine,\s*tabler,\s*weather-dashboard", lines[1]
            ):
                bad.append(f"line 2 wrong: '{lines[1]}'")
        else:
            bad.append("line 2 missing")

        for i, proj in enumerate(PROJECTS):
            if len(lines) < 3 + i:
                bad.append(f"line {3+i} missing")
                continue
            line = lines[2 + i]
            exp = summary[proj]
            m = re.fullmatch(
                rf"- {re.escape(proj)}: avg ([0-9]+(?:\.[0-9]+)?)% across "
                rf"{exp['count']} modules; {exp['below']} below 70%",
                line,
            )
            if not m:
                bad.append(f"line {3+i} format/counts wrong for {proj}: '{line}'")
                continue
            avg_f = float(m.group(1))
            if not any(abs(avg_f - c) <= AVG_TOL for c in exp["avgs"]):
                bad.append(f"line {3+i} {proj}: avg={avg_f} expected one of {sorted(exp['avgs'])}")

        ok = len(bad) == 0
        check("11. Report file content vs truth", 2, ok,
              "; ".join(bad[:3]) if bad else "5 lines, all values match recomputed truth")
    except Exception as e:
        check("11. Report file content vs truth", 2, False, f"exception: {e}")


def _op_query(sql: str) -> str:
    """Run a SQL query against OpenProject's embedded Postgres."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER, "bash", "-c",
        f"PGPASSWORD=openproject psql -U openproject -h 127.0.0.1 -d openproject -t -A -c \"{sql}\"",
        timeout=20,
    )
    if rc != 0:
        raise RuntimeError(f"psql failed: {err.strip()}")
    return out.strip()


SUBJECT_RE = re.compile(
    r"^Raise coverage: ([^/]+)/(.+?) \(([0-9]+(?:\.[0-9]+)?)%\)$"
)


def check_12_op_work_packages_exist() -> None:
    """OpenProject 'API Gateway': the 'Raise coverage:' Task subjects are
    exactly the 5 lowest-coverage modules from truth (pct numeric-equivalent,
    e.g. 88% == 88.00%; exact set equality; no extras)."""
    try:
        lowest = _five_lowest()
        # Find project id
        pid_out = _op_query(f"SELECT id FROM projects WHERE name = '{OP_PROJECT_NAME}';")
        if not pid_out:
            check("12. OpenProject 5 lowest-coverage Task WPs", 2, False,
                  f"project '{OP_PROJECT_NAME}' not found in DB")
            return
        pid = pid_out.strip().split("\n")[0]
        _bstate["op_project_id"] = pid

        # Find Task-type work packages with 'Raise coverage' in subject
        sql = (
            f"SELECT wp.id, wp.subject "
            f"FROM work_packages wp "
            f"JOIN types t ON t.id = wp.type_id "
            f"WHERE wp.project_id = {pid} "
            f"AND t.name = 'Task' "
            f"AND wp.subject LIKE 'Raise coverage:%' "
            f"ORDER BY wp.id;"
        )
        rows_out = _op_query(sql)
        rows = [line for line in rows_out.split("\n") if line.strip()] if rows_out else []
        _bstate["op_coverage_wp_ids"] = [r.split("|")[0] for r in rows]
        subjects = [r.split("|", 1)[1] if "|" in r else "" for r in rows]
        _bstate["op_coverage_wp_subjects"] = subjects

        if lowest is None:
            check("12. OpenProject 5 lowest-coverage Task WPs", 2, False,
                  "coverage truth recompute failed (see check 0) — no fallback to agent data")
            return

        bad = []
        if len(rows) != 5:
            bad.append(f"expected exactly 5 'Raise coverage:' Task WPs, got {len(rows)}")

        expected = {f"{p}/{m}": pct for (p, m), pct in lowest}
        seen: dict = {}
        for subj in subjects:
            m = SUBJECT_RE.match(subj)
            if not m:
                bad.append(f"subject format wrong: '{subj}'")
                continue
            key = f"{m.group(1)}/{m.group(2)}"
            if key not in expected:
                bad.append(f"unexpected module in subject: '{subj}'")
                continue
            if key in seen:
                bad.append(f"duplicate WP for module {key}")
                continue
            seen[key] = True
            if not _num_eq(float(m.group(3)), expected[key]):
                bad.append(f"{key}: subject pct={m.group(3)} truth={expected[key]}")
        missing = sorted(set(expected) - set(seen))
        if missing:
            bad.append(f"missing WPs for lowest-coverage modules: {missing}")

        ok = len(bad) == 0
        check("12. OpenProject 5 lowest-coverage Task WPs", 2, ok,
              "; ".join(bad[:3]) if bad else "subjects == 5 lowest-coverage modules from truth")
    except Exception as e:
        check("12. OpenProject 5 lowest-coverage Task WPs", 2, False, f"exception: {e}")


def check_13_op_assignee_priority() -> None:
    """Work packages have assignee == 'Bob Martinez' (exact) and priority=High."""
    try:
        wp_ids = _bstate.get("op_coverage_wp_ids", [])
        if not wp_ids:
            check("13. WP assignee + priority", 2, False, "no coverage WPs found")
            return

        ids_csv = ",".join(wp_ids)
        sql = (
            f"SELECT wp.id, "
            f"COALESCE((SELECT u.firstname || ' ' || u.lastname FROM users u WHERE u.id = wp.assigned_to_id), '') as assignee, "
            f"COALESCE((SELECT e.name FROM enumerations e WHERE e.id = wp.priority_id), '') as priority "
            f"FROM work_packages wp "
            f"WHERE wp.id IN ({ids_csv});"
        )
        rows_out = _op_query(sql)
        rows = [line for line in rows_out.split("\n") if line.strip()] if rows_out else []

        bad = []
        for row in rows:
            parts = row.split("|")
            if len(parts) < 3:
                bad.append(f"unexpected row format: {row}")
                continue
            wp_id, assignee, priority = parts[0], parts[1], parts[2]
            if assignee != QA_OWNER:
                bad.append(f"WP {wp_id}: assignee='{assignee}' expected exactly '{QA_OWNER}'")
            if priority != "High":
                bad.append(f"WP {wp_id}: priority='{priority}' expected 'High'")

        ok = len(bad) == 0 and len(rows) == len(wp_ids)
        check("13. WP assignee + priority", 2, ok,
              "; ".join(bad[:3]) if bad else f"all WPs: assignee=={QA_OWNER}, priority=High")
    except Exception as e:
        check("13. WP assignee + priority", 2, False, f"exception: {e}")


DESC_RE = re.compile(r"^Current: ([0-9]+(?:\.[0-9]+)?)%; Target: 70%; Audit: 2026-05-20$")


def check_14_op_description() -> None:
    """Per-WP description (paired by subject) whole-string equals
    'Current: <truth pct>%; Target: 70%; Audit: 2026-05-20' after HTML strip
    (pct numeric-equivalent vs truth)."""
    try:
        truth = _truth_pcts()
        if truth is None:
            check("14. WP description vs truth", 2, False,
                  "coverage truth recompute failed (see check 0) — no fallback to agent data")
            return
        wp_ids = _bstate.get("op_coverage_wp_ids", [])
        if not wp_ids:
            check("14. WP description vs truth", 2, False, "no coverage WPs found")
            return

        ids_csv = ",".join(wp_ids)
        # Flatten newlines inside the description so one WP == one psql row.
        sql = (
            f"SELECT id, subject, "
            f"replace(replace(COALESCE(description, ''), chr(13), ' '), chr(10), ' ') "
            f"FROM work_packages WHERE id IN ({ids_csv});"
        )
        rows_out = _op_query(sql)
        rows = [line for line in rows_out.split("\n") if line.strip()] if rows_out else []

        bad = []
        for row in rows:
            parts = row.split("|", 2)
            if len(parts) < 3:
                bad.append(f"unexpected row format: {row}")
                continue
            wp_id, subj, desc_raw = parts[0], parts[1], parts[2]

            sm = SUBJECT_RE.match(subj)
            if not sm:
                bad.append(f"WP {wp_id}: cannot pair subject to a module: '{subj}'")
                continue
            key = (sm.group(1), sm.group(2))
            if key not in truth:
                bad.append(f"WP {wp_id}: subject module {key} not in truth")
                continue
            truth_pct = truth[key]

            # Strip backslash escapes (CKEditor), HTML tags/entities, collapse whitespace.
            desc = desc_raw.replace("\\", "")
            desc = re.sub(r"<[^>]+>", " ", desc)
            desc = html.unescape(desc)
            desc = " ".join(desc.split())

            dm = DESC_RE.match(desc)
            if not dm:
                bad.append(f"WP {wp_id}: description != 'Current: <pct>%; Target: 70%; Audit: {AUDIT_DATE}': '{desc[:80]}'")
                continue
            if not _num_eq(float(dm.group(1)), truth_pct):
                bad.append(f"WP {wp_id}: Current={dm.group(1)} truth={truth_pct}")

        ok = len(bad) == 0 and len(rows) == len(wp_ids)
        check("14. WP description vs truth", 2, ok,
              "; ".join(bad[:3]) if bad else "all descriptions exact vs recomputed truth")
    except Exception as e:
        check("14. WP description vs truth", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_coverage_truth()
    check_1_baserow_db_exists()
    check_2_table1_fields()
    check_3_table1_row_count_and_ids()
    check_4_table1_project_module()
    check_5_table1_captured_at()
    check_6_table1_coverage_truth()
    check_7_table2_exists_and_rows()
    check_8_table2_avg_coverage()
    check_9_remediation_queue_view()
    check_10_report_file_exists()
    check_11_report_file_content()
    check_12_op_work_packages_exist()
    check_13_op_assignee_priority()
    check_14_op_description()

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
