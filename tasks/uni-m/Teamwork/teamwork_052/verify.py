"""
Verifier for Teamwork-052-I3: Investigate and Resolve Data Export Client Complaint from Helix Analytics

Checks: 11 weighted checks across roundcubemail, mattermost, owncloud, onlyoffice.
Strategy: docker exec (DB queries) + docker exec (maildir/data dir) + API
          (ownCloud WebDAV/OCS, OnlyOffice REST + docx download fallback)

Required env vars:
  SERVER_HOSTNAME,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import html
import io
import os
import re
import subprocess
import sys
import zipfile

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

def _require(var: str) -> str:
    val = os.getenv(var, "")
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    return val

ROUNDCUBEMAIL_PORT = _require("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = _require("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = _require("ROUNDCUBEMAIL_DB_CONTAINER")

MATTERMOST_PORT = _require("MATTERMOST_PORT")
MATTERMOST_CONTAINER = _require("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = _require("MATTERMOST_DB_CONTAINER")

OWNCLOUD_PORT = _require("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = _require("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = _require("OWNCLOUD_DB_CONTAINER")

ONLYOFFICE_PORT = _require("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = _require("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = _require("ONLYOFFICE_DB_CONTAINER")


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


def mysql_query(container: str, db: str, user: str, password: str, sql: str) -> str:
    """Run a MySQL/MariaDB query and return stdout."""
    # -h 127.0.0.1 for the ownCloud MariaDB: its anonymous ''@'localhost' users
    # shadow 'owncloud'@'%' over the unix socket.
    host_args = ["-h", "127.0.0.1"] if container == OWNCLOUD_DB_CONTAINER else []
    rc, out, err = docker_exec(
        container,
        "mysql", *host_args, f"-u{user}", f"-p{password}", db,
        "--default-character-set=utf8mb4",
        "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def psql_query(container: str, db: str, user: str, password: str, sql: str) -> str:
    """Run a PostgreSQL query and return stdout."""
    rc, out, err = docker_exec(
        container,
        "psql", "-U", user, "-d", db, "-t", "-A", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mm_query(sql: str) -> str:
    """Query the Mattermost PostgreSQL DB."""
    return psql_query(MATTERMOST_DB_CONTAINER, "mattermost", "mmuser", "mmuser_password", sql)


def oo_db_query(sql: str) -> str:
    """Query the OnlyOffice MySQL DB."""
    return mysql_query(ONLYOFFICE_DB_CONTAINER, "onlyoffice", "onlyoffice_user", "onlyoffice_pass", sql)


# ── Roundcube checks ──────────────────────────────────────────────────────────

def check_1_mail_folder_exists() -> None:
    """Mail folder 'Priority Client Issues' exists under INBOX for james.whitfield.

    Exact Maildir++ directory only (namespace variant with/without .INBOX prefix
    accepted); no fuzzy grep fallback."""
    label = "1. Mail folder 'Priority Client Issues' exists"
    try:
        base = "/var/mail/mail.local/james.whitfield"
        candidates = [
            f"{base}/.INBOX.Priority Client Issues",
            f"{base}/.Priority Client Issues",
        ]
        found = ""
        for path in candidates:
            rc, _, _ = docker_exec(ROUNDCUBEMAIL_CONTAINER, "test", "-d", path)
            if rc == 0:
                found = path
                break
        check(label, 1, bool(found),
              f"maildir dir present: {found}" if found
              else "neither .INBOX.Priority Client Issues nor .Priority Client Issues exists under james.whitfield maildir")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Mattermost checks ────────────────────────────────────────────────────────

EXPECTED_PURPOSE = ("Private coordination channel for investigating the Helix Analytics Inc. "
                    "data export pipeline failure and tracking resolution")

# Full-text anchor for the complaint brief post (exact text from description.md).
BRIEF_ANCHOR = ("Complaint Brief: Helix Analytics Inc. reported that their monthly data "
                "export pipeline has been failing for 3 consecutive days")


def check_5_mm_private_channel() -> None:
    """Private channel 'helix-analytics-export-investigation' exists with correct purpose."""
    label = "5. MM private channel exists with purpose"
    try:
        sql = (
            "SELECT c.type, c.purpose FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.name = 'helix-analytics-export-investigation' "
            "AND t.displayname = 'Engineering Hub' LIMIT 1;"
        )
        out = mm_query(sql)
        if not out:
            check(label, 1, False, "channel not found")
            return
        parts = out.split("|")
        ch_type = parts[0].strip() if len(parts) > 0 else ""
        purpose = parts[1].strip() if len(parts) > 1 else ""

        is_private = ch_type == "P"
        # One-directional containment only: the expected purpose text must be
        # present in the stored purpose (empty purpose can never pass).
        purpose_norm = " ".join(purpose.split())
        purpose_ok = EXPECTED_PURPOSE in purpose_norm

        passed = is_private and purpose_ok
        issues = []
        if not is_private:
            issues.append(f"type={ch_type}, expected P")
        if not purpose_ok:
            issues.append(f"purpose does not contain expected text; got '{purpose[:80]}'")
        check(label, 1, passed, "channel OK" if passed else "; ".join(issues))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_6_mm_complaint_brief() -> None:
    """Complaint brief message posted in investigation channel (single row must
    contain the full anchor and all required probes)."""
    label = "6. Complaint brief posted in channel"
    try:
        sql = (
            "SELECT p.id FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.name = 'helix-analytics-export-investigation' "
            "AND t.displayname = 'Engineering Hub' "
            "AND p.deleteat = 0 "
            f"AND p.message LIKE '%{BRIEF_ANCHOR}%' "
            "AND p.message LIKE '%failing for 3 consecutive days%' "
            "AND p.message LIKE '%manual data backfill%' "
            "AND p.message LIKE '%root cause analysis%' "
            "AND p.message LIKE '%Priority: Critical.%' "
            "LIMIT 1;"
        )
        out = mm_query(sql)
        if out:
            check(label, 1, True, "single post contains full brief anchor and all probes")
            return
        # Diagnostics only: is there any brief-like post at all?
        probe = mm_query(
            "SELECT p.id FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "WHERE c.name = 'helix-analytics-export-investigation' "
            "AND p.deleteat = 0 AND p.message LIKE '%Complaint Brief%' LIMIT 1;"
        )
        check(label, 1, False,
              "a 'Complaint Brief' post exists but misses required probes "
              "(3 consecutive days / manual data backfill / root cause analysis / Priority: Critical.)"
              if probe else "no complaint brief post found in channel")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def _mm_brief_post_id() -> str:
    """Resolve the complaint-brief post id via the full-text anchor."""
    sql = (
        "SELECT p.id FROM posts p "
        "JOIN channels c ON p.channelid = c.id "
        "JOIN teams t ON c.teamid = t.id "
        "WHERE c.name = 'helix-analytics-export-investigation' "
        "AND t.displayname = 'Engineering Hub' "
        "AND p.deleteat = 0 "
        f"AND p.message LIKE '%{BRIEF_ANCHOR}%' "
        "ORDER BY p.createat ASC LIMIT 1;"
    )
    out = mm_query(sql).strip()
    post_id = out.splitlines()[0].strip() if out else ""
    if post_id and not re.fullmatch(r"[a-z0-9]{26}", post_id):
        return ""
    return post_id


def check_7a_mm_reply_delana(brief_id: str) -> None:
    """A single thread reply on the brief post tags @delana with the full ask."""
    label = "7a. Thread reply to @delana with full investigation ask"
    try:
        if not brief_id:
            check(label, 2, False, "brief post not found via full-text anchor")
            return
        sql = (
            "SELECT p.id FROM posts p "
            f"WHERE p.rootid = '{brief_id}' AND p.deleteat = 0 "
            "AND p.message LIKE '%@delana%' "
            "AND p.message LIKE '%scheduler logs, API error traces, and storage metrics%' "
            "AND p.message LIKE '%past 5 days%' "
            "AND p.message LIKE '%within 12 hours%' "
            "LIMIT 1;"
        )
        out = mm_query(sql)
        check(label, 2, bool(out),
              "single reply row contains @delana + all required phrases" if out
              else "no single reply on the brief thread contains @delana, scheduler logs/API error traces/storage metrics, past 5 days and within 12 hours")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7b_mm_reply_stephany(brief_id: str) -> None:
    """A single thread reply on the brief post tags @stephany with the full ask."""
    label = "7b. Thread reply to @stephany with account-history ask"
    try:
        if not brief_id:
            check(label, 2, False, "brief post not found via full-text anchor")
            return
        sql = (
            "SELECT p.id FROM posts p "
            f"WHERE p.rootid = '{brief_id}' AND p.deleteat = 0 "
            "AND p.message LIKE '%@stephany%' "
            "AND p.message LIKE '%account history, contract SLA for data exports%' "
            "AND p.message LIKE '%executive escalation contacts%' "
            "LIMIT 1;"
        )
        out = mm_query(sql)
        check(label, 2, bool(out),
              "single reply row contains @stephany + all required phrases" if out
              else "no single reply on the brief thread contains @stephany, account history/contract SLA for data exports and executive escalation contacts")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7c_mm_follow_thread(brief_id: str) -> None:
    """Admin follows the complaint-brief thread (threadmemberships.following)."""
    label = "7c. Brief thread followed by admin"
    try:
        if not brief_id:
            check(label, 1, False, "brief post not found via full-text anchor")
            return
        tbl = mm_query(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = 'public' AND tablename LIKE 'threadmembership%' "
            "ORDER BY tablename LIMIT 1;"
        ).strip()
        if not tbl or not re.fullmatch(r"[a-z_]+", tbl):
            check(label, 1, False,
                  "thread-membership table not present in this Mattermost schema; follow state cannot be proven")
            return
        sql = (
            f"SELECT 1 FROM {tbl} "
            f"WHERE postid = '{brief_id}' "
            "AND userid = (SELECT id FROM users WHERE username = 'admin') "
            "AND following = true LIMIT 1;"
        )
        out = mm_query(sql)
        check(label, 1, bool(out),
              f"admin following=true in {tbl}" if out
              else f"no following=true row for admin on brief thread in {tbl}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── ownCloud checks ──────────────────────────────────────────────────────────

def check_8_10_oc_folders_and_log() -> None:
    """Folder structure (main + Technical-Evidence + Client-Communications) and
    export-investigation-log.txt with exact content anchors."""
    label = "8+10. ownCloud investigation folder structure and log content"
    try:
        base_url = f"http://{HOST}:{OWNCLOUD_PORT}"
        auth = ("admin", "admin")

        r = requests.request(
            "PROPFIND",
            f"{base_url}/remote.php/dav/files/admin/Helix-Analytics-Export-Investigation-2026-04/",
            auth=auth, headers={"Depth": "1"}, timeout=10)
        folder_exists = r.status_code in (207, 200)
        has_tech = "Technical-Evidence" in r.text if folder_exists else False
        has_client = "Client-Communications" in r.text if folder_exists else False

        log_r = requests.get(
            f"{base_url}/remote.php/dav/files/admin/Helix-Analytics-Export-Investigation-2026-04/export-investigation-log.txt",
            auth=auth, timeout=10)
        log_ok = log_r.status_code == 200
        content = log_r.text if log_ok else ""
        anchors = [
            "Investigation Notes - Helix Analytics Inc. Data Export Complaint",
            "Client: Helix Analytics Inc.",
            "Contact: carlos.mendez@mail.local",
            "Date Received: 2026-04-20",
            "Severity: Critical",
            "Status: Under Investigation",
            "Monthly data export pipeline failing for 3 consecutive days, blocking executive dashboard",
            "Target resolution: 24 hours with manual backfill",
        ]
        missing = [a for a in anchors if a not in content]

        passed = folder_exists and has_tech and has_client and log_ok and not missing
        issues = []
        if not folder_exists:
            issues.append("main folder missing")
        if folder_exists and not has_tech:
            issues.append("Technical-Evidence subfolder missing")
        if folder_exists and not has_client:
            issues.append("Client-Communications subfolder missing")
        if not log_ok:
            issues.append(f"log file HTTP {log_r.status_code}")
        if missing:
            issues.append("log missing anchors: " + "; ".join(m[:50] for m in missing))
        check(label, 1, passed, "folders and log content OK" if passed else "; ".join(issues))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_12_oc_public_share_upload_only() -> None:
    """Technical-Evidence has public share with upload-only (file drop) permissions."""
    label = "12. Technical-Evidence public share (upload-only)"
    try:
        base_url = f"http://{HOST}:{OWNCLOUD_PORT}"
        auth = ("admin", "admin")
        r = requests.get(
            f"{base_url}/ocs/v2.php/apps/files_sharing/api/v1/shares",
            auth=auth,
            params={"format": "json",
                    "path": "/Helix-Analytics-Export-Investigation-2026-04/Technical-Evidence",
                    "reshares": "true"},
            timeout=10)
        data = r.json()
        shares = data.get("ocs", {}).get("data", [])
        # Public link share type = 3; upload-only (file drop) = permissions 4
        public_upload = False
        for s in shares:
            share_type = s.get("share_type", -1)
            perms = s.get("permissions", 0)
            if share_type == 3 and perms == 4:
                public_upload = True
                break
        check(label, 1, public_upload,
              "public file-drop OK" if public_upload
              else f"shares: {[(s.get('share_type'), s.get('permissions')) for s in shares]}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

DOC_TITLE = "Helix Analytics Export Failure - Resolution Report"

# Content probes over concatenated <w:t> text (exact text from description.md).
DOCX_PROBES = [
    # heading
    "Resolution Report: Helix Analytics Inc. Data Export Pipeline Failure",
    # Complaint Details
    "2026-04-17 through 2026-04-19",
    "SLA credit",
    # Root Cause Analysis
    "stale credentials",
    "legacy Slack channel that had been archived",
    # Resolution
    "15% SLA credit",
    "dedicated data platform engineer has been assigned for the next 30 days",
    # Preventive Measures (one anchor per bullet, 5 bullets)
    "centralized secret manager",
    "active on-call rotation channel with pager escalation",
    "every 6 hours",
    "client-facing export status dashboard",
    "quarterly audit of all alert routing configurations",
]

TIMELINE_ANCHORS = [
    "Complaint received via email and logged",
    "Root cause identified in export worker credential rotation",
    "Manual data backfill executed for missed period",
    "SLA credit calculated, resolution drafted",
    "Delana (Senior Engineer)",
    "Stephany (Tech Lead)",
]

COMMENT_PROBES = [
    "Please confirm the 15% SLA credit figure against Helix Analytics Inc.",
    "Data Platform staffing lead",
]

_oo_session_cache: list = []          # [session|None] once resolved
_docx_candidates_cache: list | None = None


def _oo_auth_session():
    """Authenticate to OnlyOffice and return a session, or None."""
    if _oo_session_cache:
        return _oo_session_cache[0]
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    session = requests.Session()
    result = None
    try:
        resp = session.post(
            f"{base_url}/api/2.0/authentication",
            json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
            timeout=15,
        )
        if resp.status_code in (200, 201):
            token = resp.json().get("response", {}).get("token", "")
            if token:
                session.headers.update({"Authorization": token})
                session.cookies.set("asc_auth_key", token)
                session.base_url = base_url  # type: ignore[attr-defined]
                result = session
    except Exception:
        result = None
    _oo_session_cache.append(result)
    return result


def _oo_common_files() -> list:
    """List files in Common Documents via the API (raises on failure)."""
    session = _oo_auth_session()
    if session is None:
        raise RuntimeError("OnlyOffice auth failed")
    r = session.get(f"{session.base_url}/api/2.0/files/@common", timeout=15)
    return r.json().get("response", {}).get("files", [])


def _oo_common_doc_id():
    """Exact-title lookup of the resolution report in Common Documents."""
    for f in _oo_common_files():
        if f.get("title", "") in (DOC_TITLE, DOC_TITLE + ".docx"):
            return f.get("id")
    return None


def _oo_doc_exists_common() -> tuple[bool, str]:
    """Existence assertion: exact title present in @common (DB fallback if API down)."""
    try:
        if _oo_common_doc_id() is not None:
            return True, "exact title present in @common"
        return False, "exact title not found in @common"
    except Exception:
        pass
    try:
        out = oo_db_query(
            "SELECT id FROM files_file "
            f"WHERE title IN ('{DOC_TITLE}', '{DOC_TITLE}.docx') LIMIT 1;"
        )
        if out:
            return True, "exact title in files_file (API unavailable; folder unverified)"
        return False, "exact title not in files_file (API unavailable)"
    except Exception as e:
        return False, f"existence lookup failed: {e}"


def _oo_docx_bytes_from_fs() -> bytes | None:
    """Read the resolution report's content.docx from the portal data dir via
    docker exec. The find pattern's middle '*' covers version subdirs; when
    several versions match, the highest version directory wins."""
    out = oo_db_query(
        "SELECT id FROM files_file "
        f"WHERE title IN ('{DOC_TITLE}', '{DOC_TITLE}.docx') "
        "ORDER BY id DESC LIMIT 1;"
    ).strip()
    if not out:
        return None
    file_id = out.splitlines()[0].strip()
    if not re.fullmatch(r"\d+", file_id):
        return None
    rc, stdout, _ = docker_exec(
        ONLYOFFICE_CONTAINER, "bash", "-c",
        f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.docx' 2>/dev/null",
        timeout=20,
    )
    paths = [p.strip() for p in stdout.splitlines() if p.strip()]
    if not paths:
        return None

    def _version_key(p: str) -> int:
        m = re.search(r"/v?(\d+)/content\.docx$", p)
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
    if session is None:
        return None
    doc_id = _oo_common_doc_id()
    if doc_id is None:
        return None
    base_url = session.base_url  # type: ignore[attr-defined]
    dl_resp = session.get(
        f"{base_url}/products/files/httphandlers/filehandler.ashx",
        params={"action": "download", "fileid": str(doc_id)},
        timeout=30, allow_redirects=True,
    )
    if not (dl_resp.status_code == 200 and len(dl_resp.content) > 100):
        dl_resp = session.get(
            f"{base_url}/api/2.0/files/file/{doc_id}/download",
            timeout=30, allow_redirects=True,
        )
    if dl_resp.status_code != 200 or not dl_resp.content:
        return None
    return dl_resp.content


def _oo_docx_candidates() -> list[tuple[bytes, str]]:
    """Candidate docx byte blobs: fs copy first, API download second (guards
    against a stale fs copy that predates the last editor save)."""
    global _docx_candidates_cache
    if _docx_candidates_cache is not None:
        return _docx_candidates_cache
    cands: list[tuple[bytes, str]] = []
    try:
        data = _oo_docx_bytes_from_fs()
        if data:
            cands.append((data, "fs"))
    except Exception:
        pass
    try:
        data = _oo_docx_bytes_from_api()
        if data:
            cands.append((data, "api"))
    except Exception:
        pass
    _docx_candidates_cache = cands
    return cands


def _wt_text(xml: str) -> str:
    """Concatenate all <w:t> runs (OnlyOffice splits sentences across runs),
    unescape entities and normalize whitespace."""
    parts = re.findall(r"<w:t[^>]*>(.*?)</w:t>", xml, flags=re.DOTALL)
    text = "".join(html.unescape(p) for p in parts)
    return " ".join(text.split())


def _probe_docx_structure(data: bytes) -> tuple[bool, str]:
    """Structural + content probe of the resolution report docx."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"cannot read word/document.xml: {e}"

    text = _wt_text(xml)
    issues: list[str] = []

    missing = [p for p in DOCX_PROBES if p not in text]
    if missing:
        issues.append("missing text probes: " + " | ".join(m[:45] for m in missing))

    tables = re.findall(r"<w:tbl(?:\s[^>]*)?>.*?</w:tbl>", xml, flags=re.DOTALL)
    if not tables:
        issues.append("no <w:tbl> table found")
    else:
        timeline_tbl = None
        for tbl in tables:
            if "Complaint received via email and logged" in _wt_text(tbl):
                timeline_tbl = tbl
                break
        if timeline_tbl is None:
            issues.append("no table contains the timeline row anchor 'Complaint received via email and logged'")
        else:
            rows = len(re.findall(r"<w:tr[ >]", timeline_tbl))
            if not (6 <= rows <= 8):
                issues.append(f"timeline table has {rows} <w:tr> rows, expected 7 +/- 1")
            tbl_text = _wt_text(timeline_tbl)
            miss_rows = [a for a in TIMELINE_ANCHORS if a not in tbl_text]
            if miss_rows:
                issues.append("missing timeline anchors: " + " | ".join(a[:45] for a in miss_rows))

    if issues:
        return False, "; ".join(issues)
    return True, "all content, table and row probes OK"


def _probe_comments(data: bytes) -> tuple[bool, str]:
    """word/comments.xml must contain the required review-comment text."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        if "word/comments.xml" not in names:
            return False, "word/comments.xml absent from docx package"
        xml = zf.read("word/comments.xml").decode("utf-8", errors="replace")
    except Exception as e:
        return False, f"cannot read docx package: {e}"
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    text = " ".join(text.split())
    missing = [p for p in COMMENT_PROBES if p not in text]
    if missing:
        return False, "comment text missing probes: " + " | ".join(m[:50] for m in missing)
    return True, "comment contains both required probes"


def check_13_oo_document_content() -> None:
    """Resolution report exists in Common Documents (exact title) AND passes the
    full docx structural/content probe."""
    label = "13. OnlyOffice resolution report exists with required structure"
    try:
        exists, exist_detail = _oo_doc_exists_common()
        cands = _oo_docx_candidates()
        if not cands:
            check(label, 2, False,
                  f"exists={exists} [{exist_detail}]; docx bytes unavailable via fs and API")
            return
        content_ok = False
        detail = ""
        for data, src in cands:
            ok, det = _probe_docx_structure(data)
            detail = f"{det} [source: {src}]"
            if ok:
                content_ok = True
                break
        passed = exists and content_ok
        check(label, 2, passed,
              f"document in @common; {detail}" if passed
              else f"exists={exists} [{exist_detail}]; {detail}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15_oo_review_comment() -> None:
    """Review comment on the Resolution section persisted in word/comments.xml."""
    label = "15. Review comment persisted in document (comments.xml)"
    try:
        cands = _oo_docx_candidates()
        if not cands:
            check(label, 2, False, "docx bytes unavailable via fs and API")
            return
        last = ""
        for data, src in cands:
            ok, det = _probe_comments(data)
            last = f"{det} [source: {src}]"
            if ok:
                check(label, 2, True, last)
                return
        check(label, 2, False, last)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_oo_document_shared() -> None:
    """Document shared with laura.brown (view, access==2) and jun.chen (edit, access==1)."""
    label = "14. Document shared with laura.brown & jun.chen"
    try:
        session = _oo_auth_session()
        if session is None:
            check(label, 1, False, "OnlyOffice auth failed")
            return
        doc_id = _oo_common_doc_id()
        if doc_id is None:
            check(label, 1, False, "document not found in @common by exact title")
            return

        share_r = session.get(
            f"{session.base_url}/api/2.0/files/file/{doc_id}/share", timeout=15)
        shares = share_r.json().get("response", [])

        laura_view = False
        jun_edit = False
        for s in shares:
            shared_to = s.get("sharedTo", {}) or {}
            user_name = shared_to.get("userName", "")
            display_name = shared_to.get("displayName", "")
            access = s.get("access", -1)
            # access: 1 = read+write (edit), 2 = read-only (view); exact equality only
            if user_name == "laura.brown" or display_name == "Laura Brown":
                laura_view = access == 2
            if user_name == "jun.chen" or display_name == "Jun Chen":
                jun_edit = access == 1
        passed = laura_view and jun_edit
        check(label, 1, passed, f"laura_view={laura_view}, jun_edit={jun_edit}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_mail_folder_exists()
    check_5_mm_private_channel()
    check_6_mm_complaint_brief()
    try:
        brief_id = _mm_brief_post_id()
    except Exception:
        brief_id = ""
    check_7a_mm_reply_delana(brief_id)
    check_7b_mm_reply_stephany(brief_id)
    check_7c_mm_follow_thread(brief_id)
    check_8_10_oc_folders_and_log()
    check_12_oc_public_share_upload_only()
    check_13_oo_document_content()
    check_15_oo_review_comment()
    check_14_oo_document_shared()

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
