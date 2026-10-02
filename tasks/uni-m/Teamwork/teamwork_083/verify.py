"""
Verifier for Teamwork-083-I1: Process Remote Work Policy Exception Request

Checks: weighted checks across roundcubemail, onlyoffice, owncloud, mattermost.
Strategy:
  - Roundcube: parse the Sent maildir copy with the stdlib email package
    (RFC2047 subjects, folded headers, decoded bodies) instead of raw grep.
  - OnlyOffice: exact-title files_file lookup; docx content probes on the
    concatenated <w:t> text (fs fetch via docker exec, API download fallback);
    sharing asserted per-user + per-level (API primary, strict SQL fallback).
  - ownCloud: WebDAV for content (normalized full-text comparison against the
    task text), DB join for the tag mapping to the exact target folder.
  - Mattermost: team-scoped channel joins, rootid-bound thread reply,
    channelmembers-scoped DM, guarded threadmemberships follow check.

Weights sum to exactly 20 (matches the original grader total).

Required env vars:
  SERVER_HOSTNAME, plus PORT/CONTAINER/DB_CONTAINER for each site.
"""

import email
import email.header
import email.utils
import html
import io
import os
import re
import subprocess
import sys
import zipfile

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    return val


ROUNDCUBEMAIL_PORT = _require("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = _require("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = _require("ROUNDCUBEMAIL_DB_CONTAINER")

ONLYOFFICE_PORT = _require("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = _require("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = _require("ONLYOFFICE_DB_CONTAINER")

OWNCLOUD_PORT = _require("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = _require("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = _require("OWNCLOUD_DB_CONTAINER")

MATTERMOST_PORT = _require("MATTERMOST_PORT")
MATTERMOST_CONTAINER = _require("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = _require("MATTERMOST_DB_CONTAINER")

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
    """docker exec returning raw stdout bytes (for binary payloads)."""
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"docker exec failed (rc={r.returncode}): "
            f"{r.stderr.decode(errors='replace')[-300:]}"
        )
    return r.stdout


def mm_db(sql: str) -> str:
    """Query Mattermost Postgres DB."""
    rc, out, err = docker_exec(
        MATTERMOST_DB_CONTAINER,
        "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_db(sql: str) -> str:
    """Query OnlyOffice MySQL DB."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "onlyoffice",
        "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oc_db(sql: str) -> str:
    """Query ownCloud MariaDB."""
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OWNCLOUD_DB_CONTAINER,
        "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud", "owncloud",
        "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def _norm_quotes(s: str) -> str:
    """Normalize smart quotes / dashes / nbsp to ASCII equivalents."""
    return (
        s.replace("‘", "'").replace("’", "'")
        .replace("“", '"').replace("”", '"')
        .replace("–", "-").replace("—", "-")
        .replace(" ", " ")
    )


def _norm_ws(s: str) -> str:
    """Collapse all whitespace runs to single spaces and strip."""
    return re.sub(r"\s+", " ", s).strip()


def _norm(s: str) -> str:
    return _norm_ws(_norm_quotes(s))


# ── Roundcube: Sent-copy parsing (approach C) ─────────────────────────────────
MAILDIR_BASE = "/var/mail/mail.local/james.whitfield"
EXPECTED_SUBJECT = "Formal Decision: Policy Exception Request - HR-POL-014 Remote Work"
EXPECTED_TO = {"rahul.johnson34@protonmail.com"}
EXPECTED_CC = {"amit.singh@onlyoffice.local", "laura.brown@onlyoffice.local"}

_sent_msg = None          # cached parsed email.message.Message
_sent_msg_path = ""       # cached path
_sent_msg_searched = False
_sent_msg_note = ""


def _decode_header_full(value: str) -> str:
    """Decode an RFC2047 header to a normalized unicode string."""
    out = []
    for data, charset in email.header.decode_header(value):
        if isinstance(data, bytes):
            out.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(data)
    return _norm("".join(out))


def _find_decision_email():
    """Locate the Sent copy of the formal decision email by exact decoded
    subject equality. Only the sender's .Sent/{cur,new} maildirs are searched
    (protonmail.com is an external domain: the Sent copy is the only evidence)."""
    global _sent_msg, _sent_msg_path, _sent_msg_searched, _sent_msg_note
    if _sent_msg_searched:
        return _sent_msg, _sent_msg_path
    _sent_msg_searched = True
    rc, out, _ = docker_exec(
        ROUNDCUBEMAIL_CONTAINER, "bash", "-c",
        f"find {MAILDIR_BASE}/.Sent/cur {MAILDIR_BASE}/.Sent/new -type f 2>/dev/null",
        timeout=20,
    )
    paths = [ln.strip() for ln in out.splitlines() if ln.strip()]
    scanned = 0
    for path in paths:
        try:
            raw = docker_exec_bytes(ROUNDCUBEMAIL_CONTAINER, "cat", path)
            msg = email.message_from_bytes(raw)
            scanned += 1
            subject = _decode_header_full(msg.get("Subject", ""))
            if subject == EXPECTED_SUBJECT:
                _sent_msg, _sent_msg_path = msg, path
                return _sent_msg, _sent_msg_path
        except Exception:
            continue
    _sent_msg_note = f"no Sent message with exact subject among {scanned} candidate(s)"
    return None, ""


def _msg_body_text(msg) -> str:
    """Decoded body text: prefer text/plain parts; fall back to de-tagged HTML."""
    plains, htmls = [], []
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html") or part.get_filename():
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        text = payload.decode(charset, errors="replace")
        (plains if ctype == "text/plain" else htmls).append(text)
    if plains:
        return "\n".join(plains)
    if htmls:
        h = "\n".join(htmls)
        h = re.sub(r"(?i)<br\s*/?>", "\n", h)
        h = re.sub(r"(?i)</p>", "\n", h)
        h = re.sub(r"(?i)</div>", "\n", h)
        h = re.sub(r"<[^>]+>", "", h)
        return html.unescape(h)
    return ""


def check_1_exceptions_folder():
    """IMAP folder 'Policy-Exceptions-2026' exists under INBOX for james.whitfield.

    Exact maildir path only (Maildir++ layout); also accepts the
    '.Policy-Exceptions-2026' namespace variant. No maildir-wide fuzzy find."""
    label = "1. Roundcube: IMAP folder Policy-Exceptions-2026 exists under INBOX"
    try:
        candidates = [
            f"{MAILDIR_BASE}/.INBOX.Policy-Exceptions-2026",
            f"{MAILDIR_BASE}/.Policy-Exceptions-2026",
        ]
        found = ""
        for d in candidates:
            rc, _, _ = docker_exec(ROUNDCUBEMAIL_CONTAINER, "test", "-d", d)
            if rc == 0:
                found = d
                break
        check(label, 1, bool(found),
              found if found else f"neither exact maildir path exists under {MAILDIR_BASE}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_4_formal_decision_email():
    """Formal decision email present in the Sent copy with the exact subject
    (full-string equality after RFC2047 decode + whitespace normalization)."""
    label = "4. Roundcube: Formal decision email in Sent (exact subject)"
    try:
        msg, path = _find_decision_email()
        check(label, 1, msg is not None,
              path if msg is not None else (_sent_msg_note or "Sent copy not found"))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5_recipients_and_priority():
    """To/Cc address sets exactly match the task (folded headers handled by the
    stdlib parser); message priority is Normal (no X-Priority header or '3')."""
    label = "5. Roundcube: To/Cc sets exact + Normal priority"
    try:
        msg, _ = _find_decision_email()
        if msg is None:
            check(label, 1, False, _sent_msg_note or "Sent copy not found")
            return
        tos = {addr.lower() for _, addr in
               email.utils.getaddresses(msg.get_all("To") or []) if addr}
        ccs = {addr.lower() for _, addr in
               email.utils.getaddresses(msg.get_all("Cc") or []) if addr}
        xprio = msg.get("X-Priority")
        prio_ok = xprio is None or xprio.strip().startswith("3")
        problems = []
        if tos != EXPECTED_TO:
            problems.append(f"To={sorted(tos)} != {sorted(EXPECTED_TO)}")
        if ccs != EXPECTED_CC:
            problems.append(f"Cc={sorted(ccs)} != {sorted(EXPECTED_CC)}")
        if not prio_ok:
            problems.append(f"X-Priority={xprio!r} (Normal requires absent or 3)")
        check(label, 1, not problems,
              "To/Cc exact; priority Normal" if not problems else "; ".join(problems))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5b_email_body():
    """Decoded body contains decision, the 5 condition lines (line-start
    anchors, quote-normalized), effective period, and appeal instructions."""
    label = "5b. Roundcube: Formal decision email body content"
    try:
        msg, _ = _find_decision_email()
        if msg is None:
            check(label, 2, False, _sent_msg_note or "Sent copy not found")
            return
        body = _norm_quotes(_msg_body_text(msg))
        body_flat = _norm_ws(body)
        lines = [_norm_ws(_norm_quotes(ln)) for ln in body.splitlines()]

        conditions = [
            "1. Exception duration limited to 6 months.",
            "2. Tax equalization agreement must be executed prior to relocation.",
            "3. Company system access restricted to VPN-only during the exception period.",
            "4. Quarterly compliance review with Legal and HR.",
            "5. No client-data handling permitted from the international location.",
        ]
        missing = []
        if "DECISION: Approved with Conditions" not in body_flat:
            missing.append("DECISION: Approved with Conditions")
        for cond in conditions:
            if not any(ln.startswith(cond) for ln in lines):
                missing.append(f"condition line '{cond[:40]}...'")
        if "EFFECTIVE PERIOD: 2026-05-01 to 2026-10-31." not in body_flat:
            missing.append("EFFECTIVE PERIOD line")
        if "APPEAL INSTRUCTIONS" not in body_flat:
            missing.append("APPEAL INSTRUCTIONS")
        if "within 10 business days" not in body_flat:
            missing.append("within 10 business days")
        if "respond within 15 business days" not in body_flat:
            missing.append("respond within 15 business days")
        check(label, 2, not missing,
              "all body anchors present" if not missing else f"missing: {'; '.join(missing)}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5c_dsn_note():
    """DSN request is not judged: Roundcube sends DSN as an SMTP NOTIFY
    parameter which leaves no trace in the Sent copy headers. 0 weight."""
    check("5c. Roundcube: DSN requested", 0, True,
          "not judged: DSN leaves no header trace in the Sent copy (SMTP NOTIFY param); 0 weight")


# ── OnlyOffice checks ────────────────────────────────────────────────────────
OO_DOC_TITLE = "Exception Review - Rahul Johnson - Remote Work - 2026"

_oo_doc_id_cache: str | None = None
_oo_docx_cache: tuple[bytes | None, str] | None = None


def _oo_doc_id() -> str:
    """Resolve the target document's files_file id by exact title
    (with/without .docx extension), latest id."""
    global _oo_doc_id_cache
    if _oo_doc_id_cache is not None:
        return _oo_doc_id_cache
    out = oo_db(
        "SELECT id FROM files_file "
        f"WHERE title IN ('{OO_DOC_TITLE}', '{OO_DOC_TITLE}.docx') "
        "ORDER BY id DESC LIMIT 1;"
    )
    _oo_doc_id_cache = out.splitlines()[0].strip() if out else ""
    return _oo_doc_id_cache


def _oo_auth_session():
    """Authenticate to OnlyOffice; returns a requests.Session or None."""
    try:
        import requests
    except Exception:
        return None
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    session = requests.Session()
    try:
        resp = session.post(
            f"{base_url}/api/2.0/authentication",
            json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            return None
        token = resp.json().get("response", {}).get("token", "")
        if not token:
            return None
        session.headers.update({"Authorization": f"Bearer {token}"})
        session.cookies.set("asc_auth_key", token)
        session.base_url = base_url  # type: ignore[attr-defined]
        return session
    except Exception:
        return None


def _oo_docx_bytes_from_fs(file_id: str) -> bytes | None:
    """Read content.docx for file_id from the portal data dir via docker exec.
    The middle '*' in the -path glob covers version subdirs (v1/v2/...);
    among multiple hits the highest version directory wins."""
    rc, out, _ = docker_exec(
        ONLYOFFICE_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.docx' "
        "2>/dev/null",
        timeout=30,
    )
    paths = [ln.strip() for ln in out.splitlines() if ln.strip()]
    if not paths:
        return None

    def vkey(p: str):
        m = re.search(r"/v(\d+)/", p)
        return (int(m.group(1)) if m else -1, p)

    path = max(paths, key=vkey)
    try:
        data = docker_exec_bytes(ONLYOFFICE_CONTAINER, "cat", path)
    except Exception:
        return None
    return data if data else None


def _oo_docx_bytes_from_api(file_id: str) -> bytes | None:
    """Fallback: download the document via the OnlyOffice HTTP API."""
    session = _oo_auth_session()
    if not session:
        return None
    base_url = session.base_url  # type: ignore[attr-defined]
    dl_resp = session.get(
        f"{base_url}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(file_id)},
        timeout=30, allow_redirects=True,
    )
    if not (dl_resp.status_code == 200 and len(dl_resp.content) > 100):
        dl_resp = session.get(
            f"{base_url}/api/2.0/files/file/{file_id}/download",
            timeout=30, allow_redirects=True,
        )
    if dl_resp.status_code != 200:
        return None
    return dl_resp.content


def _oo_get_docx() -> tuple[bytes | None, str]:
    """Get the exception review docx bytes: fs via docker exec first, API
    download as fallback. Returns (bytes|None, source detail). Cached."""
    global _oo_docx_cache
    if _oo_docx_cache is not None:
        return _oo_docx_cache
    file_id = _oo_doc_id()
    if not file_id:
        _oo_docx_cache = (None, "document not found in files_file (exact title)")
        return _oo_docx_cache
    detail = ""
    try:
        data = _oo_docx_bytes_from_fs(file_id)
        if data:
            _oo_docx_cache = (data, "fs (content.docx via docker exec)")
            return _oo_docx_cache
        detail = "content.docx not found in data dir"
    except Exception as e:
        detail = f"fs read failed: {e}"
    try:
        data = _oo_docx_bytes_from_api(file_id)
        if data:
            _oo_docx_cache = (data, "api download (fallback)")
            return _oo_docx_cache
        detail += "; api download failed"
    except Exception as e:
        detail += f"; api download failed: {e}"
    _oo_docx_cache = (None, detail)
    return _oo_docx_cache


def _docx_member_xml(data: bytes, member: str) -> str:
    """Extract a zip member's XML text ('' if absent / bad zip)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return ""
    for name in zf.namelist():
        low = name.lower()
        if low == member or low.endswith(member):
            return zf.read(name).decode("utf-8", errors="replace")
    return ""


def _wt_concat(xml_fragment: str) -> str:
    """Concatenate all <w:t> runs (per paragraph, joined with spaces across
    paragraphs), unescape entities, normalize quotes + whitespace.
    Never grep raw XML for sentences — runs split arbitrarily."""
    paras = re.split(r"</w:p>", xml_fragment)
    texts = []
    for p in paras:
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.DOTALL)
        if runs:
            texts.append(html.unescape("".join(runs)))
    return _norm(" ".join(texts))


def check_6_onlyoffice_document():
    """Document with the exact required title exists in OnlyOffice."""
    label = "6. OnlyOffice: Exception review document exists (exact title)"
    try:
        file_id = _oo_doc_id()
        check(label, 1, bool(file_id),
              f"files_file id={file_id}" if file_id
              else f"no files_file row titled '{OO_DOC_TITLE}[.docx]'")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_6b_document_content():
    """docx content probes on the concatenated <w:t> text: heading, request
    table (structure + row anchors), policy section refs, 4 risk bullets,
    precedent sentences, recommendation sentence."""
    label = "6b. OnlyOffice: Exception review document content"
    try:
        data, source = _oo_get_docx()
        if not data:
            check(label, 2, False, f"could not read document: {source}")
            return
        doc_xml = _docx_member_xml(data, "word/document.xml")
        if not doc_xml:
            check(label, 2, False, f"word/document.xml not found (source: {source})")
            return
        text = _wt_concat(doc_xml)
        missing = []

        if "Policy Exception Review: Remote Work Arrangement Request" not in text:
            missing.append("heading")

        # Request table: >=1 <w:tbl>; the matching table has <w:tr> = 6 +/- 1
        # (5 data rows + header) and contains all 5 value anchors.
        tables = re.findall(r"<w:tbl[ >].*?</w:tbl>", doc_xml, flags=re.DOTALL)
        row_anchors = [
            "Rahul Johnson",
            "Engineering",
            "2026-04-15",
            "HR-POL-014 Remote Work Policy",
            "Full-time remote work from international location for 6 months",
        ]
        table_ok = False
        for tbl in tables:
            rows = len(re.findall(r"<w:tr[ >]", tbl))
            tbl_text = _wt_concat(tbl)
            if 5 <= rows <= 7 and all(a in tbl_text for a in row_anchors):
                table_ok = True
                break
        if not table_ok:
            missing.append(
                f"request table (need >=1 <w:tbl> with <w:tr>=6+/-1 and all 5 row anchors; "
                f"found {len(tables)} table(s))"
            )

        for probe, name in [
            ("Section 4.2", "Section 4.2 ref"),
            ("Section 7.1", "Section 7.1 ref"),
            ("Tax compliance exposure in foreign jurisdiction", "risk bullet 1"),
            ("GDPR implications", "risk bullet 2"),
            ("permanent establishment risk", "risk bullet 3"),
            ("Precedent-setting impact", "risk bullet 4"),
            ("Two prior similar requests (2025-Q2 and 2025-Q4) were approved with "
             "conditions including time-limited scope, tax equalization agreements, "
             "and data handling restrictions.", "precedent sentence 1"),
            ("One request in 2024-Q3 was denied due to insufficient business "
             "justification.", "precedent sentence 2"),
            ("Recommend approval with conditions: limit to 6-month duration, require "
             "tax equalization agreement, mandate VPN-only data access, and schedule "
             "quarterly compliance review.", "recommendation"),
        ]:
            if _norm(probe) not in text:
                missing.append(name)

        check(label, 2, not missing,
              f"all content probes present (source: {source})" if not missing
              else f"missing: {'; '.join(missing)} (source: {source})")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6c_document_comment():
    """word/comments.xml contains the Risk Assessment review comment."""
    label = "6c. OnlyOffice: Review comment on document"
    probe = "Please confirm with Finance that tax equalization coverage is budgeted"
    try:
        data, source = _oo_get_docx()
        if not data:
            check(label, 1, False, f"could not read document: {source}")
            return
        comments_xml = _docx_member_xml(data, "word/comments.xml")
        if not comments_xml:
            check(label, 1, False, f"word/comments.xml not present (source: {source})")
            return
        text = _wt_concat(comments_xml)
        if not text:
            # comments.xml without <w:t> runs — fall back to tag stripping
            text = _norm(html.unescape(re.sub(r"<[^>]+>", " ", comments_xml)))
        found = _norm(probe) in text
        check(label, 1, found,
              f"comment found (source: {source})" if found
              else f"comment text not found in comments.xml (source: {source})")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def _oo_share_sql(doc_id: str, username: str, level: int) -> bool:
    """Strict share assertion: files_security joined to core_user on the GUID
    (fs.subject and cu.id are both char(38) — direct join), exact user + level."""
    out = oo_db(
        "SELECT 1 FROM files_security fs "
        "JOIN core_user cu ON fs.subject = cu.id "
        f"WHERE fs.entry_type = 2 AND fs.entry_id = CAST({doc_id} AS CHAR) "
        f"AND cu.username = '{username}' AND fs.security = {level} LIMIT 1;"
    )
    return bool(out.strip())


def check_7_onlyoffice_sharing():
    """Document shared with laura.brown (edit, security=1) AND amit.singh
    (view, security=2). API double-assert primary (sharedTo.userName exact +
    access exact), strict SQL join fallback. No bool(shares) / join-less path."""
    label = "7. OnlyOffice: Shared laura.brown=edit + amit.singh=view"
    try:
        doc_id = _oo_doc_id()
        if not doc_id or not doc_id.isdigit():
            check(label, 2, False, "document not found (exact title)")
            return

        # Primary: API double-assert
        api_laura = api_amit = False
        api_seen = False
        try:
            session = _oo_auth_session()
            if session:
                base_url = session.base_url  # type: ignore[attr-defined]
                resp = session.get(f"{base_url}/api/2.0/files/file/{doc_id}/share", timeout=15)
                if resp.status_code == 200:
                    items = resp.json().get("response", []) or []
                    api_seen = True
                    for it in items:
                        uname = (it.get("sharedTo") or {}).get("userName", "")
                        access = it.get("access", -1)
                        if uname == "laura.brown" and access == 1:
                            api_laura = True
                        if uname == "amit.singh" and access == 2:
                            api_amit = True
        except Exception:
            api_seen = False
        if api_seen and api_laura and api_amit:
            check(label, 2, True, "via API: laura.brown access=1, amit.singh access=2")
            return

        # Fallback: two strict SQL assertions — BOTH must hold.
        sql_laura = _oo_share_sql(doc_id, "laura.brown", 1)
        sql_amit = _oo_share_sql(doc_id, "amit.singh", 2)
        passed = sql_laura and sql_amit
        missing = []
        if not sql_laura:
            missing.append("laura.brown security=1 (edit)")
        if not sql_amit:
            missing.append("amit.singh security=2 (view)")
        api_note = "API inconclusive/failed; " if not api_seen else "API did not confirm both; "
        check(label, 2, passed,
              f"via SQL join on file_id={doc_id}" if passed
              else f"{api_note}SQL missing: {', '.join(missing)} (file_id={doc_id})")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── ownCloud checks ──────────────────────────────────────────────────────────
def _oc_webdav_get(path: str) -> tuple[int, str]:
    """GET a file via ownCloud WebDAV. Returns (status_code, body)."""
    import urllib.request
    import urllib.error
    import base64

    creds = base64.b64encode(b"admin:admin").decode()
    url = f"http://{HOST}:{OWNCLOUD_PORT}/remote.php/dav/files/admin/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {creds}"})
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def _oc_propfind(path: str) -> tuple[int, str]:
    """PROPFIND on an ownCloud WebDAV path. Returns (status, body)."""
    import urllib.request
    import urllib.error
    import base64

    creds = base64.b64encode(b"admin:admin").decode()
    url = f"http://{HOST}:{OWNCLOUD_PORT}/remote.php/dav/files/admin/{path}"
    req = urllib.request.Request(url, method="PROPFIND", headers={
        "Authorization": f"Basic {creds}",
        "Depth": "1",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def check_8_owncloud_folder_structure():
    """Folder Exception-Case-RJ-2026-04 with Supporting-Documents and Decision-Records."""
    try:
        status, body = _oc_propfind("Exception-Case-RJ-2026-04/")
        if status >= 400:
            check("8. ownCloud: Case folder with subfolders exists", 1, False,
                  f"PROPFIND returned HTTP {status}")
            return
        has_supporting = "Supporting-Documents" in body
        has_decision = "Decision-Records" in body
        passed = has_supporting and has_decision
        missing = []
        if not has_supporting:
            missing.append("Supporting-Documents")
        if not has_decision:
            missing.append("Decision-Records")
        check("8. ownCloud: Case folder with subfolders exists", 1, passed,
              "both subfolders present" if passed else f"missing: {', '.join(missing)}")
    except Exception as e:
        check("8. ownCloud: Case folder with subfolders exists", 1, False, f"exception: {e}")


EXPECTED_REQUEST_SUMMARY = """Requester: Rahul Johnson (Engineering Developer)
Request Date: 2026-04-15
Policy: HR-POL-014 Remote Work Policy
Requested Exception: Full-time remote work from international location (Portugal) for 6 months starting 2026-05-01.
Business Justification: Family medical situation requiring extended presence abroad; role is fully remote-compatible.
Supporting Documents Received: Medical attestation, manager endorsement, IT security assessment."""

EXPECTED_DECISION_RECORD = """DECISION RECORD

Case: Exception-Case-RJ-2026-04
Requester: Rahul Johnson
Policy Reference: HR-POL-014 Remote Work Policy

Decision Outcome: Approved with Conditions
Conditions: (1) Duration limited to 6 months, (2) Tax equalization agreement executed before relocation, (3) VPN-only access to company systems, (4) Quarterly compliance review with Legal and HR, (5) No client-data handling from international location.
Effective Period: 2026-05-01 to 2026-10-31

Decision Authority: Legal Coordinator in consultation with General Counsel and HR Director."""


def check_9_request_summary():
    """request-summary.txt matches the full task text (normalized comparison)."""
    label = "9. ownCloud: request-summary.txt full content"
    try:
        status, content = _oc_webdav_get(
            "Exception-Case-RJ-2026-04/Supporting-Documents/request-summary.txt"
        )
        if status >= 400:
            check(label, 1, False, f"HTTP {status}")
            return
        passed = _norm(EXPECTED_REQUEST_SUMMARY) in _norm(content)
        detail = "normalized full text matches"
        if not passed:
            missing = [ln.split(":")[0] for ln in EXPECTED_REQUEST_SUMMARY.splitlines()
                       if _norm(ln) not in _norm(content)]
            detail = (f"normalized full text mismatch; lines off: {', '.join(missing)}"
                      if missing else "normalized full text mismatch (ordering/spacing)")
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_10_decision_record():
    """decision-record.txt matches the full task text (normalized comparison)."""
    label = "10. ownCloud: decision-record.txt full content"
    try:
        status, content = _oc_webdav_get(
            "Exception-Case-RJ-2026-04/Decision-Records/decision-record.txt"
        )
        if status >= 400:
            check(label, 1, False, f"HTTP {status}")
            return
        passed = _norm(EXPECTED_DECISION_RECORD) in _norm(content)
        detail = "normalized full text matches"
        if not passed:
            missing = [ln.split(":")[0][:30] for ln in EXPECTED_DECISION_RECORD.splitlines()
                       if ln.strip() and _norm(ln) not in _norm(content)]
            detail = (f"normalized full text mismatch; lines off: {', '.join(missing)}"
                      if missing else "normalized full text mismatch (ordering/spacing)")
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_12_owncloud_tag():
    """Tag PolicyException2026 is mapped to the exact target folder
    files/Exception-Case-RJ-2026-04 in admin's home storage."""
    label = "12. ownCloud: PolicyException2026 tag on Exception-Case-RJ-2026-04"
    try:
        out = oc_db(
            "SELECT 1 FROM oc_systemtag t "
            "JOIN oc_systemtag_object_mapping m ON m.systemtagid = t.id "
            "  AND m.objecttype = 'files' "
            "JOIN oc_filecache fc ON CAST(fc.fileid AS CHAR) = m.objectid "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE t.name = 'PolicyException2026' AND s.id = 'home::admin' "
            "  AND fc.path = 'files/Exception-Case-RJ-2026-04' LIMIT 1;"
        )
        found = bool(out.strip())
        check(label, 1, found,
              "tag mapped to target folder" if found
              else "no tag mapping to files/Exception-Case-RJ-2026-04 in home::admin")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Mattermost checks ────────────────────────────────────────────────────────
MM_DECISION_MESSAGE = (
    "Policy Exception Decision: Request from Rahul Johnson for international "
    "remote work under HR-POL-014 has been APPROVED WITH CONDITIONS. Decision "
    "record filed in ownCloud under Exception-Case-RJ-2026-04."
)

_mm_decision_post_id = ""


def check_13_mm_decision_post():
    """Decision message posted in 'incidents' channel of team 'Engineering Hub'
    with the full message text; captures the post id for ck14/follow checks."""
    global _mm_decision_post_id
    label = "13. Mattermost: Decision posted in Engineering Hub/incidents"
    try:
        row = mm_db(
            "SELECT p.id FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.name = 'incidents' "
            "AND t.displayname = 'Engineering Hub' "
            "AND p.deleteat = 0 "
            f"AND p.message LIKE '%{MM_DECISION_MESSAGE}%' "
            "ORDER BY p.createat ASC LIMIT 1;"
        )
        post_id = row.splitlines()[0].strip() if row else ""
        if post_id and re.fullmatch(r"[a-z0-9]{26}", post_id):
            _mm_decision_post_id = post_id
        found = bool(post_id)
        check(label, 2, found,
              f"post id={post_id}" if found
              else "no post with full decision text in Engineering Hub/incidents")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_mm_thread_reply():
    """A SINGLE reply row bound to the decision post (rootid = ck13 post id)
    contains all 5 condition phrases + the effective period (per-row EXISTS)."""
    label = "14. Mattermost: Thread reply with all 5 conditions (bound to decision post)"
    try:
        if not _mm_decision_post_id:
            check(label, 1, False, "decision post (check 13) not found; cannot bind rootid")
            return
        row = mm_db(
            "SELECT p.id FROM posts p "
            f"WHERE p.rootid = '{_mm_decision_post_id}' "
            "AND p.deleteat = 0 "
            "AND p.message LIKE '%6-month duration limit%' "
            "AND p.message LIKE '%Tax equalization agreement%' "
            "AND p.message LIKE '%VPN-only system access%' "
            "AND p.message LIKE '%Quarterly compliance review%' "
            "AND p.message LIKE '%No client-data handling%' "
            "AND p.message LIKE '%Effective period: 2026-05-01 to 2026-10-31%' "
            "LIMIT 1;"
        )
        found = bool(row)
        check(label, 1, found,
              "single reply row with all anchors" if found
              else f"no single reply on rootid={_mm_decision_post_id} with all 5 conditions + effective period")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_15_mm_dm_mercy():
    """DM sent to user 'mercy' about the policy exception decision."""
    label = "15. Mattermost: DM sent to mercy"
    try:
        mercy_id = mm_db("SELECT id FROM users WHERE username = 'mercy' LIMIT 1;")
        if not mercy_id:
            check(label, 1, False, "user 'mercy' not found")
            return

        # DM channel scoped to mercy via channelmembers; full-text anchors.
        row = mm_db(
            f"SELECT p.id FROM posts p "
            f"JOIN channels c ON p.channelid = c.id "
            f"JOIN channelmembers cm ON cm.channelid = c.id AND cm.userid = '{mercy_id}' "
            f"WHERE c.type = 'D' "
            f"AND p.message LIKE '%Rahul Johnson%' "
            f"AND p.message LIKE '%policy exception%' "
            f"AND p.message LIKE '%read-only%' "
            f"AND p.message LIKE '%acknowledge by 2026-04-25%' "
            f"AND p.deleteat = 0 "
            f"LIMIT 1;"
        )
        found = bool(row)
        check(label, 1, found,
              "DM found" if found else "no DM to mercy with required anchors")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_16_mm_follow_thread():
    """Admin follows the decision-post thread (threadmemberships.following),
    guarded by a pg_catalog table-existence probe (never crashes)."""
    label = "16. Mattermost: Decision thread followed by admin"
    try:
        if not _mm_decision_post_id:
            check(label, 1, False, "decision post (check 13) not found; cannot resolve thread root")
            return
        reg = mm_db("SELECT to_regclass('public.threadmemberships');")
        if not reg or reg.lower() in ("", "null", "-"):
            check(label, 1, False,
                  "threadmemberships table absent in this Mattermost schema; follow state not verifiable")
            return
        row = mm_db(
            "SELECT 1 FROM threadmemberships tm "
            f"WHERE tm.postid = '{_mm_decision_post_id}' "
            "AND tm.userid = (SELECT id FROM users WHERE username = 'admin') "
            "AND tm.following = true LIMIT 1;"
        )
        found = bool(row)
        check(label, 1, found,
              f"admin following thread root {_mm_decision_post_id}" if found
              else f"no following=true row for admin on root {_mm_decision_post_id}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_exceptions_folder()
    check_4_formal_decision_email()
    check_5_recipients_and_priority()
    check_5b_email_body()
    check_5c_dsn_note()
    check_6_onlyoffice_document()
    check_6b_document_content()
    check_6c_document_comment()
    check_7_onlyoffice_sharing()
    check_8_owncloud_folder_structure()
    check_9_request_summary()
    check_10_decision_record()
    check_12_owncloud_tag()
    check_13_mm_decision_post()
    check_14_mm_thread_reply()
    check_15_mm_dm_mercy()
    check_16_mm_follow_thread()

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
