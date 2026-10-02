"""
Verifier for Healthcare-026-I2: Prior Authorization Appeal Workflow for Lazaro Lang

Checks: 11 checks (weights 0/2/2/2/0/2/3/2/0/2/3 = 18) across openemr and onlyoffice.
Strategy: docker exec DB (OpenEMR MariaDB), OnlyOffice MySQL + fs (docker exec) with
REST API fallback for document bytes.

Probed truth (bare mw-openemr:latest, 2026-08 — re-queried live at verify time,
never used as hardcoded pass values):
  - pid 155 = Lazaro Lang DOB 1960-10-19 (duplicate pid 267 has NULL DOB).
  - primary insurance: UnitedHealthcare / POL908577 / GRP3143
    (insurance_data JOIN insurance_companies, type='primary').
  - active problems (lists, type='medical_problem', activity=1) distinct titles:
    'Medication review due (situation)',
    'Abnormal findings diagnostic imaging heart+coronary circulat (finding)',
    'Loss of teeth (disorder)'. Gingivitis is activity=0 — detail only, not gated.
  - 3 most recent seed encounters: enc 6 (2022-10-02), enc 67 (2022-01-08),
    enc 12 (2021-10-22); billing CPTs: enc6->99395(billed=0),
    enc67->99203(billed=0)+99213(billed=1), enc12->99395(billed=1).
  - form_misc_billing_options.prior_auth_number is varchar(20): UI entry of
    'AUTH-2026-0322-LL-APL' (21 chars) truncates to 'AUTH-2026-0322-LL-AP'.

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

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

for _var in [
    "OPENEMR_PORT", "OPENEMR_CONTAINER", "OPENEMR_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
]:
    if not os.environ.get(_var):
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

# ── Date anchors ──────────────────────────────────────────────────────────────
# probed seed max, mw-openemr:latest 2026-08: form_encounter 842 rows,
# MAX(date)='2026-03-05 00:00:00'. `date > '2026-03-05'` excludes the seed max row.
SEED_MAX_ENCOUNTER_DATE = "2026-03-05"

EXPECTED_LETTER_TITLE = "Prior Authorization Appeal Letter - Lang, Lazaro - 2026-03-22"
EXPECTED_XLSX_TITLE = "Claims and Appeal Tracking - Lang, Lazaro - 2026-03-22"


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
    """Run SQL against OpenEMR MariaDB, return raw stdout. Raises on error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-B", "-e", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str, timeout: int = 15) -> str:
    """Run SQL against OnlyOffice MySQL. Raises on error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-B", "-e", query,
        timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


# ── Text normalization / parsing helpers ──────────────────────────────────────
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


def _title_exact(title: str, expected: str) -> bool:
    """Exact title after normalization, tolerating a file-extension suffix."""
    nt = re.sub(r"\.(docx?|xlsx?|oform|pdf)$", "", _norm_text(title))
    return nt == _norm_text(expected)


def _date_variants(iso: str) -> list[str]:
    """'YYYY-MM-DD' -> [YYYY-MM-DD, MM/DD/YYYY]."""
    y, m, d = iso.split("-")
    return [iso, f"{m}/{d}/{y}"]


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


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
    except KeyError:
        return []
    out = []
    for si in re.findall(r"<si>(.*?)</si>", xml, flags=re.DOTALL):
        ts = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", si, flags=re.DOTALL)
        out.append(_xml_unescape("".join(ts)))
    return out


def _xlsx_sheets(zf: zipfile.ZipFile) -> dict[str, str]:
    """sheet name -> worksheet member path."""
    wb = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
    rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
    rid_to_target = {}
    for rel in re.finditer(r"<Relationship\b[^>]*/?>", rels):
        tag = rel.group(0)
        rid = re.search(r'\bId="([^"]+)"', tag)
        tgt = re.search(r'\bTarget="([^"]+)"', tag)
        if rid and tgt:
            t = tgt.group(1)
            if not t.startswith("xl/") and not t.startswith("/"):
                t = "xl/" + t.lstrip("./")
            rid_to_target[rid.group(1)] = t.lstrip("/")
    sheets = {}
    for m in re.finditer(r"<sheet\b[^>]*/?>", wb):
        tag = m.group(0)
        nm = re.search(r'\bname="([^"]*)"', tag)
        rid = re.search(r'\br:id="([^"]*)"', tag)
        if nm and rid and rid.group(1) in rid_to_target:
            sheets[_xml_unescape(nm.group(1))] = rid_to_target[rid.group(1)]
    return sheets


def _xlsx_rows(zf: zipfile.ZipFile, sheet_path: str,
               shared: list[str] | None = None) -> list[list[str]]:
    """Rows as lists of cell strings (shared strings resolved, inline strings
    and raw numeric <v> values included). Assertions run per joined row."""
    if shared is None:
        shared = _xlsx_shared_strings(zf)
    xml = zf.read(sheet_path).decode("utf-8", errors="replace")
    rows = []
    for rowxml in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, flags=re.DOTALL):
        cells = []
        for cm in re.finditer(r"<c\b([^>]*)>(.*?)</c>", rowxml, flags=re.DOTALL):
            attrs, inner = cm.group(1), cm.group(2)
            tm = re.search(r'\bt="([^"]+)"', attrs)
            ctype = tm.group(1) if tm else ""
            if ctype == "s":
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                try:
                    idx = int(v.group(1)) if v else -1
                except ValueError:
                    idx = -1
                cells.append(shared[idx] if 0 <= idx < len(shared) else "")
            elif ctype in ("inlineStr", "str"):
                ts = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", inner, flags=re.DOTALL)
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells.append(_xml_unescape("".join(ts)) if ts
                             else (_xml_unescape(v.group(1)) if v else ""))
            else:
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells.append(v.group(1).strip() if v else "")
        rows.append(cells)
    return rows


# ── OnlyOffice document retrieval (fs-first, API fallback) ────────────────────
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
    """Read content.docx/xlsx for a file id from the portal data dir (docker
    exec). Retries to tolerate OnlyOffice save/conversion delay."""
    for attempt in range(retries):
        try:
            rc, out, _ = docker_exec(
                ONLYOFFICE_CONTAINER, "bash", "-c",
                f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.*' "
                "2>/dev/null | sort -V | tail -1",
                timeout=25,
            )
        except Exception:
            rc, out = 1, ""
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


def _oo_get_document(like_patterns: list[str]) -> tuple[bytes | None, str | None, str]:
    """(bytes|None, title|None, source detail). fs first, API fallback."""
    try:
        found = _oo_find_file_id(like_patterns)
    except Exception as e:
        return None, None, f"files_file lookup failed: {e}"
    if not found:
        return None, None, "document not found in files_file"
    file_id, title = found
    data = _oo_bytes_from_fs(file_id)
    if data:
        return data, title, f"fs id={file_id}"
    data = _oo_bytes_from_api(file_id)
    if data:
        return data, title, f"api id={file_id}"
    return None, title, f"id={file_id} found but content unreadable (fs+api)"


# ── Shared lookups ────────────────────────────────────────────────────────────
_patient_pid: int | None = None
_pid_resolved = False
_appeal_enc: int | None = None
_enc_resolved = False


def get_patient_pid() -> int | None:
    """Canonical pid: DOB-disambiguated (seed pid 155; duplicate 267 has NULL DOB)."""
    global _patient_pid, _pid_resolved
    if _pid_resolved:
        return _patient_pid
    out = openemr_sql(
        "SELECT pid FROM patient_data "
        "WHERE fname='Lazaro' AND lname='Lang' AND DOB='1960-10-19'"
    )
    pids = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if len(pids) == 1:
        _patient_pid = int(pids[0])
    _pid_resolved = True
    return _patient_pid


def get_appeal_encounter() -> int | None:
    """Latest form_encounter dated after the seed max for the canonical pid."""
    global _appeal_enc, _enc_resolved
    if _enc_resolved:
        return _appeal_enc
    pid = get_patient_pid()
    if pid:
        out = openemr_sql(
            f"SELECT encounter FROM form_encounter "
            f"WHERE pid={pid} AND date > '{SEED_MAX_ENCOUNTER_DATE}' "
            f"ORDER BY date DESC, encounter DESC LIMIT 1"
        )
        if out:
            _appeal_enc = int(out.split("\t")[0].splitlines()[0])
    _enc_resolved = True
    return _appeal_enc


def get_seed_encounters() -> list[tuple[int, str]]:
    """3 most recent seed encounters (id, YYYY-MM-DD), queried live.
    Probed: enc 6 (2022-10-02), enc 67 (2022-01-08), enc 12 (2021-10-22)."""
    pid = get_patient_pid()
    if not pid:
        return []
    out = openemr_sql(
        f"SELECT encounter, DATE(date) FROM form_encounter "
        f"WHERE pid={pid} AND date <= '{SEED_MAX_ENCOUNTER_DATE}' "
        f"ORDER BY date DESC, encounter DESC LIMIT 3"
    )
    rows = []
    for ln in out.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2 and parts[0].strip().isdigit():
            rows.append((int(parts[0].strip()), parts[1].strip()))
    return rows


# ── Cached OnlyOffice document content / gate state ───────────────────────────
_letter_text: str | None = None
_letter_gate_ok = False
_letter_gate_reason = "not evaluated"

_wb_sheets: dict[str, list[list[str]]] = {}
_xlsx_gate_ok = False
_xlsx_gate_reason = "not evaluated"


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_patient_exists() -> None:
    """Gate (0pt): patient Lazaro Lang (DOB 1960-10-19) exists in OpenEMR."""
    try:
        pid = get_patient_pid()
        check("1. Patient Lazaro Lang exists (gate)", 0, pid is not None,
              f"pid={pid}" if pid else "patient fname=Lazaro lname=Lang DOB=1960-10-19 not found")
    except Exception as e:
        check("1. Patient Lazaro Lang exists (gate)", 0, False, f"exception: {e}")


def _pid_enc_or_none(label: str, weight: int) -> tuple[int, int] | None:
    """Resolve (pid, appeal encounter); on failure emit chain-FAIL and return None."""
    pid = get_patient_pid()
    if not pid:
        check(label, weight, False, "gate ck1 failed: patient not found")
        return None
    enc = get_appeal_encounter()
    if not enc:
        check(label, weight, False,
              f"no new encounter after {SEED_MAX_ENCOUNTER_DATE} for pid={pid} (date anchor)")
        return None
    return pid, enc


def check_2_clinical_notes() -> None:
    """New (anchored) encounter has Clinical Notes with medical necessity justification."""
    label = "2. Clinical Notes with justification"
    try:
        resolved = _pid_enc_or_none(label, 2)
        if not resolved:
            return
        pid, enc = resolved

        needles = ("recurrent exertional chest pain", "risk-stratify")
        found = False
        src = "form_clinical_notes"
        rows = openemr_sql(
            f"SELECT description FROM form_clinical_notes "
            f"WHERE pid={pid} AND encounter={enc} AND activity=1 "
            f"AND description LIKE '%recurrent exertional chest pain%' "
            f"AND description LIKE '%risk-stratify%'"
        )
        if rows:
            found = True

        # Fallback: scan every form registered to the anchored encounter.
        if not found:
            form_ids = openemr_sql(
                f"SELECT formdir, form_id FROM forms "
                f"WHERE pid={pid} AND encounter={enc} AND deleted=0"
            )
            for line in form_ids.splitlines():
                parts = line.split("\t")
                if len(parts) < 2 or not parts[1].strip().isdigit():
                    continue
                formdir, fid = parts[0].strip(), parts[1].strip()
                try:
                    content = openemr_sql(
                        f"SELECT * FROM `form_{formdir}` WHERE id={fid}", timeout=5,
                    )
                except Exception:
                    continue
                cl = content.lower()
                if all(n in cl for n in needles):
                    found = True
                    src = f"form_{formdir}"
                    break

        check(label, 2, found,
              f"enc={enc} justification found in {src}" if found
              else f"enc={enc} justification text (chest pain + risk-stratify) not found")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_billing_codes() -> None:
    """Fee Sheet on the anchored encounter has ICD-10 I20.9, R07.9 and CPT 78452."""
    label = "3. Billing codes I20.9, R07.9, 78452"
    try:
        resolved = _pid_enc_or_none(label, 2)
        if not resolved:
            return
        pid, enc = resolved
        billing = openemr_sql(
            f"SELECT code_type, code FROM billing "
            f"WHERE pid={pid} AND encounter={enc} AND activity=1"
        )
        has_i209 = "I20.9" in billing
        has_r079 = "R07.9" in billing
        has_78452 = "78452" in billing
        ok = has_i209 and has_r079 and has_78452
        check(label, 2, ok,
              f"enc={enc}, I20.9={'Y' if has_i209 else 'N'}, "
              f"R07.9={'Y' if has_r079 else 'N'}, 78452={'Y' if has_78452 else 'N'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_misc_billing() -> None:
    """Misc Billing Options on the anchored encounter: referring provider Cassi
    McClure + prior auth AUTH-2026-0322-LL-APL (varchar(20) truncates to ...-AP)."""
    label = "4. Misc billing (provider + auth)"
    try:
        resolved = _pid_enc_or_none(label, 2)
        if not resolved:
            return
        pid, enc = resolved

        form_id = openemr_sql(
            f"SELECT form_id FROM forms "
            f"WHERE pid={pid} AND encounter={enc} AND formdir='misc_billing_options' "
            f"AND deleted=0 ORDER BY id DESC LIMIT 1"
        )
        if not form_id:
            check(label, 2, False,
                  f"no misc_billing_options form on encounter {enc}")
            return
        fid = int(form_id.split("\t")[0].splitlines()[0])

        # prior_auth_number is varchar(20) — accept the truncated UI value.
        auth_cnt = openemr_sql(
            f"SELECT COUNT(*) FROM form_misc_billing_options WHERE id={fid} "
            f"AND (prior_auth_number LIKE 'AUTH-2026-0322-LL-AP%' "
            f"OR comments LIKE '%AUTH-2026-0322-LL-APL%')"
        )
        has_auth = auth_cnt.strip().isdigit() and int(auth_cnt.strip()) > 0

        # Referring provider: name in row, or provider-ish id column resolving
        # to Cassi McClure (users.id 59 probed).
        row = openemr_sql(f"SELECT * FROM form_misc_billing_options WHERE id={fid}")
        has_provider = "Cassi" in row or "McClure" in row
        if not has_provider:
            cols = openemr_sql("SHOW COLUMNS FROM form_misc_billing_options")
            provider_cols = [
                line.split("\t")[0]
                for line in cols.splitlines()
                if any(k in line.lower() for k in ("provider", "referring"))
            ]
            for col in provider_cols:
                try:
                    val = openemr_sql(
                        f"SELECT `{col}` FROM form_misc_billing_options WHERE id={fid}"
                    ).strip()
                    if val.isdigit() and int(val) > 0:
                        user_name = openemr_sql(
                            f"SELECT CONCAT(fname,' ',lname) FROM users WHERE id={val}"
                        )
                        if "Cassi" in user_name or "McClure" in user_name:
                            has_provider = True
                            break
                except Exception:
                    continue

        ok = has_auth and has_provider
        check(label, 2, ok,
              f"enc={enc}, auth={'Y' if has_auth else 'N'}, "
              f"provider={'Y' if has_provider else 'N'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_appeal_letter_exists() -> None:
    """Gate (0pt): appeal letter exists in OnlyOffice with the exact title;
    fetches + parses the docx for checks 6-8."""
    global _letter_text, _letter_gate_ok, _letter_gate_reason
    label = "5. Appeal letter exists (gate)"
    try:
        data, title, src = _oo_get_document([
            f"{EXPECTED_LETTER_TITLE}%",
            "%Prior Authorization Appeal Letter%Lang%",
        ])
        if title is None:
            _letter_gate_reason = src
            check(label, 0, False, src)
            return
        title_ok = _title_exact(title, EXPECTED_LETTER_TITLE)
        if not title_ok:
            _letter_gate_reason = f"title mismatch: '{title[:70]}'"
            check(label, 0, False, f"title mismatch: '{title[:70]}' ({src})")
            return
        if not data:
            _letter_gate_reason = src
            check(label, 0, False, src)
            return
        text = _docx_text(data)
        if not text.strip():
            _letter_gate_reason = "docx parsed but empty"
            check(label, 0, False, f"docx parsed but empty ({src})")
            return
        _letter_text = text
        _letter_gate_ok = True
        check(label, 0, True, f"title='{title[:70]}' ({src})")
    except Exception as e:
        _letter_gate_reason = f"exception: {e}"
        check(label, 0, False, f"exception: {e}")


def check_6_appeal_auth_details() -> None:
    """Appeal letter contains authorization/denial details incl. appeal ref + procedure."""
    label = "6. Auth/denial details in letter"
    try:
        if not _letter_gate_ok:
            check(label, 2, False, f"gate ck5 failed: {_letter_gate_reason}")
            return
        tl = _norm_text(_letter_text or "")
        has_auth = "auth-2026-0301-ll" in tl
        has_date = "2026-03-05" in tl
        has_reason = "medical necessity not established" in tl
        has_ref = "apl-2026-00305-ll" in tl
        has_proc = "cardiac stress test with myocardial perfusion imaging" in tl
        ok = has_auth and has_date and has_reason and has_ref and has_proc
        check(label, 2, ok,
              f"auth#={'Y' if has_auth else 'N'}, date={'Y' if has_date else 'N'}, "
              f"reason={'Y' if has_reason else 'N'}, ref={'Y' if has_ref else 'N'}, "
              f"procedure={'Y' if has_proc else 'N'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_clinical_justification() -> None:
    """Appeal letter: narrative + insurance/patient identifiers + live-queried
    active problems and 3 most recent encounter dates."""
    label = "7. Clinical justification in letter"
    try:
        if not _letter_gate_ok:
            check(label, 3, False, f"gate ck5 failed: {_letter_gate_reason}")
            return
        pid = get_patient_pid()
        if not pid:
            check(label, 3, False, "gate ck1 failed: patient not found")
            return
        tl = _norm_text(_letter_text or "")
        tl_ns = tl.replace(" ", "")

        has_narrative = "recurrent exertional chest pain" in tl

        # Live insurance truth (probed: UnitedHealthcare / POL908577 / GRP3143).
        ins = openemr_sql(
            f"SELECT ic.name, id.policy_number, id.group_number "
            f"FROM insurance_data id JOIN insurance_companies ic ON ic.id=id.provider "
            f"WHERE id.pid={pid} AND id.type='primary' ORDER BY id.date DESC LIMIT 1"
        )
        payer = policy = group = ""
        parts = ins.splitlines()[0].split("\t") if ins else []
        if len(parts) >= 3:
            payer, policy, group = parts[0].strip(), parts[1].strip(), parts[2].strip()
        has_payer = bool(payer) and _norm_text(payer).replace(" ", "") in tl_ns
        has_policy = bool(policy) and policy.lower() in tl
        has_group = bool(group) and group.lower() in tl
        has_member = "mid-pol908577-01" in tl
        has_dob = "1960-10-19" in tl or "10/19/1960" in tl

        # Live active problems (probed: Medication review due / Abnormal findings
        # diagnostic imaging heart+coronary / Loss of teeth; gingivitis activity=0).
        probs = openemr_sql(
            f"SELECT DISTINCT title FROM lists "
            f"WHERE pid={pid} AND type='medical_problem' AND activity=1"
        )
        titles = [t.strip() for t in probs.splitlines() if t.strip()]
        missing_probs = [t for t in titles if _norm_text(t)[:25] not in tl]
        problems_ok = bool(titles) and not missing_probs

        # Live 3 most recent seed encounter dates (probed: 2022-10-02 / 2022-01-08 / 2021-10-22).
        seed_encs = get_seed_encounters()
        missing_dates = [d for _, d in seed_encs
                         if not any(v in tl for v in _date_variants(d))]
        encounters_ok = len(seed_encs) == 3 and not missing_dates

        has_gingivitis = "gingivitis" in tl  # info only: seed row is activity=0

        ok = (has_narrative and has_policy and has_dob and has_payer and has_group
              and has_member and problems_ok and encounters_ok)
        check(label, 3, ok,
              f"narrative={'Y' if has_narrative else 'N'}, payer={'Y' if has_payer else 'N'}, "
              f"policy={'Y' if has_policy else 'N'}, group={'Y' if has_group else 'N'}, "
              f"member={'Y' if has_member else 'N'}, DOB={'Y' if has_dob else 'N'}, "
              f"problems={'ok' if problems_ok else 'missing ' + '; '.join(missing_probs)[:80] if titles else 'none active in DB'}, "
              f"encounters={'ok' if encounters_ok else 'missing ' + ','.join(missing_dates)[:60]}, "
              f"info:gingivitis={'Y' if has_gingivitis else 'N'}")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_8_evidence_and_codes() -> None:
    """Appeal letter contains all 3 supporting-evidence items, CPT, ICDs, NPI."""
    label = "8. Evidence + codes + NPI in letter"
    try:
        if not _letter_gate_ok:
            check(label, 2, False, f"gate ck5 failed: {_letter_gate_reason}")
            return
        tl = _norm_text(_letter_text or "")
        has_ev1 = "2026-02-18" in tl
        has_ev2 = "framingham" in tl
        has_ev3 = "hba1c" in tl
        has_cpt = "78452" in tl
        has_npi = "1871574327" in tl
        has_icd = "i20.9" in tl and "r07.9" in tl
        ok = has_ev1 and has_ev2 and has_ev3 and has_cpt and has_npi and has_icd
        check(label, 2, ok,
              f"ev1(2026-02-18)={'Y' if has_ev1 else 'N'}, "
              f"ev2(framingham)={'Y' if has_ev2 else 'N'}, "
              f"ev3(hba1c)={'Y' if has_ev3 else 'N'}, "
              f"CPT={'Y' if has_cpt else 'N'}, NPI={'Y' if has_npi else 'N'}, "
              f"ICD={'Y' if has_icd else 'N'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_tracking_spreadsheet_exists() -> None:
    """Gate (0pt): tracking spreadsheet exists with the exact title; fetches +
    parses the xlsx (sheet name -> rows) for checks 10-11."""
    global _wb_sheets, _xlsx_gate_ok, _xlsx_gate_reason
    label = "9. Tracking spreadsheet exists (gate)"
    try:
        data, title, src = _oo_get_document([
            f"{EXPECTED_XLSX_TITLE}%",
            "%Claims and Appeal Tracking%Lang%",
        ])
        if title is None:
            _xlsx_gate_reason = src
            check(label, 0, False, src)
            return
        title_ok = _title_exact(title, EXPECTED_XLSX_TITLE)
        if not title_ok:
            _xlsx_gate_reason = f"title mismatch: '{title[:70]}'"
            check(label, 0, False, f"title mismatch: '{title[:70]}' ({src})")
            return
        if not data:
            _xlsx_gate_reason = src
            check(label, 0, False, src)
            return
        zf = zipfile.ZipFile(io.BytesIO(data))
        shared = _xlsx_shared_strings(zf)
        for name, path in _xlsx_sheets(zf).items():
            _wb_sheets[name] = _xlsx_rows(zf, path, shared)
        if not _wb_sheets:
            _xlsx_gate_reason = "xlsx parsed but no sheets found"
            check(label, 0, False, f"xlsx parsed but no sheets found ({src})")
            return
        _xlsx_gate_ok = True
        check(label, 0, True, f"title='{title[:70]}' ({src})")
    except Exception as e:
        _xlsx_gate_reason = f"exception: {e}"
        check(label, 0, False, f"exception: {e}")


def check_10_spreadsheet_sheets() -> None:
    """Spreadsheet has Claims Register and Authorization Timeline sheets."""
    label = "10. Spreadsheet sheet names"
    try:
        if not _xlsx_gate_ok:
            check(label, 2, False, f"gate ck9 failed: {_xlsx_gate_reason}")
            return
        names = list(_wb_sheets.keys())
        nl = [_norm_text(n) for n in names]
        has_claims = any("claims register" in n for n in nl)
        has_timeline = any("authorization timeline" in n for n in nl)
        ok = has_claims and has_timeline
        check(label, 2, ok, f"sheets={names}"[:140])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_spreadsheet_data() -> None:
    """Per-row reconciliation against live DB: 8 header columns, one row per
    seed encounter (date + its CPTs), pending status, appeal row, timeline."""
    label = "11. Spreadsheet claim data"
    try:
        if not _xlsx_gate_ok:
            check(label, 3, False, f"gate ck9 failed: {_xlsx_gate_reason}")
            return
        pid = get_patient_pid()
        if not pid:
            check(label, 3, False, "gate ck1 failed: patient not found")
            return

        def _sheet_rows(fragment: str) -> list[list[str]] | None:
            for name, rows in _wb_sheets.items():
                if fragment in _norm_text(name):
                    return rows
            return None

        all_rows = [r for rows in _wb_sheets.values() for r in rows]
        claims_rows = _sheet_rows("claims register") or all_rows
        timeline_rows = _sheet_rows("authorization timeline") or all_rows
        claims_lines = [_norm_text(" | ".join(r)) for r in claims_rows
                        if any(c.strip() for c in r)]
        timeline_lines = [_norm_text(" | ".join(r)) for r in timeline_rows
                          if any(c.strip() for c in r)]

        header_keys = ("patient name", "encounter date", "icd-10", "cpt",
                       "claim status", "denial reason", "appeal filed", "appeal date")
        header_ok = any(all(k in ln for k in header_keys) for ln in claims_lines)

        # One row per seed encounter: encounter date + its billing CPTs, live.
        # (Probed: enc6 2022-10-02->99395; enc67 2022-01-08->99203+99213; enc12 2021-10-22->99395.)
        seed_encs = get_seed_encounters()
        missing_enc_rows = []
        for enc_id, d in seed_encs:
            cpts = [c.strip() for c in openemr_sql(
                f"SELECT DISTINCT code FROM billing WHERE pid={pid} "
                f"AND encounter={enc_id} AND activity=1 AND code_type LIKE 'CPT%'"
            ).splitlines() if c.strip()]
            hit = any(
                any(v in ln for v in _date_variants(d))
                and all(c.lower() in ln for c in cpts)
                for ln in claims_lines
            )
            if not hit:
                missing_enc_rows.append(d)
        enc_rows_ok = len(seed_encs) == 3 and not missing_enc_rows

        # 'pending' required iff live billed=0 CPT rows exist on the seed encounters.
        # (Probed: enc 6 and 67 each have a billed=0 row.)
        pending_ok = True
        if seed_encs:
            ids = ",".join(str(e) for e, _ in seed_encs)
            cnt = openemr_sql(
                f"SELECT COUNT(*) FROM billing WHERE pid={pid} AND encounter IN ({ids}) "
                f"AND activity=1 AND billed=0 AND code_type LIKE 'CPT%'"
            ).strip()
            if cnt.isdigit() and int(cnt) > 0:
                pending_ok = any("pending" in ln for ln in claims_lines)

        appeal_ok = any("78452" in ln and "yes" in ln for ln in claims_lines)

        events = ("procedure request", "authorization submission", "denial received",
                  "clinical review", "appeal encounter", "appeal letter")
        hits = sum(1 for ev in events
                   if any(ev in ln for ln in timeline_lines))
        events_ok = hits >= 5
        auth_ok = any("auth-2026-0301-ll" in ln for ln in timeline_lines)
        date_ok = any("2026-03-05" in ln or "03/05/2026" in ln for ln in timeline_lines)

        ok = (header_ok and enc_rows_ok and pending_ok and appeal_ok
              and events_ok and auth_ok and date_ok)
        check(label, 3, ok,
              f"header={'Y' if header_ok else 'N'}, "
              f"encRows={'ok' if enc_rows_ok else 'missing ' + ','.join(missing_enc_rows)[:40]}, "
              f"pending={'Y' if pending_ok else 'N'}, appealRow={'Y' if appeal_ok else 'N'}, "
              f"events={hits}/6, auth={'Y' if auth_ok else 'N'}, "
              f"denialDate={'Y' if date_ok else 'N'}")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_patient_exists()
    check_2_clinical_notes()
    check_3_billing_codes()
    check_4_misc_billing()
    check_5_appeal_letter_exists()
    check_6_appeal_auth_details()
    check_7_clinical_justification()
    check_8_evidence_and_codes()
    check_9_tracking_spreadsheet_exists()
    check_10_spreadsheet_sheets()
    check_11_spreadsheet_data()

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
