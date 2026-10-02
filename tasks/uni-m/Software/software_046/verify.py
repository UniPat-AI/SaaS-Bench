"""
Verifier for Software-046-I4: Release candidate QA sign-off workflow for data-analyzer v3.2.0-rc1

Checks: 12 weighted checks across openproject, code-server, baserow (ck8+ck9 merged;
ck13 schema check added). Total weight stays 20 (rebalanced: ck1 2->1, ck7 2->1,
merged ck8+9 3->2, ck10 1->2, new ck13 +2).
Strategy: docker exec (OpenProject DB, code-server filesystem, Baserow Postgres),
REST API (Baserow). All expected values are recomputed by the verifier from the
task-description constants (criteria 5-tuples, submission 6-tuples, approval rules
-> Status, P/F/N) plus live reads (form view slug, OP rows). Agent-filled data is
never trusted as ground truth.

Required env vars:
  SERVER_HOSTNAME, OPENPROJECT_PORT, OPENPROJECT_CONTAINER,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER
"""

import os
import re
import sys
import subprocess
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENPROJECT_PORT = os.getenv("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.getenv("OPENPROJECT_CONTAINER")
CODE_SERVER_PORT = os.getenv("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.getenv("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.getenv("BASEROW_PORT")
BASEROW_CONTAINER = os.getenv("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.getenv("BASEROW_DB_CONTAINER")

for var_name, var_val in [
    ("OPENPROJECT_PORT", OPENPROJECT_PORT),
    ("OPENPROJECT_CONTAINER", OPENPROJECT_CONTAINER),
    ("CODE_SERVER_PORT", CODE_SERVER_PORT),
    ("CODE_SERVER_CONTAINER", CODE_SERVER_CONTAINER),
    ("BASEROW_PORT", BASEROW_PORT),
    ("BASEROW_CONTAINER", BASEROW_CONTAINER),
    ("BASEROW_DB_CONTAINER", BASEROW_DB_CONTAINER),
]:
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

OPENPROJECT_BASE = f"http://{HOST}:{OPENPROJECT_PORT}"
BASEROW_BASE = f"http://{HOST}:{BASEROW_PORT}"
BASEROW_PG_PASSWORD = "kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc"

# ── Ground truth recomputed from the task description (never from agent data) ─
COMMIT_MSG = "chore(release): prepare 3.2.0-rc1 candidate"
VERSION_DESC = "Release candidate for v3.2.0; sign-off window 2025-07-07 to 2025-07-16"

CHANGELOG_LINES = [
    "## [3.2.0-rc1] - 2025-07-07",
    "### Candidate for v3.2.0",
    "- Sign-off window: 2025-07-07 to 2025-07-16",
]

# (Criterion ID, Criterion Name, Category, Required Approver Role, Target)
CRITERIA_SPEC = [
    ("SC-01", "Data ingestion pipeline validated", "Functional", "QALead",
     "All loader and reporter unit tests pass"),
    ("SC-02", "Analysis throughput >= 1M rows/min", "Performance", "QALead",
     "Benchmark completes within 60s for 1M-row dataset"),
    ("SC-03", "No secrets or credentials in repo", "Security", "SecurityEngineer",
     "truffleHog and gitleaks scans return zero findings"),
    ("SC-04", "User manual and API reference refreshed", "Documentation", "ProductOwner",
     "docs/ site builds and CHANGELOG is current"),
    ("SC-05", "Database migration rollback rehearsed", "Rollback", "ReleaseManager",
     "Rollback tested in staging within 8 minutes"),
]

# Submission 6-tuples in submission order (=> AP-001..AP-006 by this order).
# (Approver Name, Approver Role, Criterion ID, Approved, Comment)
SUBMISSIONS_SPEC = [
    ("Paul Garcia", "QALead", "SC-01", True,
     "Loader and reporter unit tests all green on CI."),
    ("Paul Garcia", "QALead", "SC-02", True,
     "Benchmark completed in 47s for 1M-row dataset."),
    ("Thomas Nickson", "SecurityEngineer", "SC-03", True,
     "truffleHog and gitleaks scans clean; no secrets detected."),
    ("Nora Mott", "ProductOwner", "SC-04", False,
     "API reference section for new reporter module is missing; needs update before GA."),
    ("Michael Robicheaux", "ReleaseManager", "SC-05", True,
     "Rollback rehearsal finished in 6 minutes on staging."),
    ("Sandra Love", "QALead", "SC-01", True,
     "Secondary QA confirmation — full regression pack clean."),
]

SUBMITTED_AT = "2025-07-14"

CATEGORY_OPTIONS = {"Functional", "Performance", "Security", "Documentation", "Rollback"}
ROLE_OPTIONS = {"QALead", "SecurityEngineer", "ProductOwner", "ReleaseManager"}
STATUS_OPTIONS = {"Pending", "Passed", "Failed"}

FORM_TITLE = "v3.2.0-rc1 Sign-off"
FORM_DESCRIPTION = ("Please submit your approval for each assigned criterion. "
                    "Window closes 2025-07-16.")
FORM_SUBMIT_MSG = "Thank you — your approval for v3.2.0-rc1 has been recorded."


def recompute_statuses() -> dict:
    """Apply the description's approval rules to the spec submissions:
    Failed if any Approved=false from the required approver role, else Passed if
    any Approved=true from the required approver role, else Pending."""
    statuses = {}
    for cid, _name, _cat, req_role, _target in CRITERIA_SPEC:
        subs = [s for s in SUBMISSIONS_SPEC if s[2] == cid and s[1] == req_role]
        if any(not s[3] for s in subs):
            statuses[cid] = "Failed"
        elif any(s[3] for s in subs):
            statuses[cid] = "Passed"
        else:
            statuses[cid] = "Pending"
    return statuses


EXPECTED_STATUSES = recompute_statuses()  # SC-01/02/03/05 Passed, SC-04 Failed
PFN = {
    "Passed": sum(1 for v in EXPECTED_STATUSES.values() if v == "Passed"),    # 4
    "Failed": sum(1 for v in EXPECTED_STATUSES.values() if v == "Failed"),    # 1
    "Pending": sum(1 for v in EXPECTED_STATUSES.values() if v == "Pending"),  # 0
}
FAILED_CRITERIA = [c for c in CRITERIA_SPEC if EXPECTED_STATUSES[c[0]] == "Failed"]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # detail must stay single-line
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


PSQL_SEP = "\x01"  # distinctive field separator; never appears in the data


def op_db_rows(sql: str) -> list[list[str]]:
    """Query OpenProject's embedded PostgreSQL.

    Returns a list of rows (each a list of column strings). Per-row splitting
    (one output line per row, \\x01 field separator) — never concatenate multiple
    rows into one string (U4 fix: the old op_db_query mashed multi-row results
    together, letting e.g. a Milestone+Phase WP pair pass a single-row check).
    Any SELECTed column that may contain newlines must be sanitized in SQL via
    regexp_replace(col, E'[\\n\\r]+', ' ', 'g')."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-U", "openproject", "-h", "127.0.0.1", "-d", "openproject",
         "-t", "-A", "-F", PSQL_SEP, "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    rows = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        rows.append(line.split(PSQL_SEP))
    return rows


def baserow_db_query(sql: str) -> str:
    """Direct query against Baserow's Postgres (fallback path for form-view
    columns not exposed over REST)."""
    r = subprocess.run(
        ["docker", "exec", "-e", f"PGPASSWORD={BASEROW_PG_PASSWORD}",
         BASEROW_DB_CONTAINER,
         "psql", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    return r.stdout.strip()


def baserow_api_get(path: str, token: str) -> dict:
    r = requests.get(
        f"{BASEROW_BASE}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def get_baserow_token() -> str:
    r = requests.post(
        f"{BASEROW_BASE}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _sel_value(val):
    """Extract a single-select field's value ({'id':..,'value':..} or None)."""
    if isinstance(val, dict):
        return val.get("value")
    return val


def _link_values(val) -> list:
    """Extract link_row values; tolerate empty list / plain strings."""
    if isinstance(val, list):
        return [x.get("value") if isinstance(x, dict) else x for x in val]
    return []


def _norm_desc(desc: str) -> str:
    """Strip CKEditor backslash escapes and collapse whitespace/newlines."""
    return " ".join(desc.replace("\\", "").split())


def _is_blank_placeholder_row(row: dict) -> bool:
    """Baserow auto-creates blank placeholder rows; treat a row as blank when
    every user field is empty/default (text '', None, boolean false, empty
    link list)."""
    for key, val in row.items():
        if key in ("id", "order"):
            continue
        if isinstance(val, bool):
            if val:
                return False
        elif isinstance(val, (list, tuple)):
            if len(val) > 0:
                return False
        elif isinstance(val, dict):
            return False
        elif val not in (None, ""):
            return False
    return True


def _fetch_rows(token: str, table_id: int) -> list[dict]:
    resp = baserow_api_get(
        f"database/rows/table/{table_id}/?user_field_names=true&size=100", token
    )
    rows = [r for r in resp.get("results", []) if not _is_blank_placeholder_row(r)]

    def _key(r):
        try:
            o = float(r.get("order", 0) or 0)
        except (TypeError, ValueError):
            o = 0.0
        return (o, r.get("id", 0))

    return sorted(rows, key=_key)


def _find_table(token: str, db_id: int, name: str):
    tables = baserow_api_get(f"database/tables/database/{db_id}/", token)
    for t in tables:
        if t.get("name") == name:
            return t
    return None


# ── Check 1: OpenProject version v3.2.0-rc1 ──────────────────────────────────
def check_1_op_version() -> None:
    """Exactly one version v3.2.0-rc1 with exact dates, status and description."""
    try:
        rows = op_db_rows(
            "SELECT v.name, v.start_date::text, v.effective_date::text, "
            "regexp_replace(coalesce(v.description,''), E'[\\n\\r]+', ' ', 'g'), v.status "
            "FROM versions v "
            "JOIN projects p ON p.id = v.project_id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND v.name = 'v3.2.0-rc1'"
        )
        if len(rows) != 1:
            check("1. OP version v3.2.0-rc1", 1, False,
                  f"expected exactly 1 version row, got {len(rows)}")
            return
        name, start, due, desc, status = (rows[0] + ["", "", "", "", ""])[:5]
        desc = desc.strip()
        ok = (
            name == "v3.2.0-rc1"
            and start == "2025-07-07"
            and due == "2025-07-16"
            and desc == VERSION_DESC        # exact equality, no lower()/substring
            and status == "open"
        )
        check("1. OP version v3.2.0-rc1", 1, ok,
              f"start={start}, due={due}, status={status}, desc={desc[:80]}")
    except Exception as e:
        check("1. OP version v3.2.0-rc1", 1, False, f"exception: {e}")


# ── Check 2: Git commit (exact message + exact staged file set) ──────────────
def check_2_git_commit() -> None:
    """Commit with exact subject exists and touches exactly
    {setup.py or pyproject.toml, CHANGELOG.md}."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "bash", "-c",
            "cd /home/coder/workspace/data-analyzer && "
            "git log --all --format='%H%x00%s'",
            timeout=15,
        )
        shas = []
        for line in out.splitlines():
            if "\x00" not in line:
                continue
            sha, subject = line.split("\x00", 1)
            if subject == COMMIT_MSG:  # exact subject, not substring
                shas.append(sha)
        if not shas:
            check("2. Git commit message & file set", 2, False,
                  "no commit with exact message found")
            return
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "bash", "-c",
            "cd /home/coder/workspace/data-analyzer && "
            f"git show {shas[0]} --pretty=format: --name-only",
            timeout=15,
        )
        files = {l.strip() for l in out.splitlines() if l.strip()}
        allowed = ({"setup.py", "CHANGELOG.md"}, {"pyproject.toml", "CHANGELOG.md"})
        ok = files in allowed
        check("2. Git commit message & file set", 2, ok,
              f"commit={shas[0][:10]}, files={sorted(files)}")
    except Exception as e:
        check("2. Git commit message & file set", 2, False, f"exception: {e}")


# ── Check 3: Manifest version bumped 0.1.0 -> 3.2.0-rc1 ──────────────────────
def check_3_manifest_version() -> None:
    """Manifest (setup.py or pyproject.toml) has zero '0.1.0' occurrences and a
    version line containing 3.2.0-rc1."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "bash", "-c",
            "cd /home/coder/workspace/data-analyzer && "
            "for f in setup.py pyproject.toml; do "
            "if [ -f \"$f\" ]; then echo \"@@MANIFEST:$f\"; cat \"$f\"; fi; done",
            timeout=15,
        )
        if "@@MANIFEST:" not in out:
            check("3. Manifest version", 1, False, "no setup.py/pyproject.toml found")
            return
        content = "\n".join(l for l in out.splitlines() if not l.startswith("@@MANIFEST:"))
        old_count = content.count("0.1.0")
        version_line = None
        for line in content.splitlines():
            if "version" in line.lower() and "3.2.0-rc1" in line:
                version_line = line.strip()
                break
        ok = old_count == 0 and version_line is not None
        check("3. Manifest version", 1, ok,
              f"'0.1.0' occurrences={old_count}, version line={version_line}")
    except Exception as e:
        check("3. Manifest version", 1, False, f"exception: {e}")


# ── Check 4: CHANGELOG.md — 3 exact lines immediately below ## [Unreleased] ──
def check_4_changelog() -> None:
    """Lines i+1..i+3 after the '## [Unreleased]' line must be exactly the three
    specified lines (order + adjacency, rstrip each).

    NOTE (seed defect, report-only): the current image ships data-analyzer
    WITHOUT a CHANGELOG.md (no '## [Unreleased]' line anywhere), so the task
    step cannot be followed literally — agents must self-create the file with
    an Unreleased section. Deliberately unchanged here; recorded for a future
    seed fix."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "bash", "-c",
            "cd /home/coder/workspace/data-analyzer && cat CHANGELOG.md",
            timeout=15,
        )
        if rc != 0:
            check("4. CHANGELOG.md entries", 2, False, "CHANGELOG.md not found")
            return
        lines = out.splitlines()
        idx = None
        for i, line in enumerate(lines):
            if line.rstrip() == "## [Unreleased]":
                idx = i
                break
        if idx is None:
            check("4. CHANGELOG.md entries", 2, False, "'## [Unreleased]' line not found")
            return
        got = [l.rstrip() for l in lines[idx + 1: idx + 4]]
        ok = got == CHANGELOG_LINES
        check("4. CHANGELOG.md entries", 2, ok,
              "3 exact lines immediately below [Unreleased]" if ok
              else f"lines after [Unreleased]={got}")
    except Exception as e:
        check("4. CHANGELOG.md entries", 2, False, f"exception: {e}")


# ── Check 5: Baserow database exists ─────────────────────────────────────────
def check_5_baserow_db(token: str):
    """Database 'RC Sign-off v3.2.0' exists in Baserow."""
    try:
        apps = baserow_api_get("applications/", token)
        db_id = None
        for app in apps:
            if app.get("name") == "RC Sign-off v3.2.0" and app.get("type") == "database":
                db_id = app["id"]
                break
        check("5. Baserow DB exists", 1, db_id is not None,
              f"id={db_id}" if db_id else "database not found")
        return db_id
    except Exception as e:
        check("5. Baserow DB exists", 1, False, f"exception: {e}")
        return None


# ── Check 6: Sign-off Criteria — 5 exact 5-tuple rows ────────────────────────
def check_6_criteria_table(token: str, db_id: int):
    """5 rows; sorted by Criterion ID they must be SC-01..SC-05 and each row's
    (Criterion ID, Criterion Name, Category, Required Approver Role, Target)
    must equal the spec record — per-field by name, no cross-field has_any."""
    try:
        table = _find_table(token, db_id, "Sign-off Criteria")
        if not table:
            check("6. Sign-off Criteria rows", 2, False, "table not found")
            return None, []
        table_id = table["id"]
        rows = _fetch_rows(token, table_id)
        if len(rows) != 5:
            check("6. Sign-off Criteria rows", 2, False,
                  f"expected exactly 5 rows, got {len(rows)}")
            return table_id, rows
        rows_sorted = sorted(rows, key=lambda r: str(r.get("Criterion ID") or ""))
        mismatches = []
        for row, spec in zip(rows_sorted, CRITERIA_SPEC):
            cid, name, cat, role, target = spec
            got = (
                (row.get("Criterion ID") or "").strip(),
                (row.get("Criterion Name") or "").strip(),
                _sel_value(row.get("Category")),
                _sel_value(row.get("Required Approver Role")),
                (row.get("Target") or "").strip(),
            )
            if got != (cid, name, cat, role, target):
                mismatches.append(f"{cid}: got {got}")
        ok = not mismatches
        check("6. Sign-off Criteria rows", 2, ok,
              "5 rows SC-01..SC-05 exact" if ok else "; ".join(mismatches)[:250])
        return table_id, rows_sorted
    except Exception as e:
        check("6. Sign-off Criteria rows", 2, False, f"exception: {e}")
        return None, []


# ── Check 7: Criteria statuses (recomputed from spec + approval rules) ───────
def check_7_criteria_statuses(rows: list) -> None:
    """Status read by field name; expected statuses recomputed by the verifier
    (SC-01/02/03/05 Passed, SC-04 Failed)."""
    try:
        status_map = {}
        for row in rows:
            cid = (row.get("Criterion ID") or "").strip()
            if cid:
                status_map[cid] = _sel_value(row.get("Status"))
        mismatches = []
        for cid, exp in EXPECTED_STATUSES.items():
            got = status_map.get(cid)
            if got != exp:
                mismatches.append(f"{cid}: expected {exp}, got {got}")
        ok = not mismatches and len(status_map) == 5
        check("7. Criteria statuses", 1, ok,
              f"{PFN['Passed']} Passed, {PFN['Failed']} Failed" if ok
              else "; ".join(mismatches)[:250])
    except Exception as e:
        check("7. Criteria statuses", 1, False, f"exception: {e}")


# ── Check 8 (merged 8+9): Stakeholder Approvals — 6 full-tuple rows in order ─
def check_8_approvals(token: str, db_id: int):
    """Exactly 6 rows in submission order; row k must equal spec submission k
    plus AP-00(k+1) (verifier-generated by submission order, not sorted()) and
    Submitted At 2025-07-14. Comment exact; Criterion ID parsed from the
    link-row [{id,value}] payload (empty list tolerated as a mismatch)."""
    label = "8. Stakeholder Approvals rows (full 6-tuple, merged ck8+ck9)"
    try:
        table = _find_table(token, db_id, "Stakeholder Approvals")
        if not table:
            check(label, 2, False, "table not found")
            return None
        table_id = table["id"]
        rows = _fetch_rows(token, table_id)
        if len(rows) != len(SUBMISSIONS_SPEC):
            check(label, 2, False,
                  f"expected exactly {len(SUBMISSIONS_SPEC)} rows, got {len(rows)}")
            return table_id
        mismatches = []
        for k, (row, spec) in enumerate(zip(rows, SUBMISSIONS_SPEC)):
            exp_ap = f"AP-{k + 1:03d}"
            name, role, cid, approved, comment = spec
            errs = []
            if (row.get("Approval ID") or "").strip() != exp_ap:
                errs.append(f"Approval ID={row.get('Approval ID')!r}!={exp_ap}")
            if (row.get("Approver Name") or "").strip() != name:
                errs.append(f"Approver Name={row.get('Approver Name')!r}")
            if (row.get("Approver Role") or "").strip() != role:
                errs.append(f"Approver Role={row.get('Approver Role')!r}")
            links = _link_values(row.get("Criterion ID"))
            if len(links) != 1 or (links[0] or "").strip() != cid:
                errs.append(f"Criterion ID links={links}!=[{cid}]")
            if bool(row.get("Approved")) != approved:
                errs.append(f"Approved={row.get('Approved')}!={approved}")
            if (row.get("Comment") or "").strip() != comment:
                errs.append(f"Comment={str(row.get('Comment'))[:40]!r}")
            sub_at = str(row.get("Submitted At") or "")
            if not (sub_at == SUBMITTED_AT or sub_at.startswith(SUBMITTED_AT)):
                errs.append(f"Submitted At={sub_at!r}")
            if errs:
                mismatches.append(f"row{k + 1}: " + ", ".join(errs))
        ok = not mismatches
        check(label, 2, ok,
              "6 rows == spec submissions, AP-001..AP-006 in submission order"
              if ok else "; ".join(mismatches)[:300])
        return table_id
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return None


# ── Check 10: Form view configuration ────────────────────────────────────────
def check_10_form_view(token: str, approvals_table_id: int):
    """Form view named exactly 'RC Sign-off Form' with exact title, description,
    post-submit message, email notification enabled, and correct field options
    (Approval ID + Submitted At hidden; the rest visible; Approver Name/
    Approver Role/Criterion ID/Approved required). Returns the slug for ck11."""
    try:
        views = baserow_api_get(f"database/views/table/{approvals_table_id}/", token)
        form_view = None
        for v in views:
            if v.get("type") == "form" and v.get("name") == "RC Sign-off Form":
                form_view = v
                break
        if not form_view:
            check("10. Form view configuration", 2, False,
                  "form view named exactly 'RC Sign-off Form' not found")
            return None
        view_id = form_view["id"]
        # Individual GET returns the full form serializer (title/description/
        # submit_action_message/slug/receive_notification_on_submit).
        try:
            fv = baserow_api_get(f"database/views/{view_id}/", token)
        except Exception:
            fv = form_view
        slug = fv.get("slug") or form_view.get("slug")

        errs = []
        if (fv.get("title") or "").strip() != FORM_TITLE:
            errs.append(f"title={fv.get('title')!r}")
        if (fv.get("description") or "").strip() != FORM_DESCRIPTION:
            errs.append(f"description={str(fv.get('description'))[:60]!r}")
        if (fv.get("submit_action_message") or "").strip() != FORM_SUBMIT_MSG:
            errs.append(f"submit_action_message={str(fv.get('submit_action_message'))[:60]!r}")

        # Email notification: REST field is serialized per requesting user (we
        # use the admin JWT). If absent from the response, fall back to Baserow
        # Postgres (database_formview / users_to_notify m2m).
        notify = fv.get("receive_notification_on_submit")
        if notify is None:
            out = baserow_db_query(
                "SELECT receive_notification_on_submit FROM database_formview "
                f"WHERE view_ptr_id = {view_id}"
            )
            if out in ("t", "true", "True"):
                notify = True
            elif out in ("f", "false", "False"):
                notify = False
            else:
                out2 = baserow_db_query(
                    "SELECT COUNT(*) FROM database_formview_users_to_notify "
                    f"WHERE formview_id = {view_id}"
                )
                notify = bool(out2.isdigit() and int(out2) > 0)
        if notify is not True:
            errs.append(f"receive_notification_on_submit={notify}")

        # Field options: enabled/required per field name.
        fields = baserow_api_get(f"database/fields/table/{approvals_table_id}/", token)
        id_to_name = {f["id"]: f["name"] for f in fields}
        fo_resp = baserow_api_get(f"database/views/{view_id}/field-options/", token)
        raw_opts = fo_resp.get("field_options", {})
        opts_by_name = {}
        for fid, opt in raw_opts.items():
            fname = id_to_name.get(int(fid))
            if fname:
                opts_by_name[fname] = opt
        hidden = {"Approval ID", "Submitted At"}
        required = {"Approver Name", "Approver Role", "Criterion ID", "Approved"}
        for fname in id_to_name.values():
            opt = opts_by_name.get(fname, {})
            enabled = bool(opt.get("enabled"))
            if fname in hidden:
                if enabled:
                    errs.append(f"{fname} should be excluded from the form")
            else:
                if not enabled:
                    errs.append(f"{fname} should be on the form")
            if fname in required and not bool(opt.get("required")):
                errs.append(f"{fname} should be required")

        ok = not errs
        check("10. Form view configuration", 2, ok,
              f"slug={slug}, title/description/message/notify/field-options all exact"
              if ok else "; ".join(errs)[:300])
        return slug
    except Exception as e:
        check("10. Form view configuration", 2, False, f"exception: {e}")
        return None


# ── Check 11: OpenProject parent Phase WP ─────────────────────────────────────
def check_11_phase_wp(form_slug) -> None:
    """Exactly one Phase WP 'RC Sign-off: v3.2.0-rc1' (COUNT==1; the old
    multi-row concatenation let a Milestone+Phase double WP pass), linked to
    version v3.2.0-rc1, start date 2025-07-16, priority High, and description
    exactly 'Sign-off form: <url>; Passed: 4; Failed: 1; Pending: 0' where the
    URL must contain /form/<slug> read live in ck10 and P/F/N are recomputed
    from the spec.

    The parent WP is Phase-type (OpenProject rejects Milestones as parents:
    'Parent cannot be a milestone')."""
    try:
        rows = op_db_rows(
            "SELECT regexp_replace(coalesce(wp.description,''), E'[\\n\\r]+', ' ', 'g'), "
            "coalesce(v.name,''), coalesce(wp.start_date::text,''), coalesce(e.name,'') "
            "FROM work_packages wp "
            "JOIN types t ON t.id = wp.type_id "
            "JOIN projects p ON p.id = wp.project_id "
            "LEFT JOIN versions v ON v.id = wp.version_id "
            "LEFT JOIN enumerations e ON e.id = wp.priority_id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND wp.subject = 'RC Sign-off: v3.2.0-rc1' "
            "AND t.name = 'Phase'"
        )
        if len(rows) != 1:
            check("11. Parent Phase WP", 2, False,
                  f"expected exactly 1 Phase WP with exact subject, got {len(rows)}")
            return
        desc, version, start, priority = (rows[0] + ["", "", "", ""])[:4]
        errs = []
        if version != "v3.2.0-rc1":
            errs.append(f"version={version!r}")
        if start != "2025-07-16":
            errs.append(f"start_date={start!r}")
        if priority != "High":
            errs.append(f"priority={priority!r}")
        desc_norm = _norm_desc(desc)
        pattern = (r"Sign-off form: (\S+); "
                   rf"Passed: {PFN['Passed']}; Failed: {PFN['Failed']}; "
                   rf"Pending: {PFN['Pending']}")
        m = re.fullmatch(pattern, desc_norm)
        if not m:
            errs.append(f"desc does not match whole template: {desc_norm[:100]!r}")
        else:
            url = m.group(1)
            if not form_slug:
                errs.append("form slug unavailable (ck10) — URL cannot be verified")
            elif f"/form/{form_slug}" not in url:
                errs.append(f"URL {url[:60]!r} does not contain /form/{form_slug}")
        ok = not errs
        check("11. Parent Phase WP", 2, ok,
              "exactly 1 Phase, version/start/priority/desc+form-URL exact"
              if ok else "; ".join(errs)[:300])
    except Exception as e:
        check("11. Parent Phase WP", 2, False, f"exception: {e}")


# ── Check 12: Bug WPs for failed criteria ─────────────────────────────────────
def check_12_bug_wps() -> None:
    """Exactly one Bug WP per Failed criterion (truth: 1), child of the Phase,
    version v3.2.0-rc1, priority High, assignee michael.robicheaux, description
    exactly 'Category: <C>; Target: <T>; Required Approver Role: <R>' from the
    spec; total 'Fix before GA:%' count == truth failed count."""
    try:
        errs = []
        cnt_rows = op_db_rows(
            "SELECT COUNT(*) FROM work_packages wp "
            "JOIN projects p ON p.id = wp.project_id "
            "WHERE p.identifier = 'data-analytics-pipeline' "
            "AND wp.subject LIKE 'Fix before GA:%'"
        )
        total = int(cnt_rows[0][0]) if cnt_rows and cnt_rows[0][0].isdigit() else -1
        if total != len(FAILED_CRITERIA):
            errs.append(f"'Fix before GA:%' WP count={total}, expected {len(FAILED_CRITERIA)}")

        for cid, name, cat, role, target in FAILED_CRITERIA:
            subject = f"Fix before GA: {name}"
            expected_desc = f"Category: {cat}; Target: {target}; Required Approver Role: {role}"
            rows = op_db_rows(
                "SELECT regexp_replace(coalesce(wp.description,''), E'[\\n\\r]+', ' ', 'g'), "
                "t.name, coalesce(parent.subject,''), coalesce(u.login,''), "
                "coalesce(v.name,''), coalesce(e.name,'') "
                "FROM work_packages wp "
                "JOIN types t ON t.id = wp.type_id "
                "JOIN projects p ON p.id = wp.project_id "
                "LEFT JOIN work_packages parent ON parent.id = wp.parent_id "
                "LEFT JOIN users u ON u.id = wp.assigned_to_id "
                "LEFT JOIN versions v ON v.id = wp.version_id "
                "LEFT JOIN enumerations e ON e.id = wp.priority_id "
                "WHERE p.identifier = 'data-analytics-pipeline' "
                f"AND wp.subject = '{subject}'"
            )
            if len(rows) != 1:
                errs.append(f"{cid}: expected exactly 1 WP {subject!r}, got {len(rows)}")
                continue
            desc, type_name, parent_subject, assignee, version, priority = \
                (rows[0] + [""] * 6)[:6]
            if type_name != "Bug":
                errs.append(f"{cid}: type={type_name!r}")
            if parent_subject != "RC Sign-off: v3.2.0-rc1":
                errs.append(f"{cid}: parent={parent_subject!r}")
            if assignee != "michael.robicheaux":
                errs.append(f"{cid}: assignee={assignee!r}")
            if version != "v3.2.0-rc1":
                errs.append(f"{cid}: version={version!r}")
            if priority != "High":
                errs.append(f"{cid}: priority={priority!r}")
            if _norm_desc(desc) != expected_desc:
                errs.append(f"{cid}: desc={_norm_desc(desc)[:80]!r}")
        ok = not errs
        check("12. Bug WPs for failed criteria", 2, ok,
              f"{len(FAILED_CRITERIA)} Bug WP(s) exact (type/parent/assignee/version/priority/desc)"
              if ok else "; ".join(errs)[:300])
    except Exception as e:
        check("12. Bug WPs for failed criteria", 2, False, f"exception: {e}")


# ── Check 13 (new): table schema — select option sets + link_row target ──────
def check_13_schema(token: str, criteria_table_id, approvals_table_id) -> None:
    """Category/Required Approver Role/Status single-select option sets exactly
    equal the spec; approvals 'Criterion ID' is a link_row pointing at the
    Sign-off Criteria table."""
    try:
        if not criteria_table_id or not approvals_table_id:
            check("13. Table schema", 2, False, "table id(s) unavailable")
            return
        errs = []
        crit_fields = {f["name"]: f for f in
                       baserow_api_get(f"database/fields/table/{criteria_table_id}/", token)}
        for fname, expected_opts in [
            ("Category", CATEGORY_OPTIONS),
            ("Required Approver Role", ROLE_OPTIONS),
            ("Status", STATUS_OPTIONS),
        ]:
            f = crit_fields.get(fname)
            if not f:
                errs.append(f"criteria field {fname!r} missing")
                continue
            if f.get("type") != "single_select":
                errs.append(f"{fname}: type={f.get('type')!r}, expected single_select")
                continue
            got_opts = {o.get("value") for o in f.get("select_options", [])}
            if got_opts != expected_opts:
                errs.append(f"{fname}: options={sorted(got_opts)} != {sorted(expected_opts)}")

        appr_fields = {f["name"]: f for f in
                       baserow_api_get(f"database/fields/table/{approvals_table_id}/", token)}
        link = appr_fields.get("Criterion ID")
        if not link:
            errs.append("approvals field 'Criterion ID' missing")
        elif link.get("type") != "link_row":
            errs.append(f"Criterion ID: type={link.get('type')!r}, expected link_row")
        else:
            target = link.get("link_row_table_id")
            if target is None and isinstance(link.get("link_row_table"), dict):
                target = link["link_row_table"].get("id")
            if target != criteria_table_id:
                errs.append(f"Criterion ID links table {target}, expected {criteria_table_id}")
        ok = not errs
        check("13. Table schema", 2, ok,
              "select option sets exact; Criterion ID link_row -> Sign-off Criteria"
              if ok else "; ".join(errs)[:300])
    except Exception as e:
        check("13. Table schema", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # OpenProject / code-server checks
    check_1_op_version()
    check_2_git_commit()
    check_3_manifest_version()
    check_4_changelog()

    # Baserow checks
    token = None
    auth_err = ""
    try:
        token = get_baserow_token()
    except Exception as e:
        auth_err = f"auth failed: {e}"

    db_id = None
    criteria_table_id = None
    approvals_table_id = None
    form_slug = None

    if token:
        db_id = check_5_baserow_db(token)
    else:
        check("5. Baserow DB exists", 1, False, auth_err)

    if token and db_id:
        criteria_table_id, criteria_rows = check_6_criteria_table(token, db_id)
        if criteria_rows:
            check_7_criteria_statuses(criteria_rows)
        else:
            check("7. Criteria statuses", 1, False, "no criteria rows to check")

        approvals_table_id = check_8_approvals(token, db_id)
        if approvals_table_id:
            form_slug = check_10_form_view(token, approvals_table_id)
        else:
            check("10. Form view configuration", 2, False, "approvals table not found")

        check_13_schema(token, criteria_table_id, approvals_table_id)
    else:
        skip = auth_err or "skipped (no DB)"
        check("6. Sign-off Criteria rows", 2, False, skip)
        check("7. Criteria statuses", 1, False, skip)
        check("8. Stakeholder Approvals rows (full 6-tuple, merged ck8+ck9)", 2, False, skip)
        check("10. Form view configuration", 2, False, skip)
        check("13. Table schema", 2, False, skip)

    # More OpenProject checks (ck11 needs the live form slug from ck10)
    check_11_phase_wp(form_slug)
    check_12_bug_wps()

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
