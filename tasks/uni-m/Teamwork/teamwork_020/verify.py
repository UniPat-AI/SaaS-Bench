"""
Verifier for Teamwork-020-I2: Roll Out Updated Healthcare Telehealth Fee Policy

Checks: 16 weighted checks (17 pt) across owncloud, onlyoffice, mattermost, roundcubemail.
Strategy: docker exec DB queries (ownCloud MariaDB, OnlyOffice MySQL, Mattermost Postgres,
          Roundcube MariaDB), REST API (ownCloud WebDAV, OnlyOffice API), docx content
          probes read from the OnlyOffice data dir (API download fallback), and raw
          Maildir parsing of the sender's Sent copy (docker exec + Python email stdlib).

Required env vars:
  SERVER_HOSTNAME,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER.
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


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"FATAL: {name} not set", file=sys.stderr)
        sys.exit(1)
    return val


OC_PORT = _require("OWNCLOUD_PORT")
OC_CONTAINER = _require("OWNCLOUD_CONTAINER")
OC_DB = _require("OWNCLOUD_DB_CONTAINER")

OO_PORT = _require("ONLYOFFICE_PORT")
OO_CONTAINER = _require("ONLYOFFICE_CONTAINER")
OO_DB = _require("ONLYOFFICE_DB_CONTAINER")

MM_PORT = _require("MATTERMOST_PORT")
MM_CONTAINER = _require("MATTERMOST_CONTAINER")
MM_DB = _require("MATTERMOST_DB_CONTAINER")

RC_PORT = _require("ROUNDCUBEMAIL_PORT")
RC_CONTAINER = _require("ROUNDCUBEMAIL_CONTAINER")
RC_DB = _require("ROUNDCUBEMAIL_DB_CONTAINER")


# ── Expected values ───────────────────────────────────────────────────────────
ARCHIVED_PATH = ("files/doc/healthcare/"
                 "Telehealth_GP_Phone_Service_Fee_Alignment_Determination_2021_ARCHIVED.docx")
ORIGINAL_PATH = ("files/doc/healthcare/"
                 "Telehealth_GP_Phone_Service_Fee_Alignment_Determination_2021.docx")
POLICY_TXT_PATH = "files/doc/healthcare/Telehealth_Fee_Alignment_Policy_v2.txt"

OO_DOC_TITLE = "Telehealth Fee Alignment Policy v2 - Change Summary"

MM_ANALYTICS_ANCHOR = (
    "📢 Policy Update: Telehealth Fee Alignment Policy v2 is now published, "
    "effective September 1, 2026. Major changes include tiered phone consultation "
    "pricing, a 15% video premium, stricter documentation requirements, and a "
    "shortened 30-day claim window. Full policy available in ownCloud "
    "(files/doc/healthcare). Please acknowledge in the HR portal by August 20, 2026."
)
MM_ANALYTICS_FRAGMENT = "Policy Update: Telehealth Fee Alignment Policy v2 is now published"
MM_HR_NOTE_ANCHOR = (
    "HR/Comms team: Telehealth Fee Alignment Policy v2 rollout underway. "
    "Old 2021 determination archived, v2 policy published, change summary shared "
    "with legal (laura.brown) for review. Please flag any branding/communication "
    "asset updates needed for provider-facing materials."
)
MM_HR_NOTE_FRAGMENT = "Telehealth Fee Alignment Policy v2 rollout underway"

RC_IDENTITY_ORG = "Acme Health Services — HR Communications"
EMAIL_SUBJECT = ("[Action Required] Telehealth Fee Alignment Policy v2 "
                 "— Effective September 1, 2026")

RC_MAIL_BASE = "/var/mail/mail.local/james.whitfield"


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Generic helpers ───────────────────────────────────────────────────────────
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


def oc_sql(sql: str) -> str:
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OC_DB, "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud",
        "--default-character-set=utf8mb4", "owncloud", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        OO_DB, "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "-N", "-B", "onlyoffice", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mm_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        MM_DB, "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def rc_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        RC_DB, "mysql", "-u", "roundcube", "-proundcube123",
        "--default-character-set=utf8mb4", "roundcubemail", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def _oc_path_count(path: str) -> int:
    """COUNT(*) of an exact path in admin's home storage."""
    escaped = path.replace("'", "''")
    out = oc_sql(
        "SELECT COUNT(*) FROM oc_filecache fc "
        "JOIN oc_storages s ON fc.storage = s.numeric_id "
        f"WHERE s.id = 'home::admin' AND fc.path = '{escaped}';"
    )
    return int(out or "0")


# ── Mail parsing helpers (raw Sent Maildir + stdlib) ─────────────────────────
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


_SENT_MSG_CACHE: tuple | None = None


def _sent_policy_msg() -> tuple[str | None, email.message.Message | None]:
    """Locate the Sent copy whose decoded Subject equals the expected subject."""
    global _SENT_MSG_CACHE
    if _SENT_MSG_CACHE is None:
        found: tuple = (None, None)
        for path, msg in _maildir_messages(".Sent"):
            if _decode_hdr(msg.get("Subject")) == EMAIL_SUBJECT:
                found = (path, msg)
                break
        _SENT_MSG_CACHE = found
    return _SENT_MSG_CACHE


# ── OnlyOffice document helpers (fs + API fetch chain) ───────────────────────
def _xml_unescape(s: str) -> str:
    s = s.replace("&quot;", '"').replace("&apos;", "'")
    return s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _xml_texts(xml: str, tag: str) -> list[str]:
    return [
        _xml_unescape(m)
        for m in re.findall(rf"<{tag}(?:\s[^>]*)?>(.*?)</{tag}>", xml, re.DOTALL)
    ]


def _oo_session() -> requests.Session:
    """Authenticate to OnlyOffice and return a session with auth header."""
    s = requests.Session()
    r = s.post(
        f"http://{HOST}:{OO_PORT}/api/2.0/authentication",
        json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json()["response"]["token"]
    s.headers["Authorization"] = token
    return s


def _oo_file_id(titles: list[str]) -> str:
    in_list = ", ".join("'" + t.replace("'", "''") + "'" for t in titles)
    out = oo_sql(
        f"SELECT id FROM files_file WHERE title IN ({in_list}) ORDER BY id DESC LIMIT 1;"
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
    s = _oo_session()
    r = s.get(
        f"http://{HOST}:{OO_PORT}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(file_id)},
        timeout=30, allow_redirects=True,
    )
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    r = s.get(
        f"http://{HOST}:{OO_PORT}/api/2.0/files/file/{file_id}/download",
        timeout=30, allow_redirects=True,
    )
    if r.status_code == 200 and len(r.content) > 100:
        return r.content
    return None


_OO_DOC_CACHE: tuple | None = None


def _oo_get_summary_docx() -> tuple[bytes | None, str]:
    """Fetch the change summary docx: data dir first, HTTP download fallback."""
    global _OO_DOC_CACHE
    if _OO_DOC_CACHE is not None:
        return _OO_DOC_CACHE
    file_id = _oo_file_id([OO_DOC_TITLE, OO_DOC_TITLE + ".docx"])
    if not file_id:
        _OO_DOC_CACHE = (None, "document not found in files_file")
        return _OO_DOC_CACHE
    detail = ""
    try:
        data = _oo_content_bytes_fs(file_id, "docx")
        if data:
            _OO_DOC_CACHE = (data, "fs (content.docx via docker exec)")
            return _OO_DOC_CACHE
        detail = "content.docx not found in data dir"
    except Exception as e:
        detail = f"fs read failed: {e}"
    try:
        data = _oo_content_bytes_api(file_id)
        if data:
            _OO_DOC_CACHE = (data, "api download (fallback)")
            return _OO_DOC_CACHE
    except Exception as e:
        _OO_DOC_CACHE = (None, f"{detail}; api download failed: {e}")
        return _OO_DOC_CACHE
    _OO_DOC_CACHE = (None, f"{detail}; api download failed")
    return _OO_DOC_CACHE


def _docx_parts(data: bytes) -> tuple[str, str]:
    zf = zipfile.ZipFile(io.BytesIO(data))
    doc_xml = zf.read("word/document.xml").decode("utf-8", "replace")
    try:
        settings_xml = zf.read("word/settings.xml").decode("utf-8", "replace")
    except KeyError:
        settings_xml = ""
    return doc_xml, settings_xml


def _docx_norm_text(doc_xml: str) -> str:
    """Concatenate all <w:t> runs per paragraph, then normalize whitespace."""
    paras = []
    for chunk in doc_xml.split("</w:p>"):
        t = "".join(_xml_texts(chunk, "w:t"))
        if t:
            paras.append(t)
    return " ".join(" ".join(paras).split())


def _docx_rows(doc_xml: str) -> list[str]:
    """Concatenated <w:t> text of every table row (<w:tr>)."""
    return [
        "".join(_xml_texts(r, "w:t"))
        for r in re.findall(r"<w:tr[\s>].*?</w:tr>", doc_xml, re.DOTALL)
    ]


# ── ownCloud checks ──────────────────────────────────────────────────────────

def check_1_archived_file() -> None:
    """Rename proven both ways: archived path exists AND original path absent."""
    try:
        new_count = _oc_path_count(ARCHIVED_PATH)
        old_count = _oc_path_count(ORIGINAL_PATH)
        passed = new_count >= 1 and old_count == 0
        check("1. Archived file renamed (new exists, original gone)", 1, passed,
              f"archived_count={new_count}, original_count={old_count}")
    except Exception as e:
        check("1. Archived file renamed (new exists, original gone)", 1, False,
              f"exception: {e}")


def check_2_archived_tagged() -> None:
    """Archived file has system tag 'archived'."""
    try:
        escaped = ARCHIVED_PATH.replace("'", "''")
        fileid = oc_sql(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            f"WHERE s.id = 'home::admin' AND fc.path = '{escaped}' LIMIT 1;"
        )
        if not fileid:
            check("2. Archived file tagged", 1, False, "archived file not found")
            return
        out = oc_sql(
            "SELECT t.name FROM oc_systemtag t "
            "JOIN oc_systemtag_object_mapping m ON t.id = m.systemtagid "
            f"WHERE m.objectid = '{fileid}' AND m.objecttype = 'files' "
            "AND t.name = 'archived';"
        )
        check("2. Archived file tagged", 1, "archived" in out, f"tags={out!r}")
    except Exception as e:
        check("2. Archived file tagged", 1, False, f"exception: {e}")


def check_3_new_policy_file() -> None:
    """New policy file exists at the exact path under home::admin."""
    try:
        count = _oc_path_count(POLICY_TXT_PATH)
        check("3. New policy file exists", 1, count >= 1,
              f"path={POLICY_TXT_PATH!r}, count={count}")
    except Exception as e:
        check("3. New policy file exists", 1, False, f"exception: {e}")


def check_4_new_policy_content() -> None:
    """New policy file content is correct (via WebDAV)."""
    try:
        url = (f"http://{HOST}:{OC_PORT}/remote.php/dav/files/admin/"
               "doc/healthcare/Telehealth_Fee_Alignment_Policy_v2.txt")
        r = requests.get(url, auth=("admin", "admin"), timeout=15)
        if r.status_code != 200:
            check("4. New policy content", 1, False, f"WebDAV HTTP {r.status_code}")
            return
        c = " ".join(r.text.split())
        probes = [
            "Tier A ($25)",
            "Tier B ($45)",
            "Tier C ($75)",
            "Effective September 1, 2026",
            "15% premium over equivalent phone tiers",
            "patient consent and clinical justification",
            "within 30 days of service",
            "claim denial",
            "per Schedule B",
        ]
        missing = [p for p in probes if p not in c]
        check("4. New policy content", 1, not missing,
              "all probes found" if not missing else f"missing: {missing}")
    except Exception as e:
        check("4. New policy content", 1, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

def check_7_oo_doc_exists() -> None:
    """Change summary document exists in OnlyOffice Common Documents (exact title)."""
    try:
        titles = {OO_DOC_TITLE, OO_DOC_TITLE + ".docx"}
        s = _oo_session()
        r = s.get(f"http://{HOST}:{OO_PORT}/api/2.0/files/@common", timeout=15)
        data = r.json().get("response", {})
        files = data.get("files", [])
        folders = data.get("folders", [])

        found = any(f.get("title", "") in titles for f in files)
        if not found:
            for folder in folders:
                fid = folder.get("id")
                r2 = s.get(f"http://{HOST}:{OO_PORT}/api/2.0/files/{fid}", timeout=15)
                sub = r2.json().get("response", {}).get("files", [])
                if any(f.get("title", "") in titles for f in sub):
                    found = True
                    break

        check("7. OnlyOffice doc exists", 1, found,
              f"files_in_common={len(files)}")
    except Exception as e:
        check("7. OnlyOffice doc exists", 1, False, f"exception: {e}")


def check_7b_oo_doc_content() -> None:
    """Change summary docx content: heading, sections, 5-row comparison table."""
    label = "7b. OnlyOffice change summary content"
    try:
        data, source = _oo_get_summary_docx()
        if not data:
            check(label, 2, False, f"could not read document: {source}")
            return
        doc_xml, _ = _docx_parts(data)
        text = _docx_norm_text(doc_xml)

        text_probes = [
            "Telehealth Fee Alignment Policy v2 — Summary of Changes",
            "supersedes the 2021 determination",
            "acknowledge this policy in the HR portal by August 20, 2026",
        ]
        missing_text = [p for p in text_probes if p not in text]

        tbl_count = len(re.findall(r"<w:tbl[\s>]", doc_xml))
        rows = _docx_rows(doc_xml)
        rows_ok = 5 <= len(rows) <= 7

        row_probes = [
            "Flat rate of $35 regardless of duration",
            "15% premium over equivalent phone tier",
            "Patient consent AND clinical justification required for every encounter",
            "Standardized exemptions per Schedule B",
        ]
        missing_rows = [p for p in row_probes if not any(p in r for r in rows)]
        same_row_ok = any(
            "60 days from service date" in r and "30 days from service date" in r
            for r in rows
        )

        passed = (not missing_text and tbl_count >= 1 and rows_ok
                  and not missing_rows and same_row_ok)
        details = []
        if missing_text:
            details.append(f"missing text: {missing_text}")
        if tbl_count < 1:
            details.append("no <w:tbl> found")
        if not rows_ok:
            details.append(f"row count {len(rows)} not in 5..7")
        if missing_rows:
            details.append(f"missing row anchors: {missing_rows}")
        if not same_row_ok:
            details.append("60->30 days pair not in one row")
        check(label, 2, passed,
              "; ".join(details) if details else f"all probes found, source: {source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7c_oo_track_changes() -> None:
    """Track changes (trackRevisions) enabled in the change summary document."""
    label = "7c. OnlyOffice track changes enabled"
    try:
        data, source = _oo_get_summary_docx()
        if not data:
            check(label, 1, False, f"could not read document: {source}")
            return
        _, settings_xml = _docx_parts(data)
        on = False
        m = re.search(r"<w:trackRevisions\b[^>]*>", settings_xml)
        if m and "false" not in m.group(0):
            on = True
        check(label, 1, on,
              f"trackRevisions element: {m.group(0) if m else 'absent'}, source: {source}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_8_oo_shared_laura() -> None:
    """Document shared with laura.brown for editing (userName exact, access==1)."""
    label = "8. OnlyOffice shared with laura.brown (edit)"
    try:
        file_id = _oo_file_id([OO_DOC_TITLE, OO_DOC_TITLE + ".docx"])
        if not file_id:
            check(label, 1, False, "doc not found")
            return
        s = _oo_session()
        r = s.get(f"http://{HOST}:{OO_PORT}/api/2.0/files/file/{file_id}/share",
                  timeout=15)
        shares = r.json().get("response", [])
        laura_edit = False
        for sh in shares:
            shared_to = sh.get("sharedTo", {}) or {}
            uname = str(shared_to.get("userName", ""))
            try:
                access = int(sh.get("access", -1))
            except (TypeError, ValueError):
                access = -1
            if uname == "laura.brown" and access == 1:
                laura_edit = True
                break
        check(label, 1, laura_edit, f"shares_count={len(shares)}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Mattermost checks ────────────────────────────────────────────────────────

def check_9_mm_announcement() -> None:
    """Full-text policy announcement posted in Analytics (Marketing & Growth)."""
    try:
        out = mm_sql(
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Marketing & Growth' "
            "AND c.displayname = 'Analytics' "
            "AND p.deleteat = 0 "
            f"AND p.message LIKE '%{MM_ANALYTICS_FRAGMENT}%';"
        )
        found = any(MM_ANALYTICS_ANCHOR in line for line in out.splitlines())
        check("9. MM announcement in Analytics", 1, found,
              "full-text anchor matched" if found else "full-text anchor not found")
    except Exception as e:
        check("9. MM announcement in Analytics", 1, False, f"exception: {e}")


def check_10_mm_pinned() -> None:
    """The full-text announcement is pinned in Analytics."""
    try:
        out = mm_sql(
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Marketing & Growth' "
            "AND c.displayname = 'Analytics' "
            "AND p.deleteat = 0 AND p.ispinned = true "
            f"AND p.message LIKE '%{MM_ANALYTICS_FRAGMENT}%';"
        )
        pinned = any(MM_ANALYTICS_ANCHOR in line for line in out.splitlines())
        check("10. MM announcement pinned", 1, pinned,
              "pinned announcement matched" if pinned else "no pinned full-text match")
    except Exception as e:
        check("10. MM announcement pinned", 1, False, f"exception: {e}")


def check_11_mm_hr_note() -> None:
    """Full-text HR internal note posted in Brand Design (root post, not reply)."""
    try:
        out = mm_sql(
            "SELECT p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Marketing & Growth' "
            "AND c.displayname = 'Brand Design' "
            "AND p.deleteat = 0 "
            "AND (p.rootid = '' OR p.rootid IS NULL) "
            f"AND p.message LIKE '%{MM_HR_NOTE_FRAGMENT}%';"
        )
        found = any(MM_HR_NOTE_ANCHOR in line for line in out.splitlines())
        check("11. MM HR note in Brand Design", 1, found,
              "full-text anchor matched" if found else "full-text anchor not found")
    except Exception as e:
        check("11. MM HR note in Brand Design", 1, False, f"exception: {e}")


def check_12_mm_thread_reply() -> None:
    """One thread reply under the HR note contains BOTH required phrases."""
    label = "12. MM thread reply in Brand Design"
    try:
        rows = mm_sql(
            "SELECT p.id || '|' || p.message FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE t.displayname = 'Marketing & Growth' "
            "AND c.displayname = 'Brand Design' "
            "AND p.deleteat = 0 "
            "AND (p.rootid = '' OR p.rootid IS NULL) "
            f"AND p.message LIKE '%{MM_HR_NOTE_FRAGMENT}%';"
        )
        root_id = ""
        first_id = ""
        for line in rows.splitlines():
            if "|" not in line:
                continue
            pid, msg = line.split("|", 1)
            if not first_id:
                first_id = pid.strip()
            if MM_HR_NOTE_ANCHOR in msg:
                root_id = pid.strip()
                break
        root_id = root_id or first_id
        if not root_id:
            check(label, 1, False, "root post not found")
            return
        out = mm_sql(
            f"SELECT p.message FROM posts p "
            f"WHERE p.rootid = '{root_id}' AND p.deleteat = 0;"
        )
        p1 = "all-staff email with read-receipt is scheduled today"
        p2 = "we need 100% by August 20"
        found = any(p1 in line and p2 in line for line in out.splitlines())
        check(label, 1, found,
              "single reply contains both phrases" if found
              else "no single reply contains both required phrases")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Roundcube checks ─────────────────────────────────────────────────────────

def check_13_rc_identity_org() -> None:
    """Default identity organization equals the full expected string (em dash)."""
    try:
        out = rc_sql(
            "SELECT organization FROM identities "
            "WHERE email = 'james.whitfield@mail.local' AND standard = 1;"
        )
        found = out.strip() == RC_IDENTITY_ORG
        check("13. RC identity organization", 1, found, f"org={out.strip()!r}")
    except Exception as e:
        check("13. RC identity organization", 1, False, f"exception: {e}")


def check_14_rc_email_sent() -> None:
    """Sent email: exact subject; To/Cc/Bcc set equality; body probes."""
    label = "14. RC email sent (To/Cc/Bcc + body)"
    try:
        _, msg = _sent_policy_msg()
        if msg is None:
            check(label, 1, False, "no Sent message with the exact subject found")
            return
        to_ok = _addr_set(msg, "To") == {"all-staff@acmehealth.local"}
        cc_ok = _addr_set(msg, "Cc") == {"hr.director@acmehealth.local"}
        bcc_ok = _addr_set(msg, "Bcc") == {"compliance.officer@acmehealth.local"}
        body = _body_text(msg)
        probes = [
            "Tier A $25, Tier B $45, Tier C $75",
            "files/doc/healthcare (Telehealth_Fee_Alignment_Policy_v2.txt)",
            "no later than August 20, 2026",
        ]
        missing = [p for p in probes if p not in body]
        passed = to_ok and cc_ok and bcc_ok and not missing
        check(label, 1, passed,
              f"to={to_ok}, cc={cc_ok}, bcc={bcc_ok}, body_missing={missing}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_14b_rc_mdn_requested() -> None:
    """Read receipt requested: Disposition-Notification-To == sender identity address."""
    label = "14b. RC read receipt (MDN) requested"
    try:
        _, msg = _sent_policy_msg()
        if msg is None:
            check(label, 1, False, "no Sent message with the exact subject found")
            return
        from_set = _addr_set(msg, "From")
        mdn_set = _addr_set(msg, "Disposition-Notification-To")
        passed = bool(mdn_set) and mdn_set == from_set
        check(label, 1, passed,
              f"mdn={sorted(mdn_set)}, from={sorted(from_set)}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_15_rc_sent_flagged() -> None:
    """Sent email is flagged (Maildir F flag; doveadm fallback)."""
    label = "15. RC sent email flagged"
    try:
        path, msg = _sent_policy_msg()
        if path and ":2," in path:
            flags = path.rsplit(":2,", 1)[-1]
            check(label, 1, "F" in flags, f"maildir flags={flags!r}")
            return
        # Fallback: doveadm (locating only; subject fragment is plain ASCII)
        rc, out, _ = docker_exec(
            RC_CONTAINER, "doveadm", "fetch", "-u", "james.whitfield@mail.local",
            "flags", "mailbox", "Sent", "subject", "Action Required",
            timeout=20,
        )
        if rc == 0 and out.strip():
            flagged = "\\Flagged" in out or "\\flagged" in out
            check(label, 1, flagged, f"doveadm flags={out.strip()!r}")
        else:
            check(label, 1, False, "sent email not found")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_archived_file()
    check_2_archived_tagged()
    check_3_new_policy_file()
    check_4_new_policy_content()
    check_7_oo_doc_exists()
    check_7b_oo_doc_content()
    check_7c_oo_track_changes()
    check_8_oo_shared_laura()
    check_9_mm_announcement()
    check_10_mm_pinned()
    check_11_mm_hr_note()
    check_12_mm_thread_reply()
    check_13_rc_identity_org()
    check_14_rc_email_sent()
    check_14b_rc_mdn_requested()
    check_15_rc_sent_flagged()

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
