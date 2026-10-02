"""
Verifier for Teamwork-042-I2: Onboard Freelance Contractor Diego Martinez

Checks: 18 check lines (17 weighted + 1 zero-weight DSN note) across owncloud,
onlyoffice, mattermost, roundcubemail. Total weight: 24.
Strategy: docker exec DB (ownCloud MariaDB, Mattermost Postgres, Roundcube MariaDB,
          OnlyOffice MySQL), REST API (OnlyOffice DocSpace, ownCloud WebDAV),
          docx content probes (fs content.docx + API download fallback),
          Sent-maildir parsing with the Python email stdlib.

Required env vars:
  SERVER_HOSTNAME,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER.
"""

import html
import io
import os
import re
import subprocess
import sys
import zipfile
from email import message_from_bytes
from email.header import decode_header
from email.utils import getaddresses

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OC_PORT = os.environ.get("OWNCLOUD_PORT")
OC_CONTAINER = os.environ.get("OWNCLOUD_CONTAINER")
OC_DB_CONTAINER = os.environ.get("OWNCLOUD_DB_CONTAINER")

OO_PORT = os.environ.get("ONLYOFFICE_PORT")
OO_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
OO_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

MM_PORT = os.environ.get("MATTERMOST_PORT")
MM_CONTAINER = os.environ.get("MATTERMOST_CONTAINER")
MM_DB_CONTAINER = os.environ.get("MATTERMOST_DB_CONTAINER")

RC_PORT = os.environ.get("ROUNDCUBEMAIL_PORT")
RC_CONTAINER = os.environ.get("ROUNDCUBEMAIL_CONTAINER")
RC_DB_CONTAINER = os.environ.get("ROUNDCUBEMAIL_DB_CONTAINER")

_required = {
    "OWNCLOUD_PORT": OC_PORT, "OWNCLOUD_CONTAINER": OC_CONTAINER,
    "OWNCLOUD_DB_CONTAINER": OC_DB_CONTAINER,
    "ONLYOFFICE_PORT": OO_PORT, "ONLYOFFICE_CONTAINER": OO_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": OO_DB_CONTAINER,
    "MATTERMOST_PORT": MM_PORT, "MATTERMOST_CONTAINER": MM_CONTAINER,
    "MATTERMOST_DB_CONTAINER": MM_DB_CONTAINER,
    "ROUNDCUBEMAIL_PORT": RC_PORT, "ROUNDCUBEMAIL_CONTAINER": RC_CONTAINER,
    "ROUNDCUBEMAIL_DB_CONTAINER": RC_DB_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
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


def docker_exec_bytes(container: str, *args: str, timeout: int = 30) -> bytes | None:
    r = subprocess.run(
        ["docker", "exec", container, *args],
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
        OC_DB_CONTAINER,
        "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud", "-D", "owncloud",
        "--default-character-set=utf8mb4", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_db(sql: str) -> str:
    """Query OnlyOffice MySQL."""
    rc, out, err = docker_exec(
        OO_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mm_db(sql: str) -> str:
    """Query Mattermost Postgres."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=mmuser_password",
         MM_DB_CONTAINER, "psql", "-U", "mmuser", "-d", "mattermost",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"mattermost psql query failed (rc={r.returncode}): {' '.join(r.stderr.split())[-300:]}"
        )
    return r.stdout.strip()


def rc_db(sql: str) -> str:
    """Query Roundcube MariaDB."""
    rc, out, err = docker_exec(
        RC_DB_CONTAINER,
        "mysql", "-u", "roundcube", "-proundcube123", "-D", "roundcubemail",
        "--default-character-set=utf8mb4", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_api_auth() -> tuple[str, dict]:
    """Authenticate to OnlyOffice DocSpace, return (base_url, headers)."""
    base = f"http://{HOST}:{OO_PORT}"
    resp = requests.post(
        f"{base}/api/2.0/authentication",
        json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
        timeout=15,
    )
    data = resp.json()
    token = data.get("response", {}).get("token", "")
    return base, {"Authorization": f"Bearer {token}"}


# ── OnlyOffice docx fetch chain (fs content.docx first, API download fallback) ─
ENGAGEMENT_TITLES = (
    "Engagement_Letter_Diego_Martinez",
    "Engagement_Letter_Diego_Martinez.docx",
)


def _oo_file_id(titles: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in titles)
    out = oo_db(
        f"SELECT id FROM files_file WHERE title IN ({quoted}) ORDER BY id DESC LIMIT 1;"
    )
    return out.splitlines()[0].strip() if out.strip() else ""


def _oo_docx_bytes_from_fs(titles: tuple[str, ...]) -> bytes | None:
    """Read content.docx from the portal data dir via docker exec.
    The middle '*' in -path covers version subdirectories (v1/v2/...)."""
    file_id = _oo_file_id(titles)
    if not file_id:
        return None
    rc, stdout, _ = docker_exec(
        OO_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.docx' "
        "2>/dev/null",
        timeout=25,
    )
    candidates = [p.strip() for p in stdout.splitlines() if p.strip()]
    if not candidates:
        return None
    # Multiple hits = version subdirectories; take the highest version.
    path = max(candidates, key=lambda p: [int(x) for x in re.findall(r"(\d+)", p)])
    return docker_exec_bytes(OO_CONTAINER, "cat", path)


def _oo_docx_bytes_from_api(titles: tuple[str, ...]) -> bytes | None:
    """Fallback: download the document via the OnlyOffice HTTP API."""
    doc_id = _oo_file_id(titles)
    if not doc_id:
        return None
    base, headers = oo_api_auth()
    dl_resp = requests.get(
        f"{base}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(doc_id)},
        headers=headers, timeout=30, allow_redirects=True,
    )
    if not (dl_resp.status_code == 200 and len(dl_resp.content) > 100):
        dl_resp = requests.get(
            f"{base}/api/2.0/files/file/{doc_id}/download",
            headers=headers, timeout=30, allow_redirects=True,
        )
    if dl_resp.status_code != 200:
        return None
    return dl_resp.content


def _oo_get_docx(titles: tuple[str, ...]) -> tuple[bytes | None, str]:
    """Get docx bytes: docker exec on the data dir first, HTTP download fallback."""
    try:
        data = _oo_docx_bytes_from_fs(titles)
        if data:
            return data, "fs (content.docx via docker exec)"
        fs_detail = "content.docx not found in data dir"
    except Exception as e:
        fs_detail = f"fs read failed: {e}"
    try:
        data = _oo_docx_bytes_from_api(titles)
        if data:
            return data, "api download (fallback)"
    except Exception as e:
        return None, f"{fs_detail}; api download failed: {e}"
    return None, f"{fs_detail}; api download failed"


def _docx_xml(data: bytes, member: str) -> str:
    """Read one XML member from a docx zip ('' if absent)."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        for name in zf.namelist():
            if name.lower() == member.lower():
                return zf.read(name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        pass
    return ""


def _norm(s: str) -> str:
    return " ".join(s.split())


def _wxml_text(xml_fragment: str) -> str:
    """Concatenate all <w:t> runs (per paragraph, then space-joined) into
    normalized plain text. OnlyOffice splits sentences across runs, so raw
    XML grep would false-negative — always match against this."""
    paras = re.split(r"</w:p>", xml_fragment)
    parts = []
    for p in paras:
        runs = re.findall(r"<w:t[^>]*>(.*?)</w:t>", p, flags=re.DOTALL)
        if runs:
            parts.append(html.unescape("".join(runs)))
    return _norm(" ".join(parts))


def _tbl_blocks(doc_xml: str) -> list[str]:
    return re.findall(r"<w:tbl>.*?</w:tbl>", doc_xml, flags=re.DOTALL)


def _tr_blocks(tbl_xml: str) -> list[str]:
    return re.findall(r"<w:tr[ >].*?</w:tr>", tbl_xml, flags=re.DOTALL)


# ── Roundcube Sent-maildir parsing (stdlib email) ─────────────────────────────
def rc_sent_messages(localpart: str) -> list[tuple[str, object]]:
    """Parse every message in the sender's Sent Maildir
    /var/mail/mail.local/<localpart>/.Sent/{cur,new}/. Returns (path, Message)."""
    base = f"/var/mail/mail.local/{localpart}/.Sent"
    rc, out, _ = docker_exec(
        RC_CONTAINER, "bash", "-c",
        f"find '{base}/cur' '{base}/new' -type f 2>/dev/null",
        timeout=20,
    )
    msgs = []
    for path in [p.strip() for p in (out or "").splitlines() if p.strip()]:
        raw = docker_exec_bytes(RC_CONTAINER, "cat", path)
        if raw:
            msgs.append((path, message_from_bytes(raw)))
    return msgs


def rc_decoded_header(msg, name: str) -> str:
    raw = msg.get(name, "") or ""
    out = []
    for val, enc in decode_header(raw):
        if isinstance(val, bytes):
            out.append(val.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(val)
    return _norm("".join(out))


def rc_addr_set(msg, header: str) -> set[str]:
    vals = msg.get_all(header, []) or []
    return {addr.strip().lower() for _, addr in getaddresses(vals) if addr and addr.strip()}


def rc_from_display_and_addr(msg) -> tuple[str, str]:
    vals = msg.get_all("From", []) or []
    pairs = getaddresses(vals)
    if not pairs:
        return "", ""
    display, addr = pairs[0]
    decoded = []
    for val, enc in decode_header(display):
        if isinstance(val, bytes):
            decoded.append(val.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(val)
    return _norm("".join(decoded)), addr.strip().lower()


def rc_body_text(msg) -> str:
    """Decode all text/* parts (CTE handled by get_payload(decode=True));
    HTML parts are tag-stripped and entity-unescaped. Whitespace-normalized."""
    chunks = []
    for part in msg.walk():
        ctype = part.get_content_type()
        if not ctype.startswith("text/"):
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if ctype == "text/html":
            text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        chunks.append(text)
    return _norm(" ".join(chunks))


# ── ownCloud Checks ──────────────────────────────────────────────────────────

def check_1_oc_user() -> None:
    """User diego.martinez exists with email and in group admin."""
    try:
        user_row = oc_db("SELECT uid FROM oc_users WHERE uid = 'diego.martinez';")
        user_exists = "diego.martinez" in user_row

        # Email stored in oc_accounts or oc_preferences
        email_row = oc_db(
            "SELECT email FROM oc_accounts WHERE user_id = 'diego.martinez';"
        )
        if not email_row:
            email_row = oc_db(
                "SELECT configvalue FROM oc_preferences "
                "WHERE userid='diego.martinez' AND appid='settings' AND configkey='email';"
            )
        email_ok = "diego.martinez@contractors.local" in (email_row or "")

        group_row = oc_db(
            "SELECT gid FROM oc_group_user "
            "WHERE uid='diego.martinez' AND gid='admin';"
        )
        group_ok = "admin" in (group_row or "")

        passed = user_exists and email_ok and group_ok
        detail = (f"user={'found' if user_exists else 'missing'}, "
                  f"email={'ok' if email_ok else 'missing'}, "
                  f"group={'ok' if group_ok else 'missing'}")
        check("1. ownCloud user diego.martinez", 1, passed, detail)
    except Exception as e:
        check("1. ownCloud user diego.martinez", 1, False, f"exception: {e}")


def check_2_oc_quota() -> None:
    """User storage quota set to 10 GB."""
    try:
        quota = oc_db(
            "SELECT configvalue FROM oc_preferences "
            "WHERE userid='diego.martinez' AND appid='files' AND configkey='quota';"
        )
        passed = False
        if quota:
            q = quota.strip().lower()
            if "10 gb" in q or "10gb" in q:
                passed = True
            try:
                if int(q) == 10737418240:  # 10 * 1024^3
                    passed = True
            except ValueError:
                pass
        check("2. ownCloud user quota 10 GB", 1, passed, f"quota={quota!r}")
    except Exception as e:
        check("2. ownCloud user quota 10 GB", 1, False, f"exception: {e}")


def check_3_oc_folders() -> None:
    """Diego_Martinez_Workspace with Deliverables and Briefs subfolders."""
    try:
        rows = oc_db(
            "SELECT f.path FROM oc_filecache f "
            "WHERE f.path IN ("
            "  'files/Diego_Martinez_Workspace',"
            "  'files/Diego_Martinez_Workspace/Deliverables',"
            "  'files/Diego_Martinez_Workspace/Briefs'"
            ");"
        )
        found = set(r.strip() for r in rows.split('\n') if r.strip()) if rows else set()
        ws = "files/Diego_Martinez_Workspace" in found
        dl = "files/Diego_Martinez_Workspace/Deliverables" in found
        br = "files/Diego_Martinez_Workspace/Briefs" in found

        passed = ws and dl and br
        detail = (f"workspace={'ok' if ws else 'missing'}, "
                  f"deliverables={'ok' if dl else 'missing'}, "
                  f"briefs={'ok' if br else 'missing'}")
        check("3. ownCloud folder structure", 1, passed, detail)
    except Exception as e:
        check("3. ownCloud folder structure", 1, False, f"exception: {e}")


def check_4_oc_brief_content() -> None:
    """Mobile_App_Brief.txt has correct project brief content."""
    try:
        expected = (
            "Project: Mobile App MVP. Goal: Ship a cross-platform iOS/Android MVP "
            "within 12 weeks using React Native. Primary deliverables include "
            "architecture document, authentication module, core feature screens, "
            "and production-ready builds for both stores. Review cadence: bi-weekly "
            "on Tuesdays."
        )
        resp = requests.get(
            f"http://{HOST}:{OC_PORT}/remote.php/dav/files/admin/"
            "Diego_Martinez_Workspace/Briefs/Mobile_App_Brief.txt",
            auth=("admin", "admin"),
            timeout=15,
        )
        if resp.status_code == 200:
            content = resp.text.strip()
            passed = expected in content
            detail = "content matches" if passed else f"got {content[:100]!r}"
        else:
            passed = False
            detail = f"HTTP {resp.status_code}"
        check("4. ownCloud Mobile_App_Brief.txt content", 1, passed, detail)
    except Exception as e:
        check("4. ownCloud Mobile_App_Brief.txt content", 1, False, f"exception: {e}")


def check_5_oc_public_link() -> None:
    """Public link for Briefs folder: read-only, expiry 2026-09-30."""
    try:
        rows = oc_db(
            "SELECT s.permissions, s.expiration "
            "FROM oc_share s "
            "JOIN oc_filecache f ON s.file_source = f.fileid "
            "WHERE s.share_type = 3 "
            "AND f.path LIKE '%Diego_Martinez_Workspace/Briefs';"
        )
        if rows:
            parts = rows.split('\t')
            perms = int(parts[0].strip()) if parts else -1
            expiry = parts[1].strip() if len(parts) > 1 else ""
            read_only = perms == 1 or (perms & 1 and not (perms & 2))
            expiry_ok = "2026-09-30" in expiry
            passed = read_only and expiry_ok
            detail = f"permissions={perms}, expiry={expiry!r}"
        else:
            passed = False
            detail = "no public link found for Briefs"
        check("5. ownCloud public link for Briefs", 1, passed, detail)
    except Exception as e:
        check("5. ownCloud public link for Briefs", 1, False, f"exception: {e}")


def check_6_oc_workspace_shared_diego() -> None:
    """Diego_Martinez_Workspace shared with user diego.martinez read-write."""
    try:
        rows = oc_db(
            "SELECT s.permissions FROM oc_share s "
            "JOIN oc_filecache f ON s.file_source = f.fileid "
            "JOIN oc_storages st ON f.storage = st.numeric_id "
            "WHERE s.share_type = 0 AND s.share_with = 'diego.martinez' "
            "AND st.id = 'home::admin' "
            "AND f.path = 'files/Diego_Martinez_Workspace';"
        )
        perms_seen = []
        passed = False
        for row in (rows or "").splitlines():
            row = row.strip()
            if not row:
                continue
            try:
                perms = int(row)
            except ValueError:
                continue
            perms_seen.append(perms)
            # read-write = read bit + update bit set (UI "read-write" is
            # usually 15/31; test the bits, not the full value)
            if (perms & 1) and (perms & 2):
                passed = True
        detail = (f"permissions={perms_seen}" if perms_seen
                  else "no user share to diego.martinez on files/Diego_Martinez_Workspace")
        check("6. ownCloud workspace shared with diego.martinez (read-write)", 2,
              passed, detail)
    except Exception as e:
        check("6. ownCloud workspace shared with diego.martinez (read-write)", 2,
              False, f"exception: {e}")


# ── OnlyOffice Checks ────────────────────────────────────────────────────────

def check_7_oo_document() -> dict | None:
    """Engagement_Letter_Diego_Martinez exists in My Documents."""
    try:
        base, headers = oo_api_auth()
        resp = requests.get(f"{base}/api/2.0/files/@my", headers=headers, timeout=15)
        data = resp.json()
        files = data.get("response", {}).get("files", [])
        if isinstance(data.get("response"), list):
            files = [f for f in data["response"] if f.get("fileType") is not None or "title" in f]

        found = None
        for f in files:
            title = f.get("title", "")
            if "Engagement_Letter_Diego_Martinez" in title:
                found = f
                break

        passed = found is not None
        detail = f"title={found['title']!r}" if found else "not found in My Documents"
        check("7. OnlyOffice document exists", 1, passed, detail)
        return found
    except Exception as e:
        check("7. OnlyOffice document exists", 1, False, f"exception: {e}")
        return None


def check_7b_oo_document_content() -> None:
    """Engagement letter docx content: heading, sections, 5-row compensation table."""
    label = "7b. OnlyOffice engagement letter content"
    try:
        data, source = _oo_get_docx(ENGAGEMENT_TITLES)
        if not data:
            check(label, 2, False, f"could not read document: {source}")
            return
        doc_xml = _docx_xml(data, "word/document.xml")
        if not doc_xml:
            check(label, 2, False, f"word/document.xml not found (source: {source})")
            return
        text = _wxml_text(doc_xml)

        probes = {
            "heading": "Independent Contractor Engagement Agreement",
            "parties": ("This agreement is entered between Acme Ventures Inc. "
                        "(the Company) and Diego Martinez (the Contractor), "
                        "effective May 4, 2026."),
            "scope": "App Store/Play Store build preparation",
            "confidentiality": "for a period of 5 years beyond termination",
            "term": "terminates on September 30, 2026",
        }
        missing = [k for k, v in probes.items() if _norm(v) not in text]

        # Compensation table: locate the table containing 'Architecture Document',
        # require 6 rows (header + 5 data rows, ±1 tolerance) and per-row anchors.
        row_anchors = [
            ("Architecture Document", "$3,000"),
            ("Authentication Module", "$5,000"),
            ("Core Feature Screens", "$8,000", "50% upfront, 50% on delivery"),
            ("Store Build & Submission", "$3,500"),
            ("Post-Launch QA & Handoff", "$2,000", "Net 15 upon final sign-off"),
        ]
        comp_tbl = None
        for tbl in _tbl_blocks(doc_xml):
            if "Architecture Document" in _wxml_text(tbl):
                comp_tbl = tbl
                break
        if comp_tbl is None:
            missing.append("compensation table (no <w:tbl> containing 'Architecture Document')")
            rows_ok = False
        else:
            trs = _tr_blocks(comp_tbl)
            row_texts = [_wxml_text(tr) for tr in trs]
            rows_ok = 5 <= len(trs) <= 7  # 6 ± 1
            if not rows_ok:
                missing.append(f"table rows={len(trs)} (want 6±1)")
            for anchors in row_anchors:
                if not any(all(a in rt for a in anchors) for rt in row_texts):
                    missing.append(f"table row {anchors[0]!r}")

        passed = not missing
        check(label, 2, passed,
              f"all anchors found (source: {source})" if passed
              else f"missing: {missing} (source: {source})")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_oo_shared(doc_id=None) -> None:
    """Document shared with jun.chen for editing (userName exact, access==1)."""
    label = "8. OnlyOffice shared with jun.chen (edit)"
    try:
        api_verdict: bool | None = None
        api_detail = ""
        if doc_id is not None:
            try:
                base, headers = oo_api_auth()
                resp = requests.get(
                    f"{base}/api/2.0/files/file/{doc_id}/share",
                    headers=headers, timeout=15,
                )
                shares = resp.json().get("response", [])
                api_verdict = False
                for s in shares:
                    user = s.get("sharedTo", {}) or {}
                    uname = user.get("userName", "") or ""
                    access = s.get("access", -1)
                    # access 1 = editing; 0 is FileShare.None, NOT edit
                    if uname == "jun.chen" and access == 1:
                        api_verdict = True
                        break
                api_detail = ("api: jun.chen access==1 found" if api_verdict
                              else "api: no share with userName=='jun.chen' and access==1")
            except Exception as e:
                api_verdict = None
                api_detail = f"api unavailable: {e}"
        else:
            api_detail = "no doc_id from check 7"

        if api_verdict is not None:
            check(label, 2, api_verdict, api_detail)
            return

        # DB fallback (approach A): join core_user on GUID subject, entry_id bound
        # to the target file resolved by exact title, security==1 (edit).
        rows = oo_db(
            "SELECT cu.username, fs.security "
            "FROM files_security fs "
            "JOIN core_user cu ON fs.subject = cu.id "
            "WHERE fs.entry_type = 2 "
            "AND fs.entry_id = (SELECT CAST(id AS CHAR) FROM files_file "
            "  WHERE title IN ('Engagement_Letter_Diego_Martinez',"
            "                  'Engagement_Letter_Diego_Martinez.docx') "
            "  ORDER BY id DESC LIMIT 1) "
            "AND cu.username = 'jun.chen' AND fs.security = 1;"
        )
        passed = bool(rows.strip())
        check(label, 2, passed,
              f"{api_detail}; db fallback: "
              + ("jun.chen security==1 row found" if passed
                 else "no jun.chen/security==1 row on target file"))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_oo_favorite(doc_id=None) -> None:
    """Document marked as favorite."""
    try:
        if doc_id is None:
            check("9. OnlyOffice favorite", 1, False, "no doc_id from check 7")
            return
        base, headers = oo_api_auth()
        resp = requests.get(f"{base}/api/2.0/files/@favorites", headers=headers, timeout=15)
        data = resp.json()

        # Handle different response structures
        response = data.get("response", {})
        if isinstance(response, dict):
            files = response.get("files", [])
        elif isinstance(response, list):
            files = response
        else:
            files = []

        found = any(
            "Engagement_Letter_Diego_Martinez" in (f.get("title", "") if isinstance(f, dict) else "")
            for f in files
        )
        check("9. OnlyOffice favorite", 1, found,
              "in favorites" if found else "not in favorites")
    except Exception as e:
        check("9. OnlyOffice favorite", 1, False, f"exception: {e}")


# ── Mattermost Checks ────────────────────────────────────────────────────────

def check_10_mm_karrie_member() -> None:
    """karrie is a member of Brand Design channel (Marketing & Growth team)."""
    try:
        row = mm_db(
            "SELECT u.username FROM channelmembers cm "
            "JOIN channels c ON cm.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "JOIN users u ON cm.userid = u.id "
            "WHERE c.displayname = 'Brand Design' "
            "AND t.displayname = 'Marketing & Growth' "
            "AND u.username = 'karrie';"
        )
        passed = "karrie" in (row or "")
        check("10. Mattermost karrie in Brand Design", 1, passed,
              "member" if passed else "not found (Brand Design @ Marketing & Growth)")
    except Exception as e:
        check("10. Mattermost karrie in Brand Design", 1, False, f"exception: {e}")


def check_11_mm_intro_message() -> None:
    """Full intro message posted in Brand Design channel (Marketing & Growth)."""
    try:
        expected = (
            "Team, please welcome Diego Martinez, our new freelance mobile developer "
            "joining us for the Mobile App MVP engagement through September 30. "
            "Diego will be partnering with brand and marketing on in-app visual "
            "assets and launch collateral — please loop him in on relevant "
            "brand reviews."
        )
        row = mm_db(
            "SELECT p.id FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.displayname = 'Brand Design' "
            "AND t.displayname = 'Marketing & Growth' "
            f"AND p.message LIKE '%{expected}%' "
            "AND p.deleteat = 0 LIMIT 1;"
        )
        passed = bool(row and row.strip())
        check("11. Mattermost intro message", 2, passed,
              "full-text anchor found" if passed else "full intro message not found")
    except Exception as e:
        check("11. Mattermost intro message", 2, False, f"exception: {e}")


def check_12_mm_dm_karrie() -> None:
    """DM from admin to karrie with workspace access details + public link."""
    try:
        row = mm_db(
            "SELECT p.id FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN channelmembers cm ON cm.channelid = c.id "
            " AND cm.userid = (SELECT id FROM users WHERE username = 'karrie') "
            "WHERE c.type = 'D' AND p.deleteat = 0 "
            "AND p.userid = (SELECT id FROM users WHERE username = 'admin') "
            "AND p.message LIKE '%Hi Diego! Welcome aboard.%' "
            "AND p.message LIKE '%Diego\\_Martinez\\_Workspace (read-write)%' "
            "AND p.message LIKE '%https://owncloud.local/s/briefs-diego-2026%' "
            "AND p.message LIKE '%Engagement\\_Letter\\_Diego\\_Martinez%' "
            "LIMIT 1;"
        )
        passed = bool(row and row.strip())
        check("12. Mattermost DM to karrie", 2, passed,
              "DM with all anchors found" if passed
              else "no admin->karrie DM with all 4 anchors")
    except Exception as e:
        check("12. Mattermost DM to karrie", 2, False, f"exception: {e}")


def check_13_mm_channel_header() -> None:
    """Brand Design channel header equals the full expected string."""
    try:
        row = mm_db(
            "SELECT c.header FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.displayname = 'Brand Design' "
            "AND t.displayname = 'Marketing & Growth' LIMIT 1;"
        )
        expected = (
            "Brand assets, style guides, and design feedback. "
            "Contractor onboarding active through 2026-09-30 — welcome "
            "Diego Martinez (Mobile App MVP)."
        )
        passed = (row or "").strip() == expected
        check("13. Mattermost channel header", 1, passed,
              "matches full string" if passed else f"got {(row or '')[:120]!r}")
    except Exception as e:
        check("13. Mattermost channel header", 1, False, f"exception: {e}")


# ── Roundcube Checks ─────────────────────────────────────────────────────────

def check_14_rc_identity() -> None:
    """Identity: exact name/org, html_signature=1, signature anchors."""
    try:
        # Apostrophe in "O'Brien" doubled for SQL; em dash is UTF-8 raw
        # (helper runs with --default-character-set=utf8mb4).
        count = rc_db(
            "SELECT COUNT(*) FROM identities "
            "WHERE email = 'sarah.obrien@mail.local' "
            "AND name = 'Sarah O''Brien — Co-Founder' "
            "AND organization = 'Acme Ventures Inc.' "
            "AND html_signature = 1 "
            "AND signature LIKE '%<strong>Sarah O''Brien</strong>%' "
            "AND signature LIKE '%mailto:sarah.obrien@mail.local%';"
        )
        passed = count.strip().isdigit() and int(count.strip()) > 0
        if passed:
            detail = "identity matches name/org/html_signature/signature anchors"
        else:
            diag = rc_db(
                "SELECT name, organization, html_signature FROM identities "
                "WHERE email = 'sarah.obrien@mail.local';"
            )
            detail = (f"no fully-matching identity; rows for address: {diag[:150]!r}"
                      if diag else "identity not found for sarah.obrien@mail.local")
        check("14. Roundcube identity", 2, passed, detail)
    except Exception as e:
        check("14. Roundcube identity", 2, False, f"exception: {e}")


def check_14b_rc_htmleditor_pref() -> None:
    """htmleditor preference explicitly set (image default is 0, so an explicit
    setting persists in users.preferences)."""
    label = "14b. Roundcube htmleditor preference"
    try:
        prefs = rc_db(
            "SELECT preferences FROM users "
            "WHERE username = 'james.whitfield@mail.local';"
        )
        m = re.search(
            r'"htmleditor"\s*;\s*(?:s:\d+:"(-?\d+)"|i:(-?\d+)|b:([01]))',
            prefs or "",
        )
        if not m:
            m = re.search(r'"htmleditor"\s*:\s*"?(-?\d+)"?', prefs or "")
            value = m.group(1) if m else None
        else:
            value = next(g for g in m.groups() if g is not None)
        # Deviation note: the exact enum for 'on reply to HTML message'
        # (commonly 2) was not confirmed live; require the key explicitly
        # present with a non-default (non-0) value and record the value.
        passed = value is not None and value != "0"
        if value is None:
            detail = "htmleditor pref not present in users.preferences"
        else:
            detail = (f"htmleditor={value} (explicit non-default; expected enum for "
                      "'on reply to HTML message' commonly 2, not confirmed live)")
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_15_rc_email_sent() -> None:
    """Onboarding email in james.whitfield's Sent copy: exact subject, To/Cc
    set equality, From identity, decoded body probes. (No mail-log fallback:
    log presence != delivery.)"""
    label = "15. Roundcube onboarding email sent"
    try:
        expected_subject = "Welcome to Acme Ventures — Mobile App MVP Onboarding"
        expected_to = {"diego.martinez@contractors.local"}
        expected_cc = {"ops@acmeventures.local"}
        body_probes = [
            "Term: May 4, 2026 – September 30, 2026",
            "https://owncloud.local/s/briefs-diego-2026",
            "countersign the engagement letter",
        ]

        msgs = rc_sent_messages("james.whitfield")
        if not msgs:
            check(label, 2, False,
                  "no messages in /var/mail/mail.local/james.whitfield/.Sent/{cur,new}")
            return

        best_detail = ""
        best_score = -1
        passed = False
        subjects_seen = []
        for _path, msg in msgs:
            subj = rc_decoded_header(msg, "Subject")
            subjects_seen.append(subj)
            if subj != expected_subject:
                continue
            to_ok = rc_addr_set(msg, "To") == expected_to
            cc_ok = rc_addr_set(msg, "Cc") == expected_cc
            display, from_addr = rc_from_display_and_addr(msg)
            from_ok = from_addr == "sarah.obrien@mail.local" and "Co-Founder" in display
            body = rc_body_text(msg)
            missing_probes = [p for p in body_probes if _norm(p) not in body]
            body_ok = not missing_probes

            score = sum((to_ok, cc_ok, from_ok, body_ok))
            if score > best_score:
                best_score = score
                best_detail = (f"to={'ok' if to_ok else sorted(rc_addr_set(msg, 'To'))}, "
                               f"cc={'ok' if cc_ok else sorted(rc_addr_set(msg, 'Cc'))}, "
                               f"from={'ok' if from_ok else (display, from_addr)!r}, "
                               f"body={'ok' if body_ok else 'missing ' + repr(missing_probes)}")
            if to_ok and cc_ok and from_ok and body_ok:
                passed = True
                break

        if best_score < 0:
            best_detail = (f"no Sent message with exact subject {expected_subject!r}; "
                           f"subjects seen: {subjects_seen[:5]!r}")
        check(label, 2, passed, best_detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15b_dsn_note() -> None:
    """DSN request: not judged (0 weight)."""
    check("15b. DSN (delivery status notification) requested", 0, True,
          "not judged: DSN travels as SMTP NOTIFY params; no Sent-copy or "
          "header trace exists (0 weight by design)")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # ownCloud (6 checks)
    check_1_oc_user()
    check_2_oc_quota()
    check_3_oc_folders()
    check_4_oc_brief_content()
    check_5_oc_public_link()
    check_6_oc_workspace_shared_diego()

    # OnlyOffice (4 checks)
    doc = check_7_oo_document()
    doc_id = doc.get("id") if doc else None
    check_7b_oo_document_content()
    check_8_oo_shared(doc_id)
    check_9_oo_favorite(doc_id)

    # Mattermost (4 checks)
    check_10_mm_karrie_member()
    check_11_mm_intro_message()
    check_12_mm_dm_karrie()
    check_13_mm_channel_header()

    # Roundcube (3 checks + 0pt DSN note)
    check_14_rc_identity()
    check_14b_rc_htmleditor_pref()
    check_15_rc_email_sent()
    check_15b_dsn_note()

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
