"""
Verifier for Teamwork-082-I2: Competitive intelligence briefing with tiered distribution

Checks: 16 weighted checks across mattermost, onlyoffice, owncloud, roundcubemail.
Total weight: 22.
Strategy: docker exec (DB queries) for mattermost/onlyoffice/owncloud/roundcube;
          docx content probes (fs content.docx + OnlyOffice API download fallback);
          Sent-maildir parsing with the Python email stdlib (no mail-log
          fallbacks: log presence != delivery).

Required env vars:
  SERVER_HOSTNAME,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER
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

REQUIRED_VARS = [
    "MATTERMOST_PORT", "MATTERMOST_CONTAINER", "MATTERMOST_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
    "OWNCLOUD_PORT", "OWNCLOUD_CONTAINER", "OWNCLOUD_DB_CONTAINER",
    "ROUNDCUBEMAIL_PORT", "ROUNDCUBEMAIL_CONTAINER", "ROUNDCUBEMAIL_DB_CONTAINER",
]

env = {}
for var in REQUIRED_VARS:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    env[var] = val

MM_DB = env["MATTERMOST_DB_CONTAINER"]
MM_CONTAINER = env["MATTERMOST_CONTAINER"]
OO_DB = env["ONLYOFFICE_DB_CONTAINER"]
OO_CONTAINER = env["ONLYOFFICE_CONTAINER"]
OO_PORT = env["ONLYOFFICE_PORT"]
OC_DB = env["OWNCLOUD_DB_CONTAINER"]
OC_CONTAINER = env["OWNCLOUD_CONTAINER"]
RC_CONTAINER = env["ROUNDCUBEMAIL_CONTAINER"]
RC_DB = env["ROUNDCUBEMAIL_DB_CONTAINER"]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []

# Cross-check state: public-link token captured in check 13, consumed by check 15.
PUBLIC_LINK_TOKEN: str | None = None


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


def mm_sql(query: str) -> str:
    """Run a psql query against Mattermost DB."""
    rc, out, err = docker_exec(
        MM_DB, "psql", "-U", "mmuser", "-d", "mattermost",
        "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oo_sql(query: str) -> str:
    """Run a mysql query against OnlyOffice DB."""
    rc, out, err = docker_exec(
        OO_DB, "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4",
        "onlyoffice", "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def oc_sql(query: str) -> str:
    """Run a mysql query against ownCloud DB."""
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OC_DB, "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud",
        "--default-character-set=utf8mb4",
        "owncloud", "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def rc_sql(query: str) -> str:
    """Run a mysql query against Roundcube DB."""
    rc, out, err = docker_exec(
        RC_DB, "mysql", "-u", "roundcube", "-proundcube123",
        "--default-character-set=utf8mb4",
        "roundcubemail", "-N", "-B", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


# ── OnlyOffice docx fetch chain (fs content.docx first, API download fallback) ─
FULL_DOC_TITLES = (
    "Nimbus Cloud Competitive Intelligence Briefing Q3",
    "Nimbus Cloud Competitive Intelligence Briefing Q3.docx",
)
SANITIZED_DOC_TITLES = (
    "Nimbus Cloud Competitive Briefing — All Hands Summary",
    "Nimbus Cloud Competitive Briefing — All Hands Summary.docx",
)


def oo_file_id(titles: tuple[str, ...]) -> str:
    quoted = ", ".join("'" + t.replace("'", "''") + "'" for t in titles)
    out = oo_sql(
        f"SELECT id FROM files_file WHERE title IN ({quoted}) ORDER BY id DESC LIMIT 1;"
    )
    return out.splitlines()[0].strip() if out.strip() else ""


def oo_api_auth() -> tuple[str, dict]:
    """Authenticate to OnlyOffice, return (base_url, headers)."""
    base = f"http://{HOST}:{OO_PORT}"
    resp = requests.post(
        f"{base}/api/2.0/authentication",
        json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
        timeout=15,
    )
    token = resp.json().get("response", {}).get("token", "")
    return base, {"Authorization": f"Bearer {token}"}


def _oo_docx_bytes_from_fs(titles: tuple[str, ...]) -> bytes | None:
    """Read content.docx from the portal data dir via docker exec.
    The middle '*' in -path covers version subdirectories (v1/v2/...)."""
    file_id = oo_file_id(titles)
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
    doc_id = oo_file_id(titles)
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


def oo_get_docx(titles: tuple[str, ...]) -> tuple[bytes | None, str]:
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


def docx_xml(data: bytes, member: str) -> str:
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


def wxml_text(xml_fragment: str) -> str:
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


def tbl_blocks(doc_xml: str) -> list[str]:
    return re.findall(r"<w:tbl>.*?</w:tbl>", doc_xml, flags=re.DOTALL)


def oo_share_check(titles: tuple[str, ...], username: str, access_level: int,
                   label: str, weight: int) -> None:
    """approach A share check: API primary (sharedTo.userName exact + access exact),
    DB fallback (files_security join core_user on GUID subject, entry_id bound
    to the target file resolved by exact title, security exact)."""
    try:
        file_id = oo_file_id(titles)
        if not file_id:
            check(label, weight, False, f"target document not found by title {titles[0]!r}")
            return

        api_verdict: bool | None = None
        api_detail = ""
        try:
            base, headers = oo_api_auth()
            resp = requests.get(
                f"{base}/api/2.0/files/file/{file_id}/share",
                headers=headers, timeout=15,
            )
            shares = resp.json().get("response", [])
            api_verdict = False
            for s in shares:
                user = s.get("sharedTo", {}) or {}
                uname = user.get("userName", "") or ""
                access = s.get("access", -1)
                if uname == username and access == access_level:
                    api_verdict = True
                    break
            api_detail = (f"api: {username} access=={access_level} found" if api_verdict
                          else f"api: no share with userName=={username!r} "
                               f"and access=={access_level}")
        except Exception as e:
            api_verdict = None
            api_detail = f"api unavailable: {e}"

        if api_verdict is not None:
            check(label, weight, api_verdict, api_detail)
            return

        rows = oo_sql(
            "SELECT cu.username, fs.security "
            "FROM files_security fs "
            "JOIN core_user cu ON fs.subject = cu.id "
            "WHERE fs.entry_type = 2 "
            f"AND fs.entry_id = CAST({file_id} AS CHAR) "
            f"AND cu.username = '{username}' AND fs.security = {access_level};"
        )
        passed = bool(rows.strip())
        check(label, weight, passed,
              f"{api_detail}; db fallback: "
              + (f"{username} security=={access_level} row found" if passed
                 else f"no {username}/security=={access_level} row on entry_id={file_id}"))
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


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


def rc_priority(msg) -> str | None:
    """First token of X-Priority (Roundcube: Highest=1, High=2, Normal=3/absent,
    Low=4, Lowest=5), or None when the header is absent."""
    v = msg.get("X-Priority")
    if v is None:
        return None
    v = v.strip()
    return v.split()[0] if v else ""


# ── Mattermost checks ────────────────────────────────────────────────────────

def _tech_talks_channel_id() -> str:
    """Tech Talks channel id, team-joined to Engineering Hub (same-named
    channels can exist across teams)."""
    return mm_sql(
        "SELECT c.id FROM channels c JOIN teams t ON c.teamid = t.id "
        "WHERE c.displayname = 'Tech Talks' "
        "AND t.displayname = 'Engineering Hub' LIMIT 1;"
    )


def check_1_mm_channel_purpose() -> None:
    """Check Tech Talks (Engineering Hub) channel purpose is set correctly."""
    try:
        expected = "Tech talks and strategic competitor deep-dives for the engineering org"
        purpose = mm_sql(
            "SELECT c.purpose FROM channels c JOIN teams t ON c.teamid = t.id "
            "WHERE c.displayname = 'Tech Talks' "
            "AND t.displayname = 'Engineering Hub' LIMIT 1;"
        )
        passed = expected.lower() in purpose.lower()
        check("1. MM Tech Talks channel purpose", 1, passed,
              f"got: {purpose[:100]}" if not passed else "")
    except Exception as e:
        check("1. MM Tech Talks channel purpose", 1, False, f"exception: {e}")


def check_2_mm_briefing_message() -> None:
    """Check briefing kickoff message posted in Tech Talks (Engineering Hub)."""
    try:
        expected_fragment = "Kicking off Q3 competitive intel briefing on Nimbus Cloud"
        channel_id = _tech_talks_channel_id()
        if not channel_id:
            check("2. MM briefing message posted", 1, False,
                  "Tech Talks channel not found in Engineering Hub")
            return
        msg = mm_sql(
            f"SELECT message FROM posts WHERE channelid = '{channel_id}' "
            f"AND deleteat = 0 "
            f"AND message LIKE '%Kicking off Q3 competitive intel%' LIMIT 1;"
        )
        passed = expected_fragment in msg
        check("2. MM briefing message posted", 1, passed,
              f"got: {msg[:100]}" if not passed else "")
    except Exception as e:
        check("2. MM briefing message posted", 1, False, f"exception: {e}")


def check_3_mm_message_pinned() -> None:
    """Check the briefing message is pinned."""
    try:
        channel_id = _tech_talks_channel_id()
        if not channel_id:
            check("3. MM briefing message pinned", 1, False,
                  "Tech Talks channel not found in Engineering Hub")
            return
        count = mm_sql(
            f"SELECT COUNT(*) FROM posts WHERE channelid = '{channel_id}' "
            f"AND deleteat = 0 "
            f"AND message LIKE '%Kicking off Q3 competitive intel%' AND ispinned = true;"
        )
        passed = count.strip() not in ("", "0")
        check("3. MM briefing message pinned", 1, passed,
              f"pinned count: {count}" if not passed else "")
    except Exception as e:
        check("3. MM briefing message pinned", 1, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

def check_4_oo_full_briefing_content() -> None:
    """Full briefing docx: heading, overview, 2 intelligence tables with header
    triplets, 4 threat bullets, 4 recommended actions."""
    label = "4. OO full briefing doc content"
    try:
        data, source = oo_get_docx(FULL_DOC_TITLES)
        if not data:
            check(label, 2, False, f"could not read document: {source}")
            return
        doc = docx_xml(data, "word/document.xml")
        if not doc:
            check(label, 2, False, f"word/document.xml not found (source: {source})")
            return
        text = wxml_text(doc)

        probes = {
            "heading": "Competitive Intelligence Briefing: Nimbus Cloud",
            "overview": "developer-first tooling and open-source integrations",
            "section Product Intelligence": "Product Intelligence",
            "section Sales Intelligence": "Sales Intelligence",
            "threat 1": "erodes our platform-engineer mindshare",
            "threat 2": "permissive licensing",
            "threat 3": "Aggressive recruiting of SRE and DevOps talent",
            "threat 4": "Conference sponsorships targeting our core technical audience",
            "action 1": "open-source SDK and reference integrations within 45 days",
            "action 2": "developer advocacy program",
            "action 3": "top 5 infrastructure events",
            "action 4": "Retention review and counter-offer plan",
        }
        missing = [k for k, v in probes.items() if _norm(v) not in text]

        # Structure: >= 2 tables, each carrying the 3-column header triplet
        # (one per Product Intelligence / Sales Intelligence section).
        headers = ("Source (channel name)", "Intelligence (message text)",
                   "Analyst (author)")
        tables = tbl_blocks(doc)
        tables_with_headers = sum(
            1 for t in tables
            if all(h in wxml_text(t) for h in headers)
        )
        if len(tables) < 2:
            missing.append(f"<w:tbl> count={len(tables)} (want >=2)")
        if tables_with_headers < 2:
            missing.append(
                f"tables with header triplet={tables_with_headers} (want >=2)")

        passed = not missing
        check(label, 2, passed,
              f"all anchors found, {len(tables)} tables (source: {source})" if passed
              else f"missing: {missing} (source: {source})")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4b_oo_track_changes() -> None:
    """Track changes enabled on the full briefing (settings.xml trackRevisions)."""
    label = "4b. OO full briefing track changes enabled"
    try:
        data, source = oo_get_docx(FULL_DOC_TITLES)
        if not data:
            check(label, 1, False, f"could not read document: {source}")
            return
        settings = docx_xml(data, "word/settings.xml")
        track_on = False
        if settings and "trackRevisions" in settings:
            # <w:trackRevisions/> present and not explicitly disabled
            if 'val="false"' not in settings and "val='false'" not in settings:
                track_on = True
        check(label, 1, track_on,
              f"trackRevisions found in settings (source: {source})" if track_on
              else f"trackRevisions not found/disabled (source: {source})")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5_oo_full_briefing_shared_jun() -> None:
    """Full briefing shared with jun.chen for editing (access/security == 1)."""
    oo_share_check(FULL_DOC_TITLES, "jun.chen", 1,
                   "5. OO full briefing shared with jun.chen (edit)", 2)


def check_6_oo_sanitized_doc_content() -> None:
    """Sanitized doc: heading + overview + 4 actions present; must NOT contain
    Threat Assessment / Product Intelligence / Sales Intelligence."""
    label = "6. OO sanitized doc content (with negative probes)"
    try:
        file_id = oo_file_id(SANITIZED_DOC_TITLES)
        if not file_id:
            check(label, 1, False,
                  f"sanitized document not found by title {SANITIZED_DOC_TITLES[0]!r}")
            return
        data, source = oo_get_docx(SANITIZED_DOC_TITLES)
        if not data:
            check(label, 1, False, f"could not read document: {source}")
            return
        doc = docx_xml(data, "word/document.xml")
        if not doc:
            check(label, 1, False, f"word/document.xml not found (source: {source})")
            return
        text = wxml_text(doc)

        positive = {
            "heading": "Competitive Landscape Summary: Nimbus Cloud",
            "overview": "developer-first tooling and open-source integrations",
            "action 1": "open-source SDK and reference integrations within 45 days",
            "action 2": "developer advocacy program",
            "action 3": "top 5 infrastructure events",
            "action 4": "Retention review and counter-offer plan",
        }
        missing = [k for k, v in positive.items() if _norm(v) not in text]

        # Negative gate: sanitized version must omit the restricted sections.
        forbidden = ["Threat Assessment", "Product Intelligence", "Sales Intelligence"]
        leaked = [f for f in forbidden if f in text]

        # Soft sub-signal (recorded only, never gates): sanitized doc should
        # carry no tables; an agent laying out the overview in a table would
        # otherwise be unfairly failed.
        n_tables = len(tbl_blocks(doc))

        passed = not missing and not leaked
        details = []
        if missing:
            details.append(f"missing: {missing}")
        if leaked:
            details.append(f"restricted content leaked: {leaked}")
        details.append(f"tables={n_tables} (soft signal, want 0)")
        details.append(f"source: {source}")
        check(label, 1, passed, "; ".join(details))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_7_oo_sanitized_shared_maria() -> None:
    """Sanitized doc shared with maria.wilson for viewing (access/security == 2)."""
    oo_share_check(SANITIZED_DOC_TITLES, "maria.wilson", 2,
                   "7. OO sanitized doc shared with maria.wilson (view)", 2)


# ── ownCloud checks ──────────────────────────────────────────────────────────

def check_8_oc_folder_structure() -> None:
    """Check folder structure: Competitive Intelligence Q3 / Restricted Dossier + Public Summary."""
    try:
        result = oc_sql(
            "SELECT path FROM oc_filecache WHERE path LIKE '%Competitive Intelligence Q3%' "
            "ORDER BY path;"
        )
        has_parent = "Competitive Intelligence Q3" in result
        has_restricted = "Restricted Dossier" in result
        has_public = "Public Summary" in result
        passed = has_parent and has_restricted and has_public
        detail = ""
        if not passed:
            missing = []
            if not has_parent:
                missing.append("parent folder")
            if not has_restricted:
                missing.append("Restricted Dossier")
            if not has_public:
                missing.append("Public Summary")
            detail = f"missing: {', '.join(missing)}"
        check("8. OC folder structure", 1, passed, detail)
    except Exception as e:
        check("8. OC folder structure", 1, False, f"exception: {e}")


def check_9_oc_raw_intel_file() -> None:
    """nimbus_raw_intel.txt in Restricted Dossier starts with the full
    CONFIDENTIAL distribution-restriction sentence."""
    try:
        result = oc_sql(
            "SELECT fileid FROM oc_filecache WHERE path LIKE "
            "'%Restricted Dossier/nimbus_raw_intel.txt';"
        )
        if not result.strip():
            check("9. OC nimbus_raw_intel.txt content", 1, False,
                  "file not found in filecache")
            return
        raw = docker_exec_bytes(
            OC_CONTAINER, "bash", "-c",
            "find /mnt/data/files -path '*Restricted Dossier/nimbus_raw_intel.txt' "
            "-exec cat {} \\; 2>/dev/null",
        )
        content = raw.decode("utf-8", errors="replace") if raw else ""
        expected_intro = (
            "CONFIDENTIAL — Raw competitive intelligence notes on Nimbus Cloud. "
            "Distribution restricted to strategy and engineering leadership."
        )
        passed = _norm(expected_intro) in _norm(content)
        check("9. OC nimbus_raw_intel.txt content", 1, passed,
              f"content starts: {content[:80]!r}" if not passed else "")
    except Exception as e:
        check("9. OC nimbus_raw_intel.txt content", 1, False, f"exception: {e}")


def check_10_oc_summary_file() -> None:
    """Check nimbus_summary.txt exists in Public Summary with correct content."""
    try:
        result = oc_sql(
            "SELECT fileid FROM oc_filecache WHERE path LIKE "
            "'%Public Summary/nimbus_summary.txt';"
        )
        if not result.strip():
            check("10. OC nimbus_summary.txt content", 1, False, "file not found in filecache")
            return
        rc, content, err = docker_exec(
            OC_CONTAINER, "bash", "-c",
            "find /mnt/data/files -path '*Public Summary/nimbus_summary.txt' "
            "-exec cat {} \\; 2>/dev/null"
        )
        expected = "Nimbus Cloud is pursuing a developer-first strategy"
        passed = expected in content
        check("10. OC nimbus_summary.txt content", 1, passed,
              f"content: {content[:80]}" if not passed else "")
    except Exception as e:
        check("10. OC nimbus_summary.txt content", 1, False, f"exception: {e}")


def _public_summary_fileid() -> str:
    fileid = oc_sql(
        "SELECT fileid FROM oc_filecache WHERE path LIKE "
        "'%Competitive Intelligence Q3/Public Summary' LIMIT 1;"
    )
    return fileid.strip().splitlines()[0] if fileid.strip() else ""


def check_12_oc_public_shared_group_admin() -> None:
    """Public Summary shared with group admin, read-only (permissions==1),
    expiry 2026-08-20."""
    label = "12. OC Public Summary shared with group admin (ro, expiry)"
    try:
        fid = _public_summary_fileid()
        if not fid:
            check(label, 1, False, "folder not found")
            return
        count = oc_sql(
            f"SELECT COUNT(*) FROM oc_share "
            f"WHERE file_source = {fid} AND share_type = 1 "
            f"AND share_with = 'admin' AND permissions = 1 "
            f"AND expiration LIKE '2026-08-20%';"
        )
        passed = count.strip().isdigit() and int(count.strip()) > 0
        detail = ""
        if not passed:
            shares = oc_sql(
                f"SELECT share_with, permissions, expiration FROM oc_share "
                f"WHERE file_source = {fid} AND share_type = 1;"
            )
            detail = ("no admin group share with permissions==1 and expiry 2026-08-20 | "
                      f"group shares: {shares[:120]!r}")
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_13_oc_public_link() -> None:
    """Public link on Public Summary, read-only (permissions==1); token cached
    for the cross-app URL assertion in check 15."""
    global PUBLIC_LINK_TOKEN
    label = "13. OC Public Summary public link (read-only)"
    try:
        fid = _public_summary_fileid()
        if not fid:
            check(label, 1, False, "folder not found")
            return
        rows = oc_sql(
            f"SELECT token, permissions FROM oc_share "
            f"WHERE file_source = {fid} AND share_type = 3;"
        )
        passed = False
        perms_seen = []
        for row in (rows or "").splitlines():
            parts = row.split("\t")
            if len(parts) < 2:
                continue
            token, perms = parts[0].strip(), parts[1].strip()
            if not token:
                continue
            perms_seen.append(perms)
            if perms == "1":
                PUBLIC_LINK_TOKEN = token
                passed = True
                break
            if PUBLIC_LINK_TOKEN is None:
                PUBLIC_LINK_TOKEN = token  # keep for ck15 even if perms wrong
        detail = (f"token captured, permissions=1" if passed
                  else (f"link exists but permissions={perms_seen} (want 1)"
                        if perms_seen else "no public link found"))
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Roundcube checks ─────────────────────────────────────────────────────────

EXEC_SUBJECT = "[CONFIDENTIAL] Nimbus Cloud Competitive Intelligence Briefing — Q3"


def check_14_rc_exec_email_sent() -> None:
    """Exec email in james.whitfield's Sent copy: exact decoded subject, To set
    equality, X-Priority==2 only, MDN header present, body probes.
    (No mail-log fallback: log presence != delivery.)"""
    label = "14. RC exec email sent"
    try:
        expected_to = {
            "rachel.goldberg@mail.local",
            "yuki.tanaka@mail.local",
            "carlos.mendez@mail.local",
        }
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
            if subj != EXEC_SUBJECT:
                continue
            to_ok = rc_addr_set(msg, "To") == expected_to
            pv = rc_priority(msg)
            # High = X-Priority 2 ONLY (1 is Highest, not High); accept
            # Importance: high as the RFC-alternate header.
            prio_ok = pv == "2" or (
                pv is None and (msg.get("Importance", "") or "").strip().lower() == "high"
            )
            mdn_ok = msg.get("Disposition-Notification-To") is not None
            body = rc_body_text(msg)
            probes = ["Restricted Dossier", "Please confirm receipt"]
            missing_probes = [p for p in probes if _norm(p) not in body]
            body_ok = not missing_probes

            score = sum((to_ok, prio_ok, mdn_ok, body_ok))
            if score > best_score:
                best_score = score
                best_detail = (f"to={'ok' if to_ok else sorted(rc_addr_set(msg, 'To'))}, "
                               f"x-priority={'ok' if prio_ok else pv!r}, "
                               f"mdn={'ok' if mdn_ok else 'header missing'}, "
                               f"body={'ok' if body_ok else 'missing ' + repr(missing_probes)}")
            if to_ok and prio_ok and mdn_ok and body_ok:
                passed = True
                break

        if best_score < 0:
            best_detail = (f"no Sent message with exact subject {EXEC_SUBJECT!r}; "
                           f"subjects seen: {subjects_seen[:5]!r}")
        check(label, 2, passed, best_detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15_rc_general_email_sent() -> None:
    """General email in Sent copy: To set equality, Normal priority, opening
    sentence, >=2 recommended-action anchors, and the public-link URL
    (/s/<token from check 13>, cross-app truth).
    (No mail-log fallback: log presence != delivery.)"""
    label = "15. RC general email sent"
    try:
        expected_to = {
            "tom.andersen@mail.local",
            "amira.hassan@mail.local",
            "lin.fang@mail.local",
            "isabelle.rousseau@mail.local",
        }
        action_anchors = [
            "open-source SDK and reference integrations within 45 days",
            "developer advocacy program",
            "top 5 infrastructure events",
            "Retention review and counter-offer plan",
        ]
        opening = "Hello team, please find below a high-level summary"

        msgs = rc_sent_messages("james.whitfield")
        if not msgs:
            check(label, 3, False,
                  "no messages in /var/mail/mail.local/james.whitfield/.Sent/{cur,new}")
            return

        best_detail = ""
        best_score = -1
        passed = False
        for _path, msg in msgs:
            to_ok = rc_addr_set(msg, "To") == expected_to
            pv = rc_priority(msg)
            prio_ok = pv is None or pv == "3"  # Normal = no X-Priority or ==3
            body = rc_body_text(msg)
            opening_ok = _norm(opening) in body
            actions_found = sum(1 for a in action_anchors if _norm(a) in body)
            actions_ok = actions_found >= 2
            if PUBLIC_LINK_TOKEN:
                link_ok = f"/s/{PUBLIC_LINK_TOKEN}" in body
                link_note = ("ok" if link_ok
                             else f"URL '/s/{PUBLIC_LINK_TOKEN}' not in body")
            else:
                link_ok = False
                link_note = "ck13 token unavailable -> link sub-assertion fails"

            score = sum((to_ok, prio_ok, opening_ok, actions_ok, link_ok))
            if score > best_score:
                best_score = score
                best_detail = (f"to={'ok' if to_ok else sorted(rc_addr_set(msg, 'To'))}, "
                               f"priority={'ok' if prio_ok else pv!r}, "
                               f"opening={'ok' if opening_ok else 'missing'}, "
                               f"actions={actions_found}/4 (need >=2), "
                               f"public-link={link_note}")
            if to_ok and prio_ok and opening_ok and actions_ok and link_ok:
                passed = True
                break

        check(label, 3, passed, best_detail)
    except Exception as e:
        check(label, 3, False, f"exception: {e}")


def check_16_rc_exec_email_flagged() -> None:
    """Exec email (matched by full decoded subject) flagged in Sent
    (Maildir 'F' flag after ':2,' in the filename)."""
    label = "16. RC exec email flagged in Sent"
    try:
        msgs = rc_sent_messages("james.whitfield")
        if not msgs:
            check(label, 1, False,
                  "no messages in /var/mail/mail.local/james.whitfield/.Sent/{cur,new}")
            return
        matched = [(path, msg) for path, msg in msgs
                   if rc_decoded_header(msg, "Subject") == EXEC_SUBJECT]
        if not matched:
            check(label, 1, False,
                  f"no Sent message with exact subject {EXEC_SUBJECT!r}")
            return
        flagged = False
        flags_seen = []
        for path, _msg in matched:
            fname = os.path.basename(path)
            flags = fname.split(":2,")[-1] if ":2," in fname else ""
            flags_seen.append(flags)
            if "F" in flags:
                flagged = True
                break
        check(label, 1, flagged,
              "F flag present" if flagged else f"maildir flags seen: {flags_seen!r}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_mm_channel_purpose()
    check_2_mm_briefing_message()
    check_3_mm_message_pinned()
    check_4_oo_full_briefing_content()
    check_4b_oo_track_changes()
    check_5_oo_full_briefing_shared_jun()
    check_6_oo_sanitized_doc_content()
    check_7_oo_sanitized_shared_maria()
    check_8_oc_folder_structure()
    check_9_oc_raw_intel_file()
    check_10_oc_summary_file()
    check_12_oc_public_shared_group_admin()
    check_13_oc_public_link()
    check_14_rc_exec_email_sent()
    check_15_rc_general_email_sent()
    check_16_rc_exec_email_flagged()

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
