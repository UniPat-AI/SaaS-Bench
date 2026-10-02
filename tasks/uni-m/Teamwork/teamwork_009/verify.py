"""
Verifier for Teamwork-009-I5: Coordinate Media Response to Data Privacy Inquiry

Checks: 9 checks (total weight 13) across mattermost and onlyoffice.
Strategy: docker exec (maildir) for roundcubemail, docker exec (DB) for mattermost,
          docker exec (DB) + API for onlyoffice.

Required env vars:
  SERVER_HOSTNAME,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import io
import json
import os
import re
import subprocess
import sys
import zipfile
from html import unescape as _xml_unescape

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

ROUNDCUBEMAIL_PORT = os.environ.get("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = os.environ.get("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = os.environ.get("ROUNDCUBEMAIL_DB_CONTAINER")

MATTERMOST_PORT = os.environ.get("MATTERMOST_PORT")
MATTERMOST_CONTAINER = os.environ.get("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = os.environ.get("MATTERMOST_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

_REQUIRED = [
    ("ROUNDCUBEMAIL_PORT", ROUNDCUBEMAIL_PORT),
    ("ROUNDCUBEMAIL_CONTAINER", ROUNDCUBEMAIL_CONTAINER),
    ("ROUNDCUBEMAIL_DB_CONTAINER", ROUNDCUBEMAIL_DB_CONTAINER),
    ("MATTERMOST_PORT", MATTERMOST_PORT),
    ("MATTERMOST_CONTAINER", MATTERMOST_CONTAINER),
    ("MATTERMOST_DB_CONTAINER", MATTERMOST_DB_CONTAINER),
    ("ONLYOFFICE_PORT", ONLYOFFICE_PORT),
    ("ONLYOFFICE_CONTAINER", ONLYOFFICE_CONTAINER),
    ("ONLYOFFICE_DB_CONTAINER", ONLYOFFICE_DB_CONTAINER),
]
for _name, _val in _REQUIRED:
    if not _val:
        print(f"FATAL: {_name} not set", file=sys.stderr)
        sys.exit(1)


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


def mm_db_query(sql: str) -> str:
    """Query Mattermost PostgreSQL DB."""
    rc, stdout, stderr = docker_exec(
        MATTERMOST_DB_CONTAINER,
        "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"psql query failed (rc={rc}): {stderr.strip()[-300:]}")
    return stdout.strip()


def oo_db_query(sql: str) -> str:
    """Query OnlyOffice MySQL DB."""
    rc, stdout, stderr = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mysql query failed (rc={rc}): {stderr.strip()[-300:]}")
    return stdout.strip()


def mail_grep(pattern: str, path: str = "/var/mail/", extra_flags: str = "") -> list[str]:
    """Grep for pattern in the maildir inside the Roundcube container. Returns matching file paths."""
    cmd = f"grep -rl {extra_flags} '{pattern}' {path} 2>/dev/null || true"
    rc, stdout, stderr = docker_exec(ROUNDCUBEMAIL_CONTAINER, "bash", "-c", cmd, timeout=20)
    return [f for f in stdout.strip().split("\n") if f]


def mail_cat(filepath: str) -> str:
    """Read a mail file from the Roundcube container."""
    rc, stdout, stderr = docker_exec(
        ROUNDCUBEMAIL_CONTAINER, "bash", "-c", f"cat '{filepath}' 2>/dev/null", timeout=15
    )
    return stdout


# ── Mattermost checks ────────────────────────────────────────────────────────

# Exact inquiry-brief text from description.md (full-text anchor).
_BRIEF_ANCHOR = (
    "Journalist: Robert Singh (Stellar Tech). Key questions: "
    "(1) data protection controls and certifications, "
    "(2) incident response readiness and breach notification commitments, "
    "(3) executive availability to discuss our security roadmap."
)


def _mm_brief_post_id() -> str:
    """Resolve the inquiry-brief post id via the exact full-text anchor.
    Returns '' when no such top-level post exists in the channel."""
    result = mm_db_query(
        "SELECT p.id FROM posts p "
        "JOIN channels c ON p.channelid = c.id "
        "JOIN teams t ON c.teamid = t.id "
        "WHERE c.name = 'media-response-privacy-security' "
        "AND t.displayname = 'Marketing & Growth' "
        "AND p.rootid = '' "
        "AND p.deleteat = 0 "
        f"AND p.message LIKE '%{_BRIEF_ANCHOR}%' "
        "ORDER BY p.createat ASC LIMIT 1;"
    )
    return result.splitlines()[0].strip() if result else ""


def check_6_mm_channel() -> None:
    """Private channel 'media-response-privacy-security' in Marketing & Growth with correct purpose."""
    try:
        result = mm_db_query(
            "SELECT c.type, c.purpose "
            "FROM channels c JOIN teams t ON c.teamid = t.id "
            "WHERE c.name = 'media-response-privacy-security' "
            "AND t.displayname = 'Marketing & Growth';"
        )

        if not result:
            check("6. MM private channel with correct purpose", 2, False, "channel not found")
            return

        parts = result.split("|")
        ch_type = parts[0].strip() if len(parts) > 0 else ""
        ch_purpose = parts[1].strip() if len(parts) > 1 else ""

        is_private = ch_type == "P"
        expected_purpose = "Coordinate response to Robert Singh media inquiry regarding customer data privacy and security posture."
        has_purpose = expected_purpose.lower() in ch_purpose.lower()

        passed = is_private and has_purpose
        details = []
        if not is_private:
            details.append(f"type={ch_type}, expected P")
        if not has_purpose:
            details.append(f"purpose mismatch: '{ch_purpose[:100]}'")
        check("6. MM private channel with correct purpose", 2, passed,
              "; ".join(details) if details else "OK")
    except Exception as e:
        check("6. MM private channel with correct purpose", 2, False, f"exception: {e}")


def check_7_mm_inquiry_brief() -> None:
    """Inquiry brief message posted in channel (exact full-text anchor)."""
    try:
        brief_id = _mm_brief_post_id()
        check("7. Inquiry brief message posted", 1, bool(brief_id),
              f"brief post id={brief_id}" if brief_id
              else "no top-level post matching exact brief text")
    except Exception as e:
        check("7. Inquiry brief message posted", 1, False, f"exception: {e}")


def check_8_mm_thread_reply() -> None:
    """Single thread reply on the brief post with @tonda review request text."""
    try:
        brief_id = _mm_brief_post_id()
        if not brief_id:
            check("8. Thread reply with @tonda review request", 2, False,
                  "brief post not found (exact-text anchor), cannot bind thread")
            return

        # Per-row judging: one reply row must carry ALL three probes itself.
        result = mm_db_query(
            "SELECT p.id FROM posts p "
            f"WHERE p.rootid = '{brief_id}' "
            "AND p.deleteat = 0 "
            "AND p.message LIKE '%@tonda%' "
            "AND p.message LIKE '%legal vetting and executive sign-off%' "
            "AND p.message LIKE '%target turnaround: 24 hours%';"
        )

        passed = bool(result)
        check("8. Thread reply with @tonda review request", 2, passed,
              "single reply in brief thread carries all probes" if passed else
              "no single reply with rootid=brief containing @tonda + "
              "'legal vetting and executive sign-off' + 'target turnaround: 24 hours'")
    except Exception as e:
        check("8. Thread reply with @tonda review request", 2, False, f"exception: {e}")


def check_9_mm_message_saved() -> None:
    """Inquiry brief message is saved/flagged by admin in Mattermost."""
    try:
        brief_id = _mm_brief_post_id()
        if not brief_id:
            check("9. Inquiry brief message saved/flagged", 1, False,
                  "brief post not found (exact-text anchor)")
            return

        result = mm_db_query(
            "SELECT pr.name FROM preferences pr "
            "WHERE pr.category = 'flagged_post' "
            "AND pr.userid = (SELECT id FROM users WHERE username = 'admin') "
            f"AND pr.name = '{brief_id}';"
        )

        passed = bool(result)
        check("9. Inquiry brief message saved/flagged", 1, passed,
              "brief post flagged by admin" if passed
              else "brief post not flagged by admin in preferences")
    except Exception as e:
        check("9. Inquiry brief message saved/flagged", 1, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

# Exact document title from description.md (with and without .docx extension).
_OO_DOC_TITLE = "Media Statement - Data Privacy and Security Inquiry"
_OO_DOC_TITLES = (_OO_DOC_TITLE, _OO_DOC_TITLE + ".docx")
_OO_DOC_TITLES_SQL = (
    "('Media Statement - Data Privacy and Security Inquiry',"
    "'Media Statement - Data Privacy and Security Inquiry.docx')"
)


def _oo_media_statement_file_id() -> str:
    """Resolve the Media Statement files_file.id by exact title (latest id)."""
    result = oo_db_query(
        "SELECT id FROM files_file "
        f"WHERE title IN {_OO_DOC_TITLES_SQL} "
        "ORDER BY id DESC LIMIT 1;"
    )
    return result.splitlines()[0].strip() if result else ""


def _oo_auth_session() -> requests.Session | None:
    """Authenticate to OnlyOffice and return a session with token."""
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
        data = resp.json()
        token = data.get("response", {}).get("token", "")
        if not token:
            return None
        session.headers.update({"Authorization": f"Bearer {token}"})
        session.cookies.set("asc_auth_key", token)
        session.base_url = base_url  # type: ignore[attr-defined]
        return session
    except Exception:
        return None


def check_10_oo_document_exists() -> None:
    """Document 'Media Statement - Data Privacy and Security Inquiry' exists (exact title)."""
    try:
        result = oo_db_query(
            "SELECT id, title FROM files_file "
            f"WHERE title IN {_OO_DOC_TITLES_SQL} "
            "ORDER BY id DESC LIMIT 5;"
        )

        passed = bool(result)
        check("10. OnlyOffice document exists", 1, passed,
              f"found: {result[:200]}" if passed
              else "no files_file row with exact title (with/without .docx)")
    except Exception as e:
        check("10. OnlyOffice document exists", 1, False, f"exception: {e}")


def check_10b_oo_in_my_documents() -> None:
    """Document is located in My Documents (API @my listing preferred, DB folder join fallback)."""
    label = "10b. Document located in My Documents"
    try:
        # Preferred: the file appears in the authenticated admin's @my listing.
        session = _oo_auth_session()
        if session:
            try:
                base_url = session.base_url  # type: ignore[attr-defined]
                resp = session.get(f"{base_url}/api/2.0/files/@my", timeout=15)
                if resp.status_code == 200:
                    files_list = resp.json().get("response", {}).get("files", [])
                    for f in files_list:
                        if f.get("title", "") in _OO_DOC_TITLES:
                            rft = f.get("rootFolderType")
                            check(label, 1, True,
                                  f"found in @my listing (rootFolderType={rft})")
                            return
            except Exception:
                pass  # fall through to DB fallback

        # Fallback: DB folder join. FolderType.USER (My Documents root) is
        # believed to be folder_type=5; value not confirmed on a live slot,
        # so the detail stays descriptive either way.
        row = oo_db_query(
            "SELECT fo.folder_type, fo.title FROM files_file ff "
            "JOIN files_folder fo ON ff.folder_id = fo.id "
            f"WHERE ff.title IN {_OO_DOC_TITLES_SQL} "
            "ORDER BY ff.id DESC LIMIT 1;"
        )
        if not row:
            check(label, 1, False, "document/parent folder not found in DB")
            return
        parts = row.splitlines()[0].split("\t")
        folder_type = parts[0].strip() if parts else ""
        folder_title = parts[1].strip() if len(parts) > 1 else ""
        passed = folder_type == "5" or folder_title == "My Documents"
        check(label, 1, passed,
              f"parent folder_type={folder_type} title='{folder_title}' "
              "(expected folder_type 5 = USER/My Documents root; "
              "mapping not confirmed live)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_11_oo_shared_with_user() -> None:
    """Document shared with amit.singh for editing (API exact match, strict DB fallback)."""
    label = "11. Document shared with amit.singh (edit)"
    try:
        file_id = _oo_media_statement_file_id()
        if not file_id:
            check(label, 2, False, "document not found by exact title")
            return

        # Primary: share API — userName full equality + access == 1 (ReadWrite).
        session = _oo_auth_session()
        if session:
            try:
                base_url = session.base_url  # type: ignore[attr-defined]
                share_resp = session.get(
                    f"{base_url}/api/2.0/files/file/{file_id}/share", timeout=15
                )
                if share_resp.status_code == 200:
                    shares = share_resp.json().get("response", [])
                    amit_shared = False
                    amit_can_edit = False
                    for s in shares:
                        shared_to = s.get("sharedTo", {})
                        if shared_to.get("userName", "") == "amit.singh":
                            amit_shared = True
                            if s.get("access") == 1:  # 1 = ReadWrite (edit)
                                amit_can_edit = True
                    passed = amit_shared and amit_can_edit
                    detail = ("userName=amit.singh with access=1 (API)" if passed else
                              "amit.singh not in share list (API, exact userName)"
                              if not amit_shared else
                              "amit.singh shared but access != 1 (API)")
                    check(label, 2, passed, detail)
                    return
            except Exception:
                pass  # fall through to DB fallback

        # Fallback: strict DB assertion (subject GUID joined to core_user,
        # entry bound to this file, security == 1 = edit).
        row = oo_db_query(
            "SELECT cu.username, fs.security "
            "FROM files_security fs "
            "JOIN core_user cu ON fs.subject = cu.id "
            "WHERE fs.entry_type = 2 "
            f"AND fs.entry_id = CAST({file_id} AS CHAR) "
            "AND cu.username = 'amit.singh' "
            "AND fs.security = 1;"
        )
        check(label, 2, bool(row),
              f"DB: amit.singh security=1 on file {file_id}" if row else
              f"DB: no files_security row for amit.singh with security=1 "
              f"on file {file_id}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def _oo_docx_bytes_from_fs() -> bytes | None:
    """Read the Media Statement document's content.docx from the portal data dir
    via docker exec. Returns raw docx bytes, or None if it cannot be located."""
    file_id = _oo_media_statement_file_id()
    if not file_id:
        return None
    # '*file_<id>/*content.docx' also covers version subdirectories (v1/v2/...);
    # when several versions match, pick the highest version directory.
    rc, stdout, _ = docker_exec(
        ONLYOFFICE_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.docx' "
        "2>/dev/null",
        timeout=20,
    )
    paths = [p.strip() for p in stdout.strip().splitlines() if p.strip()]
    if not paths:
        return None

    def _version_key(p: str) -> int:
        m = re.search(r"/v(\d+)/", p)
        return int(m.group(1)) if m else -1

    path = max(paths, key=_version_key)
    r = subprocess.run(
        ["docker", "exec", ONLYOFFICE_CONTAINER, "cat", path],
        capture_output=True, timeout=30,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout


def _oo_docx_bytes_from_api() -> bytes | None:
    """Fallback: download the document via the OnlyOffice HTTP API."""
    session = _oo_auth_session()
    if not session:
        return None
    base_url = session.base_url  # type: ignore[attr-defined]
    doc_id = _oo_media_statement_file_id()
    if not doc_id:
        # Last resort: exact-title lookup in the @my listing.
        resp = session.get(f"{base_url}/api/2.0/files/@my", timeout=15)
        files_list = resp.json().get("response", {}).get("files", [])
        for f in files_list:
            if f.get("title", "") in _OO_DOC_TITLES:
                doc_id = f.get("id")
                break
    if not doc_id:
        return None
    dl_resp = session.get(
        f"{base_url}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(doc_id)},
        timeout=30, allow_redirects=True,
    )
    if not (dl_resp.status_code == 200 and len(dl_resp.content) > 100):
        dl_resp = session.get(
            f"{base_url}/api/2.0/files/file/{doc_id}/download", timeout=30,
            allow_redirects=True,
        )
    if dl_resp.status_code != 200:
        return None
    return dl_resp.content


def _oo_get_media_statement_docx() -> tuple[bytes | None, str]:
    """Get the Media Statement docx bytes: docker exec on the data dir first,
    HTTP download as fallback. Returns (bytes|None, source detail)."""
    try:
        data = _oo_docx_bytes_from_fs()
        if data:
            return data, "fs (content.docx via docker exec)"
    except Exception as e:
        pass_detail = f"fs read failed: {e}"
    else:
        pass_detail = "content.docx not found in data dir"
    try:
        data = _oo_docx_bytes_from_api()
        if data:
            return data, "api download (fallback)"
    except Exception as e:
        return None, f"{pass_detail}; api download failed: {e}"
    return None, f"{pass_detail}; api download failed"


def _docx_body_text(content_data: bytes) -> str | None:
    """Extract the concatenated <w:t> run text of word/document.xml from raw
    docx bytes, XML-unescaped and whitespace-normalized. OnlyOffice splits
    sentences across many runs, so raw-XML grep would false-negative."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(content_data))
    except zipfile.BadZipFile:
        return None
    pieces: list[str] = []
    for name in zf.namelist():
        if "document.xml" in name.lower() or "word/document" in name.lower():
            xml_content = zf.read(name).decode("utf-8", errors="replace")
            for run_text in re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml_content, re.DOTALL):
                pieces.append(_xml_unescape(run_text))
            pieces.append(" ")  # paragraph-part separator between xml parts
    if not pieces:
        return None
    return re.sub(r"\s+", " ", "".join(pieces)).strip()


# Content probes — exact strings from description.md (incl. em dashes).
_DOCX_PROBES = {
    "heading": "Official Statement — Customer Data Privacy and Security Posture",
    "section Background": "Background",
    "section Key Messages": "Key Messages",
    "section Approved Quote": "Approved Quote",
    "bullet 1": "foundational to our business",
    "bullet 2": "industry-recognized certifications and undergo regular "
                "independent third-party audits",
    "bullet 3": "continuously tested with clear escalation paths and "
                "transparent customer notification commitments",
    "bullet 4": "multi-year security investment roadmap in greater depth",
    "approved quote": "Customer trust is earned through consistent, transparent, "
                      "and rigorous protection of their data — it is a commitment "
                      "we live by every day, not a checkbox we periodically revisit.",
    "quote attribution": "Maria Wilson, Product Manager",
}


def check_12_oo_document_content() -> None:
    """Document contains heading, section titles, 4 Key Messages bullets, and
    the Approved Quote with attribution (probed on concatenated <w:t> text)."""
    try:
        content_data, source = _oo_get_media_statement_docx()
        if not content_data:
            check("12. Document contains key content sections", 2, False,
                  f"could not read document: {source}")
            return

        text = _docx_body_text(content_data)
        if text is None:
            check("12. Document contains key content sections", 2, False,
                  f"not a readable docx (no word/document.xml text; source: {source})")
            return

        missing = [name for name, probe in _DOCX_PROBES.items() if probe not in text]
        all_found = not missing
        check("12. Document contains key content sections", 2, all_found,
              f"all {len(_DOCX_PROBES)} probes found (source: {source})" if all_found
              else f"missing: {missing} (source: {source})")
    except Exception as e:
        check("12. Document contains key content sections", 2, False, f"exception: {e}")


def check_13_oo_track_changes() -> None:
    """Track changes is enabled on the document."""
    try:
        content_data, source = _oo_get_media_statement_docx()
        if not content_data:
            check("13. Track changes enabled", 1, False,
                  f"could not read document: {source}")
            return

        track_changes_on = False
        try:
            zf = zipfile.ZipFile(io.BytesIO(content_data))
            for name in zf.namelist():
                if "settings.xml" in name.lower():
                    xml_content = zf.read(name).decode("utf-8", errors="replace")
                    # <w:trackRevisions/> or <w:trackRevisions w:val="true"/>
                    if "trackRevisions" in xml_content:
                        # Check it's not explicitly set to false
                        if 'val="false"' not in xml_content and "val='false'" not in xml_content:
                            track_changes_on = True
        except zipfile.BadZipFile:
            pass

        check("13. Track changes enabled", 1, track_changes_on,
              f"trackRevisions found in settings (source: {source})" if track_changes_on
              else f"trackRevisions not found (source: {source})")
    except Exception as e:
        check("13. Track changes enabled", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_6_mm_channel()
    check_7_mm_inquiry_brief()
    check_8_mm_thread_reply()
    check_9_mm_message_saved()
    check_10_oo_document_exists()
    check_10b_oo_in_my_documents()
    check_11_oo_shared_with_user()
    check_12_oo_document_content()
    check_13_oo_track_changes()

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
