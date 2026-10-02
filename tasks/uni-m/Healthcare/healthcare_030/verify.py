"""
Verifier for Healthcare-030-I2: Patient Complaint Intake, Clinical Review, and Formal Response

Checks: 18 checks (17 weighted + 1 zero-weight gate), total weight 27,
across opnform, openemr, onlyoffice.
Strategy: docker exec (OpnForm DB, OpenEMR DB, OnlyOffice DB/FS) + API fallback
(OnlyOffice document bytes).

Required env vars:
  SERVER_HOSTNAME, OPNFORM_PORT, OPNFORM_CONTAINER,
  OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPNFORM_PORT = os.environ.get("OPNFORM_PORT")
OPNFORM_CONTAINER = os.environ.get("OPNFORM_CONTAINER")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

_required = {
    "OPNFORM_PORT": OPNFORM_PORT,
    "OPNFORM_CONTAINER": OPNFORM_CONTAINER,
    "OPENEMR_PORT": OPENEMR_PORT,
    "OPENEMR_CONTAINER": OPENEMR_CONTAINER,
    "OPENEMR_DB_CONTAINER": OPENEMR_DB_CONTAINER,
    "ONLYOFFICE_PORT": ONLYOFFICE_PORT,
    "ONLYOFFICE_CONTAINER": ONLYOFFICE_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": ONLYOFFICE_DB_CONTAINER,
}
for var_name, var_val in _required.items():
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
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


def opnform_db_query_psql(sql: str) -> str:
    """Query OpnForm's PostgreSQL directly via psql inside the container."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-F", "|||", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def openemr_db_query(sql: str) -> str:
    """Query OpenEMR's MariaDB."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "-u", "openemr", "-popenemr_pass", "--default-character-set=utf8mb4",
        "-D", "openemr", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(sql: str) -> str:
    """Query OnlyOffice's MySQL."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() in ("true", "t", "1"))


def _num_eq(v, n: int) -> bool:
    try:
        return int(str(v).strip()) == n
    except (TypeError, ValueError):
        return False


def _logic_leaves(node) -> list[dict]:
    """Recursively collect condition leaves ({'value': {...}} dicts) from an
    OpnForm logic condition tree."""
    leaves: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("value"), dict) and (
            "operator" in node["value"] or "property_meta" in node["value"]
        ):
            leaves.append(node)
        for v in node.values():
            leaves.extend(_logic_leaves(v))
    elif isinstance(node, list):
        for item in node:
            leaves.extend(_logic_leaves(item))
    return leaves


def _leaf_trigger_id(leaf: dict) -> str:
    v = leaf.get("value") or {}
    pm = v.get("property_meta") or {}
    return str(leaf.get("identifier") or pm.get("id") or "")


# ── Slot values ───────────────────────────────────────────────────────────────
FORM_TITLE = "Patient Grievance Documentation and Review Form"
COMPLAINT_NOTE = "Complaint received regarding encounter on 2026-03-22. Patient reports concerns relating to clinical care quality and post-visit instructions. Investigation opened by Patient Experience; clinical record and SOAP documentation under review."
NOTE_RECIPIENT = "dr_dickinson"
OFFICE_NOTE = "Patient Complaint Investigation Initiated - Case CMP-2026-0204: Huey Connelly (PID 152). Complaint concerns encounter dated 2026-03-22 at Harbor Health Clinic. Chart review, SOAP note extraction, and provider discussion in progress. Formal response letter pending."
DOC_TITLE = "Formal Patient Complaint Response Letter - CMP-2026-0204 - Huey Connelly"
CLINIC_NAME = "Harbor Health Clinic"
COMPLAINT_REF = "CMP-2026-0204"
PATIENT_RIGHTS_SNIPPET = "You have the right to file a complaint without fear of retaliation"

PATIENT_PID = 152          # Huey Connelly (DOB 1966-11-17), fixed by description
SOAP_ENCOUNTER = 54        # seed encounter dated 2026-03-22 with the SOAP note
PATIENT_DOB = "1966-11-17"

# Custom CSS fragments compared after removing ALL whitespace (spacing-tolerant)
CSS_FRAGMENTS_NOSPACE = (
    "border:1pxsolid#004b8d",
    "border-radius:6px",
    "background-color:#f7fafc",
)

CATEGORY_OPTIONS = [
    "Wait Time", "Staff Communication", "Clinical Care Quality",
    "Billing Dispute", "Facility Conditions", "Privacy Concern",
]
DEPARTMENT_OPTIONS = ["Reception", "Laboratory", "Radiology", "Medical Records"]
URGENCY_OPTIONS = ["Immediate", "Within 48 Hours", "Standard (5 Business Days)"]

# probed seed max, mw-openemr:latest 2026-08
PNOTES_SEED_MAX = "2026-03-19 13:24:32"


# ── OpnForm form/properties access ───────────────────────────────────────────
_props_cache: list | None = None


def _get_props() -> list:
    """forms.properties JSON array for the target form (cached)."""
    global _props_cache
    if _props_cache is None:
        raw = opnform_db_query_psql(
            f"SELECT properties FROM forms WHERE title = '{FORM_TITLE}' "
            "ORDER BY id DESC LIMIT 1"
        )
        props = json.loads(raw) if raw else []
        if isinstance(props, dict):
            props = [props]
        _props_cache = props if isinstance(props, list) else []
    return _props_cache


def _find_field(props: list, name_pattern: str, ftype: str | None = None) -> dict | None:
    """First field whose name matches the regex (case-insensitive) and,
    if given, whose type equals ftype."""
    for f in props:
        if not isinstance(f, dict):
            continue
        if re.search(name_pattern, str(f.get("name", "")), re.IGNORECASE):
            if ftype is None or str(f.get("type", "")) == ftype:
                return f
    return None


def _option_names(field: dict, key: str) -> set[str]:
    opts = (field.get(key) or {}).get("options") or []
    return {str(o.get("name", "")).strip().lower() for o in opts if isinstance(o, dict)}


def _has_options(field: dict, key: str, wanted: list[str]) -> bool:
    """Superset check: all wanted option names present (case-insensitive)."""
    have = _option_names(field, key)
    return all(w.strip().lower() in have for w in wanted)


# ── OpnForm checks ───────────────────────────────────────────────────────────
def check_1_form_exists() -> None:
    """Check that the OpnForm form exists with the expected title."""
    try:
        raw = opnform_db_query_psql(
            f"SELECT id FROM forms WHERE title = '{FORM_TITLE}'"
        )
        found = bool(raw)
        check("1. OpnForm form exists with expected title", 1, found,
              f"title='{FORM_TITLE}'" if found else "form not found")
    except Exception as e:
        check("1. OpnForm form exists with expected title", 1, False, f"exception: {e}")


def check_2_form_settings() -> None:
    """Check form theme, visibility, custom CSS, progress bar, size, submit text, dark mode."""
    try:
        raw = opnform_db_query_psql(
            "SELECT theme, visibility, show_progress_bar, size, submit_button_text, dark_mode "
            f"FROM forms WHERE title = '{FORM_TITLE}' ORDER BY id DESC LIMIT 1"
        )
        if not raw:
            check("2. OpnForm form settings (theme/visibility/CSS/progress)", 2, False, "form not found")
            return
        parts = [p.strip() for p in raw.split("|||")]
        issues = []
        if len(parts) > 0 and parts[0] != "simple":
            issues.append(f"theme={parts[0]} expected=simple")
        if len(parts) > 1 and parts[1] != "public":
            issues.append(f"visibility={parts[1]} expected=public")
        if len(parts) > 2 and parts[2] not in ("1", "t", "true"):
            issues.append(f"progress_bar={parts[2]}")
        if len(parts) > 3 and parts[3] != "lg":
            issues.append(f"size={parts[3]} expected=lg")
        if len(parts) > 4 and parts[4] != "Submit Complaint Record":
            issues.append(f"submit_button_text={parts[4]}")
        if len(parts) > 5 and parts[5] not in ("light",):
            issues.append(f"dark_mode={parts[5]} expected=light")

        # custom_css in its own query (value may contain newlines); compare
        # after stripping ALL whitespace to tolerate spacing differences.
        css_raw = opnform_db_query_psql(
            f"SELECT custom_css FROM forms WHERE title = '{FORM_TITLE}' ORDER BY id DESC LIMIT 1"
        )
        css_nospace = re.sub(r"\s+", "", css_raw or "")
        for frag in CSS_FRAGMENTS_NOSPACE:
            if frag not in css_nospace:
                issues.append(f"custom CSS missing '{frag}'")

        passed = len(issues) == 0
        check("2. OpnForm form settings (theme/visibility/CSS/progress)", 2, passed,
              "; ".join(issues)[:300] if issues else "all settings correct")
    except Exception as e:
        check("2. OpnForm form settings (theme/visibility/CSS/progress)", 2, False, f"exception: {e}")


def check_3_auto_increment_field() -> None:
    """Complaint Reference Number: text, required, generates_auto_increment_id."""
    try:
        props = _get_props()
        field = _find_field(props, r"complaint.*reference", "text")
        issues = []
        if field is None:
            issues.append("no text field with name matching /complaint.*reference/i")
        else:
            if not _truthy(field.get("required")):
                issues.append("field not required")
            if not _truthy(field.get("generates_auto_increment_id")):
                issues.append("generates_auto_increment_id not enabled")
        passed = len(issues) == 0
        check("3. OpnForm auto-increment ID field", 1, passed,
              "; ".join(issues) if issues else "Complaint Reference Number text/required/auto-increment")
    except Exception as e:
        check("3. OpnForm auto-increment ID field", 1, False, f"exception: {e}")


def check_4_conditional_phone() -> None:
    """Callback Number phone field shown conditionally on the follow-up call checkbox."""
    try:
        props = _get_props()
        cb = _find_field(props, r"follow-?up call", "checkbox")
        ph = None
        for f in props:
            if (isinstance(f, dict) and str(f.get("type", "")) == "phone_number"
                    and "callback" in str(f.get("name", "")).lower()):
                ph = f
                break
        issues = []
        if cb is None:
            issues.append("no checkbox field matching /follow-?up call/i")
        if ph is None:
            issues.append("no phone_number field with 'callback' in name")
        if not issues:
            logic = ph.get("logic") or {}
            actions = logic.get("actions") or []
            if "show-block" not in actions:
                issues.append("callback field has no 'show-block' logic action (field exists but conditional logic not configured)")
            leaves = _logic_leaves(logic.get("conditions"))
            leaf_ok = any(
                _leaf_trigger_id(leaf) == str(cb.get("id"))
                and (leaf.get("value") or {}).get("operator") == "is_checked"
                for leaf in leaves
            )
            if not leaf_ok:
                issues.append("no logic condition (trigger=follow-up checkbox, operator=is_checked)")
        passed = len(issues) == 0
        check("4. OpnForm conditional phone number field", 2, passed,
              "; ".join(issues) if issues else "callback phone shown on is_checked of follow-up checkbox")
    except Exception as e:
        check("4. OpnForm conditional phone number field", 2, False, f"exception: {e}")


def check_5_conditional_urgency_justification() -> None:
    """Justification text field shown conditionally when Resolution Urgency == 'Immediate'."""
    try:
        props = _get_props()
        sel = _find_field(props, r"resolution urgency", "select")
        just = _find_field(props, r"justification", "text")
        issues = []
        if sel is None:
            issues.append("no 'Resolution Urgency' select field")
        else:
            if not _truthy(sel.get("required")):
                issues.append("Resolution Urgency not required")
            if not _has_options(sel, "select", URGENCY_OPTIONS):
                issues.append("Resolution Urgency options missing (need Immediate / Within 48 Hours / Standard (5 Business Days))")
        if just is None:
            issues.append("no text field with 'justification' in name")
        if sel is not None and just is not None:
            logic = just.get("logic") or {}
            actions = logic.get("actions") or []
            if "show-block" not in actions:
                issues.append("justification field has no 'show-block' logic action (field exists but conditional logic not configured)")
            leaves = _logic_leaves(logic.get("conditions"))
            leaf_ok = any(
                _leaf_trigger_id(leaf) == str(sel.get("id"))
                and (leaf.get("value") or {}).get("operator") == "equals"
                and str((leaf.get("value") or {}).get("value", "")).strip().lower() == "immediate"
                for leaf in leaves
            )
            if not leaf_ok:
                issues.append("no logic condition (trigger=Resolution Urgency, operator=equals, value=Immediate)")
        passed = len(issues) == 0
        check("5. OpnForm conditional urgency justification field", 2, passed,
              "; ".join(issues)[:300] if issues else "justification shown when Resolution Urgency equals Immediate")
    except Exception as e:
        check("5. OpnForm conditional urgency justification field", 2, False, f"exception: {e}")


def check_6_page_break() -> None:
    """nf-page-break with next button text 'Proceed to Staff Review Section'."""
    try:
        props = _get_props()
        found = any(
            isinstance(f, dict)
            and str(f.get("type", "")) == "nf-page-break"
            and str(f.get("next_btn_text", "")).strip() == "Proceed to Staff Review Section"
            for f in props
        )
        check("6. OpnForm page break", 1, found,
              "nf-page-break with next_btn_text='Proceed to Staff Review Section'" if found
              else "no nf-page-break field with next_btn_text 'Proceed to Staff Review Section'")
    except Exception as e:
        check("6. OpnForm page break", 1, False, f"exception: {e}")


def check_7_escalation_toggle() -> None:
    """'Escalation Required' checkbox with use_toggle_switch enabled."""
    try:
        props = _get_props()
        field = _find_field(props, r"escalation", "checkbox")
        issues = []
        if field is None:
            issues.append("no checkbox field with 'escalation' in name")
        elif not _truthy(field.get("use_toggle_switch")):
            issues.append("use_toggle_switch not enabled on escalation checkbox")
        passed = len(issues) == 0
        check("7. OpnForm escalation toggle switch", 1, passed,
              "; ".join(issues) if issues else "escalation checkbox with toggle switch enabled")
    except Exception as e:
        check("7. OpnForm escalation toggle switch", 1, False, f"exception: {e}")


def check_7b_field_specs() -> None:
    """Remaining per-field specs from the description (types/required/attributes)."""
    try:
        props = _get_props()
        issues = []
        if not props:
            issues.append("form properties empty or unreadable")
        else:
            f = _find_field(props, r"patient name", "text")
            if not (f and _truthy(f.get("required"))):
                issues.append("Patient Name (text, required)")
            f = _find_field(props, r"date of incident", "date")
            if not (f and _truthy(f.get("required")) and _truthy(f.get("disable_future_dates"))):
                issues.append("Date of Incident (date, required, disable_future_dates)")
            f = _find_field(props, r"complaint category", "select")
            if not (f and _truthy(f.get("required")) and _has_options(f, "select", CATEGORY_OPTIONS)):
                issues.append("Complaint Category (select, required, 6 options)")
            f = _find_field(props, r"severity rating", "scale")
            if not (f and _truthy(f.get("required")) and _num_eq(f.get("scale_max_value"), 10)
                    and (f.get("scale_min_value") is None or _num_eq(f.get("scale_min_value"), 1))
                    and (f.get("scale_step_value") is None or _num_eq(f.get("scale_step_value"), 1))):
                issues.append("Severity Rating (scale, required, max=10, min/step 1 or default)")
            f = _find_field(props, r"detailed description", "rich_text")
            if not (f and _truthy(f.get("required"))):
                issues.append("Detailed Description (rich_text, required)")
            f = _find_field(props, r"departments involved", "multi_select")
            if not (f and _truthy(f.get("required")) and _truthy(f.get("allow_creation"))
                    and _has_options(f, "multi_select", DEPARTMENT_OPTIONS)):
                issues.append("Departments Involved (multi_select, required, allow_creation, 4 options)")
            f = _find_field(props, r"reviewing staff name", "text")
            if not (f and _truthy(f.get("required"))):
                issues.append("Reviewing Staff Name (text, required)")
            f = _find_field(props, r"review date", "date")
            if not (f and _truthy(f.get("required")) and _truthy(f.get("prefill_today"))):
                issues.append("Review Date (date, required, prefill_today)")
            f = _find_field(props, r"assessment of merit", "rating")
            if not (f and (f.get("rating_max_value") is None or _num_eq(f.get("rating_max_value"), 5))):
                issues.append("Initial Assessment of Merit (rating, max 5 or default)")
            if not any(isinstance(fx, dict) and str(fx.get("type", "")) == "signature" for fx in props):
                issues.append("signature field present")
        passed = len(issues) == 0
        check("7b. OpnForm remaining field specs", 2, passed,
              ("missing/incorrect: " + "; ".join(issues))[:300] if issues else "all field specs correct")
    except Exception as e:
        check("7b. OpnForm remaining field specs", 2, False, f"exception: {e}")


# ── OpenEMR checks ───────────────────────────────────────────────────────────
def check_8_patient_note() -> None:
    """Patient note with complaint text sent to dr_dickinson on Huey Connelly's chart (pid 152)."""
    try:
        sql = (
            "SELECT id, body, assigned_to FROM pnotes "
            f"WHERE pid = {PATIENT_PID} "
            f"AND date > '{PNOTES_SEED_MAX}' "
            "AND body LIKE '%Complaint received regarding encounter on 2026-03-22%' "
            "LIMIT 1"
        )
        raw = openemr_db_query(sql)
        if not raw:
            check("8. OpenEMR patient note (complaint)", 2, False,
                  f"no new patient note (date > {PNOTES_SEED_MAX}) with complaint text for pid {PATIENT_PID}")
            return

        parts = raw.split("\t")
        body = parts[1] if len(parts) > 1 else ""
        assigned = parts[2] if len(parts) > 2 else ""

        issues = []
        if "clinical care quality" not in body.lower():
            issues.append("note body missing key phrases")
        if NOTE_RECIPIENT not in assigned.lower():
            issues.append(f"assigned_to='{assigned}' expected to contain '{NOTE_RECIPIENT}'")

        passed = len(issues) == 0
        check("8. OpenEMR patient note (complaint)", 2, passed,
              "; ".join(issues) if issues else "note found with correct body and recipient")
    except Exception as e:
        check("8. OpenEMR patient note (complaint)", 2, False, f"exception: {e}")


def check_9_office_note() -> None:
    """Office note with investigation initiation text."""
    try:
        sql = (
            "SELECT id, body FROM onotes "
            "WHERE body LIKE '%Patient Complaint Investigation Initiated%' "
            "AND body LIKE '%CMP-2026-0204%' "
            "LIMIT 1"
        )
        raw = openemr_db_query(sql)
        if not raw:
            check("9. OpenEMR office note (investigation)", 2, False, "no matching office note found")
            return

        parts = raw.split("\t")
        body = parts[1] if len(parts) > 1 else raw

        issues = []
        if "Huey Connelly" not in body:
            issues.append("missing patient name")
        if "PID 152" not in body and "pid 152" not in body.lower():
            issues.append("missing PID 152")
        if "Harbor Health Clinic" not in body:
            issues.append("missing clinic name")

        passed = len(issues) == 0
        check("9. OpenEMR office note (investigation)", 2, passed,
              "; ".join(issues) if issues else "office note found with correct content")
    except Exception as e:
        check("9. OpenEMR office note (investigation)", 2, False, f"exception: {e}")


# ── OnlyOffice document retrieval (fs-first, API fallback) ───────────────────
def _oo_find_file_id(like_patterns: list[str]) -> tuple[str, str] | None:
    """Locate a files_file row by trying LIKE patterns in order. Excludes
    editor crash-recovery copies. Returns (id, title) or None."""
    for pat in like_patterns:
        row = onlyoffice_sql(
            "SELECT id, title FROM files_file "
            f"WHERE title LIKE '{pat}' AND title NOT LIKE '%Recovery%' "
            "ORDER BY id DESC LIMIT 1;"
        )
        if row:
            parts = row.split("\t")
            if len(parts) >= 2:
                return parts[0].strip(), parts[1].strip()
    return None


def _oo_bytes_from_fs(file_id: str, retries: int = 3, delay: float = 5.0) -> bytes | None:
    """Read content.docx for a file id from the portal data dir (docker exec).
    Retries to tolerate OnlyOffice save/conversion delay."""
    for attempt in range(retries):
        rc, out, _ = docker_exec(
            ONLYOFFICE_CONTAINER, "bash", "-c",
            f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.*' "
            "2>/dev/null | sort -V | tail -1",
            timeout=25,
        )
        path = out.strip().splitlines()[0].strip() if out.strip() else ""
        if path:
            r = subprocess.run(
                ["docker", "exec", ONLYOFFICE_CONTAINER, "cat", path],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _oo_auth_session():
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    s = requests.Session()
    try:
        resp = s.post(f"{base_url}/api/2.0/authentication",
                      json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
                      timeout=15)
        if resp.status_code not in (200, 201):
            return None
        token = resp.json().get("response", {}).get("token", "")
        if not token:
            return None
        s.headers.update({"Authorization": f"Bearer {token}"})
        s.cookies.set("asc_auth_key", token)
        s.base_url = base_url  # type: ignore[attr-defined]
        return s
    except Exception:
        return None


def _oo_bytes_from_api(file_id: str) -> bytes | None:
    s = _oo_auth_session()
    if not s:
        return None
    base = s.base_url  # type: ignore[attr-defined]
    for url, params in (
        (f"{base}/products/files/httphandlers/filehandler.ashx",
         {"action": "download", "fileid": str(file_id)}),
        (f"{base}/api/2.0/files/file/{file_id}/download", None),
    ):
        try:
            r = s.get(url, params=params, timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100:
                return r.content
        except Exception:
            continue
    return None


def _oo_get_document(like_patterns: list[str]) -> tuple[bytes | None, str]:
    """(bytes|None, source detail). fs first, API fallback."""
    try:
        found = _oo_find_file_id(like_patterns)
    except Exception as e:
        return None, f"files_file lookup failed: {e}"
    if not found:
        return None, "document not found in files_file"
    file_id, title = found
    data = _oo_bytes_from_fs(file_id)
    if data:
        return data, f"fs id={file_id} title={title[:60]}"
    data = _oo_bytes_from_api(file_id)
    if data:
        return data, f"api id={file_id} title={title[:60]}"
    return None, f"id={file_id} found but content unreadable (fs+api)"


# ── docx parsing helpers ─────────────────────────────────────────────────────
def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        s = s.replace(a, b)
    return s


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _docx_text(data: bytes) -> str:
    """Plain text of word/document.xml. <w:t> runs inside one paragraph are
    concatenated with NO separator; paragraphs joined with newlines."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in xml.split("</w:p>"):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, flags=re.DOTALL)
        if runs:
            paras.append(_xml_unescape("".join(runs)))
    return "\n".join(paras)


_DOC_LIKE_PATTERNS = [
    f"{DOC_TITLE}%",
    "Formal Patient Complaint Response Letter - CMP-2026-0204%",
]

_doc_text_cache: tuple[str | None, str] | None = None


def _get_doc_text() -> tuple[str | None, str]:
    """(normalized letter text | None, source/failure detail); cached."""
    global _doc_text_cache
    if _doc_text_cache is None:
        data, src = _oo_get_document(_DOC_LIKE_PATTERNS)
        if data is None:
            _doc_text_cache = (None, src)
        else:
            try:
                _doc_text_cache = (_norm_text(_docx_text(data)), src)
            except Exception as e:
                _doc_text_cache = (None, f"docx parse failed: {e}")
    return _doc_text_cache


# ── OnlyOffice checks ────────────────────────────────────────────────────────
def check_10_doc_exists() -> None:
    """Gate (0pt): OnlyOffice document exists with the exact title (suffix tolerated)."""
    try:
        found = _oo_find_file_id([f"{DOC_TITLE}%"])
        check("10. OnlyOffice document exists (gate)", 0, found is not None,
              f"id={found[0]} title={found[1][:60]}" if found
              else f"no files_file row titled '{DOC_TITLE}' (Recovery copies excluded); checks 11-16b depend on this")
    except Exception as e:
        check("10. OnlyOffice document exists (gate)", 0, False, f"exception: {e}")


def check_11_doc_clinic_and_ref() -> None:
    """Letter contains clinic name and complaint reference."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("11. OnlyOffice clinic name & complaint reference", 1, False, f"document gate failed: {src}")
            return
        issues = []
        if CLINIC_NAME.lower() not in text:
            issues.append(f"missing '{CLINIC_NAME}'")
        if COMPLAINT_REF.lower() not in text:
            issues.append(f"missing '{COMPLAINT_REF}'")
        passed = len(issues) == 0
        check("11. OnlyOffice clinic name & complaint reference", 1, passed,
              "; ".join(issues) if issues else f"clinic name and reference found ({src})")
    except Exception as e:
        check("11. OnlyOffice clinic name & complaint reference", 1, False, f"exception: {e}")


def check_12_doc_complaint_summary() -> None:
    """Letter contains complaint summary: clarity phrase, severity, callback, incident date."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("12. OnlyOffice complaint summary", 2, False, f"document gate failed: {src}")
            return
        has_clarity = "clarity of clinical explanations" in text
        has_severity = "5/10" in text or "5 / 10" in text or "rated at 5" in text
        has_callback = "callback" in text
        has_date = "2026-03-22" in text
        missing = []
        if not has_clarity:
            missing.append("'clarity of clinical explanations'")
        if not has_severity:
            missing.append("severity rating '5/10'")
        if not has_callback:
            missing.append("'callback'")
        if not has_date:
            missing.append("incident date '2026-03-22'")
        passed = has_clarity and has_severity and has_callback and has_date
        check("12. OnlyOffice complaint summary", 2, passed,
              "missing: " + ", ".join(missing) if missing else "complaint summary present")
    except Exception as e:
        check("12. OnlyOffice complaint summary", 2, False, f"exception: {e}")


def check_13_doc_investigation_outcome() -> None:
    """Letter contains the full investigation outcome determination."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("13. OnlyOffice investigation outcome", 2, False, f"document gate failed: {src}")
            return
        has_substantiated = "complaint partially substantiated" in text
        has_inadequate = "patient-education documentation inadequate" in text
        missing = []
        if not has_substantiated:
            missing.append("'complaint partially substantiated'")
        if not has_inadequate:
            missing.append("'patient-education documentation inadequate'")
        passed = has_substantiated and has_inadequate
        check("13. OnlyOffice investigation outcome", 2, passed,
              "missing: " + ", ".join(missing) if missing else "investigation outcome found")
    except Exception as e:
        check("13. OnlyOffice investigation outcome", 2, False, f"exception: {e}")


def check_14_doc_corrective_actions() -> None:
    """Letter contains all three corrective actions."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("14. OnlyOffice corrective actions (3)", 2, False, f"document gate failed: {src}")
            return
        found_actions = 0
        missing = []
        if "after-visit summary" in text or "after visit summary" in text:
            found_actions += 1
        else:
            missing.append("action 1 (after-visit summary templates)")
        if "teach-back" in text or "teach back" in text:
            found_actions += 1
        else:
            missing.append("action 2 (teach-back methodology)")
        if "satisfaction survey" in text and "clinical quality committee" in text:
            found_actions += 1
        else:
            missing.append("action 3 (satisfaction survey + Clinical Quality Committee)")
        passed = found_actions == 3
        check("14. OnlyOffice corrective actions (3)", 2, passed,
              f"{found_actions}/3 found" + (f"; missing: {', '.join(missing)}" if missing else ""))
    except Exception as e:
        check("14. OnlyOffice corrective actions (3)", 2, False, f"exception: {e}")


def check_15_doc_patient_rights() -> None:
    """Letter contains patient rights text."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("15. OnlyOffice patient rights text", 1, False, f"document gate failed: {src}")
            return
        found = _norm_text(PATIENT_RIGHTS_SNIPPET) in text
        check("15. OnlyOffice patient rights text", 1, found,
              "patient rights text found" if found else "missing patient rights text")
    except Exception as e:
        check("15. OnlyOffice patient rights text", 1, False, f"exception: {e}")


def check_16_doc_signature_block() -> None:
    """Letter contains signature block for Michael Delacroix, MBA, CPXP / Patient Experience Manager."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("16. OnlyOffice signature block", 1, False, f"document gate failed: {src}")
            return
        missing = [p for p in ("michael delacroix", "mba", "cpxp", "patient experience manager")
                   if p not in text]
        passed = len(missing) == 0
        check("16. OnlyOffice signature block", 1, passed,
              "missing: " + ", ".join(missing) if missing else "signature block found")
    except Exception as e:
        check("16. OnlyOffice signature block", 1, False, f"exception: {e}")


def check_16b_clinical_review_findings() -> None:
    """Clinical Review Findings: letter reflects the live SOAP of pid 152 encounter 54 + patient DOB."""
    try:
        text, src = _get_doc_text()
        if text is None:
            check("16b. OnlyOffice clinical review findings (SOAP + DOB)", 2, False,
                  f"document gate failed: {src}")
            return

        # Live truth: SOAP note of encounter 54 (form_soap has no encounter
        # column -> anchor through the forms registry).
        sql = (
            "SELECT s.subjective, s.assessment FROM form_soap s "
            "JOIN forms f ON f.form_id = s.id AND f.formdir = 'soap' AND f.deleted = 0 "
            f"WHERE f.pid = {PATIENT_PID} AND f.encounter = {SOAP_ENCOUNTER}"
        )
        soap_raw = openemr_db_query(sql)
        if not soap_raw:
            check("16b. OnlyOffice clinical review findings (SOAP + DOB)", 2, False,
                  f"no SOAP row for pid {PATIENT_PID} encounter {SOAP_ENCOUNTER}")
            return
        soap = _norm_text(soap_raw)
        soap_ok = ("sore throat and nasal congestion" in soap
                   and "upper respiratory infection" in soap)

        dob_variants = [
            "1966-11-17", "11/17/1966", "17/11/1966", "11-17-1966", "17-11-1966",
            "november 17, 1966", "november 17 1966", "17 november 1966",
            "nov 17, 1966", "nov 17 1966", "17 nov 1966", "11.17.1966", "17.11.1966",
        ]
        has_dob = any(v in text for v in dob_variants)

        missing = []
        if not soap_ok:
            missing.append(f"live SOAP does not match expected seed content (got: {soap[:120]})")
        if "sore throat and nasal congestion" not in text:
            missing.append("letter missing subjective probe 'sore throat and nasal congestion'")
        if "upper respiratory infection" not in text:
            missing.append("letter missing assessment probe 'upper respiratory infection'")
        if not has_dob:
            missing.append(f"letter missing patient DOB '{PATIENT_DOB}' (any common format)")

        passed = len(missing) == 0
        check("16b. OnlyOffice clinical review findings (SOAP + DOB)", 2, passed,
              "; ".join(missing)[:300] if missing else "SOAP extraction and DOB present in letter")
    except Exception as e:
        check("16b. OnlyOffice clinical review findings (SOAP + DOB)", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_form_exists()
    check_2_form_settings()
    check_3_auto_increment_field()
    check_4_conditional_phone()
    check_5_conditional_urgency_justification()
    check_6_page_break()
    check_7_escalation_toggle()
    check_7b_field_specs()
    check_8_patient_note()
    check_9_office_note()
    check_10_doc_exists()
    check_11_doc_clinic_and_ref()
    check_12_doc_complaint_summary()
    check_13_doc_investigation_outcome()
    check_14_doc_corrective_actions()
    check_15_doc_patient_rights()
    check_16_doc_signature_block()
    check_16b_clinical_review_findings()

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
