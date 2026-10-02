"""
Verifier for Healthcare-017-I5: Merge Duplicate Patient Records for Latoyia Kertzmann
and Compile Audit Documentation

Checks: 12 checks (weights sum to 19) across openemr, onlyoffice.
Strategy: docker exec (MariaDB for OpenEMR, MySQL for OnlyOffice) + OnlyOffice
document content probes (portal data dir fs-first, API fallback).

Required env vars:
  SERVER_HOSTNAME,
  OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
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
except Exception:  # pragma: no cover - requests only needed for API fallback
    requests = None

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

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

# ── Probed seed baselines (bare mw-openemr:latest image, probed 2026-08) ──────
# pid 158 (target Latoyia Kertzmann, DOB 1974-02-10) seed: lists=1
#   (medical_problem 'Full-time employment (finding)', activity=0),
#   form_encounter=2, pnotes=2, forms=6.
# pid 249 (source duplicate, DOB NULL) seed: lists=12 (9 medical_problem incl
#   'Reports of violence in the environment (finding)' and 'Gingival disease
#   (disorder)'; 3 medication: Simvastatin 10 MG Oral Tablet x2 + sodium
#   fluoride 0.0272 MG/MG Oral Gel), form_encounter=3, forms=3, pnotes=0.
# log seed: 0 rows with event LIKE 'patient-merge-%'.
# history_data is NOT reconciled: merge deletes source history rows (does not
# migrate them), per merge_patients.php.
SRC_PID = 249
TGT_PID = 158
EXPECTED_MEDICAL_PROBLEMS = 10  # 1 (pid158 seed) + 9 (pid249 seed)
EXPECTED_MEDICATIONS = 3        # pid249 seed medications, all migrated
EXPECTED_LISTS_TOTAL = 13       # 1 + 12
MIN_FORM_ENCOUNTERS = 5         # 2 + 3; plan risk (a): merge's encounter-dedup
                                # branch could alter the count on conflicting
                                # encounter ids — use >= 5 for the first round.
EXPECTED_PNOTES = 2             # 2 (pid158 seed) + 0 (pid249 seed)
EXPECTED_FORMS = 9              # 6 + 3

AUDIT_REPORT_TITLE = "Kertzmann Duplicate Patient Merge Audit Report - 2026-03-22"
TRACKER_TITLE = "Duplicate Patient Record Merge Audit Tracker - March 2026"
REGISTRY_SHEET = "Outside Specialist Contact Registry"

# Distinctive prefixes of seed clinical item names (pre-merge comparison table
# in the audit report must reference >= 4 of these).
SEED_CLINICAL_ITEMS = [
    "reports of violence in the environment",
    "gingival disease",
    "simvastatin",
    "sodium fluoride",
    "full-time employment",
]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def openemr_sql(query: str, timeout: int = 15) -> str:
    """Run a SQL query against OpenEMR MariaDB and return stdout. Raises on error."""
    r = subprocess.run(
        [
            "docker", "exec", OPENEMR_DB_CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-u", "openemr", "-popenemr_pass", "-D", "openemr",
            "-sN", "-e", query,
        ],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"openemr mysql failed (rc={r.returncode}): {r.stderr.strip()[-300:]}"
        )
    return r.stdout.strip()


def onlyoffice_sql(query: str, timeout: int = 15) -> str:
    """Run a SQL query against OnlyOffice MySQL and return stdout. Raises on error."""
    r = subprocess.run(
        [
            "docker", "exec", ONLYOFFICE_DB_CONTAINER,
            "mysql", "--default-character-set=utf8mb4",
            "-u", "onlyoffice_user", "-ponlyoffice_pass",
            "-D", "onlyoffice",
            "-sN", "-e", query,
        ],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"onlyoffice mysql failed (rc={r.returncode}): {r.stderr.strip()[-300:]}"
        )
    return r.stdout.strip()


def _cell(s: str) -> str:
    """mysql -sN prints NULL as the literal string 'NULL'."""
    s = s.strip()
    return "" if s == "NULL" else s


# ── OnlyOffice document retrieval (fs-first with retries, API fallback) ───────
def _oo_bytes_from_fs(file_id: str, retries: int = 3, delay: float = 5.0) -> bytes | None:
    """Read content.docx/xlsx for a file id from the portal data dir (docker
    exec). Retries to tolerate OnlyOffice save/conversion delay."""
    for attempt in range(retries):
        r = subprocess.run(
            [
                "docker", "exec", ONLYOFFICE_CONTAINER, "bash", "-c",
                f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.*' "
                "2>/dev/null | sort -V | tail -1",
            ],
            capture_output=True, text=True, errors="replace", timeout=25,
        )
        path = r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""
        if path:
            rb = subprocess.run(
                ["docker", "exec", ONLYOFFICE_CONTAINER, "cat", path],
                capture_output=True, timeout=30,
            )
            if rb.returncode == 0 and rb.stdout:
                return rb.stdout
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _oo_auth_session():
    if requests is None:
        return None
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    s = requests.Session()
    try:
        resp = s.post(
            f"{base_url}/api/2.0/authentication",
            json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
            timeout=15,
        )
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


def _oo_get_bytes(file_id: str) -> tuple[bytes | None, str]:
    """(bytes|None, source detail). fs first, API fallback."""
    data = _oo_bytes_from_fs(file_id)
    if data:
        return data, f"fs id={file_id}"
    data = _oo_bytes_from_api(file_id)
    if data:
        return data, f"api id={file_id}"
    return None, f"id={file_id} found but content unreadable (fs+api)"


# ── docx / xlsx parsing helpers ───────────────────────────────────────────────
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


def _title_matches(db_title: str, expected: str) -> bool:
    """Exact title match after extension-suffix strip + _norm_text on both sides."""
    t = re.sub(r"\.(docx|doc|xlsx|xls)$", "", db_title.strip(), flags=re.IGNORECASE)
    return _norm_text(t) == _norm_text(expected)


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
    """Rows as lists of cell strings: shared strings resolved, inline strings
    and raw numeric <v> values included. Same-row assertions run on
    ' | '.join(row) per row — never on whole-document text."""
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


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_target_patient_exists() -> bool:
    """0-weight precondition gate: target Latoyia Kertzmann (pid 158) exists.

    Seed-passable (pid 158 exists in the bare image), so it carries no weight;
    checks 3/4/5 depend on it.
    """
    label = "1. Target patient pid 158 exists (gate)"
    try:
        row = openemr_sql(
            f"SELECT fname, lname FROM patient_data WHERE pid = {TGT_PID};"
        )
        if row:
            parts = row.split("\t")
            fname = _cell(parts[0]) if len(parts) > 0 else ""
            lname = _cell(parts[1]) if len(parts) > 1 else ""
            passed = "latoyia" in fname.lower() and "kertzmann" in lname.lower()
            check(label, 0, passed, f"found: {fname} {lname}")
            return passed
        check(label, 0, False, f"no patient_data row for pid {TGT_PID}")
        return False
    except Exception as e:
        check(label, 0, False, f"exception: {e}")
        return False


def check_2_source_fully_archived() -> None:
    """merge triad (a): source pid 249 fully archived — patient_data row deleted
    AND no lists / form_encounter / forms rows remain on pid 249.

    Seed pid 249 had lists=12, form_encounter=3, forms=3 — merely deleting the
    patient without migrating its data fails the pid-158 reconciliation checks.
    """
    label = "2. Source patient pid 249 fully archived"
    try:
        row = openemr_sql(
            f"SELECT (SELECT COUNT(*) FROM patient_data WHERE pid = {SRC_PID}), "
            f"(SELECT COUNT(*) FROM lists WHERE pid = {SRC_PID}), "
            f"(SELECT COUNT(*) FROM form_encounter WHERE pid = {SRC_PID}), "
            f"(SELECT COUNT(*) FROM forms WHERE pid = {SRC_PID});"
        )
        pd, ls, fe, fm = [int(_cell(x)) for x in row.split("\t")]
        errors = []
        if pd != 0:
            errors.append(f"patient_data={pd} expected 0")
        if ls != 0:
            errors.append(f"lists={ls} expected 0")
        if fe != 0:
            errors.append(f"form_encounter={fe} expected 0")
        if fm != 0:
            errors.append(f"forms={fm} expected 0")
        check(label, 2, not errors,
              "; ".join(errors) if errors
              else "pid 249 has 0 rows in patient_data/lists/form_encounter/forms")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_medical_problems_reconciled(gate_ok: bool) -> None:
    """merge triad (b), part 1: pid 158 medical_problem count == 10 and the two
    pid-249-only seed titles are present as pid-158 rows (same-row probes)."""
    label = "3. Merged record medical problems reconciled"
    if not gate_ok:
        check(label, 2, False, "gate failed: target patient pid 158 missing (check 1)")
        return
    try:
        cnt = int(_cell(openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID} AND type = 'medical_problem';"
        )))
        violence = int(_cell(openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID} AND type = 'medical_problem' "
            "AND title LIKE 'Reports of violence in the environment%';"
        )))
        gingival = int(_cell(openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID} AND type = 'medical_problem' "
            "AND title LIKE 'Gingival disease%';"
        )))
        errors = []
        if cnt != EXPECTED_MEDICAL_PROBLEMS:
            errors.append(f"count={cnt} expected {EXPECTED_MEDICAL_PROBLEMS}")
        if violence == 0:
            errors.append("missing 'Reports of violence in the environment' row on pid 158")
        if gingival == 0:
            errors.append("missing 'Gingival disease' row on pid 158")
        check(label, 2, not errors,
              "; ".join(errors) if errors
              else f"count={cnt}, both pid-249 probe titles present")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_medications_reconciled(gate_ok: bool) -> None:
    """pid 158 medication count == 3 and Simvastatin 10 MG Oral Tablet present."""
    label = "4. Merged record medications reconciled"
    if not gate_ok:
        check(label, 2, False, "gate failed: target patient pid 158 missing (check 1)")
        return
    try:
        cnt = int(_cell(openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID} AND type = 'medication';"
        )))
        simva = int(_cell(openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID} AND type = 'medication' "
            "AND title LIKE '%Simvastatin 10 MG Oral Tablet%';"
        )))
        errors = []
        if cnt != EXPECTED_MEDICATIONS:
            errors.append(f"count={cnt} expected {EXPECTED_MEDICATIONS}")
        if simva == 0:
            errors.append("'Simvastatin 10 MG Oral Tablet' not present on pid 158")
        check(label, 2, not errors,
              "; ".join(errors) if errors
              else f"count={cnt}, Simvastatin present ({simva} rows)")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_full_reconciliation(gate_ok: bool) -> None:
    """Full migration reconciliation on pid 158/249: lists totals, encounters,
    patient notes, forms registry (rebuilt — the old allergy check was
    vacuously true since both seeds have 0 allergies)."""
    label = "5. Lists/encounters/notes/forms fully migrated"
    if not gate_ok:
        check(label, 2, False, "gate failed: target patient pid 158 missing (check 1)")
        return
    try:
        row = openemr_sql(
            f"SELECT (SELECT COUNT(*) FROM lists WHERE pid = {TGT_PID}), "
            f"(SELECT COUNT(*) FROM lists WHERE pid = {SRC_PID}), "
            f"(SELECT COUNT(*) FROM form_encounter WHERE pid = {TGT_PID}), "
            f"(SELECT COUNT(*) FROM pnotes WHERE pid = {TGT_PID}), "
            f"(SELECT COUNT(*) FROM forms WHERE pid = {TGT_PID});"
        )
        l_tgt, l_src, enc, pn, fm = [int(_cell(x)) for x in row.split("\t")]
        errors = []
        if l_tgt != EXPECTED_LISTS_TOTAL:
            errors.append(f"lists pid158={l_tgt} expected {EXPECTED_LISTS_TOTAL}")
        if l_src != 0:
            errors.append(f"lists pid249={l_src} expected 0")
        if enc < MIN_FORM_ENCOUNTERS:
            errors.append(f"form_encounter pid158={enc} expected >= {MIN_FORM_ENCOUNTERS}")
        if pn != EXPECTED_PNOTES:
            errors.append(f"pnotes pid158={pn} expected {EXPECTED_PNOTES}")
        if fm != EXPECTED_FORMS:
            errors.append(f"forms pid158={fm} expected {EXPECTED_FORMS}")
        check(label, 2, not errors,
              "; ".join(errors) if errors
              else f"lists158={l_tgt} lists249={l_src} enc={enc} pnotes={pn} forms={fm}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_merge_audit_log() -> None:
    """merge triad (c): the merge UI writes EventAuditLogger events with prefix
    'patient-merge-' (patient-merge-update / patient-merge-delete). Seed log
    has 0 such rows (probed), so any hit proves a real merge ran.
    CURDATE() - INTERVAL 1 DAY tolerates a run crossing midnight (plan risk b).
    """
    label = "6. patient-merge-* audit log events present"
    try:
        cnt = int(_cell(openemr_sql(
            "SELECT COUNT(*) FROM log WHERE event LIKE 'patient-merge-%' "
            "AND date >= CURDATE() - INTERVAL 1 DAY;"
        )))
        check(label, 2, cnt > 0,
              f"found {cnt} 'patient-merge-%' log events since CURDATE()-1")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def _abook_rows() -> list[dict]:
    """Address-book rows for Dr. Yusuf Abdelrahman from the users table (the
    OpenEMR Address Book stores entries there). All ck7/ck8 field assertions
    must be satisfied by ONE row."""
    out = openemr_sql(
        "SELECT id, fname, lname, specialty, phone, phonew1, phonew2, phonecell, "
        "street, streetb, city, state, zip FROM users "
        "WHERE lname LIKE '%Abdelrahman%' AND fname LIKE '%Yusuf%';"
    )
    rows = []
    for line in out.splitlines():
        parts = [_cell(p) for p in line.split("\t")]
        parts += [""] * (13 - len(parts))
        rows.append({
            "id": parts[0], "fname": parts[1], "lname": parts[2],
            "specialty": parts[3],
            "phones": " ".join(parts[4:8]),
            "street": parts[8], "streetb": parts[9],
            "city": parts[10], "state": parts[11], "zip": parts[12],
        })
    return rows


def check_7_address_book_entry(rows: list[dict] | None) -> bool:
    """Address Book contains an entry for Dr. Yusuf Abdelrahman (users table)."""
    label = "7. Address book entry for Dr. Yusuf Abdelrahman"
    if rows is None:
        check(label, 1, False, "exception: address-book lookup failed (see check 8)")
        return False
    if rows:
        r = rows[0]
        check(label, 1, True,
              f"found {len(rows)} users row(s), first: id={r['id']} {r['fname']} {r['lname']}")
        return True
    check(label, 1, False, "no users row matching fname Yusuf / lname Abdelrahman")
    return False


def check_8_address_book_details(rows: list[dict] | None, gate_ok: bool) -> None:
    """One address-book row must carry ALL required fields: specialty
    Gastroenterology, phone 339-555-0617, street with '1153' + 'Suite 210' +
    Centre Street, city Jamaica Plain."""
    label = "8. Address book entry details correct (same row)"
    if rows is None:
        check(label, 2, False, "exception: address-book lookup failed")
        return
    if not gate_ok:
        check(label, 2, False,
              "gate failed: no address-book row for Dr. Yusuf Abdelrahman (check 7)")
        return
    try:
        best_errors: list[str] | None = None
        for r in rows:
            errors = []
            if "gastroenterology" not in r["specialty"].lower():
                errors.append(f"specialty='{r['specialty']}' expected 'Gastroenterology'")
            digits = re.sub(r"\D", "", r["phones"])
            if "3395550617" not in digits:
                errors.append(f"phones='{r['phones'].strip()}' missing 339-555-0617")
            addr = _norm_text(
                f"{r['street']} {r['streetb']} {r['city']} {r['state']} {r['zip']}"
            )
            if "1153" not in addr:
                errors.append("street missing '1153'")
            if "suite 210" not in addr:
                errors.append("street missing 'Suite 210'")
            if "centre" not in addr and "center" not in addr:
                errors.append("address missing 'Centre Street'")
            if "jamaica" not in addr:
                errors.append("address missing 'Jamaica Plain'")
            if not errors:
                check(label, 2, True, f"users id={r['id']}: all fields correct on one row")
                return
            if best_errors is None or len(errors) < len(best_errors):
                best_errors = errors
        check(label, 2, False, "; ".join(best_errors or ["no candidate rows"])[:200])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_audit_report_title() -> str | None:
    """Audit report docx exists with the EXACT required title (extension suffix
    tolerated, em-dash normalized, editor Recovery copies excluded). Gates
    check 11; returns the files_file id."""
    label = "9. Audit report document exists (exact title)"
    try:
        out = onlyoffice_sql(
            "SELECT id, title FROM files_file "
            "WHERE title LIKE '%Kertzmann%' AND title NOT LIKE '%Recovery%' "
            "ORDER BY id DESC;"
        )
        candidates = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                fid, title = parts[0].strip(), parts[1].strip()
                candidates.append(title)
                if _title_matches(title, AUDIT_REPORT_TITLE):
                    check(label, 1, True, f"id={fid} title={title[:80]}")
                    return fid
        check(label, 1, False,
              f"no exact-title match for '{AUDIT_REPORT_TITLE}'; "
              f"candidates: {'; '.join(candidates)[:120] or 'none'}")
        return None
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return None


def check_10_tracker_title() -> str | None:
    """Tracking spreadsheet exists with the EXACT required title (same
    tolerances as check 9). Gates check 12; returns the files_file id."""
    label = "10. Tracking spreadsheet exists (exact title)"
    try:
        out = onlyoffice_sql(
            "SELECT id, title FROM files_file "
            "WHERE title LIKE '%Audit Tracker%' AND title NOT LIKE '%Recovery%' "
            "ORDER BY id DESC;"
        )
        candidates = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                fid, title = parts[0].strip(), parts[1].strip()
                candidates.append(title)
                if _title_matches(title, TRACKER_TITLE):
                    check(label, 1, True, f"id={fid} title={title[:80]}")
                    return fid
        check(label, 1, False,
              f"no exact-title match for '{TRACKER_TITLE}'; "
              f"candidates: {'; '.join(candidates)[:120] or 'none'}")
        return None
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return None


def check_11_audit_report_content(file_id: str | None) -> None:
    """Audit report content probe (content.docx): CFR cite, HIM-022, both pids,
    >=4 seed clinical item names in the pre-merge table/text, specialist entry,
    Administrator signature."""
    label = "11. Audit report content (docx)"
    if not file_id:
        check(label, 3, False,
              "gate failed: check 9 found no exact-title audit report document")
        return
    try:
        data, src = _oo_get_bytes(file_id)
        if not data:
            check(label, 3, False, src)
            return
        table_text = " ".join(
            row for tbl in _docx_tables(data) for row in tbl
        )
        text = _norm_text(_docx_text(data) + " " + table_text)
        errors = []
        # '45 CFR' and '164.312(c)' matched separately — the § glyph between
        # them may render differently across editors.
        if "45 cfr" not in text:
            errors.append("missing '45 CFR'")
        if "164.312(c)" not in text:
            errors.append("missing '164.312(c)'")
        if "him-022" not in text:
            errors.append("missing 'HIM-022'")
        if "249" not in text or "158" not in text:
            errors.append("missing source/target pids (249 and 158)")
        hits = [i for i in SEED_CLINICAL_ITEMS if i in text]
        if len(hits) < 4:
            errors.append(
                f"only {len(hits)}/{len(SEED_CLINICAL_ITEMS)} seed clinical items "
                f"present ({', '.join(hits) or 'none'})"
            )
        if "yusuf abdelrahman" not in text:
            errors.append("missing 'Yusuf Abdelrahman'")
        if "gastroenterology" not in text:
            errors.append("missing 'Gastroenterology'")
        if "administrator" not in text:
            errors.append("missing 'Administrator' signature")
        check(label, 3, not errors,
              "; ".join(errors)[:200] if errors
              else f"all content probes matched, {len(hits)}/5 clinical items ({src})")
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_12_tracker_content(file_id: str | None) -> None:
    """Tracking spreadsheet content probe (content.xlsx): registry sheet name,
    same-row merge entry (249+158+Yes), same-row registry entry (Abdelrahman +
    Gastroenterology + phone). Merge Date cell deliberately NOT asserted
    (plan risk c: agent may write 2026-03-22 or the run date)."""
    label = "12. Tracking spreadsheet content (xlsx)"
    w = 1
    if not file_id:
        check(label, w, False,
              "gate failed: check 10 found no exact-title tracking spreadsheet")
        return
    try:
        data, src = _oo_get_bytes(file_id)
        if not data:
            check(label, w, False, src)
            return
        zf = zipfile.ZipFile(io.BytesIO(data))
        sheets = _xlsx_sheets(zf)
        shared = _xlsx_shared_strings(zf)
        errors = []
        reg_path = None
        for name, path in sheets.items():
            if _norm_text(name) == _norm_text(REGISTRY_SHEET):
                reg_path = path
                break
        if not reg_path:
            errors.append(
                f"sheet '{REGISTRY_SHEET}' not in workbook "
                f"(sheets: {', '.join(list(sheets)[:5])[:80]})"
            )
        merge_row_ok = False
        for path in sheets.values():
            for row in _xlsx_rows(zf, path, shared):
                j = _norm_text(" | ".join(row))
                if "249" in j and "158" in j and "yes" in j:
                    merge_row_ok = True
                    break
            if merge_row_ok:
                break
        if not merge_row_ok:
            errors.append("no single row containing '249' + '158' + 'Yes'")
        if reg_path:
            reg_row_ok = False
            for row in _xlsx_rows(zf, reg_path, shared):
                j = _norm_text(" | ".join(row))
                if ("yusuf abdelrahman" in j and "gastroenterology" in j
                        and "339-555-0617" in j):
                    reg_row_ok = True
                    break
            if not reg_row_ok:
                errors.append(
                    "registry sheet has no row with 'Yusuf Abdelrahman' + "
                    "'Gastroenterology' + '339-555-0617'"
                )
        check(label, w, not errors,
              "; ".join(errors)[:200] if errors
              else f"registry sheet + merge row + specialist row all present ({src})")
    except Exception as e:
        check(label, w, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    gate_158 = check_1_target_patient_exists()
    check_2_source_fully_archived()
    check_3_medical_problems_reconciled(gate_158)
    check_4_medications_reconciled(gate_158)
    check_5_full_reconciliation(gate_158)
    check_6_merge_audit_log()

    try:
        abook = _abook_rows()
    except Exception as e:
        print(f"WARN: address-book lookup failed: {e}", file=sys.stderr)
        abook = None
    gate_abook = check_7_address_book_entry(abook)
    check_8_address_book_details(abook, gate_abook)

    docx_id = check_9_audit_report_title()
    xlsx_id = check_10_tracker_title()
    check_11_audit_report_content(docx_id)
    check_12_tracker_content(xlsx_id)

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
