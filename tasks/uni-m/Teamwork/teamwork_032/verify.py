"""
Verifier for Teamwork-032-I2: Healthcare Document Archive & Notification

Checks: 13 weighted checks (20 pt) across owncloud, onlyoffice, mattermost, roundcubemail.
Strategy: docker exec DB queries (ownCloud MariaDB, OnlyOffice MySQL, Mattermost Postgres,
          Roundcube MariaDB), ownCloud WebDAV for content, xlsx content probes read from
          the OnlyOffice data dir (API download fallback), and raw Maildir parsing of the
          sender's mail copies (docker exec + Python email stdlib).

Required env vars:
  SERVER_HOSTNAME,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER
"""

import email
import email.message
import io
import os
import re
import subprocess
import sys
import zipfile
from email.header import decode_header
from email.utils import getaddresses

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

REQUIRED_VARS = {
    "OWNCLOUD_PORT": None, "OWNCLOUD_CONTAINER": None, "OWNCLOUD_DB_CONTAINER": None,
    "ONLYOFFICE_PORT": None, "ONLYOFFICE_CONTAINER": None, "ONLYOFFICE_DB_CONTAINER": None,
    "MATTERMOST_PORT": None, "MATTERMOST_CONTAINER": None, "MATTERMOST_DB_CONTAINER": None,
    "ROUNDCUBEMAIL_PORT": None, "ROUNDCUBEMAIL_CONTAINER": None, "ROUNDCUBEMAIL_DB_CONTAINER": None,
}
for var in REQUIRED_VARS:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    REQUIRED_VARS[var] = val

OC_PORT = REQUIRED_VARS["OWNCLOUD_PORT"]
OC_CONTAINER = REQUIRED_VARS["OWNCLOUD_CONTAINER"]
OC_DB = REQUIRED_VARS["OWNCLOUD_DB_CONTAINER"]
OO_PORT = REQUIRED_VARS["ONLYOFFICE_PORT"]
OO_CONTAINER = REQUIRED_VARS["ONLYOFFICE_CONTAINER"]
OO_DB = REQUIRED_VARS["ONLYOFFICE_DB_CONTAINER"]
MM_PORT = REQUIRED_VARS["MATTERMOST_PORT"]
MM_CONTAINER = REQUIRED_VARS["MATTERMOST_CONTAINER"]
MM_DB = REQUIRED_VARS["MATTERMOST_DB_CONTAINER"]
RC_PORT = REQUIRED_VARS["ROUNDCUBEMAIL_PORT"]
RC_CONTAINER = REQUIRED_VARS["ROUNDCUBEMAIL_CONTAINER"]
RC_DB = REQUIRED_VARS["ROUNDCUBEMAIL_DB_CONTAINER"]

# ── Expected data ─────────────────────────────────────────────────────────────
ORIGINAL_FILES = [
    "Kima_w_Medical_Center_Nursing_Position_Description.docx",
    "Inflammation_Protein_Results_Mann_Whitney_U_Test_Ratios_Sensitivity_Specificity.docx",
    "MassHealth_Medicaid_CHIP_Section_1115_Demonstration_Waiver.docx",
]
ARCHIVED_FILES = [f"ARCHIVED_{n}" for n in ORIGINAL_FILES]
REPLACEMENT_FILES = [
    "nursing_position_v3.docx",
    "inflammation_protein_final_2026.docx",
    "masshealth_1115_renewal_2026.docx",
]
ARCHIVE_DIR = "files/doc/healthcare/Obsolete_Healthcare_2026H1"
ARCHIVED_PATHS = [f"{ARCHIVE_DIR}/{n}" for n in ARCHIVED_FILES]
ORIGINAL_PATHS = [f"files/doc/healthcare/{n}" for n in ORIGINAL_FILES]

ACTIVE_DOCS_EXPECTED = (
    "Current Active Healthcare Documents:\n"
    "- Kima Medical Center Nursing Position Description v3 "
    "(doc/healthcare/nursing_position_v3.docx)\n"
    "- Inflammation Protein Study Final Report 2026 "
    "(doc/healthcare/inflammation_protein_final_2026.docx)\n"
    "- MassHealth 1115 Waiver Renewal 2026 "
    "(doc/healthcare/masshealth_1115_renewal_2026.docx)"
)

OO_SHEET_TITLE = "Healthcare_Archive_Register_2026H1"
XLSX_HEADERS = [
    "Original Filename", "Archived Filename", "Original Size",
    "Last Modified", "Archived Date", "Replacement Document",
]

MM_NOTICE_FRAGMENT = "Healthcare Archive Notice"
MM_HEADER_EXPECTED = (
    "Report and triage bugs. Healthcare document archive audit complete "
    "2026-04-20. See ACTIVE_HEALTHCARE_DOCS.txt for current versions."
)
MM_DM_FRAGMENT = "revoke any external sharing links"
MM_DM_ANCHOR = (
    "Please review and revoke any external sharing links associated with the "
    "archived healthcare files in doc/healthcare/Obsolete_Healthcare_2026H1. "
    "Appreciated."
)

EMAIL_SUBJECT = "Formal Notice: Healthcare Document Supersession Effective 2026-04-20"
EMAIL_TO_SET = {
    "carlos.mendez@mail.local", "rachel.goldberg@mail.local",
    "tom.andersen@mail.local", "amira.hassan@mail.local",
}
IDENTITY_EMAIL = "admin.healthcare@mail.local"
IDENTITY_NAME = "Admin Manager - Healthcare Records"
IDENTITY_ORG = "Corporate Records Office"
SIGNATURE_LINES = [
    "Admin Manager",
    "Healthcare Records Control",
    "Corporate Records Office",
    "admin.healthcare@mail.local",
]
ARCHIVE_NOTICES_FOLDER = "Healthcare_Archive_Notices"

RC_MAIL_BASE = "/var/mail/mail.local/james.whitfield"

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


def _docker_cat_bytes(container: str, path: str, timeout: int = 30) -> bytes | None:
    r = subprocess.run(
        ["docker", "exec", container, "cat", path],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def oc_db_query(sql: str) -> str:
    """Query ownCloud MariaDB and return stdout."""
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OC_DB, "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud",
        "--default-character-set=utf8mb4", "-N", "-B",
        "owncloud", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_db_query(sql: str) -> str:
    """Query OnlyOffice MySQL and return stdout."""
    rc, out, err = docker_exec(
        OO_DB, "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "-N", "-B",
        "onlyoffice", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mm_db_query(sql: str) -> str:
    """Query Mattermost PostgreSQL and return stdout."""
    rc, out, err = docker_exec(
        MM_DB, "psql", "-U", "mmuser", "-d", "mattermost",
        "-t", "-A", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def rc_db_query(sql: str) -> str:
    """Query Roundcube MariaDB and return stdout."""
    rc, out, err = docker_exec(
        RC_DB, "mysql", "-u", "roundcube", "-proundcube123",
        "--default-character-set=utf8mb4", "-N", "-B",
        "roundcubemail", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def _sql_in_list(values: list[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _oc_count_paths(paths: list[str]) -> int:
    """COUNT(*) of exact paths in admin's home storage."""
    out = oc_db_query(
        "SELECT COUNT(*) FROM oc_filecache fc "
        "JOIN oc_storages s ON fc.storage = s.numeric_id "
        f"WHERE s.id = 'home::admin' AND fc.path IN ({_sql_in_list(paths)});"
    )
    return int(out or "0")


# ── Mail parsing helpers (raw Maildir + stdlib) ───────────────────────────────
def _maildir_messages(folder: str) -> list[tuple[str, email.message.Message]]:
    """Read raw RFC822 files from a Maildir++ folder (e.g. '.Sent') of the
    sender's mailbox and parse them with the Python email stdlib."""
    msgs: list[tuple[str, email.message.Message]] = []
    for sub in ("cur", "new"):
        d = f"{RC_MAIL_BASE}/{folder}/{sub}"
        rc, out, _ = docker_exec(
            RC_CONTAINER, "bash", "-c", f"ls -1 '{d}' 2>/dev/null || true", timeout=20,
        )
        for fname in out.splitlines():
            fname = fname.strip()
            if not fname:
                continue
            raw = _docker_cat_bytes(RC_CONTAINER, f"{d}/{fname}")
            if raw:
                msgs.append((f"{d}/{fname}", email.message_from_bytes(raw)))
    return msgs


def _decode_hdr(value) -> str:
    """RFC2047-decode a header value and collapse folded whitespace."""
    if not value:
        return ""
    try:
        s = "".join(
            part.decode(enc or "ascii", "replace") if isinstance(part, bytes) else part
            for part, enc in decode_header(str(value))
        )
    except Exception:
        s = str(value)
    return " ".join(s.split())


def _addr_set(msg: email.message.Message, header: str) -> set[str]:
    """Lower-cased set of addresses parsed from all instances of a header."""
    return {addr.lower() for _, addr in getaddresses(msg.get_all(header) or []) if addr}


def _body_text(msg: email.message.Message) -> str:
    """Decoded, whitespace-normalized text of all text/* parts."""
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, "replace"))
    return " ".join(" ".join(chunks).split())


def _subject_matches(msg: email.message.Message) -> bool:
    return _decode_hdr(msg.get("Subject")) == EMAIL_SUBJECT


_NOTICE_MSG_CACHE: tuple | None = None


def _find_notice_msg() -> tuple[str | None, email.message.Message | None]:
    """Locate the sent formal notice: the move target folder first, then Sent."""
    global _NOTICE_MSG_CACHE
    if _NOTICE_MSG_CACHE is None:
        found: tuple = (None, None)
        for folder in (f".{ARCHIVE_NOTICES_FOLDER}", ".Sent"):
            for path, msg in _maildir_messages(folder):
                if _subject_matches(msg):
                    found = (path, msg)
                    break
            if found[1] is not None:
                break
        _NOTICE_MSG_CACHE = found
    return _NOTICE_MSG_CACHE


# ── OnlyOffice document helpers (fs + API fetch chain) ───────────────────────
def _xml_unescape(s: str) -> str:
    s = s.replace("&quot;", '"').replace("&apos;", "'")
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _xml_texts(xml: str, tag: str) -> list[str]:
    return [
        _xml_unescape(m)
        for m in re.findall(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", xml, re.DOTALL)
    ]


def _oo_file_id() -> str:
    out = oo_db_query(
        "SELECT id FROM files_file "
        f"WHERE title IN ('{OO_SHEET_TITLE}', '{OO_SHEET_TITLE}.xlsx') "
        "ORDER BY id DESC LIMIT 1;"
    )
    return out.splitlines()[0].strip() if out.strip() else ""


def _oo_content_bytes_fs(file_id: str, ext: str) -> bytes | None:
    rc, out, _ = docker_exec(
        OO_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.{ext}' "
        "2>/dev/null | sort -V | tail -1",
        timeout=30,
    )
    path = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not path:
        return None
    return _docker_cat_bytes(OO_CONTAINER, path)


def _oo_content_bytes_api(file_id: str) -> bytes | None:
    base = f"http://{HOST}:{OO_PORT}"
    auth = requests.post(
        f"{base}/api/2.0/authentication",
        json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
        timeout=15,
    )
    token = auth.json()["response"]["token"]
    s = requests.Session()
    s.headers["Authorization"] = token
    r = s.get(
        f"{base}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(file_id)},
        timeout=30, allow_redirects=True,
    )
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    r = s.get(f"{base}/api/2.0/files/file/{file_id}/download",
              timeout=30, allow_redirects=True)
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    return None


def _oo_get_register_xlsx() -> tuple[bytes | None, str]:
    """Fetch the archive register xlsx: data dir first, HTTP download fallback."""
    file_id = _oo_file_id()
    if not file_id:
        return None, "spreadsheet not found in files_file"
    detail = ""
    try:
        data = _oo_content_bytes_fs(file_id, "xlsx")
        if data:
            return data, "fs (content.xlsx via docker exec)"
        detail = "content.xlsx not found in data dir"
    except Exception as e:
        detail = f"fs read failed: {e}"
    try:
        data = _oo_content_bytes_api(file_id)
        if data:
            return data, "api download (fallback)"
    except Exception as e:
        return None, f"{detail}; api download failed: {e}"
    return None, f"{detail}; api download failed"


def _xlsx_cells_and_formulas(data: bytes) -> tuple[list[str], list[str], str]:
    """Return (sharedString cell values, formula texts, all worksheet xml)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    cells: list[str] = []
    try:
        ss_xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si(?:\s[^>]*)?>(.*?)</si>", ss_xml, re.DOTALL):
            cells.append("".join(_xml_texts(si, "t")).strip())
    except KeyError:
        pass
    formulas: list[str] = []
    sheets_xml = ""
    for name in zf.namelist():
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            xml = zf.read(name).decode("utf-8", "replace")
            sheets_xml += xml
            formulas.extend(_xml_texts(xml, "f"))
    return cells, formulas, sheets_xml


# ── ownCloud checks ──────────────────────────────────────────────────────────

def check_1_archived_files_in_folder() -> None:
    """Move+rename proven both ways: 3 exact archived paths exist AND the 3
    original paths under files/doc/healthcare are gone."""
    label = "1. Archived files in Obsolete_Healthcare_2026H1 (originals gone)"
    try:
        new_count = _oc_count_paths(ARCHIVED_PATHS)
        old_count = _oc_count_paths(ORIGINAL_PATHS)
        passed = new_count == 3 and old_count == 0
        check(label, 2, passed,
              f"archived_paths={new_count}/3, original_paths_remaining={old_count}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_files_tagged_obsolete() -> None:
    """Each of the 3 archived files (exact paths) is tagged 'obsolete'."""
    label = "2. Files tagged 'obsolete'"
    try:
        out = oc_db_query(
            "SELECT COUNT(DISTINCT fc.fileid) FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage=s.numeric_id "
            "JOIN oc_systemtag_object_mapping m ON m.objectid=CAST(fc.fileid AS CHAR) "
            "AND m.objecttype='files' "
            "JOIN oc_systemtag t ON t.id=m.systemtagid AND t.name='obsolete' "
            "WHERE s.id='home::admin' "
            f"AND fc.path IN ({_sql_in_list(ARCHIVED_PATHS)});"
        )
        count = int(out or "0")
        check(label, 1, count == 3, f"{count}/3 archived files tagged")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_3_active_docs_txt() -> None:
    """ACTIVE_HEALTHCARE_DOCS.txt content equals the task text (normalized)."""
    label = "3. ACTIVE_HEALTHCARE_DOCS.txt content"
    try:
        url = (f"http://{HOST}:{OC_PORT}/remote.php/dav/files/admin/"
               "doc/healthcare/ACTIVE_HEALTHCARE_DOCS.txt")
        r = requests.get(url, auth=("admin", "admin"), timeout=10)
        if r.status_code != 200:
            check(label, 2, False, f"HTTP {r.status_code}")
            return
        got = " ".join(r.text.split())
        expected = " ".join(ACTIVE_DOCS_EXPECTED.split())
        passed = got == expected
        check(label, 2, passed,
              "normalized full-text equality" if passed
              else f"content mismatch, got: {got[:120]!r}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

def check_5_spreadsheet_exists() -> None:
    """Spreadsheet 'Healthcare_Archive_Register_2026H1' exists (exact title)."""
    label = "5. Spreadsheet in OnlyOffice"
    try:
        file_id = _oo_file_id()
        check(label, 1, bool(file_id),
              f"files_file id={file_id}" if file_id else "not found in files_file")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5b_spreadsheet_content() -> None:
    """Register xlsx: headers, original/archived/replacement names, date,
    'Total Archived' row and a COUNTA formula over column B."""
    label = "5b. Spreadsheet register content"
    try:
        data, source = _oo_get_register_xlsx()
        if not data:
            check(label, 2, False, f"could not read spreadsheet: {source}")
            return
        cells, formulas, sheets_xml = _xlsx_cells_and_formulas(data)
        cell_set = set(cells)

        required_cells = (XLSX_HEADERS + ORIGINAL_FILES + ARCHIVED_FILES
                          + REPLACEMENT_FILES + ["Total Archived"])
        missing = [c for c in required_cells if c not in cell_set]

        # '2026-04-20' may be a shared string or an inline/date-formatted cell
        date_ok = "2026-04-20" in cell_set or "2026-04-20" in sheets_xml

        counta_ok = any(re.search(r"COUNTA\(\s*\$?B", f, re.IGNORECASE)
                        for f in formulas)

        passed = not missing and date_ok and counta_ok
        details = []
        if missing:
            details.append(f"missing cells: {missing[:4]}")
        if not date_ok:
            details.append("archived date 2026-04-20 not found")
        if not counta_ok:
            details.append(f"no COUNTA formula on column B (formulas={formulas[:3]})")
        check(label, 2, passed,
              "; ".join(details) if details else f"all probes found, source: {source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_spreadsheet_shared() -> None:
    """Spreadsheet shared with amit.singh for editing, scoped to THIS file's
    entry_id (the seed dump already contains files_security rows for
    amit.singh on other entries, so an unscoped query passes on a pristine
    environment)."""
    label = "6. Spreadsheet shared with amit.singh (edit)"
    try:
        sql = (
            "SELECT cu.username, fs.security FROM files_security fs "
            "JOIN core_user cu ON fs.subject = cu.id "
            "WHERE fs.entry_type = 2 "
            "AND fs.entry_id = (SELECT CAST(id AS CHAR) FROM files_file "
            f"WHERE title IN ('{OO_SHEET_TITLE}','{OO_SHEET_TITLE}.xlsx') "
            "ORDER BY id DESC LIMIT 1) "
            "AND cu.username = 'amit.singh' AND fs.security = 1;"
        )
        out = oo_db_query(sql)
        passed = bool(out.strip())
        check(label, 1, passed,
              f"row: {out.strip().splitlines()[0]}" if passed
              else "no edit share on this file for amit.singh")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Mattermost checks ────────────────────────────────────────────────────────

def check_7_archive_notice_posted() -> None:
    """Archive notice in bug-triage (Product & Design team) lists all 6
    filenames and the effective date, in a single post."""
    label = "7. Archive notice in bug-triage"
    try:
        sql = (
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Product & Design' "
            "AND c.name = 'bug-triage' "
            "AND p.deleteat = 0 "
            f"AND p.message LIKE '%{MM_NOTICE_FRAGMENT}%';"
        )
        out = mm_db_query(sql)
        probes = (["Healthcare Archive Notice:"] + ORIGINAL_FILES
                  + REPLACEMENT_FILES + ["Effective 2026-04-20."])
        found = any(all(p in line for p in probes) for line in out.splitlines())
        check(label, 2, found,
              "single post with all 6 filenames + effective date" if found
              else "no single post contains all required content")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_channel_header_updated() -> None:
    """bug-triage channel header equals the full expected string."""
    label = "8. bug-triage channel header updated"
    try:
        sql = (
            "SELECT c.header FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Product & Design' AND c.name = 'bug-triage';"
        )
        out = mm_db_query(sql)
        passed = out.strip() == MM_HEADER_EXPECTED
        check(label, 2, passed,
              "full-string equality" if passed else f"got: {out[:120]!r}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_dm_to_admin() -> None:
    """DM to 'admin' (self-DM channel adminid__adminid) with the full text."""
    label = "9. DM to admin re: sharing links"
    try:
        sql = (
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "WHERE c.type = 'D' "
            "AND p.deleteat = 0 "
            "AND c.name = (SELECT id || '__' || id FROM users WHERE username = 'admin') "
            "AND p.userid = (SELECT id FROM users WHERE username = 'admin') "
            f"AND p.message LIKE '%{MM_DM_FRAGMENT}%';"
        )
        out = mm_db_query(sql)
        found = any(MM_DM_ANCHOR in line for line in out.splitlines())
        check(label, 1, found,
              "full-text anchor matched in admin self-DM" if found
              else "not found in admin self-DM channel")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Roundcube checks ─────────────────────────────────────────────────────────

def check_10_identity_created() -> None:
    """Identity: exact display name, org, and all 4 signature lines."""
    label = "10. Roundcube identity created"
    try:
        name = rc_db_query(
            f"SELECT name FROM identities WHERE email = '{IDENTITY_EMAIL}';"
        )
        if not name.strip():
            check(label, 2, False, f"no identity with email {IDENTITY_EMAIL}")
            return
        org = rc_db_query(
            f"SELECT organization FROM identities WHERE email = '{IDENTITY_EMAIL}';"
        )
        sig = rc_db_query(
            f"SELECT signature FROM identities WHERE email = '{IDENTITY_EMAIL}';"
        )
        name_ok = name.strip() == IDENTITY_NAME
        org_ok = org.strip() == IDENTITY_ORG
        sig_missing = [l for l in SIGNATURE_LINES if l not in sig]
        passed = name_ok and org_ok and not sig_missing
        check(label, 2, passed,
              f"name_ok={name_ok}, org_ok={org_ok}, sig_missing={sig_missing}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_email_sent() -> None:
    """Formal notice email: exact subject; To set equality; From uses the new
    identity; body supersession lines; Normal priority."""
    label = "11. Email sent to department heads"
    try:
        _, msg = _find_notice_msg()
        if msg is None:
            check(label, 2, False,
                  "no message with the exact subject in Healthcare_Archive_Notices or Sent")
            return
        to_ok = _addr_set(msg, "To") == EMAIL_TO_SET
        pairs = getaddresses(msg.get_all("From") or [])
        from_name = _decode_hdr(pairs[0][0]) if pairs else ""
        from_addr = pairs[0][1].lower() if pairs else ""
        from_ok = from_addr == IDENTITY_EMAIL and from_name == IDENTITY_NAME
        body = _body_text(msg)
        probes = [
            ("Kima_w_Medical_Center_Nursing_Position_Description.docx -> "
             "replaced by nursing_position_v3.docx (effective 2026-04-20)"),
            ("Inflammation_Protein_Results_Mann_Whitney_U_Test_Ratios_Sensitivity_"
             "Specificity.docx -> replaced by inflammation_protein_final_2026.docx "
             "(effective 2026-04-20)"),
            ("MassHealth_Medicaid_CHIP_Section_1115_Demonstration_Waiver.docx -> "
             "replaced by masshealth_1115_renewal_2026.docx (effective 2026-04-20)"),
            "ARCHIVED_ prefix",
        ]
        missing = [p[:40] for p in probes if p not in body]
        xp = msg.get("X-Priority")
        prio_ok = xp is None or (xp.strip().split() or [""])[0] == "3"
        passed = to_ok and from_ok and not missing and prio_ok
        check(label, 2, passed,
              f"to={to_ok}, from={from_ok} ({from_name!r} <{from_addr}>), "
              f"body_missing={missing}, normal_priority={prio_ok}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_archive_notices_folder() -> None:
    """Mail folder 'Healthcare_Archive_Notices' exists (Maildir++ directory)."""
    label = "12. Healthcare_Archive_Notices folder"
    try:
        d = f"{RC_MAIL_BASE}/.{ARCHIVE_NOTICES_FOLDER}"
        rc, out, _ = docker_exec(
            RC_CONTAINER, "bash", "-c",
            f"test -d '{d}' && echo yes || echo no",
            timeout=15,
        )
        found = out.strip() == "yes"
        check(label, 1, found, f"dir={d}" if found else f"{d} not found")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_13_email_moved_to_folder() -> None:
    """Move semantics: exact-subject message present in
    Healthcare_Archive_Notices AND absent from Sent."""
    label = "13. Email moved to Healthcare_Archive_Notices"
    try:
        in_folder = any(
            _subject_matches(m)
            for _, m in _maildir_messages(f".{ARCHIVE_NOTICES_FOLDER}")
        )
        sent_count = sum(
            1 for _, m in _maildir_messages(".Sent") if _subject_matches(m)
        )
        passed = in_folder and sent_count == 0
        check(label, 1, passed,
              f"in_folder={in_folder}, sent_copies={sent_count} "
              "(move must leave no Sent copy; a copy-semantics client fails "
              "the Sent-absence sub-assertion)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_archived_files_in_folder()
    check_2_files_tagged_obsolete()
    check_3_active_docs_txt()
    check_5_spreadsheet_exists()
    check_5b_spreadsheet_content()
    check_6_spreadsheet_shared()
    check_7_archive_notice_posted()
    check_8_channel_header_updated()
    check_9_dm_to_admin()
    check_10_identity_created()
    check_11_email_sent()
    check_12_archive_notices_folder()
    check_13_email_moved_to_folder()

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
