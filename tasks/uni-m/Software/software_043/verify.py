"""
Verifier for Software-043-I4: Audit Prometheus alerts and reconcile SLOs across
code-server, Baserow, and OpenProject.

Checks: 15 weighted checks (23 points total) + 1 unweighted anti-tamper
precondition (check 0).
Strategy: docker exec (code-server), REST API (Baserow), psql via docker exec
(OpenProject).

Ground truth: the verifier re-parses the alerts yml from the code-server
container's own PRISTINE image — a throwaway `docker run` of the image the
live container was started from (yaml.safe_load + threshold regex on each
rule's expr) — and derives the expected {(service, alert): threshold} map and
the Mismatch set. All truth-dependent expectations come from this recompute —
agent-filled Baserow data is never used as an expectation source. Because the
truth read never touches the live container, the old residual risk (the
alerts yml is untracked, so an agent could edit it in the live container and
then fill the tables self-consistently) is now CLOSED: agent edits to the
live workspace cannot influence the truth. If the truth recompute fails
(file unreadable, parse failure, or tracked monitoring/ files tampered
with), every truth-dependent check FAILs; there is no fallback to agent data.

Weight rebalance vs. the previous revision (total stays 23): check 6
(SLO Target values, pure spec-constant transcription) dropped 2pt -> 1pt to
fund the new check 15 (view row decorations, 1pt).

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import sys
import re
import subprocess

import requests
import yaml

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_missing = []
for _var in [
    "CODE_SERVER_PORT", "CODE_SERVER_CONTAINER",
    "BASEROW_PORT", "BASEROW_CONTAINER",
    "OPENPROJECT_PORT", "OPENPROJECT_CONTAINER",
]:
    if not os.environ.get(_var):
        _missing.append(_var)
if _missing:
    print(f"FATAL: missing env vars: {', '.join(_missing)}", file=sys.stderr)
    sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# ── Constants from task spec ──────────────────────────────────────────────────
SERVICE_LIST = ["analytics-service", "media-service", "reporting-service"]  # sorted

SLO_TARGETS = {
    "analytics-service::EventIngestLagHigh": 10.000,
    "analytics-service::QueryLatencyP95": 1.500,
    "reporting-service::ReportRenderLatencyP99": 3.000,
    "reporting-service::ReportGenerationErrorRate": 0.004,
    "media-service::UploadLatencyP95": 0.900,
    "media-service::TranscodeFailureRateHigh": 0.020,
}

SEVERITY_MAP = {
    "EventIngestLagHigh": "Ticket",
    "QueryLatencyP95": "Info",
    "ReportRenderLatencyP99": "Ticket",
    "ReportGenerationErrorRate": "Page",
    "UploadLatencyP95": "Ticket",
    "TranscodeFailureRateHigh": "Page",
}

MISMATCH_TOLERANCE = 0.002
THRESHOLD_TOLERANCE = 0.0005
AUDIT_DATE = "2026-07-22"

DEVOPS_REPO = "/home/coder/workspace/devops-configs"
ALERTS_YML = (
    "/home/coder/workspace/devops-configs/monitoring/prometheus/"
    "analytics_reporting_media_alerts.yml"
)
ALERTS_YML_REL = "devops-configs/monitoring/prometheus/analytics_reporting_media_alerts.yml"
AUDIT_MD = "/home/coder/workspace/devops-configs/docs/alert-audit-2026-07-22.md"

# Expected order: Service alphabetical, then Alert Name alphabetical within service
EXPECTED_ORDER = [
    ("analytics-service", "EventIngestLagHigh"),
    ("analytics-service", "QueryLatencyP95"),
    ("media-service", "TranscodeFailureRateHigh"),
    ("media-service", "UploadLatencyP95"),
    ("reporting-service", "ReportGenerationErrorRate"),
    ("reporting-service", "ReportRenderLatencyP99"),
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


def baserow_auth() -> dict:
    """Get Baserow auth token and return headers."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    return {"Authorization": f"JWT {token}"}


def op_sql(query: str) -> str:
    """Run a SQL query against the OpenProject embedded Postgres DB."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "psql", "postgres://openproject:openproject@127.0.0.1/openproject",
        "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"op_sql failed: {err.strip()}")
    return out.strip()


# ── Ground truth (recomputed by the verifier, never taken from agent data) ────
# TRUTH is (thresholds, mismatch_set) or None when recompute failed/invalidated:
#   thresholds:   {(service, alert): float threshold from the yml expr}
#   mismatch_set: {(service, alert)} where abs(threshold - SLO) > tolerance
TRUTH: "tuple[dict, set] | None" = None
_truth_cache: object = "unset"


def parse_alert_truth() -> "tuple[dict, set] | None":
    """cat the alerts yml in a throwaway container from the code-server
    container's PRISTINE image -> yaml.safe_load -> derive ground truth.
    Reading from the pristine image (never the live container) means agent
    edits to the untracked yml cannot poison the truth.

    Per rule: take `alert`, `labels.service`, `expr`; extract the numeric
    threshold from the tail of the expression. Returns None when the file is
    unreadable or yields no rules (truth recompute failure -> dependent checks
    must FAIL; no fallback).
    """
    global _truth_cache
    if _truth_cache != "unset":
        return _truth_cache  # type: ignore[return-value]
    _truth_cache = None
    try:
        rc, out, err = image_exec("cat", ALERTS_YML, timeout=60)
        if rc != 0 or not out.strip():
            return None
        data = yaml.safe_load(out)
        thresholds: dict = {}
        for grp in (data or {}).get("groups") or []:
            for rule in grp.get("rules") or []:
                alert = rule.get("alert")
                svc = (rule.get("labels") or {}).get("service")
                expr = str(rule.get("expr") or "")
                m = re.search(r"[><]=?\s*([0-9.]+)\s*$", expr)
                if not alert or not svc or not m:
                    continue
                thresholds[(svc, alert)] = float(m.group(1))
        if not thresholds:
            return None
        mismatch = set()
        for key, thr in thresholds.items():
            slo = SLO_TARGETS.get(f"{key[0]}::{key[1]}")
            if slo is not None and abs(thr - slo) > MISMATCH_TOLERANCE:
                mismatch.add(key)
        _truth_cache = (thresholds, mismatch)
        return _truth_cache
    except Exception:
        return None


def _truth_counts(truth: tuple) -> tuple[int, int, int]:
    """(Total rules, Mismatched, Paging rules) derived from ground truth."""
    thresholds, mismatch = truth
    t = len(thresholds)
    m = len(mismatch)
    p = sum(1 for (_, alert) in thresholds
            if SEVERITY_MAP.get(alert, "Ticket") == "Page")
    return t, m, p


def _expected_subjects(truth: tuple) -> set:
    """Expected OpenProject WP subjects derived from the truth mismatch set."""
    return {f"Reconcile alert: {svc}/{alert}" for (svc, alert) in truth[1]}


TRUTH_FAIL_DETAIL = "yml ground-truth recompute failed/invalidated; no fallback to agent data"


# ── Check 0: anti-tamper precondition (0pt, gates the yml truth) ──────────────

def check_0_monitoring_untouched() -> bool:
    """Anti-tamper precondition (0pt): tracked monitoring/ files in the
    devops-configs git repo must be untouched; tampering with them still
    voids the yml truth and FAILs all truth-dependent checks. Note that the
    yml truth itself is now read from the PRISTINE image (parse_alert_truth),
    so live-container edits to the untracked alerts yml can no longer poison
    it — that residual risk is closed; this check remains as a belt-and-braces
    tamper gate for the tracked monitoring/ tree.

    Note: the alerts yml itself is untracked in the seed repo (pristine image
    shows `?? monitoring/prometheus/`), so untracked (`??`) entries under
    monitoring/ are expected and must be tolerated; only tracked-file
    modifications (git diff / non-?? porcelain status) count as tampering.
    """
    label = "0. Anti-tamper precondition: tracked monitoring/ files untouched"
    try:
        git_base = ["git", "-c", f"safe.directory={DEVOPS_REPO}", "-C", DEVOPS_REPO]
        rc1, out1, err1 = docker_exec(CODE_SERVER_CONTAINER, *git_base, "diff", "--name-only")
        rc2, out2, err2 = docker_exec(CODE_SERVER_CONTAINER, *git_base, "status", "--porcelain")
        if rc1 != 0 or rc2 != 0:
            check(label, 0, True,
                  f"git unavailable, tamper check skipped: {(err1 or err2).strip()[:120]}")
            return True

        touched = set()
        for line in out1.splitlines():
            path = line.strip()
            if path.startswith("monitoring/"):
                touched.add(path)
        for line in out2.splitlines():
            if len(line) <= 3:
                continue
            xy = line[:2]
            if xy == "??":
                continue  # untracked seed content (monitoring/prometheus/ is untracked)
            for part in line[3:].split(" -> "):
                path = part.strip().strip('"')
                if path.startswith("monitoring/"):
                    touched.add(path)

        if touched:
            check(label, 0, False,
                  f"tracked monitoring/ paths modified: {', '.join(sorted(touched))}")
            return False
        check(label, 0, True)
        return True
    except Exception as e:
        check(label, 0, True, f"tamper check skipped: {e}")
        return True


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_audit_file_exists() -> None:
    """Check that alert-audit-2026-07-22.md exists in code-server."""
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "test", "-f", AUDIT_MD)
        check("1. Audit markdown file exists in code-server", 1, rc == 0,
              "file not found" if rc != 0 else "")
    except Exception as e:
        check("1. Audit markdown file exists in code-server", 1, False, f"exception: {e}")


def check_2_audit_file_content() -> None:
    """Check that the audit file has exactly the 3 required lines, with the
    T/M/P counts gated against the yml ground truth."""
    label = "2. Audit file has correct 3-line content"
    try:
        rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", AUDIT_MD)
        if rc != 0:
            check(label, 2, False, "file not readable")
            return

        lines = [ln.rstrip() for ln in out.strip().split("\n")]
        issues = []

        if len(lines) != 3:
            issues.append(f"expected exactly 3 lines, got {len(lines)}")
        if len(lines) >= 3:
            # Line 1: heading
            if "Alert Rule Audit" not in lines[0] or AUDIT_DATE not in lines[0]:
                issues.append(f"line 1 mismatch: {lines[0]!r}")
            # Line 2: exact services line (sorted, comma-separated)
            expected_line2 = "Services scanned: " + ", ".join(SERVICE_LIST)
            if lines[1].strip() != expected_line2:
                issues.append(f"line 2 != {expected_line2!r}: {lines[1]!r}")
            # Line 3: counts, gated against recomputed truth
            m = re.search(
                r"Total rules:\s*(\d+);\s*Mismatched:\s*(\d+);\s*Paging rules:\s*(\d+)",
                lines[2],
            )
            if not m:
                issues.append(f"line 3 format mismatch: {lines[2]!r}")
            elif TRUTH is None:
                issues.append(TRUTH_FAIL_DETAIL)
            else:
                got = tuple(int(x) for x in m.groups())
                exp = _truth_counts(TRUTH)
                if got != exp:
                    issues.append(f"line 3 counts T/M/P={got}, expected {exp} (from yml truth)")

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Baserow checks ───────────────────────────────────────────────────────────

_baserow_headers = None
_baserow_table_id = None
_baserow_rows = None


def _init_baserow() -> bool:
    """Authenticate with Baserow. Returns True if auth succeeded."""
    global _baserow_headers
    if _baserow_headers is not None:
        return bool(_baserow_headers)
    try:
        _baserow_headers = baserow_auth()
        return True
    except Exception:
        _baserow_headers = {}
        return False


def _find_baserow_db_and_table() -> tuple[bool, str]:
    """Find 'Analytics Stack Alert Audit' database and 'Alert Rule Audit' table."""
    global _baserow_table_id
    try:
        # List all applications (databases)
        resp = requests.get(
            f"{BASEROW_URL}/api/applications/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        apps = resp.json()

        db = None
        for app in apps:
            if app.get("name") == "Analytics Stack Alert Audit":
                db = app
                break

        if not db:
            return False, "database 'Analytics Stack Alert Audit' not found"

        # Find table
        tables = db.get("tables", [])
        if not tables:
            # Fetch tables explicitly
            resp2 = requests.get(
                f"{BASEROW_URL}/api/database/tables/database/{db['id']}/",
                headers=_baserow_headers, timeout=15,
            )
            resp2.raise_for_status()
            tables = resp2.json()

        for t in tables:
            if t.get("name") == "Alert Rule Audit":
                _baserow_table_id = t["id"]
                return True, ""

        return False, "table 'Alert Rule Audit' not found in database"
    except Exception as e:
        return False, f"exception: {e}"


def _get_baserow_rows() -> list[dict]:
    """Fetch all rows from the Alert Rule Audit table."""
    global _baserow_rows
    if _baserow_rows is not None:
        return _baserow_rows
    if _baserow_table_id is None:
        return []
    try:
        resp = requests.get(
            f"{BASEROW_URL}/api/database/rows/table/{_baserow_table_id}/",
            headers=_baserow_headers,
            params={"size": 100},
            timeout=15,
        )
        resp.raise_for_status()
        _baserow_rows = resp.json().get("results", [])
        return _baserow_rows
    except Exception:
        _baserow_rows = []
        return []


def _get_baserow_fields() -> list[dict]:
    """Fetch field definitions for the table."""
    if _baserow_table_id is None:
        return []
    try:
        resp = requests.get(
            f"{BASEROW_URL}/api/database/fields/table/{_baserow_table_id}/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return []


def _field_name_map(fields: list[dict]) -> dict[str, dict]:
    """Map field name -> field metadata."""
    return {f["name"]: f for f in fields}


def _row_value(row: dict, field_name: str, fields: list[dict]) -> object:
    """Get a row's value by field name. Handles Baserow's field_<id> keys."""
    for f in fields:
        if f["name"] == field_name:
            key = f"field_{f['id']}"
            return row.get(key)
    return None


def _row_service_alert(row: dict, fields: list[dict]) -> tuple[str, str]:
    """Extract (service, alert_name) from a Baserow row."""
    svc_val = _row_value(row, "Service", fields)
    svc = svc_val.get("value", "") if isinstance(svc_val, dict) else str(svc_val or "")
    alert = _row_value(row, "Alert Name", fields) or ""
    return svc, str(alert)


def _find_view(name: str) -> "dict | None":
    """Find a view of the audit table by exact name."""
    if _baserow_table_id is None:
        return None
    resp = requests.get(
        f"{BASEROW_URL}/api/database/views/table/{_baserow_table_id}/",
        headers=_baserow_headers, timeout=15,
    )
    resp.raise_for_status()
    for v in resp.json():
        if v.get("name") == name:
            return v
    return None


def check_3_baserow_database_exists() -> None:
    """Check that Baserow database 'Analytics Stack Alert Audit' exists."""
    try:
        if not _init_baserow():
            check("3. Baserow database exists", 1, False, "auth failed")
            return

        resp = requests.get(
            f"{BASEROW_URL}/api/applications/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        apps = resp.json()
        db_names = [a.get("name") for a in apps]
        found = "Analytics Stack Alert Audit" in db_names
        check("3. Baserow database exists", 1, found,
              f"databases: {db_names}" if not found else "")
    except Exception as e:
        check("3. Baserow database exists", 1, False, f"exception: {e}")


def check_4_baserow_table_fields() -> None:
    """Check table has required fields with correct types, exact single-select
    option sets, and 3 decimal places on the number fields."""
    label = "4. Table 'Alert Rule Audit' with correct fields"
    try:
        if _baserow_table_id is None:
            check(label, 2, False, "table not found")
            return
        fields = _get_baserow_fields()
        field_map = _field_name_map(fields)

        required = {
            "Rule ID": "text",
            "Service": "single_select",
            "Alert Name": "text",
            "Threshold Value": "number",
            "SLO Target": "number",
            "Mismatch": "boolean",
            "Severity": "single_select",
            "Captured At": "date",
        }

        issues = []
        for fname, expected_type in required.items():
            if fname not in field_map:
                issues.append(f"missing field '{fname}'")
            else:
                actual_type = field_map[fname].get("type", "")
                if expected_type and actual_type != expected_type:
                    issues.append(f"'{fname}' type={actual_type}, expected {expected_type}")

        # Exact single-select option sets
        option_sets = {
            "Service": set(SERVICE_LIST),
            "Severity": {"Page", "Ticket", "Info"},
        }
        for fname, expected_opts in option_sets.items():
            f = field_map.get(fname)
            if f and f.get("type") == "single_select":
                opts = {o.get("value") for o in f.get("select_options") or []}
                if opts != expected_opts:
                    issues.append(
                        f"'{fname}' options {sorted(opts)}, expected exactly {sorted(expected_opts)}"
                    )

        # 3 decimal places on the number fields
        for fname in ("Threshold Value", "SLO Target"):
            f = field_map.get(fname)
            if f and f.get("type") == "number":
                dp = f.get("number_decimal_places")
                if dp != 3:
                    issues.append(f"'{fname}' number_decimal_places={dp}, expected 3")

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_baserow_row_count_and_ids() -> None:
    """Check 6 rows with Rule IDs AR-01..AR-06 in correct order."""
    try:
        rows = _get_baserow_rows()
        fields = _get_baserow_fields()
        if not rows:
            check("5. 6 rows with correct Rule IDs and order", 3, False, "no rows found")
            return

        issues = []
        if len(rows) != 6:
            issues.append(f"expected 6 rows, got {len(rows)}")

        # Check Rule IDs
        rule_ids = [_row_value(r, "Rule ID", fields) for r in rows]
        expected_ids = [f"AR-{i:02d}" for i in range(1, 7)]
        if rule_ids != expected_ids:
            issues.append(f"Rule IDs: {rule_ids}, expected {expected_ids}")

        # Check ordering: service alpha then alert name alpha
        for i, r in enumerate(rows):
            svc, alert = _row_service_alert(r, fields)
            if i < len(EXPECTED_ORDER):
                exp_svc, exp_alert = EXPECTED_ORDER[i]
                if svc != exp_svc or alert != exp_alert:
                    issues.append(f"row {i+1}: got ({svc}, {alert}), expected ({exp_svc}, {exp_alert})")

        check("5. 6 rows with correct Rule IDs and order", 3, len(issues) == 0,
              "; ".join(issues))
    except Exception as e:
        check("5. 6 rows with correct Rule IDs and order", 3, False, f"exception: {e}")


def check_6_slo_targets() -> None:
    """Check SLO Target values match the spec constants.

    (Weight 2 -> 1 to fund the new check 15; pure spec-constant transcription.)
    """
    try:
        rows = _get_baserow_rows()
        fields = _get_baserow_fields()
        if not rows:
            check("6. SLO Target values correct", 1, False, "no rows")
            return

        issues = []
        for r in rows:
            svc, alert = _row_service_alert(r, fields)
            slo_actual = _row_value(r, "SLO Target", fields)
            key = f"{svc}::{alert}"
            expected_slo = SLO_TARGETS.get(key)
            if expected_slo is None:
                issues.append(f"unknown key {key}")
                continue
            try:
                slo_num = float(slo_actual) if slo_actual is not None else None
            except (TypeError, ValueError):
                slo_num = None
            if slo_num is None or abs(slo_num - expected_slo) > THRESHOLD_TOLERANCE:
                issues.append(f"{key}: SLO={slo_actual}, expected {expected_slo}")

        check("6. SLO Target values correct", 1, len(issues) == 0,
              "; ".join(issues))
    except Exception as e:
        check("6. SLO Target values correct", 1, False, f"exception: {e}")


def check_7_threshold_and_mismatch_vs_truth() -> None:
    """Core check: per-row Threshold Value == yml ground truth (tolerance
    0.0005) AND Mismatch == truth mismatch set. Expectations come exclusively
    from parse_alert_truth(); agent-consistent fabrications no longer pass."""
    label = "7. Threshold Value and Mismatch match yml ground truth"
    try:
        if TRUTH is None:
            check(label, 2, False, TRUTH_FAIL_DETAIL)
            return
        thresholds, mismatch_set = TRUTH

        rows = _get_baserow_rows()
        fields = _get_baserow_fields()
        if not rows:
            check(label, 2, False, "no rows")
            return

        actual: dict = {}
        for r in rows:
            svc, alert = _row_service_alert(r, fields)
            actual[(svc, alert)] = (
                _row_value(r, "Threshold Value", fields),
                bool(_row_value(r, "Mismatch", fields)),
            )

        issues = []
        for key in sorted(actual):
            if key not in thresholds:
                issues.append(f"{key[0]}/{key[1]}: row not in yml truth")
        for key in sorted(thresholds):
            exp_thr = thresholds[key]
            if key not in actual:
                issues.append(f"{key[0]}/{key[1]}: row missing")
                continue
            thr_raw, actual_mismatch = actual[key]
            try:
                thr = float(thr_raw)
            except (TypeError, ValueError):
                issues.append(f"{key[0]}/{key[1]}: non-numeric Threshold {thr_raw!r}")
                continue
            if abs(thr - exp_thr) > THRESHOLD_TOLERANCE:
                issues.append(f"{key[0]}/{key[1]}: Threshold={thr}, yml truth={exp_thr}")
            expected_mismatch = key in mismatch_set
            if actual_mismatch != expected_mismatch:
                issues.append(
                    f"{key[0]}/{key[1]}: Mismatch={actual_mismatch}, "
                    f"truth={expected_mismatch} (thr={exp_thr}, slo={SLO_TARGETS.get(f'{key[0]}::{key[1]}')})"
                )

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_severity_values() -> None:
    """Check Severity per the yml-truth alert set: all truth alerts must be
    present as rows and each row's Severity must match SEVERITY_MAP."""
    label = "8. Severity values correct for all yml-truth alerts"
    try:
        if TRUTH is None:
            check(label, 1, False, TRUTH_FAIL_DETAIL)
            return
        thresholds, _ = TRUTH

        rows = _get_baserow_rows()
        fields = _get_baserow_fields()
        if not rows:
            check(label, 1, False, "no rows")
            return

        actual: dict = {}
        for r in rows:
            svc, alert = _row_service_alert(r, fields)
            sev_val = _row_value(r, "Severity", fields)
            sev = sev_val.get("value", "") if isinstance(sev_val, dict) else str(sev_val or "")
            actual[(svc, alert)] = sev

        issues = []
        for key in sorted(thresholds):
            expected = SEVERITY_MAP.get(key[1], "Ticket")
            if key not in actual:
                issues.append(f"{key[0]}/{key[1]}: row missing")
            elif actual[key] != expected:
                issues.append(f"{key[1]}: severity={actual[key]!r}, expected {expected!r}")

        check(label, 1, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_9_captured_at() -> None:
    """Check Captured At = 2026-07-22 for all rows."""
    try:
        rows = _get_baserow_rows()
        fields = _get_baserow_fields()
        if not rows:
            check("9. Captured At dates correct", 1, False, "no rows")
            return

        issues = []
        for r in rows:
            rid = _row_value(r, "Rule ID", fields) or "?"
            cap = _row_value(r, "Captured At", fields)
            cap_str = str(cap or "")
            if not cap_str.startswith(AUDIT_DATE):
                issues.append(f"{rid}: date={cap_str!r}, expected {AUDIT_DATE}")

        check("9. Captured At dates correct", 1, len(issues) == 0,
              "; ".join(issues))
    except Exception as e:
        check("9. Captured At dates correct", 1, False, f"exception: {e}")


def check_10_mismatched_rules_view() -> None:
    """Check 'Mismatched Rules' Grid view: filter on Mismatch with boolean/equal
    type and truthy value, plus exactly 2 sortings in order: Service ASC then
    Alert Name ASC."""
    label = "10. 'Mismatched Rules' view with filter and sortings"
    try:
        if _baserow_table_id is None:
            check(label, 2, False, "table not found")
            return

        target_view = _find_view("Mismatched Rules")
        if not target_view:
            check(label, 2, False, "view 'Mismatched Rules' not found")
            return

        issues = []
        if target_view.get("type") != "grid":
            issues.append(f"type={target_view.get('type')}, expected grid")

        view_id = target_view["id"]
        fields = _get_baserow_fields()
        field_map = _field_name_map(fields)
        mismatch_field = field_map.get("Mismatch")
        service_field = field_map.get("Service")
        alert_field = field_map.get("Alert Name")

        # Filter: on Mismatch field, boolean/equal type, truthy value
        try:
            filter_resp = requests.get(
                f"{BASEROW_URL}/api/database/views/{view_id}/filters/",
                headers=_baserow_headers, timeout=15,
            )
            filter_resp.raise_for_status()
            filters = filter_resp.json()

            mismatch_filters = [
                flt for flt in filters
                if mismatch_field and flt.get("field") == mismatch_field["id"]
            ]
            if not mismatch_filters:
                issues.append("no filter on Mismatch field found")
            else:
                good = False
                for flt in mismatch_filters:
                    ftype = flt.get("type")
                    fval = str(flt.get("value", "")).strip().lower()
                    if ftype in ("boolean", "equal") and fval in ("1", "true", "t", "yes", "on"):
                        good = True
                        break
                if not good:
                    issues.append(
                        "Mismatch filter present but not boolean/equal with truthy value: "
                        + str([(f.get('type'), f.get('value')) for f in mismatch_filters])
                    )
        except Exception as e:
            issues.append(f"could not check filters: {e}")

        # Sortings: exactly 2, in order Service ASC then Alert Name ASC
        try:
            sort_resp = requests.get(
                f"{BASEROW_URL}/api/database/views/{view_id}/sortings/",
                headers=_baserow_headers, timeout=15,
            )
            sort_resp.raise_for_status()
            sortings = sort_resp.json()

            if not service_field or not alert_field:
                issues.append("Service/Alert Name fields not found for sorting check")
            elif len(sortings) != 2:
                issues.append(f"expected exactly 2 sortings, got {len(sortings)}")
            else:
                got = [(s.get("field"), str(s.get("order", "")).upper()) for s in sortings]
                expected = [(service_field["id"], "ASC"), (alert_field["id"], "ASC")]
                if got != expected:
                    issues.append(
                        f"sortings {got}, expected Service ASC then Alert Name ASC {expected}"
                    )
        except Exception as e:
            issues.append(f"could not check sortings: {e}")

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15_view_decorations() -> None:
    """Check the 'Mismatched Rules' view has a row decoration (left border or
    background color) whose value provider is tied to Severity=Page."""
    label = "15. 'Mismatched Rules' view has Severity=Page row decoration"
    try:
        if _baserow_table_id is None:
            check(label, 1, False, "table not found")
            return
        target_view = _find_view("Mismatched Rules")
        if not target_view:
            check(label, 1, False, "view 'Mismatched Rules' not found")
            return

        fields = _get_baserow_fields()
        sev_field = _field_name_map(fields).get("Severity")
        if not sev_field:
            check(label, 1, False, "Severity field not found")
            return
        sev_id = sev_field["id"]
        page_opt_ids = {
            str(o.get("id")) for o in sev_field.get("select_options") or []
            if o.get("value") == "Page"
        }

        resp = requests.get(
            f"{BASEROW_URL}/api/database/views/{target_view['id']}/decorations/",
            headers=_baserow_headers, timeout=15,
        )
        resp.raise_for_status()
        decorations = resp.json()

        found = False
        for d in decorations:
            if d.get("type") not in ("left_border_color", "background_color"):
                continue
            vpt = d.get("value_provider_type")
            conf = d.get("value_provider_conf") or {}
            # Option A: color by the Severity single-select field itself
            if vpt == "single_select_color" and conf.get("field_id") == sev_id:
                found = True
                break
            # Option B: conditional color with a filter on Severity == Page
            if vpt == "conditional_color":
                for color in conf.get("colors") or []:
                    for flt in color.get("filters") or []:
                        if flt.get("field") == sev_id and (
                            str(flt.get("value")) in page_opt_ids
                            or str(flt.get("value")).strip().lower() == "page"
                        ):
                            found = True
        check(label, 1, found,
              "" if found else
              f"no left_border/background decoration tied to Severity=Page ({len(decorations)} decorations present)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── OpenProject checks (via docker exec DB) ──────────────────────────────────

def check_11_immediate_priority() -> None:
    """Check 'Immediate' priority enumeration exists in OpenProject."""
    try:
        out = op_sql("SELECT name FROM enumerations WHERE type='IssuePriority' ORDER BY position;")
        names = [n.strip() for n in out.split("\n") if n.strip()]
        found = "Immediate" in names
        check("11. 'Immediate' priority exists in OpenProject", 0, found,
              f"priorities: {names}" if not found else "")
    except Exception as e:
        check("11. 'Immediate' priority exists in OpenProject", 0, False, f"exception: {e}")


def check_12_op_work_packages_exist() -> None:
    """Check exactly one Bug WP per truth-derived mismatch subject: subject set
    equality, one WP per subject, all Bug-typed, and no extra
    'Reconcile alert:%' WPs."""
    label = "12. Bug work packages match yml-truth mismatch set"
    try:
        if TRUTH is None:
            check(label, 2, False, TRUTH_FAIL_DETAIL)
            return
        expected = _expected_subjects(TRUTH)

        out = op_sql(
            "SELECT wp.subject, string_agg(DISTINCT t.name, ','), COUNT(*) "
            "FROM work_packages wp "
            "JOIN types t ON wp.type_id = t.id "
            "JOIN projects p ON wp.project_id = p.id "
            "WHERE p.identifier = 'api-gateway' "
            "AND wp.subject LIKE 'Reconcile alert:%' "
            "GROUP BY wp.subject ORDER BY wp.subject;"
        )
        wp_data: dict = {}
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                try:
                    cnt = int(parts[2].strip())
                except ValueError:
                    cnt = 0
                wp_data[parts[0].strip()] = (parts[1].strip(), cnt)

        issues = []
        actual_subjects = set(wp_data)
        for subj in sorted(expected - actual_subjects):
            issues.append(f"missing WP: {subj!r}")
        for subj in sorted(actual_subjects - expected):
            issues.append(f"unexpected WP: {subj!r}")

        total = 0
        for subj, (type_names, cnt) in sorted(wp_data.items()):
            total += cnt
            if subj in expected:
                if cnt != 1:
                    issues.append(f"'{subj}': {cnt} WPs, expected exactly 1")
                if type_names != "Bug":
                    issues.append(f"'{subj}': type={type_names}, expected Bug")
        if total != len(expected):
            issues.append(
                f"total 'Reconcile alert:%' WPs = {total}, expected {len(expected)}"
            )

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_13_op_assignee_priority() -> None:
    """Check WPs (truth-derived subject set) have assignee user12 (Karen Brown)
    and priority Immediate when truth Severity=Page else High."""
    label = "13. WP assignee and priority correct"
    try:
        if TRUTH is None:
            check(label, 2, False, TRUTH_FAIL_DETAIL)
            return

        # Expected: subject -> priority, derived from the yml truth mismatch set
        expected = {}
        for svc, alert in TRUTH[1]:
            subj = f"Reconcile alert: {svc}/{alert}"
            sev = SEVERITY_MAP.get(alert, "Ticket")
            expected[subj] = "Immediate" if sev == "Page" else "High"

        out = op_sql(
            "SELECT wp.subject, e.name AS priority, u.login AS assignee "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "LEFT JOIN enumerations e ON wp.priority_id = e.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "WHERE p.identifier = 'api-gateway' "
            "AND wp.subject LIKE 'Reconcile alert:%' "
            "ORDER BY wp.subject;"
        )

        wp_map = {}
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) >= 3:
                wp_map[parts[0].strip()] = {
                    "priority": parts[1].strip(),
                    "assignee": parts[2].strip(),
                }

        issues = []
        for subj, exp_priority in sorted(expected.items()):
            if subj not in wp_map:
                issues.append(f"missing WP: {subj!r}")
                continue
            actual = wp_map[subj]
            if actual["priority"] != exp_priority:
                issues.append(f"'{subj}': priority={actual['priority']!r}, expected {exp_priority!r}")
            if actual["assignee"] != "user12":  # Karen Brown, api-gateway member
                issues.append(f"'{subj}': assignee={actual['assignee']!r}, expected 'user12' (Karen Brown)")

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_op_description() -> None:
    """Check WP descriptions equal the exact truth-derived string:
    'Current threshold: <thr>; SLO target: <slo>; File: <yml path>; Audit: <date>'
    (backslash-stripped + whitespace-normalized whole-string comparison)."""
    label = "14. WP descriptions exactly match truth-derived format"
    try:
        if TRUTH is None:
            check(label, 2, False, TRUTH_FAIL_DETAIL)
            return
        thresholds, mismatch_set = TRUTH

        expected_descs = {}
        for svc, alert in mismatch_set:
            subj = f"Reconcile alert: {svc}/{alert}"
            thr = thresholds[(svc, alert)]
            slo = SLO_TARGETS[f"{svc}::{alert}"]
            expected_descs[subj] = " ".join(
                f"Current threshold: {thr:.3f}; SLO target: {slo:.3f}; "
                f"File: {ALERTS_YML_REL}; Audit: {AUDIT_DATE}".split()
            )

        out = op_sql(
            "SELECT wp.subject, "
            "regexp_replace(coalesce(wp.description, ''), E'[\\n\\r\\t]+', ' ', 'g') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "WHERE p.identifier = 'api-gateway' "
            "AND wp.subject LIKE 'Reconcile alert:%' "
            "ORDER BY wp.subject;"
        )

        issues = []
        seen = set()
        for line in out.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) < 2:
                continue
            subj = parts[0].strip()
            desc = parts[1]

            if subj not in expected_descs:
                continue
            seen.add(subj)

            # OpenProject's CKEditor escapes characters (e.g. '\_' in the yml
            # path) when storing text; strip backslashes and normalize
            # whitespace, then require exact whole-string equality.
            desc_clean = " ".join(desc.replace("\\", "").split())
            if desc_clean != expected_descs[subj]:
                issues.append(
                    f"'{subj}': description {desc_clean[:140]!r} != expected "
                    f"{expected_descs[subj][:140]!r}"
                )

        for subj in sorted(set(expected_descs) - seen):
            issues.append(f"missing WP: {subj!r}")

        check(label, 2, len(issues) == 0, "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    global TRUTH

    # Anti-tamper precondition, then recompute ground truth from the yml.
    tamper_ok = check_0_monitoring_untouched()
    TRUTH = parse_alert_truth() if tamper_ok else None

    # code-server checks
    check_1_audit_file_exists()
    check_2_audit_file_content()

    # Baserow checks (init once, reuse)
    _init_baserow()
    _find_baserow_db_and_table()
    _get_baserow_rows()

    check_3_baserow_database_exists()
    check_4_baserow_table_fields()
    check_5_baserow_row_count_and_ids()
    check_6_slo_targets()
    check_7_threshold_and_mismatch_vs_truth()
    check_8_severity_values()
    check_9_captured_at()
    check_10_mismatched_rules_view()

    # OpenProject checks
    check_11_immediate_priority()
    check_12_op_work_packages_exist()
    check_13_op_assignee_priority()
    check_14_op_description()

    # Baserow view decorations (new, funded by ck6 2pt -> 1pt)
    check_15_view_decorations()

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
