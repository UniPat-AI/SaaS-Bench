"""
Verifier for Healthcare-025-I5: Reportable Disease Workflow - Suspected Mumps Case

Checks: 19 checks (2 zero-weight gates), total weight 25, across openemr,
opnform, onlyoffice.
Strategy: docker exec (DB queries) for all three sites; OnlyOffice document
content read from the container filesystem (API fallback).

Required env vars:
  SERVER_HOSTNAME, OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  OPNFORM_PORT, OPNFORM_CONTAINER, ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER,
  ONLYOFFICE_DB_CONTAINER
"""

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

OPNFORM_PORT = os.environ.get("OPNFORM_PORT")
OPNFORM_CONTAINER = os.environ.get("OPNFORM_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

_required = {
    "OPENEMR_PORT": OPENEMR_PORT,
    "OPENEMR_CONTAINER": OPENEMR_CONTAINER,
    "OPENEMR_DB_CONTAINER": OPENEMR_DB_CONTAINER,
    "OPNFORM_PORT": OPNFORM_PORT,
    "OPNFORM_CONTAINER": OPNFORM_CONTAINER,
    "ONLYOFFICE_PORT": ONLYOFFICE_PORT,
    "ONLYOFFICE_CONTAINER": ONLYOFFICE_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": ONLYOFFICE_DB_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

# ── Date anchors ──────────────────────────────────────────────────────────────
# probed seed max, mw-openemr:latest 2026-08
FORM_ENCOUNTER_SEED_MAX = "2026-03-05"    # form_encounter MAX(date) (datetime 00:00:00)
PNOTES_SEED_MAX = "2026-03-19 13:24:32"   # pnotes MAX(date)

GATE_FAIL = "gate failed: patient/anchored encounter not resolved (check 1)"


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


def _oneline(s: str, limit: int = 120) -> str:
    return " ".join((s or "").split())[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def openemr_sql(query: str) -> str:
    """Query OpenEMR MariaDB. Raises on non-zero exit."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def opnform_sql(query: str) -> str:
    """Query OpnForm Postgres (embedded in app container). Raises on non-zero exit."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge",
        "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Query OnlyOffice MySQL. Raises on non-zero exit."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() in ("true", "t", "1"))


# ── Patient / encounter cache ─────────────────────────────────────────────────
_pid_cache: str | None = None
_enc_cache: str | None = None


def get_pid() -> str:
    """Canonical pid: DOB-disambiguated (seed has a NULL-DOB duplicate, pid 338)."""
    global _pid_cache
    if _pid_cache is None:
        out = openemr_sql(
            "SELECT pid FROM patient_data "
            "WHERE fname='Freeda' AND lname='Stamm' AND DOB='1986-01-12'"
        )
        rows = [r.strip() for r in out.splitlines() if r.strip()]
        _pid_cache = rows[0] if len(rows) == 1 else ""
    return _pid_cache


def get_encounter() -> str:
    """Newest encounter dated strictly after the seed maximum (i.e. agent-created)."""
    global _enc_cache
    if _enc_cache is None:
        pid = get_pid()
        if pid:
            _enc_cache = openemr_sql(
                f"SELECT encounter FROM form_encounter WHERE pid={pid} "
                f"AND date > '{FORM_ENCOUNTER_SEED_MAX}' "
                f"ORDER BY date DESC, encounter DESC LIMIT 1"
            )
        else:
            _enc_cache = ""
    return _enc_cache


# ── OpenEMR checks ────────────────────────────────────────────────────────────

def check_1_encounter() -> None:
    """Gate (0pt): patient resolved by name+DOB and a NEW anchored encounter exists."""
    label = "1. Gate: new encounter for Freeda Stamm"
    try:
        pid = get_pid()
        if not pid:
            check(label, 0, False,
                  "patient Freeda Stamm DOB 1986-01-12 not uniquely resolved")
            return
        enc = get_encounter()
        check(label, 0, bool(enc),
              f"pid={pid} encounter={enc}" if enc
              else f"pid={pid}, no encounter dated > {FORM_ENCOUNTER_SEED_MAX}")
    except Exception as e:
        check(label, 0, False, f"exception: {e}")


def check_2_soap_subjective_objective() -> None:
    """SOAP Subjective (parotid swelling + nephew exposure) and Objective vitals."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("2. SOAP Subjective+Objective", 2, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT fs.subjective, fs.objective FROM form_soap fs "
            f"INNER JOIN forms f ON f.form_id=fs.id AND f.formdir='soap' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY fs.id DESC LIMIT 1"
        )
        if not result:
            check("2. SOAP Subjective+Objective", 2, False, "no SOAP form on anchored encounter")
            return
        low = result.lower()
        s_ok = "bilateral parotid swelling" in low
        nephew_ok = "nephew with confirmed mumps 16 days ago" in low
        o_ok = "102.3" in result and "parotid" in low
        check("2. SOAP Subjective+Objective", 2, s_ok and nephew_ok and o_ok,
              f"subj={'ok' if s_ok else 'missing'}, "
              f"nephew_exposure={'ok' if nephew_ok else 'missing'}, "
              f"obj={'ok' if o_ok else 'missing'}")
    except Exception as e:
        check("2. SOAP Subjective+Objective", 2, False, f"exception: {e}")


def check_3_soap_assessment_plan() -> None:
    """SOAP Assessment (mumps/parotitis) and Plan (droplet, MA DPH, contact tracing)."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("3. SOAP Assessment+Plan", 2, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT fs.assessment, fs.plan FROM form_soap fs "
            f"INNER JOIN forms f ON f.form_id=fs.id AND f.formdir='soap' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY fs.id DESC LIMIT 1"
        )
        if not result:
            check("3. SOAP Assessment+Plan", 2, False, "no SOAP form on anchored encounter")
            return
        low = result.lower()
        a_ok = "mumps" in low and "parotitis" in low
        p_ok = "droplet precautions" in low
        dph_ok = "ma dph" in low
        ct_ok = "contact tracing" in low
        check("3. SOAP Assessment+Plan", 2, a_ok and p_ok and dph_ok and ct_ok,
              f"assess={'ok' if a_ok else 'missing'}, plan_droplet={'ok' if p_ok else 'missing'}, "
              f"ma_dph={'ok' if dph_ok else 'missing'}, contact_tracing={'ok' if ct_ok else 'missing'}")
    except Exception as e:
        check("3. SOAP Assessment+Plan", 2, False, f"exception: {e}")


def check_4_ros_findings() -> None:
    """ROS form has constitutional (fever) and respiratory (cough) findings (same row)."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("4. ROS constitutional+respiratory", 2, False, GATE_FAIL)
            return
        # Select the named columns explicitly: mysql -N output contains only
        # values (YES/NO), never column names.
        result = openemr_sql(
            f"SELECT fr.fever, fr.cough FROM form_ros fr "
            f"INNER JOIN forms f ON f.form_id=fr.id AND f.formdir='ros' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY fr.id DESC LIMIT 1"
        )
        if not result:
            check("4. ROS constitutional+respiratory", 2, False, "no ROS form on anchored encounter")
            return
        parts = [p.strip().lower() for p in result.split("\t")]
        fever_ok = len(parts) > 0 and parts[0] == "yes"
        cough_ok = len(parts) > 1 and parts[1] == "yes"
        # Nausea: negation in the description is ambiguous ("mild nausea, no
        # vomiting") — informational only, not gated.
        nausea = "n/a"
        try:
            nausea = _oneline(openemr_sql(
                f"SELECT fr.nausea FROM form_ros fr "
                f"INNER JOIN forms f ON f.form_id=fr.id AND f.formdir='ros' AND f.deleted=0 "
                f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY fr.id DESC LIMIT 1"
            ), 20) or "empty"
        except Exception:
            nausea = "query error"
        check("4. ROS constitutional+respiratory", 2, fever_ok and cough_ok,
              f"fever={'found' if fever_ok else 'not found'}, "
              f"cough={'found' if cough_ok else 'not found'}, info: nausea={nausea}")
    except Exception as e:
        check("4. ROS constitutional+respiratory", 2, False, f"exception: {e}")


def check_5_physical_exam() -> None:
    """One physical-exam form on the anchored encounter mentions parotid AND Stensen."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("5. Physical Exam ENT findings", 2, False, GATE_FAIL)
            return
        # No standard form_physical_exam table — search this encounter's forms;
        # the SAME form's text must contain both probes.
        forms_data = openemr_sql(
            f"SELECT formdir, form_id FROM forms "
            f"WHERE pid={pid} AND encounter={enc} "
            f"AND formdir NOT IN ('soap','ros','newpatient','vitals') "
            f"AND deleted=0"
        )
        found_dir = ""
        for line in (forms_data or "").split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            formdir, form_id = parts[0], parts[1]
            try:
                data = openemr_sql(f"SELECT * FROM `form_{formdir}` WHERE id={form_id}")
            except Exception:
                continue
            low = (data or "").lower()
            if data and "parotid" in low and "stensen" in low:
                found_dir = formdir
                break
        check("5. Physical Exam ENT findings", 2, bool(found_dir),
              f"parotid+Stensen found in form_{found_dir}" if found_dir
              else "no single encounter form contains both 'parotid' and 'stensen'")
    except Exception as e:
        check("5. Physical Exam ENT findings", 2, False, f"exception: {e}")


def check_6_problem_mumps() -> None:
    """Active medical problem: title Mumps AND ICD-10 B26.9 on the same row."""
    try:
        pid = get_pid()
        if not pid:
            check("6. Medical problem Mumps B26.9", 1, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT title, diagnosis FROM lists "
            f"WHERE pid={pid} AND type='medical_problem' AND activity=1 "
            f"AND diagnosis LIKE '%B26.9%' AND LOWER(title) LIKE '%mumps%'"
        )
        check("6. Medical problem Mumps B26.9", 1, bool(result),
              f"found: {_oneline(result, 80)}" if result else "not found")
    except Exception as e:
        check("6. Medical problem Mumps B26.9", 1, False, f"exception: {e}")


def check_7_problem_sialadenitis() -> None:
    """Active medical problem: title Sialadenitis AND ICD-10 K11.20 on the same row."""
    try:
        pid = get_pid()
        if not pid:
            check("7. Medical problem Sialadenitis K11.20", 1, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT title, diagnosis FROM lists "
            f"WHERE pid={pid} AND type='medical_problem' AND activity=1 "
            f"AND diagnosis LIKE '%K11.20%' AND LOWER(title) LIKE '%sialadenitis%'"
        )
        check("7. Medical problem Sialadenitis K11.20", 1, bool(result),
              f"found: {_oneline(result, 80)}" if result else "not found")
    except Exception as e:
        check("7. Medical problem Sialadenitis K11.20", 1, False, f"exception: {e}")


def check_7b_issue_encounter_link() -> None:
    """Mumps problem (B26.9) is linked to the anchored encounter via issue_encounter."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("7b. Mumps problem linked to encounter", 1, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT 1 FROM issue_encounter ie JOIN lists l ON l.id=ie.list_id "
            f"WHERE ie.pid={pid} AND ie.encounter={enc} AND l.diagnosis LIKE '%B26.9%'"
        )
        check("7b. Mumps problem linked to encounter", 1, bool(result),
              f"issue_encounter link found for enc={enc}" if result
              else f"no issue_encounter row linking a B26.9 issue to encounter {enc}")
    except Exception as e:
        check("7b. Mumps problem linked to encounter", 1, False, f"exception: {e}")


def check_8_procedure_order() -> None:
    """Single order row: mumps procedure, URGENT, Riverside lab, clinical notes."""
    try:
        pid = get_pid()
        if not pid:
            check("8. Procedure order mumps serology URGENT", 2, False, GATE_FAIL)
            return
        # clinical_hx is varchar(255): assert a <=60-char prefix to dodge truncation.
        result = openemr_sql(
            f"SELECT po.procedure_order_id FROM procedure_order po "
            f"JOIN procedure_order_code poc "
            f"  ON poc.procedure_order_id=po.procedure_order_id "
            f"JOIN procedure_providers pp ON pp.ppid=po.lab_id "
            f"WHERE po.patient_id={pid} "
            f"AND po.date_ordered >= CURDATE() - INTERVAL 1 DAY "
            f"AND UPPER(po.order_priority)='URGENT' "
            f"AND LOWER(poc.procedure_name) LIKE '%mumps%' "
            f"AND pp.name='Riverside Medical Center' "
            f"AND po.clinical_hx LIKE '%Suspected acute mumps infection with bilateral parotitis%'"
        )
        check("8. Procedure order mumps serology URGENT", 2, bool(result),
              f"order id={_oneline(result, 40)}" if result
              else "no order matching mumps+URGENT+Riverside+clinical notes on one row")
    except Exception as e:
        check("8. Procedure order mumps serology URGENT", 2, False, f"exception: {e}")


def check_9_billing_codes() -> None:
    """Fee Sheet contains ICD-10 B26.9, K11.20 and CPT 99214 on the anchored encounter."""
    try:
        pid, enc = get_pid(), get_encounter()
        if not pid or not enc:
            check("9. Fee Sheet billing codes", 2, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT code_type, code FROM billing "
            f"WHERE pid={pid} AND encounter={enc} AND activity=1"
        )
        has_b269 = "B26.9" in (result or "")
        has_k1120 = "K11.20" in (result or "")
        has_99214 = "99214" in (result or "")
        passed = has_b269 and has_k1120 and has_99214
        found = [c for c, ok in [("B26.9", has_b269), ("K11.20", has_k1120), ("99214", has_99214)] if ok]
        missing = [c for c, ok in [("B26.9", has_b269), ("K11.20", has_k1120), ("99214", has_99214)] if not ok]
        detail = f"found={','.join(found) or 'none'}"
        if missing:
            detail += f", missing={','.join(missing)}"
        check("9. Fee Sheet billing codes", 2, passed, detail)
    except Exception as e:
        check("9. Fee Sheet billing codes", 2, False, f"exception: {e}")


def check_10_message() -> None:
    """Single pnotes row: title, assigned_to dr_hartmann, and body content."""
    try:
        pid = get_pid()
        if not pid:
            check("10. Message to dr_hartmann", 2, False, GATE_FAIL)
            return
        result = openemr_sql(
            f"SELECT id FROM pnotes "
            f"WHERE pid={pid} AND deleted=0 "
            f"AND date > '{PNOTES_SEED_MAX}' "
            f"AND title LIKE '%URGENT: Suspected Mumps Case%' "
            f"AND assigned_to='dr_hartmann' "
            f"AND body LIKE '%bilateral parotitis%' "
            f"AND body LIKE '%MMR vaccination status verification%'"
        )
        check("10. Message to dr_hartmann", 2, bool(result),
              f"pnote id={_oneline(result, 40)}" if result
              else "no new pnote matching title+assigned_to+body on one row")
    except Exception as e:
        check("10. Message to dr_hartmann", 2, False, f"exception: {e}")


def check_11_flow_board_status() -> None:
    """Flow Board status option exactly 'In Exam Room - Droplet Precautions', assigned."""
    try:
        pid = get_pid()
        if not pid:
            check("11. Flow Board Droplet Precautions", 1, False, GATE_FAIL)
            return
        status_rows = openemr_sql(
            "SELECT option_id, title FROM list_options "
            "WHERE list_id='apptstat' AND title='In Exam Room - Droplet Precautions'"
        )
        if not status_rows:
            check("11. Flow Board Droplet Precautions", 1, False,
                  "no apptstat option titled exactly 'In Exam Room - Droplet Precautions'")
            return
        assigned_id = ""
        for line in status_rows.splitlines():
            option_id = line.split("\t")[0].strip()
            if not option_id:
                continue
            tracker = openemr_sql(
                f"SELECT pte.status FROM patient_tracker pt "
                f"JOIN patient_tracker_element pte ON pt.id=pte.pt_tracker_id "
                f"WHERE pt.pid={pid} AND pte.status='{option_id}' "
                f"ORDER BY pte.start_datetime DESC LIMIT 1"
            )
            if tracker:
                assigned_id = option_id
                break
        check("11. Flow Board Droplet Precautions", 1, bool(assigned_id),
              f"status '{assigned_id}' assigned" if assigned_id
              else "option exists but not assigned to patient in tracker")
    except Exception as e:
        check("11. Flow Board Droplet Precautions", 1, False, f"exception: {e}")


# ── OpnForm checks ────────────────────────────────────────────────────────────

_fid_cache: str | None = None
_props_cache: list | None = None
_props_loaded = False


def get_form_id() -> str:
    global _fid_cache
    if _fid_cache is None:
        _fid_cache = opnform_sql(
            "SELECT id FROM forms "
            "WHERE (title LIKE '%Massachusetts Mumps%' "
            "   OR title LIKE '%Mumps%Parotitis%Surveillance%') "
            "AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1"
        )
    return _fid_cache


def get_form_properties() -> list | None:
    """forms.properties parsed as JSON array (cached)."""
    global _props_cache, _props_loaded
    if not _props_loaded:
        _props_loaded = True
        fid = get_form_id()
        if fid:
            raw = opnform_sql(f"SELECT properties FROM forms WHERE id={fid}")
            props = json.loads(raw) if raw else None
            _props_cache = props if isinstance(props, list) else None
    return _props_cache


def _find_field(props: list, name: str) -> dict | None:
    for f in props:
        if isinstance(f, dict) and str(f.get("name", "")).strip().lower() == name.lower():
            return f
    return None


def _option_names(field: dict, type_key: str) -> set[str]:
    opts = ((field.get(type_key) or {}).get("options")) or []
    return {str(o.get("name", "")).strip().lower() for o in opts if isinstance(o, dict)}


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


def check_12_opnform_form() -> None:
    """Form exists with correct title, visibility, theme, color, style, submit text."""
    try:
        fid = get_form_id()
        if not fid:
            check("12. OpnForm form exists (public, styled)", 2, False, "form not found")
            return
        row = opnform_sql(
            "SELECT title, visibility, theme, color, submit_button_text, "
            "show_progress_bar, presentation_style "
            f"FROM forms WHERE id={fid}"
        )
        parts = row.split("|")
        if len(parts) < 7:
            check("12. OpnForm form exists (public, styled)", 2, False,
                  f"unexpected row: {_oneline(row, 80)}")
            return
        title, visibility, theme, color, submit_text, progress, pres = \
            [p.strip() for p in parts[:7]]

        title_ok = "mumps" in title.lower() and "surveillance" in title.lower()
        vis_ok = visibility.lower() == "public"
        theme_ok = theme.lower() == "simple"
        color_ok = color.upper() == "#2563EB"
        submit_ok = submit_text == "Submit Notification"
        progress_ok = _truthy(progress)
        pres_ok = pres.lower() == "classic"

        issues = []
        if not title_ok:
            issues.append("title mismatch")
        if not vis_ok:
            issues.append(f"visibility={visibility}")
        if not theme_ok:
            issues.append(f"theme={theme}")
        if not color_ok:
            issues.append(f"color={color}")
        if not submit_ok:
            issues.append(f"submit_text={submit_text[:30]}")
        if not progress_ok:
            issues.append(f"progress_bar={progress}")
        if not pres_ok:
            issues.append(f"presentation_style={pres}")

        passed = (title_ok and vis_ok and theme_ok and color_ok
                  and submit_ok and progress_ok and pres_ok)
        check("12. OpnForm form exists (public, styled)", 2, passed,
              "all ok" if not issues else _oneline(", ".join(issues), 160))
    except Exception as e:
        check("12. OpnForm form exists (public, styled)", 2, False, f"exception: {e}")


def check_13a_opnform_core_fields() -> None:
    """Per-field assertions on the 7 core reporting fields."""
    label = "13a. OpnForm core fields"
    try:
        props = get_form_properties()
        if props is None:
            check(label, 1, False, "form not found or properties not a JSON array")
            return
        issues: list[str] = []

        def req(name: str, ftype: str) -> dict | None:
            f = _find_field(props, name)
            if not f:
                issues.append(f"{name}: missing")
                return None
            if str(f.get("type")) != ftype:
                issues.append(f"{name}: type={f.get('type')}")
            if not _truthy(f.get("required")):
                issues.append(f"{name}: not required")
            return f

        req("Reporting Clinician Name", "text")

        f = req("Reporting Date", "date")
        if f and not _truthy(f.get("prefill_today")):
            issues.append("Reporting Date: prefill_today not set")

        f = req("Patient Initials", "text")
        if f:
            if str(f.get("max_char_limit")) != "5":
                issues.append(f"Patient Initials: max_char_limit={f.get('max_char_limit')}")
            if not _truthy(f.get("show_char_limit")):
                issues.append("Patient Initials: show_char_limit not set")

        req("Date of Birth", "date")

        f = req("Reportable Condition", "select")
        if f:
            want = {"mumps", "measles (rubeola)", "rubella", "varicella"}
            have = _option_names(f, "select")
            if not want <= have:
                issues.append(f"Reportable Condition: options missing {sorted(want - have)}")

        f = req("Date of Symptom Onset", "date")
        if f and not _truthy(f.get("disable_future_dates")):
            issues.append("Date of Symptom Onset: disable_future_dates not set")

        f = req("Lab Confirmation Status", "select")
        if f:
            want = {"confirmed", "probable", "suspected", "pending"}
            have = _option_names(f, "select")
            if not want <= have:
                issues.append(f"Lab Confirmation Status: options missing {sorted(want - have)}")

        check(label, 1, not issues,
              "all 7 core fields ok" if not issues else _oneline("; ".join(issues), 200))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_13b_opnform_conditional_logic() -> None:
    """Conditional logic: Lab Test Type and Number of Close Contacts Identified."""
    label = "13b. OpnForm conditional logic"
    try:
        props = get_form_properties()
        if props is None:
            check(label, 1, False, "form not found or properties not a JSON array")
            return
        issues: list[str] = []

        lab_status = _find_field(props, "Lab Confirmation Status")
        lab_test = _find_field(props, "Lab Test Type")
        ct = _find_field(props, "Contact Tracing Initiated")
        num = _find_field(props, "Number of Close Contacts Identified")

        # Lab Test Type: shown when Lab Confirmation Status equals Confirmed/Probable.
        if not lab_test:
            issues.append("Lab Test Type: field missing")
        elif not lab_status:
            issues.append("Lab Confirmation Status: field missing (logic trigger)")
        else:
            logic = lab_test.get("logic") or {}
            actions = logic.get("actions") or []
            leaves = _logic_leaves(logic.get("conditions"))
            sid = str(lab_status.get("id"))
            vals: set[str] = set()
            for leaf in leaves:
                v = leaf.get("value") or {}
                if _leaf_trigger_id(leaf) == sid and str(v.get("operator")) == "equals":
                    vv = v.get("value")
                    if isinstance(vv, list):
                        vals.update(str(x).strip().lower() for x in vv)
                    elif vv is not None:
                        vals.add(str(vv).strip().lower())
            if "show-block" not in actions:
                issues.append("Lab Test Type: no show-block action")
            if not {"confirmed", "probable"} <= vals:
                issues.append(f"Lab Test Type: equals-values={sorted(vals) or 'none'}")

        # Number of Close Contacts: shown when Contact Tracing Initiated is checked.
        if not num:
            issues.append("Number of Close Contacts Identified: field missing")
        elif not ct:
            issues.append("Contact Tracing Initiated: field missing (logic trigger)")
        else:
            logic = num.get("logic") or {}
            actions = logic.get("actions") or []
            leaves = _logic_leaves(logic.get("conditions"))
            cid = str(ct.get("id"))
            hit = any(
                _leaf_trigger_id(leaf) == cid
                and str((leaf.get("value") or {}).get("operator")) == "is_checked"
                for leaf in leaves
            )
            if "show-block" not in actions:
                issues.append("Contacts count: no show-block action")
            if not hit:
                issues.append("Contacts count: no is_checked leaf on Contact Tracing Initiated")

        check(label, 1, not issues,
              "both conditional rules ok" if not issues else _oneline("; ".join(issues), 200))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_13c_opnform_structure() -> None:
    """Symptoms multi-select, protocol code block, rich text, divider, signature."""
    label = "13c. OpnForm structure blocks"
    try:
        props = get_form_properties()
        if props is None:
            check(label, 1, False, "form not found or properties not a JSON array")
            return
        issues: list[str] = []

        f = _find_field(props, "Presenting Symptoms")
        if not f:
            issues.append("Presenting Symptoms: missing")
        else:
            if str(f.get("type")) != "multi_select":
                issues.append(f"Presenting Symptoms: type={f.get('type')}")
            if not _truthy(f.get("required")):
                issues.append("Presenting Symptoms: not required")
            allow = f.get("allow_creation")
            if allow is None:
                allow = (f.get("multi_select") or {}).get("allow_creation")
            if not _truthy(allow):
                issues.append("Presenting Symptoms: allow_creation not set")
            want = {"fever", "cough", "diarrhea", "rash", "fatigue", "headache", "myalgia"}
            have = _option_names(f, "multi_select")
            if not want <= have:
                issues.append(f"Presenting Symptoms: options missing {sorted(want - have)}")

        code_ok = False
        for f in props:
            if isinstance(f, dict) and f.get("type") == "nf-code":
                content = re.sub(r"<[^>]+>", " ", str(f.get("content") or "")).lower()
                if "617-983-6800" in content and "maven" in content:
                    code_ok = True
                    break
        if not code_ok:
            issues.append("nf-code block with 617-983-6800 + MAVEN not found")

        rich = [f for f in props if isinstance(f, dict) and f.get("type") == "rich_text"
                and str(f.get("name", "")).strip().lower() == "additional clinical details"]
        if not rich:
            issues.append("rich_text 'Additional Clinical Details' not found")

        if not any(isinstance(f, dict) and f.get("type") == "nf-divider" for f in props):
            issues.append("nf-divider not found")

        if not any(isinstance(f, dict) and "signature" in str(f.get("type", "")).lower()
                   for f in props):
            issues.append("signature field not found")

        check(label, 1, not issues,
              "all structure blocks ok" if not issues else _oneline("; ".join(issues), 200))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def _json_strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_json_strings(v))
    elif isinstance(obj, list):
        for item in obj:
            out.extend(_json_strings(item))
    return out


def check_14_opnform_email_notification() -> None:
    """Email integration row sends to mumps.surveillance@mass.gov (parsed data JSON)."""
    try:
        fid = get_form_id()
        if not fid:
            check("14. OpnForm email notification", 1, False, "form not found")
            return
        rows = opnform_sql(
            f"SELECT data FROM form_integrations "
            f"WHERE form_id={fid} AND integration_id='email'"
        )
        if not rows:
            check("14. OpnForm email notification", 1, False,
                  "no form_integrations row with integration_id='email'")
            return
        found = False
        parsed_any = False
        for line in rows.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            parsed_any = True
            if any("mumps.surveillance@mass.gov" in s.lower() for s in _json_strings(obj)):
                found = True
                break
        detail = ("mumps.surveillance@mass.gov found in email integration data" if found
                  else ("address not found in email integration data" if parsed_any
                        else "email integration data not parseable as JSON"))
        check("14. OpnForm email notification", 1, found, detail)
    except Exception as e:
        check("14. OpnForm email notification", 1, False, f"exception: {e}")


# ── OnlyOffice checks ─────────────────────────────────────────────────────────

_OO_TITLE_PATTERNS = [
    "%MUM-2026-0509-FS%",
    "%Communicable Disease Case Report%",
]


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
    try:
        import requests
    except ImportError:
        return None
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    s = requests.Session()
    try:
        resp = s.post(f"{base_url}/api/2.0/authentication",
                      json={"userName": "admin@onlyoffice.local",
                            "password": "NewAdmin123!"},
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


def check_15_onlyoffice_document() -> None:
    """Gate (0pt): report document exists (tolerates suffix/em-dash, no Recovery copy)."""
    try:
        found = _oo_find_file_id(_OO_TITLE_PATTERNS)
        if found:
            file_id, title = found
            ok = "mum-2026-0509-fs" in _norm_text(title)
            check("15. Gate: OnlyOffice document exists", 0, ok,
                  f"id={file_id} title={_oneline(title, 80)}" if ok
                  else f"title lacks case ref: {_oneline(title, 80)}")
        else:
            check("15. Gate: OnlyOffice document exists", 0, False, "document not found")
    except Exception as e:
        check("15. Gate: OnlyOffice document exists", 0, False, f"exception: {e}")


def check_15b_onlyoffice_content() -> None:
    """Report body contains the case-report facts from the description."""
    label = "15b. OnlyOffice report content"
    try:
        data, src = _oo_get_document(_OO_TITLE_PATTERNS)
        if not data:
            check(label, 1, False, f"gate/content failed: {src}")
            return
        text = _norm_text(_docx_text(data))
        probes = {
            "case_ref": "mum-2026-0509-fs" in text,
            "address": "100 health blvd" in text,
            "dob": "1986-01-12" in text,
            "onset": "2026-05-06" in text,
            "icd_codes": "b26.9" in text and "k11.20" in text,
            "contacts_8": bool(re.search(r"contacts identified\D{0,10}8", text)),
            "maven": "maven portal" in text,
            "signoff": "dr. simone laurent" in text,
        }
        missing = [k for k, ok in probes.items() if not ok]
        check(label, 1, not missing,
              f"all content probes ok ({src})" if not missing
              else f"missing={','.join(missing)} ({src})")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_encounter()
    check_2_soap_subjective_objective()
    check_3_soap_assessment_plan()
    check_4_ros_findings()
    check_5_physical_exam()
    check_6_problem_mumps()
    check_7_problem_sialadenitis()
    check_7b_issue_encounter_link()
    check_8_procedure_order()
    check_9_billing_codes()
    check_10_message()
    check_11_flow_board_status()
    check_12_opnform_form()
    check_13a_opnform_core_fields()
    check_13b_opnform_conditional_logic()
    check_13c_opnform_structure()
    check_14_opnform_email_notification()
    check_15_onlyoffice_document()
    check_15b_onlyoffice_content()

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
