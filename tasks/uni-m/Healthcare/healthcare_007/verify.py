"""
Verifier for Healthcare-007-I5: Prepare Discharge Summary and Follow-Up Plan for Adrianne Simonis

Checks: 15 checks across openemr (8) and onlyoffice (7); total weight 23.
ck1 (patient), ck2 (new encounter) and ck9 (document lookup) are 0-weight
precondition gates — when a gate fails, all dependent checks FAIL.
Strategy: docker exec (MariaDB for OpenEMR, MySQL for OnlyOffice) + filesystem inspection.

Required env vars:
  SERVER_HOSTNAME, OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import io
import os
import re
import sys
import time
import zipfile
import subprocess

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.getenv("OPENEMR_PORT")
OPENEMR_CONTAINER = os.getenv("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.getenv("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.getenv("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.getenv("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.getenv("ONLYOFFICE_DB_CONTAINER")

for var_name, var_val in [
    ("OPENEMR_PORT", OPENEMR_PORT),
    ("OPENEMR_CONTAINER", OPENEMR_CONTAINER),
    ("OPENEMR_DB_CONTAINER", OPENEMR_DB_CONTAINER),
    ("ONLYOFFICE_PORT", ONLYOFFICE_PORT),
    ("ONLYOFFICE_CONTAINER", ONLYOFFICE_CONTAINER),
    ("ONLYOFFICE_DB_CONTAINER", ONLYOFFICE_DB_CONTAINER),
]:
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

# probed seed max, mw-openemr:latest 2026-08 (form_encounter MAX(date)='2026-03-05 00:00:00')
FORM_ENCOUNTER_SEED_MAX = "2026-03-05"

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


def openemr_sql(query: str) -> str:
    """Run SQL against OpenEMR MariaDB and return stdout. Raises on SQL error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass",
        "-D", "openemr",
        "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Run SQL against OnlyOffice MySQL and return stdout. Raises on SQL error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice",
        "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def _num_eq(val: str, expected: float, tol: float = 0.5) -> bool:
    """Numeric compare tolerant of DB float formatting ('72.000000') and units suffixes."""
    try:
        return abs(float(str(val).replace("%", "").strip()) - expected) <= tol
    except (ValueError, TypeError):
        return False


def _sl(s, n: int = 120) -> str:
    """Single-line, whitespace-collapsed, truncated string for details."""
    return " ".join(str(s).split())[:n]


# ── Shared: find patient PID ─────────────────────────────────────────────────
_patient_pid = None


def get_patient_pid() -> int | None:
    global _patient_pid
    if _patient_pid is not None:
        return _patient_pid
    row = openemr_sql(
        "SELECT pid FROM patient_data "
        "WHERE fname LIKE '%Adrianne%' AND lname LIKE '%Simonis%' "
        "ORDER BY (DOB = '1964-11-15') DESC, pid ASC LIMIT 1;"
    )
    if row:
        _patient_pid = int(row.split("\t")[0].strip())
    return _patient_pid


# ── Check 1 (gate): Patient exists ───────────────────────────────────────────
def check_1_patient_exists() -> None:
    """Gate: patient Adrianne Simonis exists in OpenEMR (DOB-disambiguated pid)."""
    try:
        pid = get_patient_pid()
        check("1. Patient Adrianne Simonis exists in OpenEMR", 0, pid is not None,
              f"pid={pid}" if pid else "not found")
    except Exception as e:
        check("1. Patient Adrianne Simonis exists in OpenEMR", 0, False, f"exception: {e}")


# ── Check 2 (gate): New discharge encounter ──────────────────────────────────
_encounter_id = None


def check_2_encounter_exists() -> None:
    """Gate: a NEW discharge encounter (date > seed max) exists for the patient."""
    global _encounter_id
    try:
        pid = get_patient_pid()
        if not pid:
            check("2. New discharge encounter exists", 0, False,
                  "gate ck1 failed: patient not found")
            return
        row = openemr_sql(
            f"SELECT encounter, date FROM form_encounter WHERE pid={pid} "
            f"AND date > '{FORM_ENCOUNTER_SEED_MAX}' "
            f"ORDER BY date DESC LIMIT 1;"
        )
        if row:
            parts = row.split("\t")
            _encounter_id = int(parts[0].strip())
            enc_date = parts[1].strip() if len(parts) > 1 else "?"
            check("2. New discharge encounter exists", 0, True,
                  f"encounter={_encounter_id}, date={enc_date}")
        else:
            check("2. New discharge encounter exists", 0, False,
                  f"no encounter with date > '{FORM_ENCOUNTER_SEED_MAX}' for pid={pid}")
    except Exception as e:
        check("2. New discharge encounter exists", 0, False, f"exception: {e}")


# ── Check 3: Vitals ──────────────────────────────────────────────────────────
def check_3_vitals() -> None:
    """Vitals on the new encounter: BP 118/74, pulse 72, temp 98.8, RR 14, O2 99%, weight 62 kg."""
    try:
        pid = get_patient_pid()
        if not pid or not _encounter_id:
            check("3. Discharge vitals recorded", 2, False,
                  "gate ck1/ck2 failed: no patient or new encounter")
            return
        row = openemr_sql(
            f"SELECT v.bps, v.bpd, v.pulse, v.temperature, v.respiration, "
            f"v.oxygen_saturation, v.weight "
            f"FROM forms f JOIN form_vitals v ON f.form_id = v.id "
            f"WHERE f.pid={pid} AND f.encounter={_encounter_id} "
            f"AND f.formdir='vitals' AND f.deleted=0 "
            f"ORDER BY f.date DESC LIMIT 1;"
        )
        if not row:
            check("3. Discharge vitals recorded", 2, False,
                  f"no vitals form anchored to encounter {_encounter_id}")
            return
        parts = row.split("\t")
        if len(parts) < 7:
            check("3. Discharge vitals recorded", 2, False,
                  f"unexpected format: {_sl(row)}")
            return
        bps, bpd, pulse, temp, rr, o2, wt = [p.strip() for p in parts[:7]]
        issues = []
        # Numeric columns are floats in form_vitals (e.g. pulse stored '72.000000'),
        # so compare numerically rather than as strings.
        if not _num_eq(bps, 118):
            issues.append(f"bps={bps} expected 118")
        if not _num_eq(bpd, 74):
            issues.append(f"bpd={bpd} expected 74")
        if not _num_eq(pulse, 72):
            issues.append(f"pulse={pulse} expected 72")
        if not _num_eq(temp, 98.8, tol=0.1):
            issues.append(f"temp={temp} expected 98.8")
        if not _num_eq(rr, 14):
            issues.append(f"rr={rr} expected 14")
        # O2 sat may be stored as 99 or 99%
        if not _num_eq(o2, 99):
            issues.append(f"o2={o2} expected 99")
        # Description asks for weight 62 kg with unit selector set to kg; OpenEMR
        # stores weight in lbs (62 kg = 136.687 lbs). Only the converted value passes.
        if not _num_eq(wt, 136.687, tol=0.1):
            issues.append(f"weight={wt} expected 136.687 lbs (62 kg)")
        check("3. Discharge vitals recorded", 2, not issues,
              "all vitals correct" if not issues else _sl("; ".join(issues), 200))
    except Exception as e:
        check("3. Discharge vitals recorded", 2, False, f"exception: {e}")


# ── Check 4: Transfer Summary ────────────────────────────────────────────────
def check_4_transfer_summary() -> None:
    """Transfer Summary on the new encounter with correct reason and facility 'Home'."""
    try:
        pid = get_patient_pid()
        if not pid or not _encounter_id:
            check("4. Transfer Summary form", 2, False,
                  "gate ck1/ck2 failed: no patient or new encounter")
            return
        row = openemr_sql(
            f"SELECT f.form_id, f.date FROM forms f "
            f"WHERE f.pid={pid} AND f.encounter={_encounter_id} "
            f"AND f.formdir='transfer_summary' AND f.deleted=0 "
            f"ORDER BY f.date DESC LIMIT 1;"
        )
        if not row:
            check("4. Transfer Summary form", 2, False,
                  f"no transfer summary form anchored to encounter {_encounter_id}")
            return
        parts = row.split("\t")
        form_id = parts[0].strip()
        form_date = parts[1].strip() if len(parts) > 1 else "?"
        # Real columns (see the form's table.sql): client_name, provider, transfer_to,
        # transfer_date, status_of_admission, diagnosis, intervention_provided,
        # overall_status_of_discharge. The reason text may land in any free-text
        # field, so match reason by substring across the row; the receiving facility
        # must be in the transfer_to column itself.
        detail_row = openemr_sql(
            f"SELECT transfer_to, status_of_admission, diagnosis, "
            f"intervention_provided, overall_status_of_discharge "
            f"FROM form_transfer_summary WHERE id={form_id} LIMIT 1;"
        )
        if not detail_row:
            check("4. Transfer Summary form", 2, False,
                  f"forms row exists (form_id={form_id}, date={form_date}) but "
                  f"form_transfer_summary has no row with id={form_id}")
            return
        parts = detail_row.split("\t")
        transfer_to = parts[0].strip() if len(parts) > 0 else ""
        text = detail_row.lower()
        reason_ok = ("resolved hypoxemia" in text and "pneumonia" in text
                     and "outpatient follow-up" in text)
        facility_ok = "home" in transfer_to.lower()
        check("4. Transfer Summary form", 2, reason_ok and facility_ok,
              f"reason_ok={reason_ok}, transfer_to='{_sl(transfer_to, 40)}', "
              f"facility_ok={facility_ok}")
    except Exception as e:
        check("4. Transfer Summary form", 2, False, f"exception: {e}")


# ── Check 5: Clinical Instructions ───────────────────────────────────────────
def check_5_clinical_instructions() -> None:
    """Clinical Instructions form on the new encounter with the discharge instructions text."""
    try:
        pid = get_patient_pid()
        if not pid or not _encounter_id:
            check("5. Clinical Instructions form", 2, False,
                  "gate ck1/ck2 failed: no patient or new encounter")
            return
        detail_row = openemr_sql(
            f"SELECT c.instruction "
            f"FROM forms f JOIN form_clinical_instructions c ON f.form_id = c.id "
            f"WHERE f.pid={pid} AND f.encounter={_encounter_id} "
            f"AND f.formdir='clinical_instructions' AND f.deleted=0 "
            f"ORDER BY f.date DESC LIMIT 1;"
        )
        if not detail_row:
            check("5. Clinical Instructions form", 2, False,
                  f"no clinical instructions form anchored to encounter {_encounter_id}")
            return
        text = detail_row.strip().lower()
        has_antibiotics = "antibiotics" in text
        has_spirometer = "10 times every hour" in text or "incentive spirometer" in text
        has_hydration = "2 liters" in text
        has_fever = "101.5" in text
        ok = has_antibiotics and has_spirometer and has_hydration and has_fever
        check("5. Clinical Instructions form", 2, ok,
              f"antibiotics={has_antibiotics}, spirometer={has_spirometer}, "
              f"hydration={has_hydration}, fever_101.5={has_fever}")
    except Exception as e:
        check("5. Clinical Instructions form", 2, False, f"exception: {e}")


# ── Check 6: Care Plan ───────────────────────────────────────────────────────
def check_6_care_plan() -> None:
    """Care Plan on the new encounter with BOTH the goal and the instructions text."""
    try:
        pid = get_patient_pid()
        if not pid or not _encounter_id:
            check("6. Care Plan form", 2, False,
                  "gate ck1/ck2 failed: no patient or new encounter")
            return
        # form_care_plan has an encounter column (verified) — anchor directly.
        detail_row = openemr_sql(
            f"SELECT care_plan_type, description, code, codetext "
            f"FROM form_care_plan WHERE pid={pid} AND encounter={_encounter_id};"
        )
        if not detail_row:
            check("6. Care Plan form", 2, False,
                  f"no care plan rows anchored to encounter {_encounter_id}")
            return
        text = detail_row.lower()
        has_goal = "resolution" in text and "pneumonia" in text and "pulmonary" in text
        has_instructions = ("azithromycin" in text and "spirometer" in text
                            and "x-ray" in text and "pneumococcal" in text)
        check("6. Care Plan form", 2, has_goal and has_instructions,
              f"goal_keywords={has_goal}, instruction_keywords={has_instructions}")
    except Exception as e:
        check("6. Care Plan form", 2, False, f"exception: {e}")


# ── Check 7: Fee Sheet ICD-10 codes ──────────────────────────────────────────
def check_7_fee_sheet() -> None:
    """Fee Sheet on the new encounter contains ICD-10 codes J18.9 and J96.01."""
    try:
        pid = get_patient_pid()
        if not pid or not _encounter_id:
            check("7. Fee Sheet ICD-10 codes", 2, False,
                  "gate ck1/ck2 failed: no patient or new encounter")
            return
        rows = openemr_sql(
            f"SELECT code FROM billing WHERE pid={pid} "
            f"AND encounter={_encounter_id} AND code_type='ICD10' AND activity=1;"
        )
        all_codes = rows.lower() if rows else ""
        has_j189 = "j18.9" in all_codes
        has_j9601 = "j96.01" in all_codes
        check("7. Fee Sheet ICD-10 codes", 2, has_j189 and has_j9601,
              f"J18.9={has_j189}, J96.01={has_j9601}")
    except Exception as e:
        check("7. Fee Sheet ICD-10 codes", 2, False, f"exception: {e}")


# ── Check 8: Follow-up appointment ───────────────────────────────────────────
def check_8_appointment() -> None:
    """Follow-up appointment 2026-05-22 13:45, Dr. Lorinda Pouros, category 'Follow-Up',
    comment mentioning chest X-ray and pneumococcal vaccination."""
    try:
        pid = get_patient_pid()
        if not pid:
            check("8. Follow-up appointment", 3, False,
                  "gate ck1 failed: patient not found")
            return
        rows = openemr_sql(
            f"SELECT e.pc_eventDate, e.pc_startTime, e.pc_hometext, "
            f"c.pc_catname, u.fname, u.lname "
            f"FROM openemr_postcalendar_events e "
            f"LEFT JOIN users u ON e.pc_aid=u.id "
            f"LEFT JOIN openemr_postcalendar_categories c ON e.pc_catid=c.pc_catid "
            f"WHERE e.pc_pid={pid} AND e.pc_eventDate='2026-05-22';"
        )
        if not rows:
            check("8. Follow-up appointment", 3, False,
                  "no appointment found for 2026-05-22")
            return
        # Parse rows — may be multiple, find the right one
        found_match = False
        detail_parts = []
        for line in rows.split("\n"):
            parts = line.split("\t")
            if len(parts) < 6:
                continue
            evt_date, evt_time, comment, catname, pfname, plname = [p.strip() for p in parts[:6]]
            provider_name = f"{pfname} {plname}".strip().lower()
            comment_l = comment.lower()
            date_ok = evt_date == "2026-05-22"
            time_ok = evt_time.startswith("13:45")
            provider_ok = "lorinda" in provider_name and "pouros" in provider_name
            comment_ok = "chest x-ray" in comment_l and "pneumococcal" in comment_l
            cat_ok = catname.strip().lower() == "follow-up"
            if date_ok and time_ok and provider_ok and comment_ok and cat_ok:
                found_match = True
                detail_parts.insert(0,
                    f"date={evt_date}, time={evt_time}, provider={pfname} {plname}, "
                    f"category={catname}, comment_ok={comment_ok}")
                break
            detail_parts.append(
                f"date={evt_date}, time={evt_time}, provider={pfname} {plname}, "
                f"category={catname}, comment_ok={comment_ok}")
        check("8. Follow-up appointment", 3, found_match,
              _sl(detail_parts[0], 200) if detail_parts else "no matching row")
    except Exception as e:
        check("8. Follow-up appointment", 3, False, f"exception: {e}")


# ── OnlyOffice document retrieval + parsing ──────────────────────────────────
EXPECTED_DOC_TITLE = "Discharge Summary - Adrianne Simonis - 2026-05-02"

# Full-title patterns only; second one tolerates em-dash / different separators.
# Editor crash-recovery copies are excluded in _oo_find_file_id.
DOC_TITLE_PATTERNS = [
    "Discharge Summary - Adrianne Simonis - 2026-05-02%",
    "Discharge Summary%Adrianne Simonis%2026-05-02%",
]


def _oo_find_file_id(like_patterns: list[str]) -> tuple[str, str] | None:
    """Locate a files_file row by trying LIKE patterns in order. Excludes
    editor crash-recovery copies. Returns (id, title) or None. DB titles may
    carry a .docx extension; patterns end with '%'."""
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
    import requests
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
    concatenated with NO separator (runs get split arbitrarily by the editor);
    paragraphs joined with newlines."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in xml.split("</w:p>"):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, flags=re.DOTALL)
        if runs:
            paras.append(_xml_unescape("".join(runs)))
    return "\n".join(paras)


_doc_state: dict = {"fetched": False, "text": None, "detail": "", "has_tbl": False}


def get_doc_text() -> tuple[str | None, str]:
    """Normalized (lowercase, dash/whitespace-normalized) document text, cached.
    Returns (text|None, source/failure detail)."""
    if _doc_state["fetched"]:
        return _doc_state["text"], _doc_state["detail"]
    _doc_state["fetched"] = True
    data, detail = _oo_get_document(DOC_TITLE_PATTERNS)
    _doc_state["detail"] = detail
    if data:
        try:
            _doc_state["text"] = _norm_text(_docx_text(data))
            xml = zipfile.ZipFile(io.BytesIO(data)).read(
                "word/document.xml").decode("utf-8", errors="replace")
            _doc_state["has_tbl"] = "<w:tbl" in xml
        except Exception as e:
            _doc_state["text"] = None
            _doc_state["detail"] = f"docx parse failed: {e} ({detail})"
    return _doc_state["text"], _doc_state["detail"]


# ── Check 9 (gate): Document exists with full title ──────────────────────────
def check_9_doc_exists() -> None:
    """Gate: document 'Discharge Summary - Adrianne Simonis - 2026-05-02' exists
    (full title only) and its content is readable."""
    try:
        text, detail = get_doc_text()
        check("9. OnlyOffice document exists", 0, text is not None, _sl(detail, 160))
    except Exception as e:
        check("9. OnlyOffice document exists", 0, False, f"exception: {e}")


def check_10_doc_header_patient_info() -> None:
    """Document header: clinic 'Pepperell Primary Care Clinic', patient name, DOB."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("10. Doc header & patient info", 2, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_clinic = "pepperell primary care clinic" in text
        has_patient = "adrianne" in text and "simonis" in text
        has_dob = any(d in text for d in ("1964-11-15", "11/15/1964", "november 15, 1964"))
        check("10. Doc header & patient info", 2,
              has_clinic and has_patient and has_dob,
              f"clinic={has_clinic}, patient={has_patient}, dob={has_dob}")
    except Exception as e:
        check("10. Doc header & patient info", 2, False, f"exception: {e}")


def check_11_doc_hospital_course() -> None:
    """Document Hospital Course narrative: all four key phrases required."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("11. Doc hospital course", 2, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_cough = "productive cough" in text
        has_pneumonia = "community-acquired pneumonia" in text
        has_strep = "streptococcus pneumoniae" in text
        has_defervesced = "defervesced" in text
        ok = has_cough and has_pneumonia and has_strep and has_defervesced
        check("11. Doc hospital course", 2, ok,
              f"cough={has_cough}, pneumonia={has_pneumonia}, "
              f"strep={has_strep}, defervesced={has_defervesced}")
    except Exception as e:
        check("11. Doc hospital course", 2, False, f"exception: {e}")


def check_12_doc_medications() -> None:
    """Document lists all three discharge medications with dosages."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("12. Doc discharge medications", 2, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_azithromycin = bool(re.search(r"azithromycin\s*500\s*mg", text, re.IGNORECASE))
        has_guaifenesin = bool(re.search(r"guaifenesin\s*600\s*mg", text, re.IGNORECASE))
        has_acetaminophen = bool(re.search(r"acetaminophen\s*650\s*mg", text, re.IGNORECASE))
        ok = has_azithromycin and has_guaifenesin and has_acetaminophen
        check("12. Doc discharge medications", 2, ok,
              f"azithromycin500={has_azithromycin}, guaifenesin600={has_guaifenesin}, "
              f"acetaminophen650={has_acetaminophen}")
    except Exception as e:
        check("12. Doc discharge medications", 2, False, f"exception: {e}")


def check_13_doc_signature_condition() -> None:
    """Document discharge condition ('Stable, afebrile, ... room air') and signature."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("13. Doc condition & signature", 1, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_condition = "stable" in text and "afebrile" in text and "room air" in text
        has_signature = "lorinda pouros" in text
        ok = has_condition and has_signature
        check("13. Doc condition & signature", 1, ok,
              f"condition={has_condition}, signature={has_signature}")
    except Exception as e:
        check("13. Doc condition & signature", 1, False, f"exception: {e}")


def check_14_doc_admission_dx_vitals() -> None:
    """Document Admission Diagnosis (J18.9, J96.01) + Discharge Vitals values.
    Document shows the 62 kg display value (not the DB lbs value)."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("14. Doc admission dx & vitals", 1, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_j189 = "j18.9" in text
        has_j9601 = "j96.01" in text
        vitals_needed = ("118/74", "72", "98.8", "14", "99", "62")
        missing = [v for v in vitals_needed if v not in text]
        ok = has_j189 and has_j9601 and not missing
        check("14. Doc admission dx & vitals", 1, ok,
              f"J18.9={has_j189}, J96.01={has_j9601}, "
              f"vitals_missing={missing if missing else 'none'}, "
              f"info:w_tbl={_doc_state['has_tbl']}")
    except Exception as e:
        check("14. Doc admission dx & vitals", 1, False, f"exception: {e}")


def check_15_doc_instructions_followup() -> None:
    """Document Discharge Instructions text + Follow-Up Plan (date/time + goal)."""
    try:
        text, detail = get_doc_text()
        if text is None:
            check("15. Doc instructions & follow-up plan", 2, False,
                  f"gate ck9 failed: {_sl(detail, 140)}")
            return
        has_fever = "101.5" in text
        has_spirometer = "incentive spirometer" in text
        has_date = "2026-05-22" in text
        has_time = "1:45" in text or "13:45" in text
        has_goal = "restore baseline pulmonary function" in text
        ok = has_fever and has_spirometer and has_date and has_time and has_goal
        check("15. Doc instructions & follow-up plan", 2, ok,
              f"fever_101.5={has_fever}, spirometer={has_spirometer}, "
              f"date={has_date}, time={has_time}, goal={has_goal}")
    except Exception as e:
        check("15. Doc instructions & follow-up plan", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_patient_exists()
    check_2_encounter_exists()
    check_3_vitals()
    check_4_transfer_summary()
    check_5_clinical_instructions()
    check_6_care_plan()
    check_7_fee_sheet()
    check_8_appointment()
    check_9_doc_exists()
    check_10_doc_header_patient_info()
    check_11_doc_hospital_course()
    check_12_doc_medications()
    check_13_doc_signature_condition()
    check_14_doc_admission_dx_vitals()
    check_15_doc_instructions_followup()

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
