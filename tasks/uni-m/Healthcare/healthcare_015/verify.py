"""
Verifier for Healthcare-015-I3: Care Transition Handover for Mora Ernser to Orthopedics

Checks: 15 weighted checks across openemr and onlyoffice (total weight 25).
Strategy: docker exec (MariaDB for OpenEMR, MySQL for OnlyOffice)

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

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB = os.environ.get("OPENEMR_DB_CONTAINER")
ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB = os.environ.get("ONLYOFFICE_DB_CONTAINER")

for _name, _val in [
    ("OPENEMR_PORT", OPENEMR_PORT),
    ("OPENEMR_CONTAINER", OPENEMR_CONTAINER),
    ("OPENEMR_DB_CONTAINER", OPENEMR_DB),
    ("ONLYOFFICE_PORT", ONLYOFFICE_PORT),
    ("ONLYOFFICE_CONTAINER", ONLYOFFICE_CONTAINER),
    ("ONLYOFFICE_DB_CONTAINER", ONLYOFFICE_DB),
]:
    if not _val:
        print(f"FATAL: {_name} not set", file=sys.stderr)
        sys.exit(1)

# ── Slot values ───────────────────────────────────────────────────────────────
PATIENT_FNAME = "Mora"
PATIENT_LNAME = "Ernser"
PATIENT_DOB = "1989-06-10"
FUNCTIONAL_OBS = "Non-weight-bearing on right lower extremity, uses rolling walker, requires assistance with lower-body ADLs"
COGNITIVE_OBS = "Alert and oriented x4, MMSE 29/30, intact judgment and reasoning"
TREATMENT_GOALS = "Surgical evaluation for right hip osteoarthritis, pre-operative optimization, post-operative rehabilitation planning, and restoration of ambulatory independence"
TREATMENT_TARGET = "2026-11-30"
TRANSFER_REASON_SNIPPET = "progressive functional decline despite conservative management"
TRANSFER_FACILITY = "Brigham and Women's Hospital - Orthopedic Surgery Department"
ICD10_1 = "M16.11"
ICD10_2 = "Z47.1"
DISCLOSURE_RECIPIENT = "Dr. Gertrud Kuhic"
MSG_SUBJECT_KEYS = [
    "Care Transition Handover",
    "Mora Ernser",
    "Orthopedic Surgery Referral",
]
MSG_ASSIGNED_TO = "dr_kuhic"
MSG_BODY_KEYS = [
    "transferring the care",
    "orthopedic surgery service",
    "total hip arthroplasty",
    "care transition summary document",
]
DOC_TITLE = "Care Transition Summary — Mora Ernser — 2026-04-18"
DOC_ID = "CTS-2026-0418-ERNSER-003"
DOC_SECTIONS = [
    "Patient Demographics",
    "Active Problem List",
    "Active Medications",
    "Allergies",
    "Recent Encounter Summary",
    "Functional and Cognitive Assessment",
    "Immunization Status",
    "Transfer Plan",
]

# probed seed max, mw-openemr:latest 2026-08
SEED_MAX_ENCOUNTER_DATE = "2026-03-05"      # form_encounter MAX(date)
SEED_MAX_PNOTES_DATE = "2026-03-19 13:24:32"  # pnotes MAX(date)
EXTLOG_DATE_GUARD = "2026-03-22 13:24:32"   # extended_log seed = 0 rows; harmless guard

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


def openemr_sql(query: str, timeout: int = 15) -> str:
    """Run SQL against OpenEMR MariaDB. Returns stdout. Raises on SQL error."""
    rc, out, err = docker_exec(
        OPENEMR_DB,
        "mysql", "-u", "openemr", "-popenemr_pass",
        "--default-character-set=utf8mb4",
        "openemr", "-N", "-e", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out


def onlyoffice_sql(query: str, timeout: int = 15) -> str:
    """Run SQL against OnlyOffice MySQL. Returns stdout. Raises on SQL error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4",
        "onlyoffice", "-N", "-e", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        s = s.replace(a, b)
    return s


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


# ── OnlyOffice document retrieval (fs first, API fallback) ───────────────────
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
    except Exception:
        return None
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


# ── Shared state ──────────────────────────────────────────────────────────────
_pid: str = ""
_enc: str = ""
_msg_id: str = ""          # pnotes row matched by ck11 (ck12 binds to it)
_doc_file_id: str = ""     # files_file id matched by ck13 gate
_doc_text: str | None = None   # normalized document text (cached)
_doc_fetch_detail: str = ""


def _get_doc_text() -> str | None:
    """Fetch + parse the ck13-gated document once; return _norm_text of it."""
    global _doc_text, _doc_fetch_detail
    if _doc_text is not None:
        return _doc_text
    if not _doc_file_id:
        _doc_fetch_detail = "gate: document title match failed (ck13)"
        return None
    data = _oo_bytes_from_fs(_doc_file_id)
    src = "fs"
    if not data:
        data = _oo_bytes_from_api(_doc_file_id)
        src = "api"
    if not data:
        _doc_fetch_detail = f"id={_doc_file_id} found but content unreadable (fs+api)"
        return None
    try:
        _doc_text = _norm_text(_docx_text(data))
        _doc_fetch_detail = f"{src} id={_doc_file_id} ({len(_doc_text)} chars)"
        return _doc_text
    except Exception as e:
        _doc_fetch_detail = f"docx parse failed: {e}"
        return None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_01_patient_exists() -> None:
    """Gate: patient Mora Ernser (DOB 1989-06-10) resolves to a unique pid."""
    global _pid
    try:
        out = openemr_sql(
            f"SELECT pid FROM patient_data "
            f"WHERE fname='{PATIENT_FNAME}' AND lname='{PATIENT_LNAME}' "
            f"AND DOB='{PATIENT_DOB}';"
        )
        pids = [ln.strip() for ln in out.strip().split("\n") if ln.strip()]
        if len(pids) == 1:
            _pid = pids[0]
            check("1. Patient Mora Ernser exists (gate)", 0, True, f"pid={_pid}")
        else:
            check("1. Patient Mora Ernser exists (gate)", 0, False,
                  f"expected exactly 1 pid, got {len(pids)}: {','.join(pids)[:80]}")
    except Exception as e:
        check("1. Patient Mora Ernser exists (gate)", 0, False, f"exception: {e}")


def check_02_new_encounter() -> None:
    """A NEW encounter (post-seed date anchor) exists for Mora Ernser."""
    global _enc
    if not _pid:
        check("2. Transition encounter exists", 2, False, "gate: patient lookup failed (ck1)")
        return
    try:
        out = openemr_sql(
            f"SELECT encounter FROM form_encounter "
            f"WHERE pid={_pid} AND date > '{SEED_MAX_ENCOUNTER_DATE}' "
            f"ORDER BY date DESC, encounter DESC LIMIT 1;"
        )
        enc = out.strip().split("\n")[0].strip() if out.strip() else ""
        _enc = enc
        check("2. Transition encounter exists", 2, bool(enc),
              f"encounter={enc}" if enc else f"no encounter with date > {SEED_MAX_ENCOUNTER_DATE}")
    except Exception as e:
        check("2. Transition encounter exists", 2, False, f"exception: {e}")


def _fcs_descriptions() -> str:
    """form_functional_cognitive_status rows bound to the anchored encounter
    via the forms registry (table has no encounter column)."""
    return openemr_sql(
        f"SELECT t.description FROM form_functional_cognitive_status t "
        f"JOIN forms f ON f.form_id = t.id AND f.formdir='functional_cognitive_status' "
        f"AND f.deleted=0 AND f.pid={_pid} AND f.encounter={_enc};"
    )


def check_03_functional_status() -> None:
    """Functional status observation recorded on the transition encounter."""
    if not _pid or not _enc:
        check("3. Functional status observation", 2, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        text = _norm_text(_fcs_descriptions())
        found = _norm_text(FUNCTIONAL_OBS) in text
        check("3. Functional status observation", 2, found,
              "matches" if found else "functional observation sentence not found on encounter")
    except Exception as e:
        check("3. Functional status observation", 2, False, f"exception: {e}")


def check_04_cognitive_status() -> None:
    """Cognitive status observation recorded on the transition encounter."""
    if not _pid or not _enc:
        check("4. Cognitive status observation", 2, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        text = _norm_text(_fcs_descriptions())
        found = _norm_text(COGNITIVE_OBS) in text
        check("4. Cognitive status observation", 2, found,
              "matches" if found else "cognitive observation sentence not found on encounter")
    except Exception as e:
        check("4. Cognitive status observation", 2, False, f"exception: {e}")


def check_05_treatment_plan_goals() -> None:
    """Treatment Plan goals in form_care_plan bound to the transition encounter."""
    if not _pid or not _enc:
        check("5. Treatment Plan goals", 2, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        out = openemr_sql(
            f"SELECT t.description FROM form_care_plan t "
            f"JOIN forms f ON f.form_id = t.id AND f.formdir='care_plan' "
            f"AND f.deleted=0 AND f.pid={_pid} AND f.encounter={_enc};"
        )
        text = _norm_text(out)
        found = ("surgical evaluation" in text and "pre-operative optimization" in text
                 and "post-operative rehabilitation planning" in text)
        check("5. Treatment Plan goals", 2, found,
              "goals match" if found else "goal keywords not found on encounter care plan")
    except Exception as e:
        check("5. Treatment Plan goals", 2, False, f"exception: {e}")


def check_06_treatment_plan_target() -> None:
    """Treatment Plan target date 2026-11-30 (proposed_date/date_end/description)."""
    if not _pid or not _enc:
        check("6. Treatment Plan target date", 1, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        out = openemr_sql(
            f"SELECT t.proposed_date, t.date_end, t.description FROM form_care_plan t "
            f"JOIN forms f ON f.form_id = t.id AND f.formdir='care_plan' "
            f"AND f.deleted=0 AND f.pid={_pid} AND f.encounter={_enc};"
        )
        found = TREATMENT_TARGET in out
        check("6. Treatment Plan target date", 1, found,
              "date found" if found else "2026-11-30 not found in encounter care plan")
    except Exception as e:
        check("6. Treatment Plan target date", 1, False, f"exception: {e}")


def _find_transfer_form_text() -> str:
    """Transfer summary form content bound to the transition encounter."""
    rows = openemr_sql(
        f"SELECT form_id, formdir FROM forms "
        f"WHERE pid={_pid} AND encounter={_enc} AND deleted=0 "
        f"AND (formdir LIKE '%transfer%' OR form_name LIKE '%Transfer%');"
    ).strip()
    if not rows:
        return ""
    first = rows.split("\n")[0]
    parts = first.split("\t")
    form_id = parts[0].strip()
    formdir = parts[1].strip() if len(parts) > 1 else ""
    table = f"form_{formdir}" if formdir else "form_transfer_summary"
    try:
        out = openemr_sql(f"SELECT * FROM `{table}` WHERE id={form_id};")
        if out.strip():
            return out
    except Exception:
        pass
    try:
        out = openemr_sql(
            f"SELECT field_id, field_value FROM lbf_data WHERE form_id={form_id};"
        )
        if out.strip():
            return out
    except Exception:
        pass
    return ""


def check_07_transfer_summary_reason() -> None:
    """Transfer Summary on the transition encounter contains the transfer reason."""
    if not _pid or not _enc:
        check("7. Transfer Summary reason", 2, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        text = _norm_text(_find_transfer_form_text())
        found = _norm_text(TRANSFER_REASON_SNIPPET) in text
        check("7. Transfer Summary reason", 2, found,
              "reason matches" if found else "reason text not found on encounter transfer form")
    except Exception as e:
        check("7. Transfer Summary reason", 2, False, f"exception: {e}")


def check_08_transfer_summary_facility() -> None:
    """Transfer Summary on the transition encounter contains receiving facility."""
    if not _pid or not _enc:
        check("8. Transfer Summary facility", 1, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        text = _norm_text(_find_transfer_form_text())
        found = "brigham and women" in text and "orthopedic surgery" in text
        check("8. Transfer Summary facility", 1, found,
              "facility found" if found else "facility text not found on encounter transfer form")
    except Exception as e:
        check("8. Transfer Summary facility", 1, False, f"exception: {e}")


def check_09_icd10_codes() -> None:
    """ICD-10 codes M16.11 and Z47.1 both billed on the transition encounter."""
    if not _pid or not _enc:
        check("9. ICD-10 codes M16.11 and Z47.1", 2, False,
              "gate: no anchored transition encounter (ck2)")
        return
    try:
        out = openemr_sql(
            f"SELECT code FROM billing "
            f"WHERE pid={_pid} AND encounter={_enc} AND activity=1 "
            f"AND code IN ('{ICD10_1}','{ICD10_2}');"
        )
        codes = {line.strip() for line in out.strip().split("\n") if line.strip()}
        has_both = ICD10_1 in codes and ICD10_2 in codes
        check("9. ICD-10 codes M16.11 and Z47.1", 2, has_both,
              f"found: {sorted(codes)}" if codes else "no matching codes billed on encounter")
    except Exception as e:
        check("9. ICD-10 codes M16.11 and Z47.1", 2, False, f"exception: {e}")


def check_10_disclosure_record() -> None:
    """One extended_log row with recipient Kuhic AND 'care transition' (same row)."""
    if not _pid:
        check("10. Disclosure record", 2, False, "gate: patient lookup failed (ck1)")
        return
    try:
        out = openemr_sql(
            f"SELECT event, recipient, description FROM extended_log "
            f"WHERE patient_id='{_pid}' AND date > '{EXTLOG_DATE_GUARD}';"
        )
        passed = False
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            low = _norm_text(line)
            if "kuhic" in low and "care transition" in low:
                passed = True
                break
        check("10. Disclosure record", 2, passed,
              "matches" if passed
              else "no single disclosure row with recipient Kuhic and purpose 'Care Transition'")
    except Exception as e:
        check("10. Disclosure record", 2, False, f"exception: {e}")


def check_11_message_subject() -> None:
    """One pnotes row: title has all subject parts, assigned_to=dr_kuhic, post-seed date."""
    global _msg_id
    if not _pid:
        check("11. Message subject and recipient", 3, False, "gate: patient lookup failed (ck1)")
        return
    try:
        out = openemr_sql(
            f"SELECT id, title FROM pnotes "
            f"WHERE pid={_pid} AND deleted=0 AND assigned_to='{MSG_ASSIGNED_TO}' "
            f"AND date > '{SEED_MAX_PNOTES_DATE}' ORDER BY id DESC;"
        )
        keys = [_norm_text(k) for k in MSG_SUBJECT_KEYS]
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            title = _norm_text(parts[1])
            if all(k in title for k in keys):
                _msg_id = parts[0].strip()
                break
        check("11. Message subject and recipient", 3, bool(_msg_id),
              f"pnotes id={_msg_id}" if _msg_id
              else "no pnotes row to dr_kuhic whose title has all 3 subject parts")
    except Exception as e:
        check("11. Message subject and recipient", 3, False, f"exception: {e}")


def check_12_message_body() -> None:
    """Body of the ck11 message row contains all 4 key phrases + DOB."""
    if not _msg_id:
        check("12. Message body content", 2, False, "gate: no matching message row (ck11)")
        return
    try:
        out = openemr_sql(f"SELECT body FROM pnotes WHERE id={_msg_id};")
        body = _norm_text(out)
        matches = sum(1 for kw in MSG_BODY_KEYS if _norm_text(kw) in body)
        has_dob = f"dob {PATIENT_DOB}" in body
        passed = matches == len(MSG_BODY_KEYS) and has_dob
        check("12. Message body content", 2, passed,
              f"matched {matches}/{len(MSG_BODY_KEYS)} key phrases; "
              f"DOB {'found' if has_dob else 'missing'}")
    except Exception as e:
        check("12. Message body content", 2, False, f"exception: {e}")


def check_13_onlyoffice_document() -> None:
    """Gate: OnlyOffice document with the exact expected title exists."""
    global _doc_file_id
    try:
        expected = _norm_text(DOC_TITLE)
        out = onlyoffice_sql(
            "SELECT id, title FROM files_file "
            "WHERE title LIKE '%Care Transition Summary%Mora Ernser%' "
            "AND title NOT LIKE '%Recovery%' ORDER BY id DESC;"
        )
        matched_title = ""
        for line in out.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            norm = _norm_text(parts[1])
            if norm == expected or norm == expected + ".docx":
                _doc_file_id = parts[0].strip()
                matched_title = parts[1].strip()
                break
        if _doc_file_id:
            check("13. OnlyOffice document exists (gate)", 0, True,
                  f"id={_doc_file_id} title={matched_title[:80]}")
        else:
            similar = out.strip().replace("\n", " | ")[:100]
            check("13. OnlyOffice document exists (gate)", 0, False,
                  f"no exact-title match; similar: {similar}" if similar
                  else "no matching files")
    except Exception as e:
        check("13. OnlyOffice document exists (gate)", 0, False, f"exception: {e}")


def check_14_onlyoffice_doc_id() -> None:
    """The document ID appears inside THIS document (ck13-gated file)."""
    try:
        text = _get_doc_text()
        if text is None:
            check("14. Document contains ID CTS-2026-0418-ERNSER-003", 1, False,
                  _doc_fetch_detail)
            return
        found = DOC_ID.lower() in text
        check("14. Document contains ID CTS-2026-0418-ERNSER-003", 1, found,
              _doc_fetch_detail if found else f"ID not in document ({_doc_fetch_detail})")
    except Exception as e:
        check("14. Document contains ID CTS-2026-0418-ERNSER-003", 1, False, f"exception: {e}")


def check_15_doc_content() -> None:
    """Document content probe: sections, assessments, transfer plan, signatures,
    and live-DB cross-checks (allergy + recent encounter dates)."""
    label = "15. Document content and DB cross-checks"
    if not _pid:
        check(label, 3, False, "gate: patient lookup failed (ck1)")
        return
    try:
        text = _get_doc_text()
        if text is None:
            check(label, 3, False, _doc_fetch_detail)
            return

        sec_hits = sum(1 for s in DOC_SECTIONS if _norm_text(s) in text)
        sections_ok = sec_hits >= 7
        functional_ok = _norm_text(FUNCTIONAL_OBS) in text
        cognitive_ok = _norm_text(COGNITIVE_OBS) in text
        reason_ok = _norm_text(TRANSFER_REASON_SNIPPET) in text
        target_ok = TREATMENT_TARGET in text
        signatures_ok = "dr. janet crooks" in text and "dr. gertrud kuhic" in text

        # Live DB cross-check: active allergies of the patient must be in the doc
        # (seed: Lisinopril + Allergic disposition, both activity=1; only assert
        # Lisinopril — pid has no active problems/medications, so no row asserts there).
        alg_out = openemr_sql(
            f"SELECT title FROM lists WHERE pid={_pid} AND type='allergy' AND activity=1;"
        )
        live_has_lisinopril = "lisinopril" in _norm_text(alg_out)
        allergy_ok = (not live_has_lisinopril) or ("lisinopril" in text)

        # Live DB cross-check: >=3 of the patient's pre-existing encounter dates
        # (dates strictly before the transition anchor) appear in the doc.
        enc_out = openemr_sql(
            f"SELECT DISTINCT DATE(date) FROM form_encounter "
            f"WHERE pid={_pid} AND date < '2026-03-06' ORDER BY date DESC;"
        )
        seed_dates = [ln.strip() for ln in enc_out.strip().split("\n") if ln.strip()]
        date_hits = 0
        for d in seed_dates:
            y, m, dd = d.split("-")
            variants = [d, f"{m}/{dd}/{y}", f"{int(m)}/{int(dd)}/{y}"]
            if any(v in text for v in variants):
                date_hits += 1
        dates_needed = min(3, len(seed_dates))
        dates_ok = date_hits >= dates_needed

        passed = (sections_ok and functional_ok and cognitive_ok and reason_ok
                  and target_ok and signatures_ok and allergy_ok and dates_ok)
        fails = []
        if not sections_ok:
            fails.append(f"sections {sec_hits}/{len(DOC_SECTIONS)} (<7)")
        if not functional_ok:
            fails.append("functional sentence missing")
        if not cognitive_ok:
            fails.append("cognitive sentence missing")
        if not reason_ok:
            fails.append("transfer reason missing")
        if not target_ok:
            fails.append("target date 2026-11-30 missing")
        if not signatures_ok:
            fails.append("dual signatures missing")
        if not allergy_ok:
            fails.append("allergy Lisinopril missing")
        if not dates_ok:
            fails.append(f"encounter dates {date_hits}/{len(seed_dates)} (<{dates_needed})")
        check(label, 3, passed,
              f"sections {sec_hits}/{len(DOC_SECTIONS)}; enc dates {date_hits}/{len(seed_dates)}; "
              f"all content present" if passed else "; ".join(fails))
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_01_patient_exists()
    check_02_new_encounter()
    check_03_functional_status()
    check_04_cognitive_status()
    check_05_treatment_plan_goals()
    check_06_treatment_plan_target()
    check_07_transfer_summary_reason()
    check_08_transfer_summary_facility()
    check_09_icd10_codes()
    check_10_disclosure_record()
    check_11_message_subject()
    check_12_message_body()
    check_13_onlyoffice_document()
    check_14_onlyoffice_doc_id()
    check_15_doc_content()

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
