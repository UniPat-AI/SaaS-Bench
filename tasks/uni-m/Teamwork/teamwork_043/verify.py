"""
Verifier for Teamwork-043-I3: Archive expiring PPT files and notify stakeholders

Checks: 14 weighted checks (20 pt) across owncloud, onlyoffice, mattermost, roundcubemail.
Strategy: docker exec DB queries (ownCloud MariaDB, OnlyOffice MySQL, Mattermost Postgres,
          Roundcube MariaDB), REST API for OnlyOffice sharing, xlsx content probes read
          from the OnlyOffice data dir (API download fallback), and raw Maildir parsing
          of the sender's mail copies (docker exec + Python email stdlib).

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


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    return val


OWNCLOUD_PORT = _require("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = _require("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = _require("OWNCLOUD_DB_CONTAINER")
ONLYOFFICE_PORT = _require("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = _require("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = _require("ONLYOFFICE_DB_CONTAINER")
MATTERMOST_PORT = _require("MATTERMOST_PORT")
MATTERMOST_CONTAINER = _require("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = _require("MATTERMOST_DB_CONTAINER")
ROUNDCUBEMAIL_PORT = _require("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = _require("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = _require("ROUNDCUBEMAIL_DB_CONTAINER")

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


def oc_db(sql: str) -> str:
    """Query ownCloud MariaDB."""
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OWNCLOUD_DB_CONTAINER,
        "mysql", "-h", "127.0.0.1", "--default-character-set=utf8mb4",
        "-u", "owncloud", "-powncloud", "owncloud", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_db(sql: str) -> str:
    """Query OnlyOffice MySQL."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "-N", "-B", "onlyoffice", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mm_db(sql: str) -> str:
    """Query Mattermost PostgreSQL."""
    rc, out, err = docker_exec(
        MATTERMOST_DB_CONTAINER,
        "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def rc_db(sql: str) -> str:
    """Query Roundcube MariaDB."""
    rc, out, err = docker_exec(
        ROUNDCUBEMAIL_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "roundcube", "-proundcube123", "roundcubemail", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def _sql_in_list(values: list[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def _oc_count_paths(paths: list[str]) -> int:
    """COUNT(*) of exact paths in admin's home storage."""
    out = oc_db(
        "SELECT COUNT(*) FROM oc_filecache fc "
        "JOIN oc_storages s ON fc.storage = s.numeric_id "
        f"WHERE s.id = 'home::admin' AND fc.path IN ({_sql_in_list(paths)});"
    )
    return int(out or "0")


def _oc_tagged_count(paths: list[str], tag: str) -> int:
    """COUNT(DISTINCT fileid) of exact paths carrying a system tag."""
    out = oc_db(
        "SELECT COUNT(DISTINCT fc.fileid) FROM oc_filecache fc "
        "JOIN oc_storages s ON fc.storage = s.numeric_id "
        "JOIN oc_systemtag_object_mapping m ON m.objectid = CAST(fc.fileid AS CHAR) "
        "AND m.objecttype = 'files' "
        f"JOIN oc_systemtag t ON t.id = m.systemtagid AND t.name = '{tag}' "
        "WHERE s.id = 'home::admin' "
        f"AND fc.path IN ({_sql_in_list(paths)});"
    )
    return int(out or "0")


# ── Constants ─────────────────────────────────────────────────────────────────
ORIGINAL_FILES = [
    "IDCC2022_FosterinCollaborationDMP_JAC.pptx",
    "Ionescu_S1.pptx",
    "Li_et_al_ESR1_mutations_paper_updated_SM.pptx",
    "Module5-Repositories_presentation.pptx",
    "Fathallah_Exeter University_November 2023_slides.pptx",
]

EXPIRED_FILES = [
    "IDCC2022_FosterinCollaborationDMP_JAC_EXPIRED_2025-09-30.pptx",
    "Ionescu_S1_EXPIRED_2025-09-30.pptx",
    "Li_et_al_ESR1_mutations_paper_updated_SM_EXPIRED_2025-09-30.pptx",
    "Module5-Repositories_presentation_EXPIRED_2025-09-30.pptx",
    "Fathallah_Exeter University_November 2023_slides_EXPIRED_2025-09-30.pptx",
]

REPLACEMENT_FILES = [
    "IDCC2022_FosterinCollaborationDMP_JAC_2025Q4.txt",
    "Ionescu_S1_2025Q4.txt",
    "Li_et_al_ESR1_mutations_paper_updated_SM_2025Q4.txt",
    "Module5-Repositories_presentation_2025Q4.txt",
    "Fathallah_Exeter_University_November_2023_slides_2025Q4.txt",
]

RETIRED_DIR = "files/ppt/Retired_2025Q3"
EXPIRED_PATHS = [f"{RETIRED_DIR}/{n}" for n in EXPIRED_FILES]
ORIGINAL_PATHS = [f"files/ppt/{n}" for n in ORIGINAL_FILES]
REPLACEMENT_PATHS = [f"files/ppt/{n}" for n in REPLACEMENT_FILES]

OO_SHEET_TITLE = "Presentation_Renewal_Register_2025Q3"
XLSX_HEADERS = ["Document Name", "Archived Filename", "Replacement Filename"]

MM_NOTICE_FRAGMENT = "Presentation Document Renewal Notice"
MM_NOTICE_MAPPINGS = [
    f"{orig} -> {repl}" for orig, repl in zip(ORIGINAL_FILES, REPLACEMENT_FILES)
]
MM_DM_FRAGMENT = "presentation files you previously contributed"
MM_DM_ANCHOR = (
    "Hi research team, the presentation files you previously contributed to in "
    "/ppt have been retired as of 2025-10-01. Please review the replacement "
    "placeholder files in ownCloud and upload the updated content versions."
)

EMAIL_SUBJECT = "Q3 2025 Presentation Document Renewal Notification"
EMAIL_TO_SET = {"compliance-oversight@regulator.gov"}
EMAIL_BCC_SET = {"compliance-internal@mail.local"}
EMAIL_BODY_PROBES = [
    "IDCC2022_FosterinCollaborationDMP_JAC (replaced by IDCC2022_FosterinCollaborationDMP_JAC_2025Q4)",
    "Ionescu_S1 (replaced by Ionescu_S1_2025Q4)",
    "Li_et_al_ESR1_mutations_paper_updated_SM (replaced by Li_et_al_ESR1_mutations_paper_updated_SM_2025Q4)",
    "Module5-Repositories_presentation (replaced by Module5-Repositories_presentation_2025Q4)",
    "Fathallah_Exeter_University_November_2023_slides (replaced by Fathallah_Exeter_University_November_2023_slides_2025Q4)",
    "Please acknowledge receipt",
]

RC_MAIL_BASE = "/var/mail/mail.local/james.whitfield"


# ── Mail parsing helpers (raw Maildir + stdlib) ───────────────────────────────
def _maildir_messages(folder: str) -> list[tuple[str, email.message.Message]]:
    """Read raw RFC822 files from a Maildir++ folder (e.g. '.Sent') of the
    sender's mailbox and parse them with the Python email stdlib."""
    msgs: list[tuple[str, email.message.Message]] = []
    for sub in ("cur", "new"):
        d = f"{RC_MAIL_BASE}/{folder}/{sub}"
        rc, out, _ = docker_exec(
            ROUNDCUBEMAIL_CONTAINER, "bash", "-c",
            f"ls -1 '{d}' 2>/dev/null || true", timeout=20,
        )
        for fname in out.splitlines():
            fname = fname.strip()
            if not fname:
                continue
            raw = _docker_cat_bytes(ROUNDCUBEMAIL_CONTAINER, f"{d}/{fname}")
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


def _find_renewal_msg() -> tuple[str | None, email.message.Message | None]:
    """Locate the sent renewal email: Archive first (ck13 archives it), then Sent."""
    global _NOTICE_MSG_CACHE
    if _NOTICE_MSG_CACHE is None:
        found: tuple = (None, None)
        for folder in (".Archive", ".Sent"):
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


def _oo_file_id_db() -> str:
    out = oo_db(
        "SELECT id FROM files_file "
        f"WHERE title IN ('{OO_SHEET_TITLE}', '{OO_SHEET_TITLE}.xlsx') "
        "ORDER BY id DESC LIMIT 1;"
    )
    return out.splitlines()[0].strip() if out.strip() else ""


def _oo_content_bytes_fs(file_id: str, ext: str) -> bytes | None:
    rc, out, _ = docker_exec(
        ONLYOFFICE_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.{ext}' "
        "2>/dev/null | sort -V | tail -1",
        timeout=30,
    )
    path = out.strip().splitlines()[-1].strip() if out.strip() else ""
    if not path:
        return None
    return _docker_cat_bytes(ONLYOFFICE_CONTAINER, path)


def _oo_auth_headers() -> dict:
    base = f"http://{HOST}:{ONLYOFFICE_PORT}"
    auth = requests.post(
        f"{base}/api/2.0/authentication",
        json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
        timeout=15,
    )
    token = auth.json()["response"]["token"]
    return {"Authorization": f"Bearer {token}"}


def _oo_content_bytes_api(file_id: str) -> bytes | None:
    base = f"http://{HOST}:{ONLYOFFICE_PORT}"
    hdrs = _oo_auth_headers()
    r = requests.get(
        f"{base}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(file_id)},
        headers=hdrs, timeout=30, allow_redirects=True,
    )
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    r = requests.get(f"{base}/api/2.0/files/file/{file_id}/download",
                     headers=hdrs, timeout=30, allow_redirects=True)
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    return None


def _oo_get_register_xlsx() -> tuple[bytes | None, str]:
    """Fetch the renewal register xlsx: data dir first, HTTP download fallback."""
    file_id = _oo_file_id_db()
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


def _xlsx_cells_and_formulas(data: bytes) -> tuple[list[str], list[str]]:
    """Return (sharedString cell values, formula texts)."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    cells: list[str] = []
    try:
        ss_xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        for si in re.findall(r"<si(?:\s[^>]*)?>(.*?)</si>", ss_xml, re.DOTALL):
            cells.append("".join(_xml_texts(si, "t")).strip())
    except KeyError:
        pass
    formulas: list[str] = []
    for name in zf.namelist():
        if name.startswith("xl/worksheets/") and name.endswith(".xml"):
            xml = zf.read(name).decode("utf-8", "replace")
            formulas.extend(_xml_texts(xml, "f"))
    return cells, formulas


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_retired_folder() -> None:
    """Verify Retired_2025Q3 folder exists at the exact path in ownCloud."""
    try:
        count = _oc_count_paths([RETIRED_DIR])
        found = count >= 1
        check("1. ownCloud: Retired_2025Q3 folder in ppt", 1, found,
              "found" if found else f"{RETIRED_DIR} not found")
    except Exception as e:
        check("1. ownCloud: Retired_2025Q3 folder in ppt", 1, False, f"exception: {e}")


def check_2_expired_files() -> None:
    """Move+rename proven both ways: 5 exact EXPIRED paths exist in
    Retired_2025Q3 AND the 5 original names are gone from files/ppt/."""
    label = "2. ownCloud: expired files in Retired_2025Q3 (originals gone)"
    try:
        new_count = _oc_count_paths(EXPIRED_PATHS)
        old_count = _oc_count_paths(ORIGINAL_PATHS)
        passed = new_count == 5 and old_count == 0
        check(label, 2, passed,
              f"expired_paths={new_count}/5, original_paths_remaining={old_count}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_archived_tags() -> None:
    """Each of the 5 exact archived paths tagged 'archived' and none 'pending'."""
    label = "3. ownCloud: expired files tagged archived/not pending"
    try:
        archived_count = _oc_tagged_count(EXPIRED_PATHS, "archived")
        pending_count = _oc_tagged_count(EXPIRED_PATHS, "pending")
        passed = archived_count == 5 and pending_count == 0
        check(label, 2, passed,
              f"archived={archived_count}/5, pending={pending_count}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_replacement_files() -> None:
    """Verify the 5 replacement .txt files exist at exact paths in files/ppt/."""
    label = "4. ownCloud: replacement files in ppt"
    try:
        count = _oc_count_paths(REPLACEMENT_PATHS)
        check(label, 1, count == 5, f"{count}/5 found at exact paths")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5_approved_tags() -> None:
    """Verify the 5 replacement files (exact paths) are tagged 'approved'."""
    label = "5. ownCloud: replacement files tagged approved"
    try:
        approved_count = _oc_tagged_count(REPLACEMENT_PATHS, "approved")
        check(label, 1, approved_count == 5, f"{approved_count}/5 tagged")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_6_oo_spreadsheet() -> None:
    """Verify Presentation_Renewal_Register_2025Q3 exists (exact title)."""
    try:
        titles = {OO_SHEET_TITLE, OO_SHEET_TITLE + ".xlsx"}
        base = f"http://{HOST}:{ONLYOFFICE_PORT}"
        hdrs = _oo_auth_headers()
        resp = requests.get(f"{base}/api/2.0/files/@common", headers=hdrs, timeout=15)
        data = resp.json().get("response", {})
        files = data.get("files", []) if isinstance(data, dict) else []
        found = any(f.get("title", "") in titles for f in files)
        check("6. OnlyOffice: spreadsheet exists", 1, found,
              "found" if found else "not in Common Documents")
    except Exception as e:
        check("6. OnlyOffice: spreadsheet exists", 1, False, f"exception: {e}")


def check_6b_oo_spreadsheet_content() -> None:
    """Register xlsx: 3 headers, all 15 filenames, 'Documents Renewed' row,
    and a COUNTA formula over column C."""
    label = "6b. OnlyOffice: spreadsheet register content"
    try:
        data, source = _oo_get_register_xlsx()
        if not data:
            check(label, 2, False, f"could not read spreadsheet: {source}")
            return
        cells, formulas = _xlsx_cells_and_formulas(data)
        cell_set = set(cells)

        required_cells = (XLSX_HEADERS + ORIGINAL_FILES + EXPIRED_FILES
                          + REPLACEMENT_FILES + ["Documents Renewed"])
        missing = [c for c in required_cells if c not in cell_set]

        counta_ok = any(re.search(r"COUNTA\(\s*\$?C", f, re.IGNORECASE)
                        for f in formulas)

        passed = not missing and counta_ok
        details = []
        if missing:
            details.append(f"missing cells: {missing[:4]}")
        if not counta_ok:
            details.append(f"no COUNTA formula on column C (formulas={formulas[:3]})")
        check(label, 2, passed,
              "; ".join(details) if details else f"all probes found, source: {source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_oo_sharing() -> None:
    """Spreadsheet shared with jun.chen (edit, access==1) and laura.brown
    (view, access==2); userName exact equality primary, displayName fallback."""
    label = "7. OnlyOffice: spreadsheet sharing"
    try:
        file_id = _oo_file_id_db()
        if not file_id:
            check(label, 2, False, "file not found")
            return

        base = f"http://{HOST}:{ONLYOFFICE_PORT}"
        hdrs = _oo_auth_headers()
        share_resp = requests.get(
            f"{base}/api/2.0/files/file/{file_id}/share", headers=hdrs, timeout=15,
        )
        shares = share_resp.json().get("response", [])
        jun_edit = False
        laura_view = False
        for s in shares:
            shared_to = s.get("sharedTo", {}) or {}
            uname = str(shared_to.get("userName", ""))
            dname = str(shared_to.get("displayName", ""))
            try:
                access = int(s.get("access", -1))
            except (TypeError, ValueError):
                access = -1
            # OnlyOffice access levels: 1=ReadWrite (edit), 2=Read (view).
            is_jun = uname == "jun.chen" or (not uname and dname == "Jun Chen")
            is_laura = uname == "laura.brown" or (not uname and dname == "Laura Brown")
            if is_jun and access == 1:
                jun_edit = True
            if is_laura and access == 2:
                laura_view = True
        passed = jun_edit and laura_view
        check(label, 2, passed,
              f"jun_edit(access==1)={jun_edit}, laura_view(access==2)={laura_view}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_mm_renewal_notice() -> None:
    """Renewal notice in UX Research (Product & Design team) lists all 5
    'original -> replacement' mappings in a single post."""
    label = "8. Mattermost: renewal notice in UX Research"
    try:
        result = mm_db(
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Product & Design' "
            "AND c.displayname = 'UX Research' "
            "AND p.deleteat = 0 "
            f"AND p.message LIKE '%{MM_NOTICE_FRAGMENT}%';"
        )
        probes = [MM_NOTICE_FRAGMENT + ":"] + MM_NOTICE_MAPPINGS
        found = any(all(p in line for p in probes) for line in result.splitlines())
        check(label, 2, found,
              "single post with all 5 mappings" if found
              else "no single post contains all 5 original -> replacement mappings")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_mm_purpose() -> None:
    """Verify UX Research channel purpose updated (Product & Design team)."""
    try:
        result = mm_db(
            "SELECT c.purpose FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Product & Design' "
            "AND c.displayname = 'UX Research' LIMIT 1;"
        )
        expected = "track Q3 2025 presentation renewals"
        passed = expected in (result or "")
        check("9. Mattermost: UX Research purpose updated", 1, passed,
              "matched" if passed else f"got: {(result or '')[:80]}")
    except Exception as e:
        check("9. Mattermost: UX Research purpose updated", 1, False, f"exception: {e}")


def check_10_mm_group_dm() -> None:
    """Group DM whose member set is exactly {genesis, ginny, nilda, admin}
    contains the full notification text."""
    label = "10. Mattermost: group DM to genesis/ginny/nilda"
    try:
        result = mm_db(
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "WHERE c.type = 'G' AND p.deleteat = 0 "
            f"AND p.message LIKE '%{MM_DM_FRAGMENT}%' "
            "AND (SELECT COUNT(*) FROM channelmembers cm "
            "JOIN users u ON cm.userid = u.id "
            "WHERE cm.channelid = c.id "
            "AND u.username IN ('genesis','ginny','nilda','admin')) = 4;"
        )
        found = any(MM_DM_ANCHOR in line for line in result.splitlines())
        check(label, 2, found,
              "full-text anchor in group DM with all 4 members" if found
              else "not found in a group DM containing genesis+ginny+nilda+admin")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_rc_archive_pref() -> None:
    """Archive folder preference resolves to 'Archive': explicit serialized
    pref, or absent while the container default is already 'Archive'
    (the task step is verify-and-set-if-needed; Roundcube drops prefs that
    equal the configured default)."""
    label = "11. Roundcube: archive folder set to Archive"
    try:
        prefs = rc_db(
            "SELECT preferences FROM users "
            "WHERE username = 'james.whitfield@mail.local';"
        )
        m = re.search(r'"archive_mbox";s:\d+:"([^"]*)"', prefs or "")
        if m:
            passed = m.group(1) == "Archive"
            check(label, 1, passed, f"explicit pref archive_mbox={m.group(1)!r}")
            return
        # No explicit pref: inspect the container's configured default.
        rc, out, _ = docker_exec(
            ROUNDCUBEMAIL_CONTAINER, "bash", "-c",
            "find /var/www/html /usr/src/roundcubemail -maxdepth 4 "
            "\\( -name 'config.inc.php' -o -name 'defaults.inc.php' \\) 2>/dev/null "
            "| xargs grep -h archive_mbox 2>/dev/null || true",
            timeout=20,
        )
        dm = re.search(r"archive_mbox'\]\s*=\s*'([^']*)'", out or "")
        if dm and dm.group(1) == "Archive":
            check(label, 1, True,
                  "pref absent; container default archive_mbox='Archive' "
                  "already satisfies the verify-only step")
        else:
            default_val = repr(dm.group(1)) if dm else "not found"
            check(label, 1, False,
                  f"pref absent and container default is {default_val}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_12_rc_email_sent() -> None:
    """Renewal email: exact subject; To/Bcc set equality; X-Priority == 2
    only; body lists all 5 replacements + acknowledgement request."""
    label = "12. Roundcube: email sent with high priority"
    try:
        _, msg = _find_renewal_msg()
        if msg is None:
            check(label, 1, False,
                  "no message with the exact subject in Archive or Sent")
            return
        to_ok = _addr_set(msg, "To") == EMAIL_TO_SET
        bcc_ok = _addr_set(msg, "Bcc") == EMAIL_BCC_SET
        xp = msg.get("X-Priority") or ""
        prio_ok = (xp.strip().split() or [""])[0] == "2"
        body = _body_text(msg)
        missing = [p[:40] for p in EMAIL_BODY_PROBES if p not in body]
        passed = to_ok and bcc_ok and prio_ok and not missing
        check(label, 1, passed,
              f"to={to_ok}, bcc={bcc_ok}, x_priority={xp.strip()!r} (must be 2), "
              f"body_missing={missing}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_13_rc_email_archived() -> None:
    """Archive semantics: exact-subject message present in Archive AND absent
    from Sent."""
    label = "13. Roundcube: email archived"
    try:
        in_archive = any(
            _subject_matches(m) for _, m in _maildir_messages(".Archive")
        )
        sent_count = sum(
            1 for _, m in _maildir_messages(".Sent") if _subject_matches(m)
        )
        passed = in_archive and sent_count == 0
        check(label, 1, passed,
              f"in_archive={in_archive}, sent_copies={sent_count} "
              "(archive must move the message out of Sent; a copy-semantics "
              "config fails the Sent-absence sub-assertion)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_retired_folder()
    check_2_expired_files()
    check_3_archived_tags()
    check_4_replacement_files()
    check_5_approved_tags()
    check_6_oo_spreadsheet()
    check_6b_oo_spreadsheet_content()
    check_7_oo_sharing()
    check_8_mm_renewal_notice()
    check_9_mm_purpose()
    check_10_mm_group_dm()
    check_11_rc_archive_pref()
    check_12_rc_email_sent()
    check_13_rc_email_archived()

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
