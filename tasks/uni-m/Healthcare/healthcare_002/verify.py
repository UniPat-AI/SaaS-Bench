"""
Verifier for Healthcare-002-I4: Compile Cardiology Referral for Dortha Brakus

Checks: 12 weighted checks (3 zero-weight gates) across openemr, onlyoffice.
Strategy: docker exec (MariaDB) for OpenEMR; OnlyOffice document read fs-first
(portal data dir via docker exec), REST API fallback.

Required env vars:
  SERVER_HOSTNAME, OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import io
import os
import re
import subprocess
import sys
import time
import zipfile

try:
    import requests
except ImportError:
    print("FATAL: requests library not available", file=sys.stderr)
    sys.exit(1)

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

for _var_name, _var_val in [
    ("OPENEMR_PORT", OPENEMR_PORT),
    ("OPENEMR_CONTAINER", OPENEMR_CONTAINER),
    ("OPENEMR_DB_CONTAINER", OPENEMR_DB_CONTAINER),
    ("ONLYOFFICE_PORT", ONLYOFFICE_PORT),
    ("ONLYOFFICE_CONTAINER", ONLYOFFICE_CONTAINER),
    ("ONLYOFFICE_DB_CONTAINER", ONLYOFFICE_DB_CONTAINER),
]:
    if not _var_val:
        print(f"FATAL: {_var_name} not set", file=sys.stderr)
        sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
# probed seed max, mw-openemr:latest 2026-08
SEED_MAX_ENCOUNTER_DATE = "2026-03-05"
# probed seed max, mw-openemr:latest 2026-08
SEED_MAX_LISTS_DATE = "2026-03-22 13:24:32"

# Full-title LIKE patterns (em-dash / hyphen / any-dash variants); DB titles may
# carry a .docx suffix, so patterns end with '%'.
OO_TITLE_PATTERNS = [
    "Cardiology Referral — Dortha Brakus%",
    "Cardiology Referral - Dortha Brakus%",
    "Cardiology Referral%Dortha Brakus%",
]

# Strong distinguishing fragments of the required referral-reason sentence
# (exact phrases from description.md); shared by check 4 and check 8, AND-ed.
REFERRAL_FRAGMENTS = (
    "progressive dyspnea on exertion",
    "reduced ejection fraction",
    "suspected heart failure",
)


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
    """Execute SQL on OpenEMR MariaDB and return stdout. Raises on error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Execute SQL on OnlyOffice MySQL and return stdout. Raises on error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


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


def _docx_tables(data: bytes) -> list[list[str]]:
    """Per <w:tbl>: list of row texts (cells joined with ' | ')."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    tables = []
    for tbl in re.findall(r"<w:tbl(?:\s[^>]*)?>.*?</w:tbl>", xml, flags=re.DOTALL):
        rows = []
        for tr in re.findall(r"<w:tr(?:\s[^>]*)?>.*?</w:tr>", tbl, flags=re.DOTALL):
            cells = []
            for tc in tr.split("</w:tc>"):
                runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", tc, flags=re.DOTALL)
                if runs:
                    cells.append(_xml_unescape("".join(runs)))
            if cells:
                rows.append(" | ".join(cells))
        tables.append(rows)
    return tables


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


# ── Resolution (gates) ────────────────────────────────────────────────────────
def _resolve_patient() -> tuple[str | None, list[str], str]:
    """Canonical pid via DOB disambiguation (79 duplicate-name pairs in seed).
    Returns (pid|None, all_name_pids, detail)."""
    dob_rows = openemr_sql(
        "SELECT pid FROM patient_data "
        "WHERE fname='Dortha' AND lname='Brakus' AND DOB='1953-11-26';"
    )
    dob_pids = [ln.strip() for ln in dob_rows.splitlines() if ln.strip()]
    all_rows = openemr_sql(
        "SELECT pid FROM patient_data WHERE fname='Dortha' AND lname='Brakus';"
    )
    pid_set = [ln.strip() for ln in all_rows.splitlines() if ln.strip()]
    if len(dob_pids) != 1:
        return None, pid_set, f"DOB lookup returned {len(dob_pids)} rows (expected 1)"
    return dob_pids[0], pid_set, f"pid={dob_pids[0]} name-pids={','.join(pid_set)}"


def _resolve_new_encounter(pid: str, pid_set: list[str]) -> tuple[str | None, str | None, str]:
    """Anchor encounter: date > seed max, preferring the canonical DOB pid,
    falling back to the duplicate-name pids (agent may have used either chart).
    Returns (encounter|None, owning_pid|None, detail)."""
    ordered = [pid] + [p for p in pid_set if p != pid]
    for p in ordered:
        rows = openemr_sql(
            "SELECT encounter, date FROM form_encounter "
            f"WHERE pid={p} AND date > '{SEED_MAX_ENCOUNTER_DATE}' "
            "ORDER BY date DESC, encounter DESC;"
        )
        if rows:
            parts = rows.splitlines()[0].split("\t")
            enc = parts[0].strip()
            enc_date = parts[1].strip() if len(parts) > 1 else ""
            return enc, p, f"encounter={enc} pid={p} date={enc_date}"
    return None, None, (
        f"no encounter with date > '{SEED_MAX_ENCOUNTER_DATE}' on pids "
        + ",".join(ordered)
    )


# ── OpenEMR Checks ────────────────────────────────────────────────────────────
def check_2_allergy_entry(pid_set: list[str], pid_ok: bool) -> None:
    """Allergy 'Iodinated contrast media': reaction, severity 'Severe' (hard
    gate), active, entered after the seed lists max date."""
    label = "2. Allergy 'Iodinated contrast media' (reaction/severity/active/new)"
    if not pid_ok:
        check(label, 2, False, "gate: patient pid unresolved (check 1 failed)")
        return
    try:
        pid_in = ",".join(pid_set)
        result = openemr_sql(
            "SELECT l.title, l.reaction, IFNULL(lo.title,''), l.severity_al, l.activity "
            "FROM lists l "
            "LEFT JOIN list_options lo "
            "ON lo.list_id='reaction' AND lo.option_id=l.reaction "
            f"WHERE l.pid IN ({pid_in}) AND l.type='allergy' "
            "AND l.title LIKE '%Iodinated contrast media%' "
            f"AND l.date > '{SEED_MAX_LISTS_DATE}';"
        )
        if not result:
            check(label, 2, False,
                  f"no new allergy row (title LIKE Iodinated contrast media, date > '{SEED_MAX_LISTS_DATE}')")
            return
        best_detail = ""
        passed = False
        for line in result.splitlines():
            parts = line.split("\t")
            title = parts[0] if len(parts) > 0 else ""
            reaction = parts[1] if len(parts) > 1 else ""
            reaction_title = parts[2] if len(parts) > 2 else ""
            severity = parts[3] if len(parts) > 3 else ""
            activity = parts[4] if len(parts) > 4 else ""
            reaction_ok = (
                reaction.strip() == "anaphylactoid_hypotension"
                or _norm_text(reaction_title) == "anaphylactoid reaction with hypotension"
            )
            severity_ok = severity.strip().lower() == "severe"
            activity_ok = activity.strip() == "1"
            row_detail = (f"title='{title[:40]}', reaction='{reaction[:40]}', "
                          f"severity='{severity}', activity={activity}")
            if not best_detail:
                best_detail = row_detail
            if reaction_ok and severity_ok and activity_ok:
                passed = True
                best_detail = row_detail
                break
        check(label, 2, passed, best_detail[:200])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_clinical_notes(enc: str | None, enc_pid: str | None, enc_detail: str) -> None:
    """Clinical Notes on the anchored new encounter contain the full referral
    reason (AND of the three distinguishing fragments)."""
    label = "4. Clinical Notes on new encounter contain referral reason"
    if enc is None:
        check(label, 2, False, f"gate: new encounter unresolved ({enc_detail[:120]})")
        return
    try:
        result = openemr_sql(
            "SELECT cn.description FROM form_clinical_notes cn "
            f"WHERE cn.pid={enc_pid} AND cn.encounter={enc};"
        )
        text = _norm_text(result)
        missing = [f for f in REFERRAL_FRAGMENTS if f not in text]
        check(label, 2, bool(result) and not missing,
              f"all fragments present (encounter={enc})" if result and not missing
              else f"missing fragments: {missing}; got: '{text[:120]}'")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_fee_sheet_icd(enc: str | None, enc_pid: str | None, enc_detail: str) -> None:
    """Billing row on the anchored new encounter: ICD10 I50.22, active."""
    label = "5. Fee Sheet on new encounter has active ICD10 I50.22"
    if enc is None:
        check(label, 2, False, f"gate: new encounter unresolved ({enc_detail[:120]})")
        return
    try:
        result = openemr_sql(
            "SELECT b.code, b.code_type, b.activity FROM billing b "
            f"WHERE b.pid={enc_pid} AND b.encounter={enc} "
            "AND b.code_type='ICD10' AND b.code='I50.22' AND b.activity=1;"
        )
        passed = bool(result)
        check(label, 2, passed,
              f"found: {result[:120]}" if passed
              else f"no active ICD10 I50.22 billing row on encounter {enc}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OnlyOffice Checks ─────────────────────────────────────────────────────────
def check_7_clinic_and_demographics(text: str | None, doc_detail: str) -> None:
    label = "7. Document has clinic name & patient demographics"
    if text is None:
        check(label, 2, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    try:
        has_clinic = "hingham senior care medical group" in text
        has_dob = ("1953-11-26" in text or "11/26/1953" in text
                   or "november 26" in text or "11-26-1953" in text)
        has_name = "dortha" in text and "brakus" in text
        issues = []
        if not has_clinic:
            issues.append("full clinic name missing")
        if not has_dob:
            issues.append("DOB missing")
        if not has_name:
            issues.append("patient name missing")
        check(label, 2, not issues,
              "all present" if not issues else "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_referral_reason(text: str | None, doc_detail: str) -> None:
    label = "8. Document has full referral reason"
    if text is None:
        check(label, 2, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    try:
        missing = [f for f in REFERRAL_FRAGMENTS if f not in text]
        check(label, 2, not missing,
              "all fragments present" if not missing
              else f"missing fragments: {missing}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_provider_names(text: str | None, doc_detail: str) -> None:
    label = "9. Document has provider full names & specialty"
    if text is None:
        check(label, 2, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    try:
        has_requesting = "rebecca lindstrom" in text
        has_receiving = "hiroshi tanaka" in text
        has_specialty = ("advanced heart failure" in text
                         or "transplant cardiology" in text)
        issues = []
        if not has_requesting:
            issues.append("'Rebecca Lindstrom' not found")
        if not has_receiving:
            issues.append("'Hiroshi Tanaka' not found")
        if not has_specialty:
            issues.append("specialty (advanced heart failure / transplant cardiology) not found")
        check(label, 2, not issues,
              "providers and specialty found" if not issues else "; ".join(issues))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_10_allergy_in_doc(text: str | None, doc_detail: str) -> None:
    label = "10. Document has allergy with reaction & severity"
    if text is None:
        check(label, 1, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    try:
        has_allergy = "iodinated contrast" in text
        has_reaction = "anaphylactoid reaction with hypotension" in text
        has_severity = "severe" in text
        issues = []
        if not has_allergy:
            issues.append("'iodinated contrast' missing")
        if not has_reaction:
            issues.append("reaction phrase missing")
        if not has_severity:
            issues.append("'severe' missing")
        check(label, 1, not issues,
              "allergy, reaction and severity present" if not issues else "; ".join(issues))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def _date_variants(iso: str) -> list[str]:
    """ISO date + MM/DD/YYYY renderings (zero-padded and not)."""
    y, m, d = iso.split("-")
    return [iso, f"{m}/{d}/{y}", f"{int(m)}/{int(d)}/{y}"]


def check_11_encounter_table(tables: list[list[str]] | None, doc_detail: str,
                             pid_set: list[str], pid_ok: bool) -> None:
    """Encounter summary table: required header columns, and >=2 real seed
    encounter dates (queried live) from a single pid's chart in the data rows.
    Both duplicate-name pids' date sets are accepted (task-data flaw: the
    agent may legitimately have used either chart)."""
    label = "11. Document encounter summary table (header + real encounter dates)"
    if tables is None:
        check(label, 2, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    if not pid_ok:
        check(label, 2, False, "gate: patient pid unresolved (check 1 failed)")
        return
    try:
        header_req = ["date", "diagnosis code", "diagnosis description", "medications"]
        date_sets: dict[str, list[str]] = {}
        for p in pid_set:
            rows = openemr_sql(
                "SELECT DISTINCT DATE(date) FROM form_encounter "
                f"WHERE pid={p} AND date <= '{SEED_MAX_ENCOUNTER_DATE}';"
            )
            dates = [ln.strip() for ln in rows.splitlines()
                     if ln.strip() and ln.strip() != "NULL"]
            if dates:
                date_sets[p] = sorted(set(dates))
        header_found = False
        passed = False
        detail = "no <w:tbl> with required header row found"
        for tbl in tables:
            if not tbl:
                continue
            hdr = _norm_text(tbl[0])
            if not all(h in hdr for h in header_req):
                continue
            header_found = True
            body = _norm_text(" | ".join(tbl[1:]))
            for p, dates in date_sets.items():
                hits = [d for d in dates
                        if any(v in body for v in _date_variants(d))]
                if len(hits) >= 2:
                    passed = True
                    detail = f"header ok; pid {p} seed dates matched: {','.join(hits)[:80]}"
                    break
            if passed:
                break
            detail = (f"header ok but <2 seed encounter dates in data rows; "
                      f"expected from {dict((k, ','.join(v)) for k, v in date_sets.items())}")[:200]
        if not header_found:
            detail = f"no table with header columns {header_req} (tables={len(tables)})"
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_problems_and_signature(text: str | None, doc_detail: str,
                                    pid_set: list[str], pid_ok: bool) -> None:
    """Active problems section (either a 'no active problems' statement or a
    real active problem title queried live) plus a signature line."""
    label = "12. Document has active problems section & signature line"
    if text is None:
        check(label, 1, False, f"gate: document unavailable ({doc_detail[:120]})")
        return
    if not pid_ok:
        check(label, 1, False, "gate: patient pid unresolved (check 1 failed)")
        return
    try:
        pid_in = ",".join(pid_set)
        rows = openemr_sql(
            "SELECT title FROM lists "
            f"WHERE pid IN ({pid_in}) AND type='medical_problem' AND activity=1;"
        )
        titles = [_norm_text(ln) for ln in rows.splitlines() if ln.strip()]
        matched = [t for t in titles if t and t in text]
        no_active_stmt = "no active" in text and "problem" in text
        problems_ok = no_active_stmt or bool(matched)
        sig_ok = "signature" in text or "signed" in text
        issues = []
        if not problems_ok:
            issues.append(f"no active-problems content (live titles: {'; '.join(titles)[:80]})")
        if not sig_ok:
            issues.append("no signature/signed line")
        detail = ("problems: " + ("statement" if no_active_stmt else ",".join(matched)[:60])
                  + "; signature present") if not issues else "; ".join(issues)
        check(label, 1, not issues, detail[:200])
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Gate 1: canonical patient pid (DOB-disambiguated; duplicate chart 307 has
    # DOB NULL).
    pid: str | None = None
    pid_set: list[str] = []
    pid_detail = ""
    try:
        pid, pid_set, pid_detail = _resolve_patient()
    except Exception as e:
        pid_detail = f"exception: {e}"
    check("1. [gate] Patient Dortha Brakus (DOB 1953-11-26) resolved", 0,
          pid is not None, pid_detail)

    check_2_allergy_entry(pid_set, pid is not None)

    # Gate 3: new encounter anchored past the seed max date.
    enc: str | None = None
    enc_pid: str | None = None
    enc_detail = ""
    if pid is None:
        enc_detail = "gate: patient pid unresolved (check 1 failed)"
    else:
        try:
            enc, enc_pid, enc_detail = _resolve_new_encounter(pid, pid_set)
        except Exception as e:
            enc_detail = f"exception: {e}"
    check(f"3. [gate] New encounter (date > '{SEED_MAX_ENCOUNTER_DATE}') resolved", 0,
          enc is not None, enc_detail)

    check_4_clinical_notes(enc, enc_pid, enc_detail)
    check_5_fee_sheet_icd(enc, enc_pid, enc_detail)

    # Gate 6: locate + fetch + parse the referral document (fs-first).
    doc_text: str | None = None
    doc_tables: list[list[str]] | None = None
    doc_detail = ""
    try:
        doc_bytes, doc_detail = _oo_get_document(OO_TITLE_PATTERNS)
        if doc_bytes is not None:
            try:
                doc_text = _norm_text(_docx_text(doc_bytes))
                doc_tables = _docx_tables(doc_bytes)
            except Exception as e:
                doc_text, doc_tables = None, None
                doc_detail += f"; docx parse failed: {e}"
    except Exception as e:
        doc_detail = f"exception: {e}"
    check("6. [gate] OnlyOffice referral document located & readable", 0,
          doc_text is not None, doc_detail[:200])

    check_7_clinic_and_demographics(doc_text, doc_detail)
    check_8_referral_reason(doc_text, doc_detail)
    check_9_provider_names(doc_text, doc_detail)
    check_10_allergy_in_doc(doc_text, doc_detail)
    check_11_encounter_table(doc_tables, doc_detail, pid_set, pid is not None)
    check_12_problems_and_signature(doc_text, doc_detail, pid_set, pid is not None)

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
