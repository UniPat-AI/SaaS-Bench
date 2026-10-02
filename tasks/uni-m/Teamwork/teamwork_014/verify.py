"""
Verifier for Teamwork-014-I3: Client onboarding package across OnlyOffice, ownCloud, Mattermost, Roundcube.

Checks: 16 weighted checks across 4 sites (total weight 21).
Strategy: DB queries (OnlyOffice MySQL, Mattermost Postgres, Roundcube MariaDB),
          REST/WebDAV API (ownCloud, incl. public-link behavior probe),
          docx content probes (OnlyOffice data dir via docker exec + API download fallback),
          Sent-maildir header/body parsing (Roundcube, python email stdlib).

Required env vars:
  SERVER_HOSTNAME,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER.
"""

import email
import email.utils
import io
import os
import re
import subprocess
import sys
import zipfile
from email.header import decode_header, make_header

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

OWNCLOUD_PORT = os.environ.get("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = os.environ.get("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = os.environ.get("OWNCLOUD_DB_CONTAINER")

MATTERMOST_PORT = os.environ.get("MATTERMOST_PORT")
MATTERMOST_CONTAINER = os.environ.get("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = os.environ.get("MATTERMOST_DB_CONTAINER")

ROUNDCUBEMAIL_PORT = os.environ.get("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = os.environ.get("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = os.environ.get("ROUNDCUBEMAIL_DB_CONTAINER")

_required = {
    "ONLYOFFICE_PORT": ONLYOFFICE_PORT,
    "ONLYOFFICE_CONTAINER": ONLYOFFICE_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": ONLYOFFICE_DB_CONTAINER,
    "OWNCLOUD_PORT": OWNCLOUD_PORT,
    "OWNCLOUD_CONTAINER": OWNCLOUD_CONTAINER,
    "OWNCLOUD_DB_CONTAINER": OWNCLOUD_DB_CONTAINER,
    "MATTERMOST_PORT": MATTERMOST_PORT,
    "MATTERMOST_CONTAINER": MATTERMOST_CONTAINER,
    "MATTERMOST_DB_CONTAINER": MATTERMOST_DB_CONTAINER,
    "ROUNDCUBEMAIL_PORT": ROUNDCUBEMAIL_PORT,
    "ROUNDCUBEMAIL_CONTAINER": ROUNDCUBEMAIL_CONTAINER,
    "ROUNDCUBEMAIL_DB_CONTAINER": ROUNDCUBEMAIL_DB_CONTAINER,
}
for var_name, var_val in _required.items():
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
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


def docker_exec_bytes(container: str, *args: str, timeout: int = 30) -> bytes:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, timeout=timeout,
    )
    return r.stdout if r.returncode == 0 else b""


def mysql_query(container: str, db: str, user: str, password: str, sql: str) -> str:
    # -h 127.0.0.1 for the ownCloud MariaDB: its anonymous ''@'localhost' users
    # shadow 'owncloud'@'%' over the unix socket.
    host_args = ["-h", "127.0.0.1"] if container == OWNCLOUD_DB_CONTAINER else []
    rc, out, err = docker_exec(
        container, "mysql", *host_args, f"-u{user}", f"-p{password}",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql, db,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def pg_query(container: str, db: str, user: str, sql: str) -> str:
    rc, out, err = docker_exec(
        container, "psql", "-U", user, "-d", db, "-t", "-A", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def _norm(s: str) -> str:
    """Normalize whitespace for text comparisons."""
    return re.sub(r"\s+", " ", s).strip()


# ── OnlyOffice docx probe helpers ─────────────────────────────────────────────
def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for ent, ch in (("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&apos;", "'"), ("&amp;", "&")):
        s = s.replace(ent, ch)
    return s


def _oo_auth_token() -> str | None:
    try:
        r = requests.post(
            f"http://{HOST}:{ONLYOFFICE_PORT}/api/2.0/authentication",
            json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
            timeout=15,
        )
        return r.json().get("response", {}).get("token")
    except Exception:
        return None


def _oo_docx_fetch(title: str) -> tuple[bytes | None, str]:
    """Fetch the docx bytes for the given exact title (with/without .docx).

    Chain: files_file latest id -> data-dir content.docx (highest version dir)
    via docker exec -> API download fallback."""
    notes: list[str] = []
    fid = None
    try:
        out = mysql_query(
            ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass",
            f"SELECT id FROM files_file WHERE title IN ('{title}', '{title}.docx') "
            "ORDER BY id DESC LIMIT 1;",
        )
        if out.strip():
            fid = out.strip().splitlines()[0].strip()
    except Exception as e:
        notes.append(f"db id lookup failed: {e}")

    if fid:
        try:
            rc, out, _ = docker_exec(
                ONLYOFFICE_CONTAINER, "bash", "-c",
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
                data = docker_exec_bytes(ONLYOFFICE_CONTAINER, "cat", path, timeout=30)
                if data[:2] == b"PK":
                    return data, f"fs {path}"
                notes.append("fs cat failed or not a zip")
            else:
                notes.append("fs content.docx not found")
        except Exception as e:
            notes.append(f"fs read failed: {e}")

    # API download fallback
    try:
        token = _oo_auth_token()
        if not token:
            notes.append("api auth failed")
            return None, "; ".join(notes)
        base = f"http://{HOST}:{ONLYOFFICE_PORT}"
        if not fid:
            r = requests.get(f"{base}/api/2.0/files/@my",
                             headers={"Authorization": token}, timeout=15)
            for f in r.json().get("response", {}).get("files", []):
                t = f.get("title", "")
                if t == title or t == f"{title}.docx":
                    fid = str(f.get("id"))
                    break
        if not fid:
            notes.append("file id not found via api")
            return None, "; ".join(notes)
        r = requests.get(f"{base}/api/2.0/files/file/{fid}/download",
                         headers={"Authorization": token}, timeout=30,
                         allow_redirects=True)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content, "api download"
        r = requests.get(f"{base}/products/files/httphandlers/filehandler.ashx",
                         params={"action": "download", "fileid": fid},
                         cookies={"asc_auth_key": token}, timeout=30,
                         allow_redirects=True)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content, "api filehandler download"
        notes.append(f"api download failed (HTTP {r.status_code})")
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


def _docx_table_row_counts(xml: str) -> list[int]:
    """Per-table <w:tr> counts, in document order."""
    counts: list[int] = []
    for seg in re.split(r"<w:tbl[ >]", xml)[1:]:
        body = seg.split("</w:tbl>")[0]
        counts.append(len(re.findall(r"<w:tr[ >]", body)))
    return counts


# ── Roundcube Sent-maildir helpers (approach C) ───────────────────────────────
SENDER_LOCALPART = "james.whitfield"  # roundcube login: james.whitfield@mail.local


def _sent_candidate_paths() -> list[str]:
    """Candidate mail files: ONLY the sender's Sent Maildir (cur + new)."""
    cmd = (
        f"find /var/mail/mail.local/{SENDER_LOCALPART}/.Sent/cur "
        f"/var/mail/mail.local/{SENDER_LOCALPART}/.Sent/new "
        "-type f 2>/dev/null || true"
    )
    rc, out, _ = docker_exec(ROUNDCUBEMAIL_CONTAINER, "bash", "-c", cmd, timeout=20)
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


def _body_text(msg) -> str:
    """Decoded body of all text/* parts (handles quoted-printable / base64)."""
    chunks: list[str] = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, "replace"))
    return "\n".join(chunks)


def _find_sent_message(subject: str):
    """Return (Message, path) for the Sent copy whose decoded Subject matches exactly."""
    want = _norm(subject)
    for path in _sent_candidate_paths():
        raw = docker_exec_bytes(ROUNDCUBEMAIL_CONTAINER, "cat", path, timeout=15)
        if not raw:
            continue
        try:
            msg = email.message_from_bytes(raw)
        except Exception:
            continue
        if _hdr_decoded(msg, "Subject") == want:
            return msg, path
    return None, ""


# ── OnlyOffice checks ────────────────────────────────────────────────────────
def check_1_welcome_letter_exists():
    """Check that 'Meridian Biotech - Welcome Letter' exists in OnlyOffice files."""
    try:
        # OnlyOffice stores titles WITH the file extension
        sql = ("SELECT id, title FROM files_file WHERE title IN "
               "('Meridian Biotech - Welcome Letter', 'Meridian Biotech - Welcome Letter.docx');")
        out = mysql_query(ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass", sql)
        found = "Meridian Biotech - Welcome Letter" in out
        check("1. OnlyOffice: Welcome Letter document exists", 1, found,
              f"found={found}, query_result={out[:200]}")
    except Exception as e:
        check("1. OnlyOffice: Welcome Letter document exists", 1, False, f"exception: {e}")


def check_1b_welcome_letter_content():
    """Docx content probes for the Welcome Letter (approach B)."""
    label = "1b. OnlyOffice: Welcome Letter content (docx probes)"
    try:
        data, source = _oo_docx_fetch("Meridian Biotech - Welcome Letter")
        if not data:
            check(label, 2, False, f"could not fetch docx: {source}")
            return
        xml = _docx_document_xml(data)
        if xml is None:
            check(label, 2, False, f"word/document.xml unreadable (source: {source})")
            return
        text = _docx_plain_text(xml)
        probes = [
            "Welcome to the Meridian Biotech Journey",
            "Dear Dr. Eleanor Chadwick,",
            "rigorous data governance",
            "laura.brown@onlyoffice.local",
            "amit.singh@onlyoffice.local",
            "jun.chen@onlyoffice.local",
            "maria.wilson@onlyoffice.local",
            "alice@onlyoffice.com",
            "Customer Success Lead",
            "Sincerely and with excitement for what lies ahead",
        ]
        missing = [p for p in probes if _norm(p) not in text]
        rows = _docx_table_row_counts(xml)
        table_ok = any(5 <= r <= 7 for r in rows)  # 6 rows expected, header ±1 tolerance
        passed = (not missing) and table_ok
        check(label, 2, passed,
              f"missing_probes={missing[:4]}, table_row_counts={rows}, "
              f"table_6rows_pm1={table_ok}, source={source}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_service_agreement_exists():
    """Check that 'Meridian Biotech - Service Agreement' exists in OnlyOffice files."""
    try:
        # OnlyOffice stores titles WITH the file extension
        sql = ("SELECT id, title FROM files_file WHERE title IN "
               "('Meridian Biotech - Service Agreement', 'Meridian Biotech - Service Agreement.docx');")
        out = mysql_query(ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass", sql)
        found = "Meridian Biotech - Service Agreement" in out
        check("2. OnlyOffice: Service Agreement document exists", 1, found,
              f"found={found}, query_result={out[:200]}")
    except Exception as e:
        check("2. OnlyOffice: Service Agreement document exists", 1, False, f"exception: {e}")


def check_2b_service_agreement_content():
    """Docx content probes for the Service Agreement (approach B)."""
    label = "2b. OnlyOffice: Service Agreement content (docx probes)"
    try:
        data, source = _oo_docx_fetch("Meridian Biotech - Service Agreement")
        if not data:
            check(label, 1, False, f"could not fetch docx: {source}")
            return
        xml = _docx_document_xml(data)
        if xml is None:
            check(label, 1, False, f"word/document.xml unreadable (source: {source})")
            return
        text = _docx_plain_text(xml)
        probes = [
            "Professional Services Agreement",
            "HIPAA-aligned integration",
            "36 months commencing",
            "billed in advance",
            "99.95% platform availability",
            "applicable privacy laws",
            "Pre-existing IP remains with the originating party",
            "uncured material breach upon 30 days",
            "intending to be legally bound",
        ]
        missing = [p for p in probes if _norm(p) not in text]
        check(label, 1, not missing,
              f"missing_probes={missing[:4]}, source={source}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_3_service_agreement_favorite():
    """Check that Service Agreement is marked as favorite."""
    try:
        # files_tag / files_tag_link or a flag column -- try checking tag/favorite status
        # OnlyOffice uses files_tag with tag_name='favorite' linked via files_tag_link
        # entry_type = 2 is a file in files_tag_link (1 = folder);
        # titles are stored WITH the file extension.
        sql = (
            "SELECT f.title FROM files_file f "
            "INNER JOIN files_tag_link tl ON tl.entry_id = f.id AND tl.entry_type = 2 "
            "INNER JOIN files_tag t ON t.id = tl.tag_id AND t.name = 'favorite' "
            "WHERE f.title IN ('Meridian Biotech - Service Agreement', "
            "'Meridian Biotech - Service Agreement.docx');"
        )
        out = mysql_query(ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass", sql)
        found = "Meridian Biotech - Service Agreement" in out
        if not found:
            # Fallback: try flag column approach
            sql2 = (
                "SELECT f.title FROM files_file f "
                "INNER JOIN files_tag_link tl ON tl.entry_id = CAST(f.id AS CHAR) "
                "INNER JOIN files_tag t ON t.id = tl.tag_id "
                "WHERE f.title IN ('Meridian Biotech - Service Agreement', "
                "'Meridian Biotech - Service Agreement.docx') AND t.name = 'favorite';"
            )
            out2 = mysql_query(ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass", sql2)
            found = "Meridian Biotech - Service Agreement" in out2
        check("3. OnlyOffice: Service Agreement marked as favorite", 1, found,
              f"found={found}")
    except Exception as e:
        check("3. OnlyOffice: Service Agreement marked as favorite", 1, False, f"exception: {e}")


# ── ownCloud checks (WebDAV + OCS API) ───────────────────────────────────────
def _oc_base():
    return f"http://{HOST}:{OWNCLOUD_PORT}"


def check_4_owncloud_folder_structure():
    """Check Meridian-Biotech-Onboarding folder with Client-Messages and Signed-Agreements subfolders."""
    try:
        base = _oc_base()
        auth = ("admin", "admin")
        # PROPFIND on the main folder
        url = f"{base}/remote.php/dav/files/admin/Meridian-Biotech-Onboarding/"
        r = requests.request("PROPFIND", url, auth=auth, headers={"Depth": "1"}, timeout=15)
        body = r.text
        has_main = r.status_code in (207, 200)
        has_client_messages = "Client-Messages" in body
        has_signed_agreements = "Signed-Agreements" in body
        all_ok = has_main and has_client_messages and has_signed_agreements
        check("4. ownCloud: Folder structure (main + 2 subfolders)", 1, all_ok,
              f"main={has_main}, Client-Messages={has_client_messages}, Signed-Agreements={has_signed_agreements}")
    except Exception as e:
        check("4. ownCloud: Folder structure (main + 2 subfolders)", 1, False, f"exception: {e}")


def check_5_onboarding_plan_txt():
    """Check meridian-onboarding-plan.txt exists with correct content."""
    try:
        base = _oc_base()
        auth = ("admin", "admin")
        url = f"{base}/remote.php/dav/files/admin/Meridian-Biotech-Onboarding/meridian-onboarding-plan.txt"
        r = requests.get(url, auth=auth, timeout=15)
        if r.status_code != 200:
            check("5. ownCloud: meridian-onboarding-plan.txt content", 1, False,
                  f"HTTP {r.status_code}")
            return
        content = r.text
        expected_lines = [
            "Meridian Biotech Onboarding Plan:",
            "Kickoff workshop scheduled",
            "Welcome letter distributed",
            "Service agreement executed",
            "Lab systems integration scoped",
            "Validation protocol draft circulated",
            "Production cutover date confirmed",
            "60-day post-launch review booked",
        ]
        missing = [ln for ln in expected_lines if ln not in content]
        check("5. ownCloud: meridian-onboarding-plan.txt content", 1, len(missing) == 0,
              f"missing_lines={missing}" if missing else "all lines present")
    except Exception as e:
        check("5. ownCloud: meridian-onboarding-plan.txt content", 1, False, f"exception: {e}")


_share_token: str | None = None


def check_6_public_share_client_messages():
    """Check Client-Messages has a public share link with password and read-only permissions."""
    global _share_token
    try:
        base = _oc_base()
        auth = ("admin", "admin")
        url = f"{base}/ocs/v2.php/apps/files_sharing/api/v1/shares?format=json"
        r = requests.get(url, auth=auth, headers={"OCS-APIREQUEST": "true"}, timeout=15)
        data = r.json()
        shares = data.get("ocs", {}).get("data", []) or []
        # Public link shares (share_type 3) on Client-Messages
        cands = [s for s in shares
                 if s.get("share_type") == 3 and "Client-Messages" in (s.get("path") or "")]
        found_public = bool(cands)
        best = None
        perms_seen = []
        for s in cands:
            has_pw = bool(s.get("share_with") or s.get("share_with_displayname"))
            try:
                perms = int(s.get("permissions", -1))
            except (TypeError, ValueError):
                perms = -1
            perms_seen.append(perms)
            if has_pw and perms == 1:
                best = s
                break
        if best is None and cands:
            best = cands[0]
        if best is not None and best.get("token"):
            _share_token = str(best.get("token"))
        has_password = bool(best and (best.get("share_with") or best.get("share_with_displayname")))
        perms_ok = False
        if best is not None:
            try:
                perms_ok = int(best.get("permissions", -1)) == 1
            except (TypeError, ValueError):
                perms_ok = False
        passed = found_public and has_password and perms_ok
        check("6. ownCloud: Client-Messages public share with password + read-only", 2, passed,
              f"public_link={found_public}, password_set={has_password}, "
              f"permissions={perms_seen} expected 1=read-only")
    except Exception as e:
        check("6. ownCloud: Client-Messages public share with password + read-only", 2, False,
              f"exception: {e}")


def check_6b_public_link_password_behavior():
    """Behavior probe: public link accepts 'Meridian$Bio2026' and rejects a wrong password."""
    label = "6b. ownCloud: public link password behavior"
    try:
        token = _share_token
        if not token:
            # DB fallback for the link token
            try:
                out = mysql_query(
                    OWNCLOUD_DB_CONTAINER, "owncloud", "owncloud", "owncloud",
                    "SELECT token FROM oc_share WHERE share_type = 3 "
                    "AND file_target LIKE '%Client-Messages%' ORDER BY id DESC LIMIT 1;",
                )
                if out.strip():
                    token = out.strip().splitlines()[0].strip()
            except Exception:
                token = None
        if not token:
            check(label, 1, False, "no public link token found via OCS or oc_share")
            return
        url = f"{_oc_base()}/public.php/webdav/"
        r_ok = requests.get(url, auth=(token, "Meridian$Bio2026"),
                            timeout=15, allow_redirects=True)
        r_bad = requests.get(url, auth=(token, "definitely-wrong-password"),
                             timeout=15, allow_redirects=True)
        good = r_ok.status_code in (200, 207)
        bad = r_bad.status_code == 401
        check(label, 1, good and bad,
              f"correct_pw HTTP {r_ok.status_code} want 200/207; "
              f"wrong_pw HTTP {r_bad.status_code} want 401")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Mattermost checks (DB) ───────────────────────────────────────────────────
KICKOFF_MSG = (
    "Kicking off the Meridian Biotech client channel! Onboarding is officially "
    "underway — please review the pinned welcome letter and agreement, and "
    "introduce yourselves so the client knows their dedicated team."
)


def check_8_mm_channel_exists():
    """Check client-meridian-biotech channel exists in Product & Design with correct purpose."""
    try:
        sql = (
            "SELECT c.name, c.purpose, c.type FROM channels c "
            "INNER JOIN teams t ON t.id = c.teamid "
            "WHERE c.name = 'client-meridian-biotech' "
            "AND t.displayname = 'Product & Design';"
        )
        out = pg_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", sql)
        if not out:
            check("8. Mattermost: Channel exists in Product & Design", 1, False, "channel not found")
            return
        parts = out.split("|")
        channel_name = parts[0].strip() if len(parts) > 0 else ""
        purpose = parts[1].strip() if len(parts) > 1 else ""
        ch_type = parts[2].strip() if len(parts) > 2 else ""
        expected_purpose = "Dedicated workspace for coordinating the Meridian Biotech onboarding program, product alignment, and ongoing partnership updates."
        name_ok = channel_name == "client-meridian-biotech"
        purpose_ok = expected_purpose in purpose
        type_ok = ch_type == "O"  # O = open/public
        passed = name_ok and purpose_ok and type_ok
        check("8. Mattermost: Channel exists in Product & Design", 1, passed,
              f"name={name_ok}, purpose={purpose_ok}, public={type_ok}")
    except Exception as e:
        check("8. Mattermost: Channel exists in Product & Design", 1, False, f"exception: {e}")


def check_9_mm_kickoff_message():
    """Check kickoff message posted in channel (full-text anchor)."""
    try:
        sql = (
            "SELECT p.id FROM posts p "
            "INNER JOIN channels c ON c.id = p.channelid "
            "INNER JOIN teams t ON t.id = c.teamid "
            "WHERE c.name = 'client-meridian-biotech' "
            "AND t.displayname = 'Product & Design' "
            f"AND p.message LIKE '%{KICKOFF_MSG}%' "
            "AND p.deleteat = 0 "
            "LIMIT 1;"
        )
        out = pg_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", sql)
        found = bool(out.strip())
        check("9. Mattermost: Kickoff message posted (full text)", 1, found,
              f"found={found}")
    except Exception as e:
        check("9. Mattermost: Kickoff message posted (full text)", 1, False, f"exception: {e}")


def check_10_mm_thread_reply():
    """Check a single threaded reply bound to the kickoff post carries all 6 action items."""
    try:
        # Find the kickoff post id first (bound to channel + team)
        sql_root = (
            "SELECT p.id FROM posts p "
            "INNER JOIN channels c ON c.id = p.channelid "
            "INNER JOIN teams t ON t.id = c.teamid "
            "WHERE c.name = 'client-meridian-biotech' "
            "AND t.displayname = 'Product & Design' "
            "AND p.message LIKE '%Kicking off the Meridian Biotech client channel%' "
            "AND p.deleteat = 0 "
            "LIMIT 1;"
        )
        root_id = pg_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", sql_root).strip()
        if not root_id:
            check("10. Mattermost: Thread reply with all 6 action items", 2, False, "root post not found")
            return
        items = [
            "read the welcome letter",
            "countersign the service agreement",
            "access the Meridian ownCloud workspace",
            "RSVP for the kickoff workshop",
            "import the Meridian contacts into your Roundcube address book",
            "subscribe to this channel for updates",
        ]
        # Per-row judging: a single reply row must contain every item (no cross-row assembly)
        conds = " ".join(f"AND message LIKE '%{it}%'" for it in items)
        sql_reply = (
            "SELECT COUNT(*) FROM posts "
            f"WHERE rootid = '{root_id}' "
            "AND deleteat = 0 "
            "AND message LIKE '%Action items%' "
            f"{conds};"
        )
        out = pg_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", sql_reply)
        n = int(out.strip() or "0")
        check("10. Mattermost: Thread reply with all 6 action items", 2, n >= 1,
              f"single-row replies matching all 6 items under kickoff thread: {n}")
    except Exception as e:
        check("10. Mattermost: Thread reply with all 6 action items", 2, False, f"exception: {e}")


def check_11_mm_handshake_reaction():
    """Check handshake reaction on kickoff message."""
    try:
        sql = (
            "SELECT r.emojiname FROM reactions r "
            "INNER JOIN posts p ON p.id = r.postid "
            "INNER JOIN channels c ON c.id = p.channelid "
            "INNER JOIN teams t ON t.id = c.teamid "
            "WHERE c.name = 'client-meridian-biotech' "
            "AND t.displayname = 'Product & Design' "
            "AND p.message LIKE '%Kicking off the Meridian Biotech client channel%' "
            "AND r.emojiname = 'handshake' "
            "AND p.deleteat = 0 LIMIT 1;"
        )
        out = pg_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", sql)
        found = "handshake" in out
        check("11. Mattermost: Handshake reaction on kickoff message", 1, found,
              f"found={found}")
    except Exception as e:
        check("11. Mattermost: Handshake reaction on kickoff message", 1, False, f"exception: {e}")


# ── Roundcube checks (DB + mail) ─────────────────────────────────────────────
EXPECTED_CONTACTS = [
    ("eleanor.chadwick@meridianbiotech.com", "Eleanor", "Chadwick"),
    ("rajiv.venkatraman@meridianbiotech.com", "Rajiv", "Venkatraman"),
    ("astrid.lindqvist@meridianbiotech.com", "Astrid", "Lindqvist"),
    ("omar.elsayed@meridianbiotech.com", "Omar", "El-Sayed"),
    ("nora.whitfield@meridianbiotech.com", "Nora", "Whitfield"),
]
MERIDIAN_EMAILS = {em for em, _, _ in EXPECTED_CONTACTS}


def check_12_roundcube_contacts():
    """Check 5 contacts (email + firstname/surname) owned by james.whitfield@mail.local."""
    try:
        sql = (
            "SELECT c.email, c.firstname, c.surname FROM contacts c "
            "JOIN users u ON c.user_id = u.user_id "
            "WHERE u.username = 'james.whitfield@mail.local' AND c.del = 0;"
        )
        out = mysql_query(ROUNDCUBEMAIL_DB_CONTAINER, "roundcubemail", "roundcube", "roundcube123", sql)
        rows: list[tuple[list[str], str, str]] = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                emails = [e.strip().lower() for e in parts[0].split(",") if e.strip()]
                rows.append((emails, parts[1].strip(), parts[2].strip()))
        missing = []
        for em, fn, sn in EXPECTED_CONTACTS:
            if not any(em in r_emails and r_fn == fn and r_sn == sn
                       for r_emails, r_fn, r_sn in rows):
                missing.append(f"{fn} {sn} <{em}>")
        check("12. Roundcube: 5 Meridian contacts in james.whitfield's address book", 2,
              not missing,
              f"missing/mismatched: {missing}" if missing else "all 5 contacts with correct names")
    except Exception as e:
        check("12. Roundcube: 5 Meridian contacts in james.whitfield's address book", 2, False,
              f"exception: {e}")


def check_13_roundcube_contact_group():
    """Check contact group 'Meridian Biotech Contacts' has exactly the 5 Meridian members."""
    try:
        sql = (
            "SELECT c.email FROM contactgroups g "
            "JOIN users u ON g.user_id = u.user_id "
            "AND u.username = 'james.whitfield@mail.local' "
            "JOIN contactgroupmembers m ON m.contactgroup_id = g.contactgroup_id "
            "JOIN contacts c ON c.contact_id = m.contact_id AND c.del = 0 "
            "WHERE g.name = 'Meridian Biotech Contacts' AND g.del = 0;"
        )
        out = mysql_query(ROUNDCUBEMAIL_DB_CONTAINER, "roundcubemail", "roundcube", "roundcube123", sql)
        member_emails: set[str] = set()
        for line in out.splitlines():
            for e in line.split("\t")[0].split(","):
                e = e.strip().lower()
                if e:
                    member_emails.add(e)
        passed = member_emails == MERIDIAN_EMAILS
        check("13. Roundcube: group members == 5 Meridian contacts", 1, passed,
              f"member_emails={sorted(member_emails)}")
    except Exception as e:
        check("13. Roundcube: group members == 5 Meridian contacts", 1, False, f"exception: {e}")


def check_14_roundcube_email_sent():
    """Sent mail: exact subject, To == 5 Meridian addresses, X-Priority 4 (Low), body probes."""
    label = "14. Roundcube: onboarding email (recipients, Low priority, body)"
    try:
        subject = "Welcome to Meridian Biotech Onboarding - Your Partnership Package Inside"
        msg, path = _find_sent_message(subject)
        if msg is None:
            check(label, 2, False,
                  "no mail with exact subject in james.whitfield/.Sent Maildir")
            return
        to_set = _addr_set(msg, "To")
        # Primary form: Roundcube expands the contact group into the 5 addresses.
        # If the group display name was kept instead, this fails as a deviation.
        to_ok = to_set == MERIDIAN_EMAILS
        prio_raw = str(msg.get("X-Priority", "") or "")
        m = re.search(r"\d", prio_raw)
        prio_ok = bool(m) and m.group(0) == "4"
        body_norm = _norm(_body_text(msg))
        probe_link = "Secure Client Messages Folder: https://owncloud.local/s/MeridianClientMessages2026"
        probe_pw = "Meridian$Bio2026"
        body_ok = _norm(probe_link) in body_norm and probe_pw in body_norm
        passed = to_ok and prio_ok and body_ok
        check(label, 2, passed,
              f"to_ok={to_ok} got={sorted(to_set)} want 5 meridianbiotech.com addrs "
              f"group-expanded form; x_priority={prio_raw!r} want '4'=Low; "
              f"body_probes={body_ok}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_welcome_letter_exists()
    check_1b_welcome_letter_content()
    check_2_service_agreement_exists()
    check_2b_service_agreement_content()
    check_3_service_agreement_favorite()
    check_4_owncloud_folder_structure()
    check_5_onboarding_plan_txt()
    check_6_public_share_client_messages()
    check_6b_public_link_password_behavior()
    check_8_mm_channel_exists()
    check_9_mm_kickoff_message()
    check_10_mm_thread_reply()
    check_11_mm_handshake_reaction()
    check_12_roundcube_contacts()
    check_13_roundcube_contact_group()
    check_14_roundcube_email_sent()

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
