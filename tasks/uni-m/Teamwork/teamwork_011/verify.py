"""
Verifier for Teamwork-011-I2: Prepare and Distribute Annual Strategy Board Meeting Package

Checks: 15 weighted checks (21 pts) across onlyoffice, owncloud, mattermost, roundcubemail.
Strategy: API + DB + data-dir pptx content probe (OnlyOffice), DB + WebDAV (ownCloud),
DB (Mattermost), DB + Sent-maildir header parsing (Roundcube)

Required env vars:
  SERVER_HOSTNAME,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER,
  OWNCLOUD_PORT, OWNCLOUD_CONTAINER, OWNCLOUD_DB_CONTAINER,
  MATTERMOST_PORT, MATTERMOST_CONTAINER, MATTERMOST_DB_CONTAINER,
  ROUNDCUBEMAIL_PORT, ROUNDCUBEMAIL_CONTAINER, ROUNDCUBEMAIL_DB_CONTAINER
"""

import os
import re
import sys
import subprocess

try:
    import requests
except ImportError:
    print("FATAL: requests library not available", file=sys.stderr)
    sys.exit(1)

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def _require(var: str) -> str:
    val = os.getenv(var)
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)
    return val


ONLYOFFICE_PORT = _require("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = _require("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = _require("ONLYOFFICE_DB_CONTAINER")

OWNCLOUD_PORT = _require("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = _require("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = _require("OWNCLOUD_DB_CONTAINER")

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


def onlyoffice_db(sql: str) -> str:
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def owncloud_db(sql: str) -> str:
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OWNCLOUD_DB_CONTAINER,
        "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud", "-D", "owncloud",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mattermost_db(sql: str) -> str:
    rc, out, err = docker_exec(
        MATTERMOST_DB_CONTAINER,
        "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def roundcube_db(sql: str) -> str:
    rc, out, err = docker_exec(
        ROUNDCUBEMAIL_DB_CONTAINER,
        "mysql", "-u", "roundcube", "-proundcube123", "-D", "roundcubemail",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


# ── Shared state for OnlyOffice checks ────────────────────────────────────────
_oo_token: str = ""
_oo_base: str = ""
_oo_pres_id: int | None = None


def _oo_auth() -> bool:
    """Authenticate to OnlyOffice and cache token. Returns True on success."""
    global _oo_token, _oo_base
    if _oo_token:
        return True
    _oo_base = f"http://{HOST}:{ONLYOFFICE_PORT}"
    try:
        resp = requests.post(
            f"{_oo_base}/api/2.0/authentication",
            json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
            timeout=15,
        )
        _oo_token = resp.json().get("response", {}).get("token", "")
        return bool(_oo_token)
    except Exception:
        return False


# ── OnlyOffice document fetch chain (fs primary, API download fallback) ───────
_oo_file_cache: dict[str, bytes | None] = {}


def _oo_file_id(titles: list[str]) -> str:
    """files_file by exact title (with/without extension), latest id."""
    quoted = ", ".join("'" + t + "'" for t in titles)
    out = onlyoffice_db(
        f"SELECT id FROM files_file WHERE title IN ({quoted}) ORDER BY id DESC LIMIT 1;"
    )
    return out.splitlines()[0].strip() if out.strip() else ""


def _oo_file_bytes(title: str, ext: str) -> tuple[bytes | None, str]:
    """Fetch a document's raw bytes: files_file exact title -> data-dir
    content.<ext> (middle '*' covers version subdirs; highest version wins on
    multiple hits) -> API download fallback. Returns (bytes|None, detail)."""
    key = f"{title}.{ext}"
    if key in _oo_file_cache:
        return _oo_file_cache[key], "cached"
    notes: list[str] = []
    fid = ""
    try:
        fid = _oo_file_id([title, f"{title}.{ext}"])
    except Exception as e:
        notes.append(f"db id lookup failed: {e}")
    if not fid:
        notes.append("exact title not found in files_file")
    data: bytes | None = None
    if fid:
        try:
            rc, out, _err = docker_exec(
                ONLYOFFICE_CONTAINER, "bash", "-c",
                f"find /var/www/onlyoffice/Data -type f "
                f"-path '*file_{fid}/*content.{ext}' 2>/dev/null",
                timeout=30,
            )
            paths = [p.strip() for p in out.splitlines() if p.strip()]
            if paths:
                def _ver(p: str) -> int:
                    parent = p.rsplit("/", 2)[-2]
                    m = re.fullmatch(r"v?(\d+)", parent)
                    return int(m.group(1)) if m else 0
                path = sorted(paths, key=_ver)[-1]
                r = subprocess.run(
                    ["docker", "exec", ONLYOFFICE_CONTAINER, "cat", path],
                    capture_output=True, timeout=30,
                )
                if r.returncode == 0 and r.stdout:
                    data = r.stdout
                else:
                    notes.append("cat of content file failed")
            else:
                notes.append("content file not found in data dir")
        except Exception as e:
            notes.append(f"fs read failed: {e}")
        if data is None and _oo_auth():
            try:
                headers = {"Authorization": _oo_token}
                resp = requests.get(
                    f"{_oo_base}/api/2.0/files/file/{fid}/download",
                    headers=headers, timeout=30, allow_redirects=True,
                )
                if resp.status_code == 200 and len(resp.content) > 100:
                    data = resp.content
                else:
                    resp = requests.get(
                        f"{_oo_base}/products/files/httphandlers/filehandler.ashx",
                        params={"action": "download", "fileid": fid},
                        headers=headers, timeout=30, allow_redirects=True,
                    )
                    if resp.status_code == 200 and len(resp.content) > 100:
                        data = resp.content
                    else:
                        notes.append(f"api download failed (status={resp.status_code})")
            except Exception as e:
                notes.append(f"api download failed: {e}")
    _oo_file_cache[key] = data
    return data, ("fs/api ok" if data else "; ".join(notes) or "unavailable")


def _pptx_slide_texts(data: bytes) -> tuple[dict[int, str], dict[int, str]]:
    """Unzip pptx; per slide concatenate all <a:t> runs paragraph-wise (no
    injected spaces inside runs — OnlyOffice splits sentences across runs) and
    normalise whitespace. Returns ({slide_no: text}, {slide_no: raw xml})."""
    import zipfile
    import io
    import html
    zf = zipfile.ZipFile(io.BytesIO(data))
    texts: dict[int, str] = {}
    raws: dict[int, str] = {}
    for name in zf.namelist():
        m = re.fullmatch(r"ppt/slides/slide(\d+)\.xml", name)
        if not m:
            continue
        xml = zf.read(name).decode("utf-8", "replace")
        num = int(m.group(1))
        raws[num] = xml
        paras = []
        for chunk in xml.split("</a:p>"):
            runs = re.findall(r"<a:t(?: [^>]*)?>(.*?)</a:t>", chunk, re.S)
            if runs:
                paras.append("".join(html.unescape(r) for r in runs))
        texts[num] = " ".join("\n".join(paras).split())
    return texts, raws


# ── Roundcube sent-mail parsing (header-level, no whole-file grep) ────────────
SENT_SUBJECT = "Annual Strategy Session 2026 - Governance Binder and Resolutions"
EXPECTED_TO = {"sarah.obrien@mail.local", "ben.kowalski@mail.local", "emma.larsson@mail.local"}
EXPECTED_CC = {"rachel.goldberg@mail.local"}
EXPECTED_FROM_ADDR = "marcus.torres@mail.local"
EXPECTED_FROM_NAME = "Marcus Torres - Corporate Secretariat"
BODY_PROBES = (
    "complete governance binder for the Annual Strategy Session 2026",
    "come prepared to vote on the listed resolutions",
)
_SENT_DIRS = (
    "/var/mail/mail.local/james.whitfield/.Sent/cur",
    "/var/mail/mail.local/james.whitfield/.Sent/new",
)

_sent_state: dict = {"done": False, "path": "", "msg": None, "candidates": 0, "subject_hits": 0}


def _decode_mime_header(value: str) -> str:
    from email.header import decode_header
    out = []
    for part, enc in decode_header(value or ""):
        out.append(part.decode(enc or "ascii", "replace") if isinstance(part, bytes) else part)
    return "".join(out)


def _addr_set(msg, header: str) -> set[str]:
    from email.utils import getaddresses
    vals = msg.get_all(header) or []
    return {addr.strip().lower() for _n, addr in getaddresses(vals) if addr and addr.strip()}


def _from_pair(msg) -> tuple[str, str]:
    from email.utils import getaddresses
    pairs = getaddresses(msg.get_all("From") or [])
    if not pairs:
        return "", ""
    name, addr = pairs[0]
    return " ".join(_decode_mime_header(name).split()), addr.strip().lower()


def _body_norm(msg) -> str:
    chunks = []
    for part in msg.walk():
        if part.get_content_maintype() != "text":
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, "replace"))
    return " ".join("\n".join(chunks).split())


def _score_sent_msg(msg) -> int:
    score = 0
    if _addr_set(msg, "To") == EXPECTED_TO:
        score += 1
    if _addr_set(msg, "Cc") == EXPECTED_CC:
        score += 1
    name, addr = _from_pair(msg)
    if addr == EXPECTED_FROM_ADDR and name == EXPECTED_FROM_NAME:
        score += 1
    body = _body_norm(msg)
    if all(p in body for p in BODY_PROBES):
        score += 1
    return score


def _sent_email() -> dict:
    """Locate the sent mail with the exact expected subject in the sender's
    .Sent Maildir. On multiple copies pick the one satisfying the most
    assertions (each candidate is judged as a whole message)."""
    if _sent_state["done"]:
        return _sent_state
    _sent_state["done"] = True
    import email as _email
    try:
        _rc, out, _err = docker_exec(
            ROUNDCUBEMAIL_CONTAINER, "bash", "-c",
            f"find {_SENT_DIRS[0]} {_SENT_DIRS[1]} -type f 2>/dev/null",
            timeout=20,
        )
    except Exception:
        return _sent_state
    paths = [p.strip() for p in out.splitlines() if p.strip()]
    _sent_state["candidates"] = len(paths)
    best_score = -1
    for path in paths:
        try:
            r = subprocess.run(
                ["docker", "exec", ROUNDCUBEMAIL_CONTAINER, "cat", path],
                capture_output=True, timeout=20,
            )
        except Exception:
            continue
        if r.returncode != 0 or not r.stdout:
            continue
        try:
            msg = _email.message_from_bytes(r.stdout)
        except Exception:
            continue
        subj = " ".join(_decode_mime_header(msg.get("Subject", "")).split())
        if subj != SENT_SUBJECT:
            continue
        _sent_state["subject_hits"] += 1
        s = _score_sent_msg(msg)
        if s > best_score:
            best_score = s
            _sent_state["path"] = path
            _sent_state["msg"] = msg
    return _sent_state


# ── OnlyOffice checks ────────────────────────────────────────────────────────
def check_1_presentation_exists() -> None:
    """Presentation 'Annual_Strategy_Board_Session_2026' exists in Common Documents."""
    global _oo_pres_id
    try:
        if not _oo_auth():
            check("1. OnlyOffice presentation exists", 1, False, "auth failed")
            return
        headers = {"Authorization": _oo_token}
        resp = requests.get(f"{_oo_base}/api/2.0/files/@common", headers=headers, timeout=15)
        data = resp.json().get("response", {})
        files = data.get("files", [])

        pres = [f for f in files if "Annual_Strategy_Board_Session_2026" in f.get("title", "")]
        if pres:
            _oo_pres_id = pres[0]["id"]
        found = bool(pres)
        check("1. OnlyOffice presentation exists", 1, found,
              f"found={found}, titles={[f.get('title') for f in files[:5]]}")
    except Exception as e:
        check("1. OnlyOffice presentation exists", 1, False, f"exception: {e}")


def check_1b_presentation_content() -> None:
    """pptx content probe: exactly 3 slides, per-slide text anchors, agenda table."""
    label = "1b. Presentation slide content (pptx probe)"
    try:
        data, src = _oo_file_bytes("Annual_Strategy_Board_Session_2026", "pptx")
        if not data:
            check(label, 2, False, f"could not fetch pptx: {src}")
            return
        texts, raws = _pptx_slide_texts(data)
        missing: list[str] = []
        if len(texts) != 3:
            missing.append(f"slide count={len(texts)} (expected 3)")
        t1 = texts.get(1, "")
        t2 = texts.get(2, "")
        t3 = texts.get(3, "")
        for probe in (
            "Annual Strategy Board Session 2026",
            "Year-End Strategic Review and Forward Planning - Prepared by the Office of the Board Secretary",
        ):
            if probe not in t1:
                missing.append(f"slide1 {probe[:44]!r}")
        slide2_probes = (
            "Session Agenda",
            "Opening Remarks and Prior Minutes",
            "FY2026 Financial Close Preview",
            "2027 Product Strategy",
            "Engineering Roadmap and Platform Investments",
            "Talent and Culture Report",
            "Administrator",
            "Laura Brown",
            "Maria Wilson",
            "Jun Chen",
            "Amit Singh",
            "10 min",
            "30 min",
            "25 min",
            "20 min",
            "15 min",
        )
        for probe in slide2_probes:
            if probe not in t2:
                missing.append(f"slide2 {probe[:44]!r}")
        raw2 = raws.get(2, "")
        if not re.search(r"<a:tbl[ >]", raw2):
            missing.append("slide2 has no <a:tbl> table")
        else:
            rows = len(re.findall(r"<a:tr[ >]", raw2))
            if rows not in (5, 6):
                missing.append(f"slide2 table rows={rows} (expected 5 or 6 = data rows +/- header)")
        for probe in ("Resolutions and Closing", "record votes on the FY2027 operating plan"):
            if probe not in t3:
                missing.append(f"slide3 {probe[:44]!r}")
        passed = not missing
        detail = (f"source={src}, slides={sorted(texts)}" if passed
                  else "missing: " + "; ".join(missing[:6])
                  + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""))
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_shared_laura_edit() -> None:
    """Presentation shared with laura.brown for editing."""
    try:
        if not _oo_token or _oo_pres_id is None:
            check("2. Shared with laura.brown (edit)", 2, False, "no token or presentation not found")
            return
        headers = {"Authorization": _oo_token}
        resp = requests.get(
            f"{_oo_base}/api/2.0/files/file/{_oo_pres_id}/share",
            headers=headers, timeout=15,
        )
        shares = resp.json().get("response", [])

        # access: 1 = ReadWrite (edit), 2 = Read
        laura_edit = any(
            s.get("sharedTo", {}).get("userName", "") == "laura.brown"
            and s.get("access", -1) == 1
            for s in shares
        )
        check("2. Shared with laura.brown (edit)", 2, laura_edit,
              f"shares={[(s.get('sharedTo', {}).get('userName', '?'), s.get('access')) for s in shares]}")
    except Exception as e:
        check("2. Shared with laura.brown (edit)", 2, False, f"exception: {e}")


def check_3_shared_jun_view() -> None:
    """Presentation shared with jun.chen for viewing."""
    try:
        if not _oo_token or _oo_pres_id is None:
            check("3. Shared with jun.chen (view)", 2, False, "no token or presentation not found")
            return
        headers = {"Authorization": _oo_token}
        resp = requests.get(
            f"{_oo_base}/api/2.0/files/file/{_oo_pres_id}/share",
            headers=headers, timeout=15,
        )
        shares = resp.json().get("response", [])

        # access: 2 = Read (view)
        jun_view = any(
            s.get("sharedTo", {}).get("userName", "") == "jun.chen"
            and s.get("access", -1) == 2
            for s in shares
        )
        check("3. Shared with jun.chen (view)", 2, jun_view,
              f"shares={[(s.get('sharedTo', {}).get('userName', '?'), s.get('access')) for s in shares]}")
    except Exception as e:
        check("3. Shared with jun.chen (view)", 2, False, f"exception: {e}")


# ── ownCloud checks ──────────────────────────────────────────────────────────
def check_4_owncloud_folder_structure() -> None:
    """Folder 'Annual_Strategy_Session_2026' with subfolders exists."""
    try:
        main_id = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026';"
        )
        sub_fin = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026/Financial_Close';"
        )
        sub_gov = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026/Governance_Materials';"
        )
        passed = bool(main_id) and bool(sub_fin) and bool(sub_gov)
        check("4. ownCloud folder structure", 1, passed,
              f"main={bool(main_id)}, Financial_Close={bool(sub_fin)}, Governance_Materials={bool(sub_gov)}")
    except Exception as e:
        check("4. ownCloud folder structure", 1, False, f"exception: {e}")


def check_5_pdf_in_folder() -> None:
    """Exported presentation PDF (Annual_Strategy_Board_Session_2026*.pdf) in the main folder."""
    try:
        pdf = owncloud_db(
            "SELECT fc.name FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path LIKE 'files/Annual\\_Strategy\\_Session\\_2026/"
            "Annual\\_Strategy\\_Board\\_Session\\_2026%.pdf' "
            "AND fc.path NOT LIKE 'files/Annual\\_Strategy\\_Session\\_2026/%/%';"
        )
        passed = bool(pdf)
        check("5. Presentation PDF in Annual_Strategy_Session_2026", 1, passed,
              f"name={pdf or 'no Annual_Strategy_Board_Session_2026*.pdf at folder top level'}")
    except Exception as e:
        check("5. Presentation PDF in Annual_Strategy_Session_2026", 1, False, f"exception: {e}")


def check_6_resolution_log_content() -> None:
    """resolution_log.txt in Governance_Materials with correct content."""
    try:
        # Check existence in DB
        exists = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026/Governance_Materials/resolution_log.txt';"
        )
        if not exists:
            check("6. resolution_log.txt content", 1, False, "file not found in DB")
            return

        # Read via WebDAV
        resp = requests.get(
            f"http://{HOST}:{OWNCLOUD_PORT}/remote.php/dav/files/admin/"
            "Annual_Strategy_Session_2026/Governance_Materials/resolution_log.txt",
            auth=("admin", "admin"),
            timeout=15,
        )
        if resp.status_code != 200:
            check("6. resolution_log.txt content", 1, False, f"WebDAV GET status={resp.status_code}")
            return

        expected = (
            "Annual Strategy Session 2026 Resolution Log\n"
            "Resolution A: Adoption of FY2027 Operating Plan - PENDING\n"
            "Resolution B: Renewal of Audit Committee Charter - PENDING\n"
            "Resolution C: Ratification of Director Compensation - PENDING\n"
            "Quorum Verification: TBD\n"
            "Minutes Certified By: Office of the Board Secretary"
        )
        content = resp.text.rstrip("\n")
        passed = content == expected
        detail = "exact match" if passed else f"len={len(content)} vs expected={len(expected)}"
        check("6. resolution_log.txt content", 1, passed, detail)
    except Exception as e:
        check("6. resolution_log.txt content", 1, False, f"exception: {e}")


def check_7_tag_restricted() -> None:
    """Folder tagged 'Restricted'."""
    try:
        folder_id = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026';"
        )
        if not folder_id:
            check("7. Tag 'Restricted' on folder", 1, False, "folder not found")
            return

        tag = owncloud_db(
            "SELECT st.name FROM oc_systemtag st "
            "JOIN oc_systemtag_object_mapping stom ON st.id = stom.systemtagid "
            f"WHERE stom.objectid = '{folder_id}' AND stom.objecttype = 'files';"
        )
        passed = "Restricted" in tag
        check("7. Tag 'Restricted' on folder", 1, passed, f"tags={tag or 'none'}")
    except Exception as e:
        check("7. Tag 'Restricted' on folder", 1, False, f"exception: {e}")


def check_8_shared_admin_readonly() -> None:
    """Folder shared with group 'admin' read-only."""
    try:
        folder_id = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026';"
        )
        if not folder_id:
            check("8. Shared with group admin (read-only)", 1, False, "folder not found")
            return

        # share_type=1 is group share; permissions=1 is read-only
        share = owncloud_db(
            "SELECT share_type, share_with, permissions FROM oc_share "
            f"WHERE file_source = {folder_id} AND share_type = 1 AND share_with = 'admin';"
        )
        if not share:
            check("8. Shared with group admin (read-only)", 1, False, "no group share found")
            return

        parts = share.split("\t")
        permissions = int(parts[2]) if len(parts) >= 3 else -1
        passed = permissions == 1
        check("8. Shared with group admin (read-only)", 1, passed,
              f"permissions={permissions} (expected 1=read-only)")
    except Exception as e:
        check("8. Shared with group admin (read-only)", 1, False, f"exception: {e}")


# ── Mattermost checks ────────────────────────────────────────────────────────
def check_9_pinned_message_roadmap() -> None:
    """Pinned message in Roadmap with announcement anchors + the real ownCloud private link."""
    try:
        fid_out = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Annual_Strategy_Session_2026';"
        )
        fid = fid_out.splitlines()[0].strip() if fid_out.strip() else ""
        if not fid:
            check("9. Pinned message in Roadmap", 2, False,
                  "ownCloud folder fileid not found (cannot verify private link)")
            return

        channel_out = mattermost_db(
            "SELECT c.id FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.displayname = 'Roadmap' AND t.displayname = 'Product & Design';"
        )
        channel_id = channel_out.splitlines()[0].strip() if channel_out.strip() else ""
        if not channel_id:
            check("9. Pinned message in Roadmap", 2, False, "channel not found")
            return

        rows = mattermost_db(
            "SELECT replace(replace(p.message, chr(10), ' '), chr(13), ' ') "
            "FROM posts p "
            f"WHERE p.channelid = '{channel_id}' "
            "AND p.ispinned = true AND p.deleteat = 0 "
            "AND p.message LIKE '%Annual Strategy Session 2026 materials have been finalized%';"
        )
        link_re = re.compile(rf"(/f/{re.escape(fid)}\b|fileid={re.escape(fid)}\b)")
        anchor1 = ("Annual Strategy Session 2026 materials have been finalized. "
                   "Directors may access the complete governance binder through the "
                   "ownCloud private link")
        anchor2 = "Please review before the session."
        passed = False
        link_seen = False
        n_rows = 0
        for row in rows.splitlines():
            msg = " ".join(row.split())
            if not msg:
                continue
            n_rows += 1
            has_link = bool(link_re.search(msg))
            link_seen = link_seen or has_link
            if anchor1 in msg and anchor2 in msg and has_link:
                passed = True
                break
        check("9. Pinned message in Roadmap", 2, passed,
              f"folder_fileid={fid}, pinned_candidates={n_rows}, private_link_match={link_seen}")
    except Exception as e:
        check("9. Pinned message in Roadmap", 2, False, f"exception: {e}")


def check_10_dm_katheleen() -> None:
    """DM from admin in a D-channel whose members include katheleen, full-text anchor."""
    full_anchor = (
        "Hello Katheleen, kindly perform a legal review of the Annual Strategy "
        "Session 2026 governance binder referenced in the Roadmap channel. The "
        "resolution log and FY2027 plan need counsel sign-off prior to circulation. "
        "Thank you."
    )
    try:
        rows = mattermost_db(
            "SELECT replace(replace(p.message, chr(10), ' '), chr(13), ' ') "
            "FROM posts p "
            "JOIN channels c ON p.channelid = c.id "
            "JOIN channelmembers cm ON cm.channelid = c.id "
            "AND cm.userid = (SELECT id FROM users WHERE username = 'katheleen') "
            "WHERE c.type = 'D' AND p.deleteat = 0 "
            "AND p.userid = (SELECT id FROM users WHERE username = 'admin') "
            "AND p.message LIKE '%Hello Katheleen, kindly perform a legal review%';"
        )
        passed = any(
            full_anchor in " ".join(row.split())
            for row in rows.splitlines() if row.strip()
        )
        check("10. DM to katheleen", 2, passed,
              "full-text anchor matched in admin->katheleen DM" if passed
              else f"no DM row matched full anchor (candidates={len([r for r in rows.splitlines() if r.strip()])})")
    except Exception as e:
        check("10. DM to katheleen", 2, False, f"exception: {e}")


# ── Roundcube checks ─────────────────────────────────────────────────────────
def check_11_roundcube_identity() -> None:
    """Identity with correct email/org, all 4 plain-text signature lines, html_signature=0."""
    try:
        rows = roundcube_db(
            "SELECT email, organization, signature, html_signature FROM identities "
            "WHERE name = 'Marcus Torres - Corporate Secretariat' AND del = 0;"
        )
        if not rows:
            check("11. Roundcube identity", 1, False, "identity not found")
            return

        sig_lines = ("Marcus Torres", "Acting Board Secretary",
                     "Office of the Corporate Secretariat", "marcus.torres@mail.local")
        passed = False
        detail = ""
        for row in rows.splitlines():
            parts = row.split("\t")
            if len(parts) < 4:
                continue
            email_v, org, sig, html_sig = parts[0], parts[1], parts[2], parts[3].strip()
            email_ok = email_v == "marcus.torres@mail.local"
            org_ok = org == "Office of the Corporate Secretariat"
            sig_ok = all(line in sig for line in sig_lines)
            plain_ok = html_sig == "0"
            detail = (f"email_ok={email_ok}, org_ok={org_ok}, "
                      f"sig_4lines_ok={sig_ok}, html_signature={html_sig} (expected 0)")
            if email_ok and org_ok and sig_ok and plain_ok:
                passed = True
                break
        check("11. Roundcube identity", 1, passed, detail)
    except Exception as e:
        check("11. Roundcube identity", 1, False, f"exception: {e}")


def check_12_email_sent_subject_recipients() -> None:
    """Sent email: exact subject, To/Cc set equality, From identity, body probes."""
    label = "12. Email sent with correct subject/recipients"
    try:
        st = _sent_email()
        msg = st["msg"]
        if msg is None:
            check(label, 2, False,
                  f"no Sent mail with exact subject; scanned {st['candidates']} file(s) in "
                  "/var/mail/mail.local/james.whitfield/.Sent")
            return
        to_set = _addr_set(msg, "To")
        cc_set = _addr_set(msg, "Cc")
        name, addr = _from_pair(msg)
        body = _body_norm(msg)
        to_ok = to_set == EXPECTED_TO
        cc_ok = cc_set == EXPECTED_CC
        from_ok = addr == EXPECTED_FROM_ADDR and name == EXPECTED_FROM_NAME
        body_ok = all(p in body for p in BODY_PROBES)
        passed = to_ok and cc_ok and from_ok and body_ok
        check(label, 2, passed,
              f"to_ok={to_ok} ({sorted(to_set)}), cc_ok={cc_ok} ({sorted(cc_set)}), "
              f"from_ok={from_ok} ({name!r} <{addr}>), body_ok={body_ok}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_13_email_priority_high() -> None:
    """Sent email priority High: X-Priority == 2 only (or Importance: high)."""
    try:
        st = _sent_email()
        msg = st["msg"]
        if msg is None:
            check("13. Email priority high", 1, False, "sent email not found")
            return
        xp = (msg.get("X-Priority") or "").strip()
        token = xp.split()[0] if xp else ""
        imp = (msg.get("Importance") or "").strip().lower()
        # Roundcube "High" emits X-Priority: 2; 1 = "Highest" and is NOT accepted.
        passed = token == "2" or imp == "high"
        check("13. Email priority high", 1, passed,
              f"X-Priority={xp or 'absent'}, Importance={imp or 'absent'} "
              "(High requires X-Priority 2 or Importance high)")
    except Exception as e:
        check("13. Email priority high", 1, False, f"exception: {e}")


def check_14_email_flagged_sent() -> None:
    """The sent email's Maildir copy carries the F (flagged) flag."""
    try:
        st = _sent_email()
        msg = st["msg"]
        path = st["path"]
        if msg is None or not path:
            check("14. Sent email flagged", 1, False, "sent email not found in .Sent maildir")
            return
        base = path.rsplit("/", 1)[-1]
        flags = base.split(":2,", 1)[1] if ":2," in base else ""
        passed = "F" in flags
        check("14. Sent email flagged", 1, passed,
              f"maildir flags={flags or 'none'} (file={base[:60]})")
    except Exception as e:
        check("14. Sent email flagged", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_presentation_exists()
    check_1b_presentation_content()
    check_2_shared_laura_edit()
    check_3_shared_jun_view()
    check_4_owncloud_folder_structure()
    check_5_pdf_in_folder()
    check_6_resolution_log_content()
    check_7_tag_restricted()
    check_8_shared_admin_readonly()
    check_9_pinned_message_roadmap()
    check_10_dm_katheleen()
    check_11_roundcube_identity()
    check_12_email_sent_subject_recipients()
    check_13_email_priority_high()
    check_14_email_flagged_sent()

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
