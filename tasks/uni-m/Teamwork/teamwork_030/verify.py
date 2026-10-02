"""
Verifier for Teamwork-030-I2: Cross-Team Status Report across Mattermost, OnlyOffice, ownCloud, Roundcube

Checks: 12 weighted checks across 4 sites (total weight 15; one 0-weight informational check).
Strategy: DB (Mattermost, Roundcube prefs, OnlyOffice MySQL), API (OnlyOffice, ownCloud),
          docx content probes (OnlyOffice data dir via docker exec + API download fallback),
          Sent-maildir header/body parsing (Roundcube, python email stdlib).

Required env vars:
  SERVER_HOSTNAME,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER
"""

import email
import email.utils
import io
import json
import os
import re
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
import zipfile
from email.header import decode_header, make_header

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

REQUIRED_VARS = [
    "MATTERMOST_PORT", "MATTERMOST_CONTAINER", "MATTERMOST_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
    "OWNCLOUD_PORT", "OWNCLOUD_CONTAINER", "OWNCLOUD_DB_CONTAINER",
    "ROUNDCUBEMAIL_PORT", "ROUNDCUBEMAIL_CONTAINER", "ROUNDCUBEMAIL_DB_CONTAINER",
]

_env = {}
for var in REQUIRED_VARS:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    _env[var] = val

MM_DB = _env["MATTERMOST_DB_CONTAINER"]
MM_PORT = _env["MATTERMOST_PORT"]
MM_CONTAINER = _env["MATTERMOST_CONTAINER"]
OO_PORT = _env["ONLYOFFICE_PORT"]
OO_CONTAINER = _env["ONLYOFFICE_CONTAINER"]
OO_DB = _env["ONLYOFFICE_DB_CONTAINER"]
OC_PORT = _env["OWNCLOUD_PORT"]
OC_CONTAINER = _env["OWNCLOUD_CONTAINER"]
OC_DB = _env["OWNCLOUD_DB_CONTAINER"]
RC_PORT = _env["ROUNDCUBEMAIL_PORT"]
RC_CONTAINER = _env["ROUNDCUBEMAIL_CONTAINER"]
RC_DB = _env["ROUNDCUBEMAIL_DB_CONTAINER"]

REPORT_TITLE = "Cross-Team Bi-Weekly Status Report - W26-W27 2026"
REPORT_SUBJECT = "Bi-Weekly Cross-Team Status Report - June 22 to July 3, 2026"
SENDER_ADDR = "james.whitfield@mail.local"
SENDER_LOCALPART = "james.whitfield"

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


def docker_exec_bytes(container: str, *args: str, timeout: int = 30) -> bytes:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, timeout=timeout,
    )
    return r.stdout if r.returncode == 0 else b""


def mm_db_query(sql: str) -> str:
    """Query Mattermost Postgres DB."""
    rc, out, err = docker_exec(
        MM_DB, "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oc_db_query(sql: str) -> str:
    """Query ownCloud MariaDB."""
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OC_DB, "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud", "owncloud",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_db_query(sql: str) -> str:
    """Query OnlyOffice MySQL DB."""
    rc, out, err = docker_exec(
        OO_DB, "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql, "onlyoffice"
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def rc_db_query(sql: str) -> str:
    """Query Roundcube MariaDB."""
    rc, out, err = docker_exec(
        RC_DB, "mysql", "-u", "roundcube", "-proundcube123", "roundcubemail",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def http_request(url: str, method: str = "GET", data: bytes | None = None,
                 headers: dict | None = None, timeout: int = 15) -> tuple[int, str, dict]:
    """Make an HTTP request. Returns (status_code, body, response_headers)."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        return e.code, body, dict(e.headers) if e.headers else {}
    except Exception as e:
        return 0, str(e), {}


def http_get_bytes(url: str, headers: dict | None = None, timeout: int = 30) -> tuple[int, bytes]:
    """Binary GET (for docx downloads)."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if e.fp else b""
    except Exception:
        return 0, b""


def oo_api_get_token() -> str | None:
    """Authenticate to OnlyOffice and return auth token."""
    url = f"http://{HOST}:{OO_PORT}/api/2.0/authentication"
    payload = json.dumps({"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"}).encode()
    status, body, _ = http_request(url, "POST", payload, {"Content-Type": "application/json"})
    if status == 200 or status == 201:
        resp = json.loads(body)
        return resp.get("response", {}).get("token")
    return None


def oo_api(endpoint: str, token: str) -> tuple[int, dict]:
    """Make an authenticated OnlyOffice API call."""
    url = f"http://{HOST}:{OO_PORT}/api/2.0/{endpoint}"
    status, body, _ = http_request(url, "GET", headers={"Authorization": token})
    try:
        return status, json.loads(body)
    except Exception:
        return status, {}


def _norm(s: str) -> str:
    """Normalize whitespace for text comparisons."""
    return re.sub(r"\s+", " ", s).strip()


# ── OnlyOffice docx probe helpers (approach B) ────────────────────────────────
def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        s = s.replace(ent, ch)
    return s


def _oo_docx_fetch(title: str) -> tuple[bytes | None, str]:
    """Fetch the docx bytes for the given exact title (with/without .docx).

    Chain: files_file latest id -> data-dir content.docx (highest version dir)
    via docker exec -> API download fallback."""
    notes: list[str] = []
    fid = None
    try:
        out = oo_db_query(
            f"SELECT id FROM files_file WHERE title IN ('{title}', '{title}.docx') "
            "ORDER BY id DESC LIMIT 1;"
        )
        if out.strip():
            fid = out.strip().splitlines()[0].strip()
    except Exception as e:
        notes.append(f"db id lookup failed: {e}")

    if fid:
        try:
            rc, out, _ = docker_exec(
                OO_CONTAINER, "bash", "-c",
                f"find /var/www/onlyoffice/Data -type f -path '*file_{fid}/*content.docx' "
                "2>/dev/null || true",
                timeout=20,
            )
            paths = [p.strip() for p in out.splitlines() if p.strip()]
            if paths:
                def _vkey(p: str) -> list[int]:
                    tail = p.split(f"file_{fid}/", 1)[-1]
                    nums = re.findall(r"\d+", tail)
                    return [int(n) for n in nums] if nums else [0]

                path = max(paths, key=_vkey)
                data = docker_exec_bytes(OO_CONTAINER, "cat", path, timeout=30)
                if data[:2] == b"PK":
                    return data, f"fs {path}"
                notes.append("fs cat failed or not a zip")
            else:
                notes.append("fs content.docx not found")
        except Exception as e:
            notes.append(f"fs read failed: {e}")

    # API download fallback
    try:
        token = oo_api_get_token()
        if not token:
            notes.append("api auth failed")
            return None, "; ".join(notes)
        if not fid:
            did = _oo_get_doc_id(token)
            fid = str(did) if did else None
        if not fid:
            notes.append("file id not found via api")
            return None, "; ".join(notes)
        status, data = http_get_bytes(
            f"http://{HOST}:{OO_PORT}/api/2.0/files/file/{fid}/download",
            {"Authorization": token},
        )
        if status == 200 and data[:2] == b"PK":
            return data, "api download"
        status, data = http_get_bytes(
            f"http://{HOST}:{OO_PORT}/products/files/httphandlers/filehandler.ashx"
            f"?action=download&fileid={fid}",
            {"Cookie": f"asc_auth_key={token}"},
        )
        if status == 200 and data[:2] == b"PK":
            return data, "api filehandler download"
        notes.append(f"api download failed (HTTP {status})")
    except Exception as e:
        notes.append(f"api download failed: {e}")
    return None, "; ".join(notes)


def _docx_document_xml(data: bytes) -> str | None:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        return zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception:
        return None


def _docx_plain_text(xml: str) -> str:
    """Concatenate all <w:t> runs (per paragraph), strip tags, normalize whitespace.
    OnlyOffice splits sentences across runs -- never grep the raw XML for sentences."""
    chunks: list[str] = []
    for para in xml.split("</w:p>"):
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", para, flags=re.S)
        if runs:
            chunks.append("".join(runs))
    return _norm(_xml_unescape(" ".join(chunks)))


# ── Roundcube Sent-maildir helpers (approach C) ───────────────────────────────
def _sent_candidate_paths() -> list[str]:
    """Candidate mail files: ONLY the sender's Sent Maildir (cur + new)."""
    cmd = (
        f"find /var/mail/mail.local/{SENDER_LOCALPART}/.Sent/cur "
        f"/var/mail/mail.local/{SENDER_LOCALPART}/.Sent/new "
        "-type f 2>/dev/null || true"
    )
    rc, out, _ = docker_exec(RC_CONTAINER, "bash", "-c", cmd, timeout=20)
    return [p.strip() for p in out.splitlines() if p.strip()]


def _hdr_decoded(msg, name: str) -> str:
    raw = msg.get(name)
    if raw is None:
        return ""
    try:
        return _norm(str(make_header(decode_header(str(raw)))))
    except Exception:
        parts = []
        for part, enc in decode_header(str(raw)):
            parts.append(part.decode(enc or "utf-8", "replace")
                         if isinstance(part, bytes) else part)
        return _norm("".join(parts))


def _addr_set(msg, name: str) -> set[str]:
    vals = [str(v) for v in (msg.get_all(name) or [])]
    return {addr.lower() for _, addr in email.utils.getaddresses(vals) if addr}


_sent_msg = None
_sent_path = ""
_sent_searched = False


def _get_report_email():
    """Locate + parse the Sent copy with the exact report subject (cached)."""
    global _sent_msg, _sent_path, _sent_searched
    if _sent_searched:
        return _sent_msg, _sent_path
    _sent_searched = True
    want = _norm(REPORT_SUBJECT)
    for path in _sent_candidate_paths():
        raw = docker_exec_bytes(RC_CONTAINER, "cat", path, timeout=15)
        if not raw:
            continue
        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            continue
        if _hdr_decoded(msg, "Subject") == want:
            _sent_msg, _sent_path = msg, path
            break
    return _sent_msg, _sent_path


# ── Individual checks ─────────────────────────────────────────────────────────

def check_3_oo_document_exists() -> None:
    """Verify OnlyOffice document 'Cross-Team Bi-Weekly Status Report - W26-W27 2026' exists in Common Documents."""
    try:
        token = oo_api_get_token()
        if not token:
            check("3. OO document exists", 1, False, "auth failed")
            return
        # List common documents
        status, data = oo_api("files/@common", token)
        files = data.get("response", {}).get("files", [])
        # OnlyOffice reports titles WITH the file extension (e.g. '<title>.docx')
        found = any(
            f.get("title", "") == REPORT_TITLE or f.get("title", "").startswith(REPORT_TITLE + ".")
            for f in files
        )
        check("3. OO document exists", 1, found,
              f"not found among {len(files)} files" if not found else "")
    except Exception as e:
        check("3. OO document exists", 1, False, f"exception: {e}")


def check_3b_oo_document_content() -> None:
    """Docx content probes for the status report (approach B): headings, 3 tables, risks section."""
    label = "3b. OO document content (docx probes)"
    try:
        data, source = _oo_docx_fetch(REPORT_TITLE)
        if not data:
            check(label, 2, False, f"could not fetch docx: {source}")
            return
        xml = _docx_document_xml(data)
        if xml is None:
            check(label, 2, False, f"word/document.xml unreadable (source: {source})")
            return
        text = _docx_plain_text(xml)
        probes = [
            "Cross-Team Bi-Weekly Status Report",
            "Bi-Weekly Period: June 22 – July 3, 2026",  # en dash, exact per task text
            "Engineering Updates",
            "Marketing Updates",
            "Product Updates",
            "Author",
            "Update",
            "Date",
            "Cross-Team Risks",
            "recurring incident postmortem findings not being actioned within SLA",
            "delayed brand asset approvals impacting campaign timelines",
            "too late in the sprint cycle to influence design decisions",
        ]
        missing = [p for p in probes if _norm(p) not in text]
        tbl_count = len(re.findall(r"<w:tbl[ >]", xml))
        tables_ok = tbl_count >= 3
        passed = (not missing) and tables_ok
        check(label, 2, passed,
              f"missing_probes={missing[:4]}, table_count={tbl_count} want >=3, source={source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def _oo_get_doc_id(token: str) -> int | None:
    """Find the document ID for the status report."""
    status, data = oo_api("files/@common", token)
    files = data.get("response", {}).get("files", [])
    # OnlyOffice reports titles WITH the file extension (e.g. '<title>.docx')
    for f in files:
        t = f.get("title", "")
        if t == REPORT_TITLE or t.startswith(REPORT_TITLE + "."):
            return f.get("id")
    return None


def _oo_shares_via_api() -> list | None:
    """Return the share list for the report document via API, or None if unavailable."""
    token = oo_api_get_token()
    if not token:
        return None
    doc_id = _oo_get_doc_id(token)
    if not doc_id:
        return None
    status, data = oo_api(f"files/file/{doc_id}/share", token)
    resp = data.get("response")
    if status == 200 and isinstance(resp, list):
        return resp
    return None


def _oo_share_db_fallback(username: str, security: int) -> bool:
    """Strict approach A DB check: subject GUID joined to core_user, entry_id bound
    to the target file, exact username and exact security level."""
    sql = (
        "SELECT 1 FROM files_security fs "
        "JOIN core_user cu ON fs.subject = cu.id "
        "WHERE fs.entry_type = 2 "
        "AND fs.entry_id = (SELECT CAST(id AS CHAR) FROM files_file "
        f"WHERE title IN ('{REPORT_TITLE}', '{REPORT_TITLE}.docx') "
        "ORDER BY id DESC LIMIT 1) "
        f"AND cu.username = '{username}' AND fs.security = {security};"
    )
    return bool(oo_db_query(sql).strip())


def _judge_share(shares: list, username: str, access_want: int) -> bool:
    """Exact userName equality + exact access level (1=edit, 2=view only)."""
    for s in shares:
        user = s.get("sharedTo", {}) or {}
        uname = user.get("userName", "")
        try:
            access = int(s.get("access", -1))
        except (TypeError, ValueError):
            access = -1
        if uname == username and access == access_want:
            return True
    return False


def check_4_oo_shared_junchen() -> None:
    """Verify document shared with jun.chen for viewing (access == 2 ONLY)."""
    label = "4. OO shared with jun.chen (view, access==2)"
    try:
        shares = _oo_shares_via_api()
        if shares is not None:
            found = _judge_share(shares, "jun.chen", 2)
            check(label, 2, found,
                  f"api shares={len(shares)}; require userName=='jun.chen' AND access==2 view only")
            return
        # DB fallback (strict approach A)
        found = _oo_share_db_fallback("jun.chen", 2)
        check(label, 2, found,
              "db fallback: files_security join core_user, entry_id bound, security==2")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5_oo_shared_amitsingh() -> None:
    """Verify document shared with amit.singh for editing (access == 1, exact username)."""
    label = "5. OO shared with amit.singh (edit, access==1)"
    try:
        shares = _oo_shares_via_api()
        if shares is not None:
            found = _judge_share(shares, "amit.singh", 1)
            check(label, 2, found,
                  f"api shares={len(shares)}; require userName=='amit.singh' AND access==1 edit")
            return
        # DB fallback (strict approach A)
        found = _oo_share_db_fallback("amit.singh", 1)
        check(label, 2, found,
              "db fallback: files_security join core_user, entry_id bound, security==1")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_oc_folder_structure() -> None:
    """Verify ownCloud folder structure: Leadership_BiWeekly_Reports/2026-P13-P14 exists."""
    try:
        url = f"http://{HOST}:{OC_PORT}/remote.php/dav/files/admin/Leadership_BiWeekly_Reports/2026-P13-P14/"
        import base64
        auth = base64.b64encode(b"admin:admin").decode()
        status, body, _ = http_request(url, "PROPFIND", headers={
            "Authorization": f"Basic {auth}",
            "Depth": "0",
        })
        passed = status in (200, 207)
        check("6. OC folder structure exists", 1, passed,
              f"HTTP {status}" if not passed else "")
    except Exception as e:
        check("6. OC folder structure exists", 1, False, f"exception: {e}")


def check_7_oc_exec_summary_content() -> None:
    """Verify exec_summary.txt exists with the department findings (probed, not prefix-only)."""
    try:
        url = f"http://{HOST}:{OC_PORT}/remote.php/dav/files/admin/Leadership_BiWeekly_Reports/2026-P13-P14/exec_summary.txt"
        import base64
        auth = base64.b64encode(b"admin:admin").decode()
        status, body, _ = http_request(url, "GET", headers={
            "Authorization": f"Basic {auth}",
        })
        if status != 200:
            check("7. OC exec_summary.txt content", 2, False, f"HTTP {status}")
            return
        probes = [
            "Executive Summary - Bi-Weekly Period June 22",
            "4 P1 incidents",
            "refreshed logo system",
            "3 rounds of user interviews",
            "onboarding redesign",
            "brand asset handoff cadence",
        ]
        body_norm = _norm(body)
        missing = [p for p in probes if _norm(p) not in body_norm]
        check("7. OC exec_summary.txt content", 2, not missing,
              f"missing_probes={missing}" if missing else "all probes present")
    except Exception as e:
        check("7. OC exec_summary.txt content", 2, False, f"exception: {e}")


def check_10_oc_tag() -> None:
    """Verify tag 'biweekly-leadership' applied to admin's Leadership_BiWeekly_Reports folder."""
    try:
        # home::admin storage join + exact path guard against same-named objects
        sql = (
            "SELECT t.name FROM oc_systemtag t "
            "JOIN oc_systemtag_object_mapping m ON t.id = m.systemtagid "
            "AND m.objecttype = 'files' "
            "JOIN oc_filecache f ON m.objectid = f.fileid "
            "JOIN oc_storages s ON f.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND f.path = 'files/Leadership_BiWeekly_Reports' "
            "AND t.name = 'biweekly-leadership'"
        )
        out = oc_db_query(sql)
        passed = "biweekly-leadership" in out
        check("10. OC tag biweekly-leadership applied", 1, passed,
              f"got: {out!r}" if not passed else "")
    except Exception as e:
        check("10. OC tag biweekly-leadership applied", 1, False, f"exception: {e}")


def check_11_rc_email_subject_in_sent() -> None:
    """Verify email with the exact subject exists in james.whitfield's Sent Maildir."""
    try:
        msg, path = _get_report_email()
        passed = msg is not None
        check("11. RC email with correct subject in sent", 1, passed,
              f"found {path}" if passed
              else "no mail with exact subject in james.whitfield/.Sent Maildir")
    except Exception as e:
        check("11. RC email with correct subject in sent", 1, False, f"exception: {e}")


def check_12_rc_email_recipients() -> None:
    """Verify To recipient set equality (parsed headers, not raw grep)."""
    try:
        expected_to = {
            "jun.chen@onlyoffice.local",
            "amit.singh@onlyoffice.local",
            "laura.brown@onlyoffice.local",
        }
        msg, _ = _get_report_email()
        if msg is None:
            check("12. RC email To recipients exact set", 1, False, "sent email not found")
            return
        to_set = _addr_set(msg, "To")
        passed = to_set == expected_to
        check("12. RC email To recipients exact set", 1, passed,
              f"got To={sorted(to_set)}")
    except Exception as e:
        check("12. RC email To recipients exact set", 1, False, f"exception: {e}")


def check_12b_rc_email_bcc() -> None:
    """Verify Bcc set == {records@onlyoffice.local} on the sent copy."""
    try:
        msg, _ = _get_report_email()
        if msg is None:
            check("12b. RC email Bcc records@onlyoffice.local", 1, False, "sent email not found")
            return
        bcc_set = _addr_set(msg, "Bcc")
        passed = bcc_set == {"records@onlyoffice.local"}
        check("12b. RC email Bcc records@onlyoffice.local", 1, passed,
              f"got Bcc={sorted(bcc_set)}")
    except Exception as e:
        check("12b. RC email Bcc records@onlyoffice.local", 1, False, f"exception: {e}")


def check_13_rc_mdn_requested() -> None:
    """Verify read receipt (MDN): Disposition-Notification-To parsed value == sender address."""
    try:
        msg, _ = _get_report_email()
        if msg is None:
            check("13. RC read receipt (MDN) requested", 1, False, "sent email not found")
            return
        mdn_set = _addr_set(msg, "Disposition-Notification-To")
        passed = mdn_set == {SENDER_ADDR}
        check("13. RC read receipt (MDN) requested", 1, passed,
              f"Disposition-Notification-To={sorted(mdn_set)} want [{SENDER_ADDR}]")
    except Exception as e:
        check("13. RC read receipt (MDN) requested", 1, False, f"exception: {e}")


def check_14_rc_draft_interval() -> None:
    """Auto-save draft interval '5 minutes': structurally undecidable, kept at weight 0."""
    label = "14. RC draft interval set to 5 min (not judged, weight 0)"
    detail_base = (
        "structurally undecidable: the image default draft_autosave is already 300s = 5 min "
        "and Roundcube drops prefs equal to the server default, so a correct explicit setting "
        "never persists to the DB while an absent pref is also the zero-action state"
    )
    try:
        out = rc_db_query(
            "SELECT preferences FROM users WHERE username = 'james.whitfield@mail.local'"
        )
        m = re.search(r'"draft_autosave";(?:i:(\d+)|s:\d+:"(\d+)")', out or "")
        val = (m.group(1) or m.group(2)) if m else None
        check(label, 0, True, f"{detail_base}; observed draft_autosave={val!r}")
    except Exception as e:
        check(label, 0, True, f"{detail_base}; prefs read failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_3_oo_document_exists()
    check_3b_oo_document_content()
    check_4_oo_shared_junchen()
    check_5_oo_shared_amitsingh()
    check_6_oc_folder_structure()
    check_7_oc_exec_summary_content()
    check_10_oc_tag()
    check_11_rc_email_subject_in_sent()
    check_12_rc_email_recipients()
    check_12b_rc_email_bcc()
    check_13_rc_mdn_requested()
    check_14_rc_draft_interval()

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
