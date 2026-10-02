"""
Verifier for Healthcare-023-I4: Preventive Health Counseling Visit with Personalized Letter for Norman Rath

Checks: 15 weighted checks across openemr (13) and onlyoffice (2); total weight 22.
Strategy: docker exec (MariaDB for OpenEMR, MySQL for OnlyOffice) + fs-first docx
retrieval (OnlyOffice Data dir by files_file id, REST API fallback).

Patient disambiguation: two Norman Rath rows exist in the seed DB (pid 174 with
DOB 1987-01-23, pid 331 with NULL DOB); pid is resolved via fname+lname+DOB.
Encounter anchoring: the graded encounter is the latest form_encounter row dated
after the seed maximum; if absent, all encounter-level checks chain-FAIL.

Required env vars:
  SERVER_HOSTNAME, OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER.
"""

import io
import os
import re
import subprocess
import sys
import time
import zipfile

import requests

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

OPENEMR_BASE = f"http://{HOST}:{OPENEMR_PORT}"
ONLYOFFICE_BASE = f"http://{HOST}:{ONLYOFFICE_PORT}"

# Date anchors  # probed seed max, mw-openemr:latest 2026-08
SEED_MAX_ENCOUNTER_DATE = "2026-03-05"          # form_encounter MAX(date) = 2026-03-05 00:00:00
SEED_MAX_HISTORY_DATE = "2026-03-22 05:30:20"   # history_data MAX(date)

# Exact required strings from description.md
TOBACCO_SENTENCE = "Occasional cigar smoker, 2-3 cigars per month"
ALCOHOL_SENTENCE = "Moderate drinker, 4-5 drinks per week, mostly wine"
EXERCISE_SENTENCE = "Moderately active, cycles 2 times weekly and yard work on weekends"
LETTER_TITLE = "Preventive Health Summary Letter - Norman Rath - April 2026"

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
    """Run a SQL query against OpenEMR MariaDB and return stdout. Raises on error."""
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
    """Run a SQL query against OnlyOffice MySQL and return stdout. Raises on error."""
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


def get_patient_pid() -> str | None:
    """Norman Rath's pid, disambiguated by DOB (seed has a NULL-DOB duplicate)."""
    result = openemr_sql(
        "SELECT pid FROM patient_data "
        "WHERE fname='Norman' AND lname='Rath' AND DOB='1987-01-23';"
    )
    rows = [r.strip() for r in result.splitlines() if r.strip()]
    return rows[0] if len(rows) == 1 else None


def get_anchor_encounter(pid: str) -> str | None:
    """Latest form_encounter dated after the seed max — the graded encounter."""
    result = openemr_sql(
        f"SELECT encounter FROM form_encounter "
        f"WHERE pid={pid} AND date > '{SEED_MAX_ENCOUNTER_DATE}' "
        f"ORDER BY date DESC, encounter DESC LIMIT 1;"
    )
    return result.strip() or None


def _approx(val_str: str, expected: float, tol: float = 0.1) -> bool:
    """Check if a numeric string is approximately equal to expected."""
    try:
        return abs(float(val_str) - expected) <= tol
    except (ValueError, TypeError):
        return False


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


# ── OnlyOffice document retrieval (fs-first, API fallback) ───────────────────
def _oo_find_letter() -> tuple[str, str] | None:
    """Locate the letter in files_file by EXACT title (a '.docx' suffix is
    tolerated; editor crash-recovery copies excluded). Returns (id, title)."""
    out = onlyoffice_sql(
        "SELECT id, title FROM files_file "
        f"WHERE title LIKE '{LETTER_TITLE}%' AND title NOT LIKE '%Recovery%' "
        "ORDER BY id DESC;"
    )
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            fid, title = parts[0].strip(), parts[1].strip()
            if title in (LETTER_TITLE, LETTER_TITLE + ".docx"):
                return fid, title
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
    s = requests.Session()
    try:
        resp = s.post(f"{ONLYOFFICE_BASE}/api/2.0/authentication",
                      json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
                      timeout=15)
        if resp.status_code not in (200, 201):
            return None
        token = resp.json().get("response", {}).get("token", "")
        if not token:
            return None
        s.headers.update({"Authorization": f"Bearer {token}"})
        s.cookies.set("asc_auth_key", token)
        return s
    except Exception:
        return None


def _oo_bytes_from_api(file_id: str) -> bytes | None:
    s = _oo_auth_session()
    if not s:
        return None
    for url, params in (
        (f"{ONLYOFFICE_BASE}/products/files/httphandlers/filehandler.ashx",
         {"action": "download", "fileid": str(file_id)}),
        (f"{ONLYOFFICE_BASE}/api/2.0/files/file/{file_id}/download", None),
    ):
        try:
            r = s.get(url, params=params, timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100:
                return r.content
        except Exception:
            continue
    return None


# ── Individual checks ─────────────────────────────────────────────────────────

def get_history_row(pid: str) -> tuple[str, str, str] | None:
    """Latest history_data row dated after the seed max (the agent's update)."""
    result = openemr_sql(
        f"SELECT tobacco, alcohol, exercise_patterns FROM history_data "
        f"WHERE pid={pid} AND date > '{SEED_MAX_HISTORY_DATE}' "
        f"ORDER BY date DESC, id DESC LIMIT 1;"
    )
    if not result.strip():
        return None
    parts = result.split("\t")
    parts += [""] * (3 - len(parts))
    return parts[0], parts[1], parts[2]


def check_1_social_history_tobacco(hist: tuple[str, str, str] | None,
                                   fail_detail: str) -> None:
    """Verify tobacco column contains the full required sentence."""
    try:
        if hist is None:
            check("1. Social history - tobacco", 1, False, fail_detail)
            return
        tobacco = hist[0]
        passed = TOBACCO_SENTENCE.lower() in tobacco.lower()
        check("1. Social history - tobacco", 1, passed, f"got: {tobacco[:80]}")
    except Exception as e:
        check("1. Social history - tobacco", 1, False, f"exception: {e}")


def check_2_social_history_alcohol(hist: tuple[str, str, str] | None,
                                   fail_detail: str) -> None:
    """Verify alcohol column contains the full required sentence."""
    try:
        if hist is None:
            check("2. Social history - alcohol", 1, False, fail_detail)
            return
        alcohol = hist[1]
        passed = ALCOHOL_SENTENCE.lower() in alcohol.lower()
        check("2. Social history - alcohol", 1, passed, f"got: {alcohol[:80]}")
    except Exception as e:
        check("2. Social history - alcohol", 1, False, f"exception: {e}")


def check_3_social_history_exercise(hist: tuple[str, str, str] | None,
                                    fail_detail: str) -> None:
    """Verify exercise_patterns column contains the full required sentence."""
    try:
        if hist is None:
            check("3. Social history - exercise", 1, False, fail_detail)
            return
        exercise = hist[2]
        passed = EXERCISE_SENTENCE.lower() in exercise.lower()
        check("3. Social history - exercise", 1, passed, f"got: {exercise[:80]}")
    except Exception as e:
        check("3. Social history - exercise", 1, False, f"exception: {e}")


def check_5_vitals_and_bmi(pid: str, enc: str | None) -> None:
    """Verify vitals on the anchored encounter (forms registry join) and the
    auto-calculated BMI value. Emits check 5 (2pt) and check 5b (1pt)."""
    if enc is None:
        gate = f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}"
        check("5. Vitals recorded", 2, False, gate)
        check("5b. BMI auto-calculated in range", 1, False, gate)
        return
    try:
        result = openemr_sql(
            f"SELECT v.bps, v.bpd, v.pulse, v.temperature, v.respiration, "
            f"v.height, v.weight, v.BMI "
            f"FROM form_vitals v JOIN forms f ON f.form_id = v.id "
            f"AND f.formdir='vitals' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} "
            f"ORDER BY v.id DESC LIMIT 1;"
        )
        if not result.strip():
            check("5. Vitals recorded", 2, False, "no vitals form on anchored encounter")
            check("5b. BMI auto-calculated in range", 1, False,
                  "no vitals form on anchored encounter")
            return
        parts = result.split("\t")
        if len(parts) < 8:
            check("5. Vitals recorded", 2, False, f"unexpected format: {result[:100]}")
            check("5b. BMI auto-calculated in range", 1, False,
                  f"unexpected format: {result[:100]}")
            return

        bps, bpd, pulse, temp, resp, height, weight, bmi = [p.strip() for p in parts[:8]]
        issues = []
        if not _approx(bps, 134):
            issues.append(f"bps={bps}")
        if not _approx(bpd, 86):
            issues.append(f"bpd={bpd}")
        if not _approx(pulse, 74):
            issues.append(f"pulse={pulse}")
        if not _approx(temp, 98.5, tol=0.5):
            issues.append(f"temp={temp}")
        if not _approx(resp, 15):
            issues.append(f"resp={resp}")
        if not _approx(height, 69, tol=1):
            issues.append(f"height={height}")
        if not _approx(weight, 195, tol=1):
            issues.append(f"weight={weight}")

        check("5. Vitals recorded", 2, not issues,
              "all vitals match" if not issues else f"mismatches: {issues}")

        try:
            bmi_val = float(bmi)
            bmi_ok = 28.5 <= bmi_val <= 29.1
        except (ValueError, TypeError):
            bmi_ok = False
        check("5b. BMI auto-calculated in range", 1, bmi_ok,
              f"BMI={bmi} (expected 28.5-29.1)")
    except Exception as e:
        check("5. Vitals recorded", 2, False, f"exception: {e}")
        check("5b. BMI auto-calculated in range", 1, False, f"exception: {e}")


def check_6_ros_form(pid: str, enc: str | None) -> None:
    """Verify a ROS form is registered on the anchored encounter.

    Structural ceiling: form_ros has only varchar(3) YES/NO columns — the three
    free-text ROS findings from the description have no column to land in, so
    only anchored existence is checkable.
    """
    if enc is None:
        check("6. ROS form completed", 1, False,
              f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}")
        return
    try:
        result = openemr_sql(
            f"SELECT COUNT(*) FROM form_ros r JOIN forms f ON f.form_id = r.id "
            f"AND f.formdir='ros' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc};"
        )
        count = int(result.strip()) if result.strip().isdigit() else 0
        check("6. ROS form completed", 1, count > 0,
              f"anchored form_ros count: {count} (content unverifiable: no free-text columns)")
    except Exception as e:
        check("6. ROS form completed", 1, False, f"exception: {e}")


def check_7_observation_form(pid: str, enc: str | None) -> None:
    """Verify Observation form rows on the anchored encounter contain the
    general-appearance and cardiovascular exam findings."""
    if enc is None:
        check("7. Physical Exam (Observation) form", 1, False,
              f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}")
        return
    try:
        result = openemr_sql(
            f"SELECT observation, ob_value, description FROM form_observation "
            f"WHERE pid={pid} AND encounter={enc} AND activity=1;"
        )
        if not result.strip():
            check("7. Physical Exam (Observation) form", 1, False,
                  "no form_observation rows on anchored encounter")
            return
        rows = [r.lower() for r in result.splitlines() if r.strip()]
        # <=80-char probes to dodge varchar(255) truncation of long values
        probe_general = "well-groomed, alert and cooperative"
        probe_cardio = "normal s1 s2, regular rhythm"
        has_general = any(probe_general in r for r in rows)
        has_cardio = any(probe_cardio in r for r in rows)
        issues = []
        if not has_general:
            issues.append("missing general appearance finding")
        if not has_cardio:
            issues.append("missing cardiovascular finding")
        check("7. Physical Exam (Observation) form", 1, not issues,
              f"{len(rows)} row(s), both findings present" if not issues
              else f"issues: {issues}")
    except Exception as e:
        check("7. Physical Exam (Observation) form", 1, False, f"exception: {e}")


def _get_soap_row(pid: str, enc: str) -> list[str] | None:
    """Latest form_soap row on the anchored encounter via the forms registry."""
    result = openemr_sql(
        f"SELECT s.subjective, s.objective, s.assessment, s.plan "
        f"FROM form_soap s JOIN forms f ON f.form_id = s.id "
        f"AND f.formdir='soap' AND f.deleted=0 "
        f"WHERE f.pid={pid} AND f.encounter={enc} "
        f"ORDER BY s.id DESC LIMIT 1;"
    )
    if not result.strip():
        return None
    parts = result.split("\t")
    parts += [""] * (4 - len(parts))
    return parts[:4]


def check_8_soap_note(soap: list[str] | None, gate_detail: str) -> None:
    """Verify SOAP note Subjective and Objective content on the anchored encounter."""
    try:
        if soap is None:
            check("8. SOAP note (S/O)", 3, False, gate_detail)
            return
        subjective = soap[0].lower()
        objective = soap[1].lower()

        issues = []
        if "preventive health counseling" not in subjective:
            issues.append("subjective missing 'preventive health counseling'")
        if "borderline" not in subjective and "cholesterol" not in subjective:
            issues.append("subjective missing cholesterol reference")
        if "strategies to improve cardiovascular health" not in subjective:
            issues.append("subjective missing 'strategies to improve cardiovascular health'")
        if "134/86" not in objective:
            issues.append("objective missing BP 134/86")
        if "bmi calculated in overweight range" not in objective:
            issues.append("objective missing 'BMI calculated in overweight range'")

        check("8. SOAP note (S/O)", 3, not issues,
              "S/O content matches" if not issues else f"issues: {issues}"[:160])
    except Exception as e:
        check("8. SOAP note (S/O)", 3, False, f"exception: {e}")


def check_9_soap_assessment_plan(soap: list[str] | None, gate_detail: str) -> None:
    """Verify SOAP note Assessment and Plan content on the anchored encounter."""
    try:
        if soap is None:
            check("9. SOAP note (A/P)", 2, False, gate_detail)
            return
        assessment = soap[2].lower()
        plan = soap[3].lower()

        issues = []
        if "pre-hypertension" not in assessment:
            issues.append("assessment missing 'Pre-hypertension'")
        if "borderline dyslipidemia" not in assessment:
            issues.append("assessment missing 'Borderline dyslipidemia'")
        if "mediterranean" not in plan:
            issues.append("plan missing 'Mediterranean'")
        if "150 min" not in plan:
            issues.append("plan missing '150 min'")
        if "cigar cessation" not in plan:
            issues.append("plan missing 'cigar cessation'")

        # Second Assessment/Plan group in the description is optional (info only)
        second = "moderate elevated cardiovascular risk" in assessment
        detail = ("A/P content matches" if not issues else f"issues: {issues}"[:140])
        check("9. SOAP note (A/P)", 2, not issues,
              f"{detail}; info: second-assessment present={second}")
    except Exception as e:
        check("9. SOAP note (A/P)", 2, False, f"exception: {e}")


def check_10_care_plan(pid: str, enc: str | None) -> None:
    """Verify Care Plan rows on the anchored encounter carry the exact goal and
    the home-BP-monitoring instruction."""
    if enc is None:
        check("10. Care Plan", 2, False,
              f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}")
        return
    try:
        result = openemr_sql(
            f"SELECT description, codetext FROM form_care_plan "
            f"WHERE pid={pid} AND encounter={enc};"
        )
        if not result.strip():
            check("10. Care Plan", 2, False,
                  "no form_care_plan rows on anchored encounter")
            return

        combined = result.lower()
        issues = []
        if "achieve bp <130/80, ldl <100, and bmi <25" not in combined:
            issues.append("missing full goal sentence 'Achieve BP <130/80, LDL <100, and BMI <25'")
        if "monitor bp at home twice weekly" not in combined:
            issues.append("missing 'Monitor BP at home twice weekly'")

        check("10. Care Plan", 2, not issues,
              "care plan matches" if not issues else f"issues: {issues}"[:160])
    except Exception as e:
        check("10. Care Plan", 2, False, f"exception: {e}")


def check_11_fee_sheet_codes(pid: str, enc: str | None) -> None:
    """Verify ICD10 Z71.3 and CPT4 99403 billed on the anchored encounter
    (same-row code_type assertions; extra codes allowed)."""
    if enc is None:
        check("11. Fee Sheet codes", 2, False,
              f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}")
        return
    try:
        result = openemr_sql(
            f"SELECT code_type, code FROM billing "
            f"WHERE pid={pid} AND encounter={enc} AND activity=1;"
        )
        if not result.strip():
            check("11. Fee Sheet codes", 2, False,
                  "no billing records on anchored encounter")
            return

        codes = set()
        for line in result.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                codes.add((parts[0].strip(), parts[1].strip()))

        issues = []
        if ("ICD10", "Z71.3") not in codes:
            issues.append("ICD10 Z71.3 not found")
        if ("CPT4", "99403") not in codes:
            issues.append("CPT4 99403 not found")

        detail = (f"found codes: {sorted(codes)}" if not issues
                  else f"issues: {issues}, found: {sorted(codes)}")
        check("11. Fee Sheet codes", 2, not issues, detail[:160])
    except Exception as e:
        check("11. Fee Sheet codes", 2, False, f"exception: {e}")


def check_12_followup_appointment(pid: str) -> None:
    """Verify follow-up appointment 2026-07-20 09:30, category 'Office Visit'
    (pc_catid resolved dynamically), comment mentions lipid panel recheck."""
    try:
        result = openemr_sql(
            f"SELECT pc_eventDate, pc_startTime, pc_catid, pc_hometext "
            f"FROM openemr_postcalendar_events "
            f"WHERE pc_pid='{pid}' AND pc_eventDate='2026-07-20' "
            f"AND pc_startTime='09:30:00' "
            f"AND pc_catid IN (SELECT pc_catid FROM openemr_postcalendar_categories "
            f"WHERE pc_catname='Office Visit') "
            f"AND pc_hometext LIKE '%lipid panel recheck%';"
        )
        passed = bool(result.strip())
        flat = result.replace("\t", " | ").replace("\n", " / ")
        check("12. Follow-up appointment", 2, passed,
              f"matched: {flat[:120]}" if passed
              else "no appointment matching date=2026-07-20, time=09:30:00, "
                   "category='Office Visit', comment~'lipid panel recheck'")
    except Exception as e:
        check("12. Follow-up appointment", 2, False, f"exception: {e}")


def check_13_onlyoffice_document_exists() -> tuple[str, str] | None:
    """Gate (0pt): the letter exists in files_file with the exact required title
    ('.docx' suffix tolerated, Recovery copies excluded). Returns (id, title)."""
    try:
        found = _oo_find_letter()
        check("13. OnlyOffice doc exists (gate)", 0, found is not None,
              f"found: id={found[0]} title={found[1][:80]}" if found
              else f"no files_file row with exact title '{LETTER_TITLE}'")
        return found
    except Exception as e:
        check("13. OnlyOffice doc exists (gate)", 0, False, f"exception: {e}")
        return None


def check_14_onlyoffice_document_content(found: tuple[str, str] | None) -> None:
    """Verify letter content: greeting, vitals values, BMI in range, the four
    recommendations, follow-up details, signature, phone, clinic name."""
    try:
        if found is None:
            check("14. OnlyOffice doc content", 3, False,
                  "gate failed: letter not found in files_file (check 13)")
            return
        file_id, _title = found
        data = _oo_bytes_from_fs(file_id)
        source = "fs"
        if data is None:
            data = _oo_bytes_from_api(file_id)
            source = "api"
        if data is None:
            check("14. OnlyOffice doc content", 3, False,
                  f"id={file_id} found but content unreadable (fs+api)")
            return

        t = _norm_text(_docx_text(data))
        nums = []
        for tok in re.findall(r"\d+(?:\.\d+)?", t):
            try:
                nums.append(float(tok))
            except ValueError:
                continue

        groups = {
            "greeting": "dear norman rath" in t,
            "bp": "134/86" in t or ("134" in t and "86" in t),
            "pulse74": "74" in t,
            "temp98.5": "98.5" in t,
            "rr15": "15" in t,
            "height69": "69" in t,
            "weight195": "195" in t,
            "bmi28.8+-0.3": any(28.5 <= n <= 29.1 for n in nums),
            "rec-mediterranean": "mediterranean-style diet rich in fish" in t,
            "rec-exercise": "150 minutes per week" in t,
            "rec-alcohol": "no more than 3 drinks per week" in t,
            "rec-cigar": "discontinue cigar smoking entirely" in t,
            "followup": "2026-07-20" in t and "9:30" in t,
            "signature": "dr. administrator, md" in t,
            "phone": "(978) 555-0400" in t,
            "clinic": "tewksbury family health associates" in t,
        }
        missing = [k for k, ok in groups.items() if not ok]
        check("14. OnlyOffice doc content", 3, not missing,
              f"all content groups found ({source} id={file_id})" if not missing
              else f"missing: {missing}"[:160])
    except Exception as e:
        check("14. OnlyOffice doc content", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Resolve canonical pid (DOB-disambiguated) and anchored encounter up front;
    # a DB error here must not kill the process before the SCORE line.
    pid: str | None = None
    pid_detail = ""
    try:
        pid = get_patient_pid()
        if pid is None:
            pid_detail = "patient Norman Rath (DOB 1987-01-23) not found or ambiguous"
    except Exception as e:
        pid_detail = f"exception: {e}"

    enc: str | None = None
    if pid is not None:
        try:
            enc = get_anchor_encounter(pid)
        except Exception as e:
            pid_detail = f"exception resolving anchor encounter: {e}"

    if pid is None:
        gate = f"gate failed: {pid_detail}"
        for label, weight in [
            ("1. Social history - tobacco", 1),
            ("2. Social history - alcohol", 1),
            ("3. Social history - exercise", 1),
            ("4. New encounter created (gate)", 0),
            ("5. Vitals recorded", 2),
            ("5b. BMI auto-calculated in range", 1),
            ("6. ROS form completed", 1),
            ("7. Physical Exam (Observation) form", 1),
            ("8. SOAP note (S/O)", 3),
            ("9. SOAP note (A/P)", 2),
            ("10. Care Plan", 2),
            ("11. Fee Sheet codes", 2),
            ("12. Follow-up appointment", 2),
        ]:
            check(label, weight, False, gate)
    else:
        hist: tuple[str, str, str] | None = None
        hist_fail = f"no history_data row after seed max {SEED_MAX_HISTORY_DATE}"
        try:
            hist = get_history_row(pid)
        except Exception as e:
            hist = None
            hist_fail = f"exception: {e}"

        check_1_social_history_tobacco(hist, hist_fail)
        check_2_social_history_alcohol(hist, hist_fail)
        check_3_social_history_exercise(hist, hist_fail)

        check("4. New encounter created (gate)", 0, enc is not None,
              f"anchored encounter id: {enc}" if enc is not None
              else f"no form_encounter with date > '{SEED_MAX_ENCOUNTER_DATE}' "
                   f"({pid_detail or 'none created'})")

        check_5_vitals_and_bmi(pid, enc)
        check_6_ros_form(pid, enc)
        check_7_observation_form(pid, enc)

        soap: list[str] | None = None
        soap_gate = f"gate failed: no new encounter after {SEED_MAX_ENCOUNTER_DATE}"
        if enc is not None:
            try:
                soap = _get_soap_row(pid, enc)
                if soap is None:
                    soap_gate = "no form_soap on anchored encounter"
            except Exception as e:
                soap_gate = f"exception: {e}"
        check_8_soap_note(soap, soap_gate)
        check_9_soap_assessment_plan(soap, soap_gate)

        check_10_care_plan(pid, enc)
        check_11_fee_sheet_codes(pid, enc)
        check_12_followup_appointment(pid)

    found = check_13_onlyoffice_document_exists()
    check_14_onlyoffice_document_content(found)

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
