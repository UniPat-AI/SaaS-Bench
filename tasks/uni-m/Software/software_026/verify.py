"""
Verifier for Software-026-I1: Q4-2024 Engineering Investment Portfolio Review

Checks: 14 weighted checks (total weight 28) across openproject, baserow, code-server.
Strategy: OpenProject embedded Postgres, Baserow REST API, code-server docker exec.

Required env vars:
  SERVER_HOSTNAME, OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER
"""

import os
import re
import sys
import subprocess
import json
import unicodedata
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OP_PORT = os.environ.get("OPENPROJECT_PORT")
OP_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

BR_PORT = os.environ.get("BASEROW_PORT")
BR_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BR_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")

CS_PORT = os.environ.get("CODE_SERVER_PORT")
CS_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")

_required = {
    "OPENPROJECT_PORT": OP_PORT, "OPENPROJECT_CONTAINER": OP_CONTAINER,
    "BASEROW_PORT": BR_PORT, "BASEROW_CONTAINER": BR_CONTAINER,
    "BASEROW_DB_CONTAINER": BR_DB_CONTAINER, "CODE_SERVER_CONTAINER": CS_CONTAINER,
}
_missing = [k for k, v in _required.items() if not v]
if _missing:
    print(f"FATAL: {', '.join(_missing)} not set", file=sys.stderr)
    sys.exit(1)

# ── Classification constants ──────────────────────────────────────────────────
SECURITY_KW = ['security', 'auth', 'vulnerability', 'sso', 'saml', 'encrypt', 'secure']
RELIABILITY_KW = ['reliability', 'sla', 'alert', 'monitor', 'latency', 'timeout',
                  '502', 'error', 'uptime', 'availability']
TECHDEBT_KW = ['refactor', 'cleanup', 'migrate', 'upgrade', 'debt', 'legacy', 'tuning']

ASSIGNEE_TEAM = {
    'David Kim': 'Platform', 'Frank Nguyen': 'Product', 'Grace Patel': 'Product',
    'Henry Johnson': 'Data', 'James Lee': 'Platform', 'Liam Robinson': 'Product',
    'Mia Anderson': 'Platform', 'Paul Harris': 'Platform', 'Samuel Clark': 'Security',
    'OpenProject Admin': 'Platform',
}
TARGET_PCT = {'NewFeature': 50.0, 'TechDebt': 25.0, 'Reliability': 15.0, 'Security': 10.0}
BUCKET_ORDER = ['NewFeature', 'TechDebt', 'Reliability', 'Security']

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


def op_sql(sql: str) -> str:
    """Query OpenProject embedded Postgres."""
    rc, out, err = docker_exec(
        OP_CONTAINER, "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
        "-t", "-A", "-c", sql, timeout=15,
    )
    return out.strip()


def classify_bucket(subject: str, wp_type: str) -> str:
    subj_lower = subject.lower()
    if any(kw in subj_lower for kw in SECURITY_KW):
        return 'Security'
    if any(kw in subj_lower for kw in RELIABILITY_KW):
        return 'Reliability'
    if wp_type == 'Bug' or any(kw in subj_lower for kw in TECHDEBT_KW):
        return 'TechDebt'
    return 'NewFeature'


def get_assignee_team(assignee: str) -> str:
    return ASSIGNEE_TEAM.get(assignee, 'Platform')


# ── Ground truth from OpenProject ─────────────────────────────────────────────
_gt = None
_gt_err = None


def compute_ground_truth():
    """Query OpenProject for closed WPs and compute expected Baserow data."""
    global _gt, _gt_err
    if _gt is not None or _gt_err is not None:
        return

    project_id = op_sql("SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1")
    if not project_id:
        _gt_err = "Project 'API Gateway' not found"
        return

    closed_id = op_sql("SELECT id FROM statuses WHERE name = 'Closed' LIMIT 1")
    if not closed_id:
        _gt_err = "Status 'Closed' not found"
        return

    rows = op_sql(f"""
        SELECT wp.id, wp.subject, t.name,
               COALESCE(u.firstname || ' ' || u.lastname, 'Unassigned'),
               wp.updated_at::date
        FROM work_packages wp
        JOIN types t ON t.id = wp.type_id
        LEFT JOIN users u ON u.id = wp.assigned_to_id
        WHERE wp.project_id = {project_id}
          AND wp.status_id = {closed_id}
          AND wp.updated_at >= '2024-10-01'
          AND wp.updated_at < '2025-01-01'
        ORDER BY wp.id ASC
        LIMIT 30
    """)

    if not rows:
        _gt_err = "No closed WPs found in date range"
        return

    wps = []
    for line in rows.split('\n'):
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        if len(parts) < 5:
            continue
        wp_id, subject, wp_type, assignee, closed_date = parts[0], parts[1], parts[2], parts[3], parts[4]
        bucket = classify_bucket(subject, wp_type)
        team = get_assignee_team(assignee)
        wps.append({
            'wp_id': int(wp_id), 'subject': subject, 'type': wp_type,
            'assignee': assignee, 'bucket': bucket, 'team': team,
            'closed_date': closed_date,
        })

    total = len(wps)
    bucket_counts = {b: 0 for b in BUCKET_ORDER}
    for wp in wps:
        bucket_counts[wp['bucket']] += 1

    bucket_totals = []
    for b in BUCKET_ORDER:
        count = bucket_counts[b]
        share = round(count / total * 100, 1) if total else 0.0
        target = TARGET_PCT[b]
        gap = round(target - share, 1)
        bucket_totals.append({
            'bucket': b, 'count': count,
            'share_pct': share, 'target_pct': target, 'gap_pct': gap,
        })

    _gt = {'wps': wps, 'total': total, 'bucket_totals': bucket_totals}


# ── Baserow API helpers ───────────────────────────────────────────────────────
_br_token = None


def baserow_token() -> str:
    global _br_token
    if _br_token:
        return _br_token
    r = requests.post(
        f"http://{HOST}:{BR_PORT}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"}, timeout=10,
    )
    r.raise_for_status()
    _br_token = r.json()["access_token"]
    return _br_token


def br_get(path: str) -> dict | list:
    r = requests.get(
        f"http://{HOST}:{BR_PORT}/api/{path}",
        headers={"Authorization": f"JWT {baserow_token()}"}, timeout=15,
    )
    r.raise_for_status()
    return r.json()


def find_baserow_db(name: str) -> int | None:
    """Find a Baserow database application by name, return its ID."""
    apps = br_get("applications/")
    for a in apps:
        if a.get("name") == name and a.get("type") == "database":
            return a["id"]
    return None


def find_baserow_table(db_id: int, table_name: str) -> int | None:
    """Find a table by name in a Baserow database."""
    tables = br_get(f"database/tables/database/{db_id}/")
    for t in tables:
        if t.get("name") == table_name:
            return t["id"]
    return None


def get_baserow_rows(table_id: int) -> list[dict]:
    """Get all rows from a Baserow table using human-readable field names."""
    rows = []
    url = f"database/rows/table/{table_id}/?user_field_names=true&size=200"
    data = br_get(url)
    rows.extend(data.get("results", []))
    while data.get("next"):
        # Parse next URL path
        next_url = data["next"]
        # Extract path after /api/
        path = next_url.split("/api/", 1)[-1]
        data = br_get(path)
        rows.extend(data.get("results", []))
    return rows


def select_value(val) -> str:
    """Extract value from a Baserow single-select field."""
    if isinstance(val, dict):
        return val.get("value", "")
    return str(val or "")


# ── Cached Baserow state ─────────────────────────────────────────────────────
_br_db_id = None
_br_cwp_tid = None   # Closed Work Packages table id
_br_bt_tid = None    # Bucket Totals table id
_br_cwp_rows = None  # Closed Work Packages rows
_br_bt_rows = None   # Bucket Totals rows


def load_baserow_state():
    """Load Baserow database, tables and rows into cache."""
    global _br_db_id, _br_cwp_tid, _br_bt_tid, _br_cwp_rows, _br_bt_rows
    if _br_db_id is not None:
        return

    db_id = find_baserow_db("Portfolio Review Q4-2024")
    if db_id is None:
        _br_db_id = -1
        return
    _br_db_id = db_id

    _br_cwp_tid = find_baserow_table(db_id, "Closed Work Packages")
    if _br_cwp_tid:
        _br_cwp_rows = get_baserow_rows(_br_cwp_tid)

    _br_bt_tid = find_baserow_table(db_id, "Bucket Totals")
    if _br_bt_tid:
        _br_bt_rows = get_baserow_rows(_br_bt_tid)


def _cwp_rows_by_wp_id() -> dict[int, dict]:
    """Index Closed Work Packages rows by integer WP ID (rows with an empty or
    non-numeric WP ID are simply not indexable — the GT-driven checks then
    report the corresponding GT WP as missing)."""
    by_id: dict[int, dict] = {}
    for row in _br_cwp_rows or []:
        try:
            wp_id = int(float(row.get("WP ID")))
        except (TypeError, ValueError):
            continue
        by_id.setdefault(wp_id, row)
    return by_id


# ── Individual checks ─────────────────────────────────────────────────────────
def check_1_baserow_db_exists() -> None:
    """Baserow database 'Portfolio Review Q4-2024' exists."""
    try:
        load_baserow_state()
        found = _br_db_id is not None and _br_db_id > 0
        check("1. Baserow DB exists", 1, found,
              f"db_id={_br_db_id}" if found else "not found")
    except Exception as e:
        check("1. Baserow DB exists", 1, False, f"exception: {e}")


def check_2_cwp_table_rows() -> None:
    """'Closed Work Packages' table has exactly the GT rows: row count AND
    WP ID set equality (rows with empty/non-numeric WP IDs count as extra)."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err:
            check("2. CWP table rows", 2, False, f"ground truth error: {_gt_err}")
            return
        if _br_cwp_rows is None:
            check("2. CWP table rows", 2, False, "table not found")
            return

        expected_ids = {wp['wp_id'] for wp in _gt['wps']}
        actual_ids: list[int] = []
        bad_id_rows = 0
        for row in _br_cwp_rows:
            try:
                actual_ids.append(int(float(row.get("WP ID"))))
            except (TypeError, ValueError):
                bad_id_rows += 1

        missing = sorted(expected_ids - set(actual_ids))
        extra = sorted(set(actual_ids) - expected_ids)
        count_ok = len(_br_cwp_rows) == len(_gt['wps'])
        ok = count_ok and not missing and not extra and bad_id_rows == 0

        detail = f"gt_rows={len(_gt['wps'])}, table_rows={len(_br_cwp_rows)}"
        if missing:
            detail += f", missing WP IDs: {missing}"
        if extra:
            detail += f", extra WP IDs: {extra}"
        if bad_id_rows:
            detail += f", {bad_id_rows} row(s) with empty/invalid WP ID"
        check("2. CWP table rows", 2, ok, detail)
    except Exception as e:
        check("2. CWP table rows", 2, False, f"exception: {e}")


def check_3_cwp_bucket_classification() -> None:
    """Every GT WP has a row with the correct Investment Bucket (GT-driven:
    a missing row or an empty bucket is wrong, no short-circuits)."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err or _br_cwp_rows is None:
            check("3. CWP bucket classification", 2, False,
                  _gt_err or "table not found")
            return

        by_id = _cwp_rows_by_wp_id()
        wrong = []
        for wp in _gt['wps']:
            row = by_id.get(wp['wp_id'])
            if row is None:
                wrong.append(f"WP#{wp['wp_id']}: row missing")
                continue
            actual_bucket = select_value(row.get("Investment Bucket", ""))
            if actual_bucket != wp['bucket']:
                wrong.append(f"WP#{wp['wp_id']}: expected={wp['bucket']}, "
                             f"got={actual_bucket or '<empty>'}")

        ok = len(wrong) == 0 and len(_gt['wps']) > 0
        check("3. CWP bucket classification", 2, ok,
              f"{len(wrong)} wrong of {len(_gt['wps'])}" if wrong
              else f"all {len(_gt['wps'])} correct")
        if wrong:
            for w in wrong[:5]:
                print(f"  detail: {w}", file=sys.stderr)
    except Exception as e:
        check("3. CWP bucket classification", 2, False, f"exception: {e}")


def check_4_cwp_team_assignment() -> None:
    """Every GT WP has a row with the correct Team (GT-driven: a missing row
    or an empty team is wrong, no short-circuits)."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err or _br_cwp_rows is None:
            check("4. CWP team assignment", 2, False,
                  _gt_err or "table not found")
            return

        by_id = _cwp_rows_by_wp_id()
        wrong = []
        for wp in _gt['wps']:
            row = by_id.get(wp['wp_id'])
            if row is None:
                wrong.append(f"WP#{wp['wp_id']}: row missing")
                continue
            actual_team = select_value(row.get("Team", ""))
            if actual_team != wp['team']:
                wrong.append(f"WP#{wp['wp_id']}: expected={wp['team']}, "
                             f"got={actual_team or '<empty>'}")

        ok = len(wrong) == 0 and len(_gt['wps']) > 0
        check("4. CWP team assignment", 2, ok,
              f"{len(wrong)} wrong of {len(_gt['wps'])}" if wrong
              else f"all {len(_gt['wps'])} correct")
    except Exception as e:
        check("4. CWP team assignment", 2, False, f"exception: {e}")


def check_4b_cwp_row_fields() -> None:
    """Per GT WP: Subject, Type, Assignee and Closed Date match the GT values
    recomputed from OpenProject (Closed Date compared as ::date)."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err or _br_cwp_rows is None:
            check("4b. CWP row field equality", 3, False,
                  _gt_err or "table not found")
            return

        by_id = _cwp_rows_by_wp_id()
        wrong = []
        for wp in _gt['wps']:
            row = by_id.get(wp['wp_id'])
            if row is None:
                wrong.append(f"WP#{wp['wp_id']}: row missing")
                continue
            subject = str(row.get("Subject") or "")
            if subject != wp['subject']:
                wrong.append(f"WP#{wp['wp_id']}: Subject={subject!r}, expected={wp['subject']!r}")
            wp_type = select_value(row.get("Type", ""))
            if wp_type != wp['type']:
                wrong.append(f"WP#{wp['wp_id']}: Type={wp_type!r}, expected={wp['type']!r}")
            assignee = str(row.get("Assignee") or "")
            if assignee != wp['assignee']:
                wrong.append(f"WP#{wp['wp_id']}: Assignee={assignee!r}, expected={wp['assignee']!r}")
            closed = str(row.get("Closed Date") or "")[:10]
            if closed != wp['closed_date']:
                wrong.append(f"WP#{wp['wp_id']}: Closed Date={closed!r}, expected={wp['closed_date']!r}")

        ok = len(wrong) == 0 and len(_gt['wps']) > 0
        check("4b. CWP row field equality", 3, ok,
              f"all {len(_gt['wps'])} rows match" if ok else "; ".join(wrong[:4]))
    except Exception as e:
        check("4b. CWP row field equality", 3, False, f"exception: {e}")


def check_5_bucket_totals_exists() -> None:
    """'Bucket Totals' table exists with 4 rows in correct order."""
    try:
        load_baserow_state()
        if _br_bt_rows is None:
            check("5. Bucket Totals table", 1, False, "table not found")
            return
        count = len(_br_bt_rows)
        if count != 4:
            check("5. Bucket Totals table", 1, False, f"expected 4 rows, got {count}")
            return
        actual_order = [select_value(r.get("Bucket", "")) for r in _br_bt_rows]
        ok = actual_order == BUCKET_ORDER
        check("5. Bucket Totals table", 1, ok,
              f"order={actual_order}")
    except Exception as e:
        check("5. Bucket Totals table", 1, False, f"exception: {e}")


def check_6_bucket_totals_count_share() -> None:
    """Bucket Totals Count and Share Pct match ground truth."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err or _br_bt_rows is None:
            check("6. Bucket Count & Share Pct", 3, False,
                  _gt_err or "table not found")
            return

        expected_by_bucket = {bt['bucket']: bt for bt in _gt['bucket_totals']}
        wrong = []
        for row in _br_bt_rows:
            bucket = select_value(row.get("Bucket", ""))
            exp = expected_by_bucket.get(bucket)
            if not exp:
                wrong.append(f"{bucket}: unexpected bucket")
                continue

            actual_count = row.get("Count")
            if isinstance(actual_count, str):
                actual_count = float(actual_count)
            if actual_count is not None:
                actual_count = int(actual_count)

            actual_share = row.get("Share Pct")
            if isinstance(actual_share, str):
                actual_share = float(actual_share)

            if actual_count != exp['count']:
                wrong.append(f"{bucket} Count: expected={exp['count']}, got={actual_count}")
            if actual_share is None:
                wrong.append(f"{bucket} Share: expected={exp['share_pct']}, got=<empty>")
            elif abs(float(actual_share) - exp['share_pct']) > 0.15:
                wrong.append(f"{bucket} Share: expected={exp['share_pct']}, got={actual_share}")

        ok = len(wrong) == 0 and len(_br_bt_rows) > 0
        check("6. Bucket Count & Share Pct", 3, ok,
              "all correct" if ok else "; ".join(wrong[:5]))
    except Exception as e:
        check("6. Bucket Count & Share Pct", 3, False, f"exception: {e}")


def check_7_bucket_totals_target_gap() -> None:
    """Bucket Totals Target Pct and Gap Pct match expected values."""
    try:
        compute_ground_truth()
        load_baserow_state()
        if _gt_err or _br_bt_rows is None:
            check("7. Bucket Target & Gap Pct", 2, False,
                  _gt_err or "table not found")
            return

        expected_by_bucket = {bt['bucket']: bt for bt in _gt['bucket_totals']}
        wrong = []
        for row in _br_bt_rows:
            bucket = select_value(row.get("Bucket", ""))
            exp = expected_by_bucket.get(bucket)
            if not exp:
                wrong.append(f"{bucket or '<empty>'}: unexpected bucket")
                continue

            actual_target = row.get("Target Pct")
            if isinstance(actual_target, str):
                actual_target = float(actual_target)
            actual_gap = row.get("Gap Pct")
            if isinstance(actual_gap, str):
                actual_gap = float(actual_gap)

            if actual_target is None:
                wrong.append(f"{bucket} Target: expected={exp['target_pct']}, got=<empty>")
            elif abs(float(actual_target) - exp['target_pct']) > 0.15:
                wrong.append(f"{bucket} Target: expected={exp['target_pct']}, got={actual_target}")
            if actual_gap is None:
                wrong.append(f"{bucket} Gap: expected={exp['gap_pct']}, got=<empty>")
            elif abs(float(actual_gap) - exp['gap_pct']) > 0.15:
                wrong.append(f"{bucket} Gap: expected={exp['gap_pct']}, got={actual_gap}")

        ok = len(wrong) == 0 and len(_br_bt_rows) > 0
        check("7. Bucket Target & Gap Pct", 2, ok,
              "all correct" if ok else "; ".join(wrong[:5]))
    except Exception as e:
        check("7. Bucket Target & Gap Pct", 2, False, f"exception: {e}")


# Field spec per table: name -> (type, select option set or None,
#                                number_decimal_places or None)
_CWP_FIELD_SPEC = {
    "WP ID": ("number", None, None),
    "Subject": ("text", None, None),
    "Type": ("single_select", {"Task", "Bug", "Feature", "Epic", "Milestone"}, None),
    "Assignee": ("text", None, None),
    "Investment Bucket": ("single_select",
                          {"NewFeature", "TechDebt", "Reliability", "Security"}, None),
    "Team": ("single_select",
             {"Platform", "Product", "Data", "Security", "Reliability"}, None),
    "Closed Date": ("date", None, None),
}
_BT_FIELD_SPEC = {
    "Bucket": ("single_select",
               {"NewFeature", "TechDebt", "Reliability", "Security"}, None),
    "Count": ("number", None, None),
    "Share Pct": ("number", None, 1),
    "Target Pct": ("number", None, 1),
    "Gap Pct": ("number", None, 1),
}


def check_7b_field_schema() -> None:
    """Both Baserow tables carry the field types/options/decimals the task
    specifies, with the correct primary fields (REST fields API)."""
    try:
        load_baserow_state()
        if not _br_cwp_tid or not _br_bt_tid:
            check("7b. Field schema", 2, False, "table(s) not found")
            return

        issues = []

        def _check_table(tid: int, spec: dict, primary_name: str, label: str) -> None:
            fields = br_get(f"database/fields/table/{tid}/")
            by_name = {f.get("name"): f for f in fields}
            for name, (ftype, options, decimals) in spec.items():
                f = by_name.get(name)
                if f is None:
                    issues.append(f"{label}.{name}: field missing")
                    continue
                if f.get("type") != ftype:
                    issues.append(f"{label}.{name}: type={f.get('type')!r}, expected {ftype!r}")
                if options is not None:
                    got = {o.get("value") for o in f.get("select_options", [])}
                    if got != options:
                        issues.append(f"{label}.{name}: options={sorted(got)}, "
                                      f"expected {sorted(options)}")
                if decimals is not None and f.get("number_decimal_places") != decimals:
                    issues.append(f"{label}.{name}: decimals="
                                  f"{f.get('number_decimal_places')}, expected {decimals}")
                if name == primary_name and not f.get("primary"):
                    issues.append(f"{label}.{name}: not the primary field")

        _check_table(_br_cwp_tid, _CWP_FIELD_SPEC, "WP ID", "CWP")
        _check_table(_br_bt_tid, _BT_FIELD_SPEC, "Bucket", "BT")

        check("7b. Field schema", 2, not issues,
              "both tables OK" if not issues else "; ".join(issues[:5]))
    except Exception as e:
        check("7b. Field schema", 2, False, f"exception: {e}")


def check_8_codeserver_file_exists() -> None:
    """Markdown file exists in code-server at the expected path."""
    try:
        rc, out, err = docker_exec(
            CS_CONTAINER, "test", "-f",
            "/home/coder/workspace/devops-configs/docs/portfolio-review-Q4-2024.md",
        )
        ok = rc == 0
        check("8. Code-server file exists", 1, ok,
              "found" if ok else "file not found")
    except Exception as e:
        check("8. Code-server file exists", 1, False, f"exception: {e}")


def _norm_report_line(line: str) -> str:
    """NFC-normalize (canonicalizes em-dash/arrow lookalikes) and collapse
    internal whitespace so only the characters of the template are compared."""
    line = unicodedata.normalize("NFC", line)
    return re.sub(r"\s+", " ", line).strip()


# Matches one "<num>%" slot of the share-line templates.
_PCT_RE = r"(-?\d+(?:\.\d+)?)%"


def _check_share_line(line: str, prefix: str, expected: dict[str, float]) -> str | None:
    """Validate 'Actual/Target shares' line structure exactly and each numeric
    value against GT (tolerant numeric regex per value; structure is fixed:
    bucket order, '; ' separators, em dash after the prefix)."""
    pattern = (
        rf"^{re.escape(prefix)} — "
        rf"NewFeature: {_PCT_RE}; TechDebt: {_PCT_RE}; "
        rf"Reliability: {_PCT_RE}; Security: {_PCT_RE}$"
    )
    m = re.match(pattern, line)
    if not m:
        return f"structure mismatch: {line!r}"
    for got, bucket in zip(m.groups(), BUCKET_ORDER):
        if abs(float(got) - expected[bucket]) > 0.05:
            return f"{bucket}: got {got}%, expected {expected[bucket]}%"
    return None


def check_9_codeserver_file_content() -> None:
    """Markdown file consists of exactly the five GT-derived lines (exact
    sequence equality after unicode normalization; share values via tolerant
    numeric regex inside the fixed template)."""
    try:
        compute_ground_truth()
        if _gt_err:
            check("9. Code-server file content", 3, False,
                  f"ground truth error: {_gt_err}")
            return
        rc, out, err = docker_exec(
            CS_CONTAINER, "cat",
            "/home/coder/workspace/devops-configs/docs/portfolio-review-Q4-2024.md",
        )
        if rc != 0:
            check("9. Code-server file content", 3, False, "cannot read file")
            return

        lines = [_norm_report_line(l) for l in out.split('\n')]
        lines = [l for l in lines if l]

        issues = []
        if len(lines) != 5:
            issues.append(f"expected exactly 5 non-empty lines, got {len(lines)}")

        expected_fixed = [
            _norm_report_line("# Engineering Investment Portfolio — Q4-2024"),
            _norm_report_line("Window: 2024-10-01 → 2024-12-31"),
            _norm_report_line(f"Closed work packages: {_gt['total']}"),
        ]
        for i, exp in enumerate(expected_fixed):
            if len(lines) <= i:
                issues.append(f"line {i + 1} missing")
            elif lines[i] != exp:
                issues.append(f"line {i + 1}: {lines[i]!r} != {exp!r}")

        actual_shares = {bt['bucket']: bt['share_pct'] for bt in _gt['bucket_totals']}
        if len(lines) >= 4:
            err4 = _check_share_line(lines[3], "Actual shares", actual_shares)
            if err4:
                issues.append(f"line 4 {err4}")
        else:
            issues.append("line 4 missing")
        if len(lines) >= 5:
            err5 = _check_share_line(lines[4], "Target shares", TARGET_PCT)
            if err5:
                issues.append(f"line 5 {err5}")
        else:
            issues.append("line 5 missing")

        ok = len(issues) == 0
        check("9. Code-server file content", 3, ok,
              "all 5 lines exact" if ok else "; ".join(issues[:3]))
    except Exception as e:
        check("9. Code-server file content", 3, False, f"exception: {e}")


def check_10_rebalance_wp_count() -> None:
    """OpenProject has correct number of rebalance WPs for under-invested buckets."""
    try:
        compute_ground_truth()
        if _gt_err:
            check("10. Rebalance WP count", 2, False, _gt_err)
            return

        expected_count = sum(1 for bt in _gt['bucket_totals'] if bt['gap_pct'] > 0)

        project_id = op_sql("SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1")
        count = op_sql(f"""
            SELECT COUNT(*) FROM work_packages wp
            JOIN types t ON t.id = wp.type_id
            WHERE wp.project_id = {project_id}
              AND t.name = 'Task'
              AND wp.subject LIKE 'Rebalance next quarter:%'
        """)
        actual = int(count) if count else 0
        ok = actual == expected_count
        check("10. Rebalance WP count", 2, ok,
              f"expected={expected_count}, actual={actual}")
    except Exception as e:
        check("10. Rebalance WP count", 2, False, f"exception: {e}")


def check_11_rebalance_wp_subjects() -> None:
    """Rebalance WPs have correct subject format with bucket name and gap percentage."""
    try:
        compute_ground_truth()
        if _gt_err:
            check("11. Rebalance WP subjects", 2, False, _gt_err)
            return

        project_id = op_sql("SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1")
        rows = op_sql(f"""
            SELECT wp.subject FROM work_packages wp
            JOIN types t ON t.id = wp.type_id
            WHERE wp.project_id = {project_id}
              AND t.name = 'Task'
              AND wp.subject LIKE 'Rebalance next quarter:%'
        """)

        actual_subjects = set()
        if rows:
            for line in rows.split('\n'):
                line = line.strip()
                if line:
                    actual_subjects.add(line)

        expected_subjects = set()
        for bt in _gt['bucket_totals']:
            if bt['gap_pct'] > 0:
                expected_subjects.add(
                    f"Rebalance next quarter: {bt['bucket']} (+{bt['gap_pct']}%)"
                )

        missing = expected_subjects - actual_subjects
        extra = actual_subjects - expected_subjects
        ok = not missing and not extra
        detail = "all correct" if ok else ""
        if missing:
            detail += f"missing: {missing}"
        if extra:
            detail += f" extra: {extra}"
        check("11. Rebalance WP subjects", 2, ok, detail.strip())
    except Exception as e:
        check("11. Rebalance WP subjects", 2, False, f"exception: {e}")


def check_12_rebalance_wp_details() -> None:
    """Rebalance WPs have the correct assignee, priority and an exactly
    matching description. Row retrieval is per-id single-value queries so
    multiline descriptions or '|' characters cannot corrupt parsing."""
    try:
        compute_ground_truth()
        if _gt_err:
            check("12. Rebalance WP details", 2, False, _gt_err)
            return

        project_id = op_sql("SELECT id FROM projects WHERE name = 'API Gateway' LIMIT 1")
        admin_id = op_sql("SELECT id FROM users WHERE login = 'admin' LIMIT 1")

        id_rows = op_sql(f"""
            SELECT wp.id FROM work_packages wp
            JOIN types t ON t.id = wp.type_id
            WHERE wp.project_id = {project_id}
              AND t.name = 'Task'
              AND wp.subject LIKE 'Rebalance next quarter:%'
            ORDER BY wp.id
        """)
        wp_ids = [l.strip() for l in id_rows.split('\n') if l.strip()]
        if not wp_ids:
            check("12. Rebalance WP details", 2, False, "no rebalance WPs found")
            return

        expected_by_bucket = {bt['bucket']: bt for bt in _gt['bucket_totals']}
        issues = []
        for wp_id in wp_ids:
            subject = op_sql(f"SELECT subject FROM work_packages WHERE id = {wp_id}")
            assigned_id = op_sql(
                f"SELECT COALESCE(assigned_to_id::text, '') FROM work_packages WHERE id = {wp_id}"
            )
            priority = op_sql(f"""
                SELECT COALESCE(p.name, '') FROM work_packages wp
                LEFT JOIN enumerations p ON p.id = wp.priority_id
                WHERE wp.id = {wp_id}
            """)
            description = op_sql(
                f"SELECT COALESCE(description, '') FROM work_packages WHERE id = {wp_id}"
            )

            if not admin_id or assigned_id != admin_id:
                issues.append(f"WP#{wp_id}: wrong assignee "
                              f"(id={assigned_id or '<none>'}, expected={admin_id})")
            if priority.lower() != 'normal':
                issues.append(f"WP#{wp_id}: priority={priority}, expected=Normal")

            m = re.search(r'Rebalance next quarter: (\w+)', subject)
            bt = expected_by_bucket.get(m.group(1)) if m else None
            if not bt:
                issues.append(f"WP#{wp_id}: subject {subject!r} does not map to a bucket")
                continue
            expected_desc = (
                f"Current: {bt['share_pct']}%; "
                f"Target: {bt['target_pct']}%; "
                f"Quarter under review: Q4-2024"
            )
            # Normalize: strip HTML tags, collapse whitespace — then require
            # exact equality (no substring fallback).
            desc_clean = re.sub(r"<[^>]+>", " ", description)
            desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
            if desc_clean != expected_desc:
                issues.append(f"WP#{wp_id} ({bt['bucket']}): "
                              f"desc={desc_clean!r} != expected {expected_desc!r}")

        ok = len(issues) == 0
        check("12. Rebalance WP details", 2, ok,
              "all correct" if ok else "; ".join(issues[:3]))
    except Exception as e:
        check("12. Rebalance WP details", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_baserow_db_exists()
    check_2_cwp_table_rows()
    check_3_cwp_bucket_classification()
    check_4_cwp_team_assignment()
    check_4b_cwp_row_fields()
    check_5_bucket_totals_exists()
    check_6_bucket_totals_count_share()
    check_7_bucket_totals_target_gap()
    check_7b_field_schema()
    check_8_codeserver_file_exists()
    check_9_codeserver_file_content()
    check_10_rebalance_wp_count()
    check_11_rebalance_wp_subjects()
    check_12_rebalance_wp_details()

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
