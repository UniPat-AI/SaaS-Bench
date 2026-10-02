"""
Verifier for Teamwork-074-I2: Globex Q2 2026 QBR Package Preparation and Distribution

Checks: 17 weighted checks (25 pts) across onlyoffice, owncloud, mattermost, roundcubemail.
Strategy: docker exec (DB) + data-dir xlsx/pptx content probes + API share double-assert
(OnlyOffice); DB + WebDAV (ownCloud); DB with per-row judging (Mattermost); DB +
Sent-maildir header parsing (Roundcube).

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
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")


def require_env(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"FATAL: {name} not set", file=sys.stderr)
        sys.exit(1)
    return val


ONLYOFFICE_PORT = require_env("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = require_env("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = require_env("ONLYOFFICE_DB_CONTAINER")

OWNCLOUD_PORT = require_env("OWNCLOUD_PORT")
OWNCLOUD_CONTAINER = require_env("OWNCLOUD_CONTAINER")
OWNCLOUD_DB_CONTAINER = require_env("OWNCLOUD_DB_CONTAINER")

MATTERMOST_PORT = require_env("MATTERMOST_PORT")
MATTERMOST_CONTAINER = require_env("MATTERMOST_CONTAINER")
MATTERMOST_DB_CONTAINER = require_env("MATTERMOST_DB_CONTAINER")

ROUNDCUBEMAIL_PORT = require_env("ROUNDCUBEMAIL_PORT")
ROUNDCUBEMAIL_CONTAINER = require_env("ROUNDCUBEMAIL_CONTAINER")
ROUNDCUBEMAIL_DB_CONTAINER = require_env("ROUNDCUBEMAIL_DB_CONTAINER")

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


def docker_exec_env(container: str, env: dict, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    cmd = ["docker", "exec"]
    for k, v in env.items():
        cmd += ["-e", f"{k}={v}"]
    cmd.append(container)
    cmd.extend(args)
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def onlyoffice_db(sql: str) -> str:
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "onlyoffice", "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def owncloud_db(sql: str) -> str:
    # -h 127.0.0.1: the ownCloud MariaDB image ships anonymous ''@'localhost'
    # users that shadow 'owncloud'@'%' over the unix socket.
    rc, out, err = docker_exec(
        OWNCLOUD_DB_CONTAINER,
        "mysql", "-h", "127.0.0.1", "-u", "owncloud", "-powncloud",
        "--default-character-set=utf8mb4", "owncloud", "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"owncloud mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def mattermost_db(sql: str) -> str:
    rc, out, err = docker_exec_env(
        MATTERMOST_DB_CONTAINER,
        {"PGPASSWORD": "mmuser_password"},
        "psql", "-U", "mmuser", "-d", "mattermost", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"mattermost psql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


def roundcube_db(sql: str) -> str:
    rc, out, err = docker_exec(
        ROUNDCUBEMAIL_DB_CONTAINER,
        "mysql", "-u", "roundcube", "-proundcube123",
        "--default-character-set=utf8mb4", "roundcubemail", "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"roundcube mysql query failed (rc={rc}): {' '.join(err.split())[-300:]}")
    return out.strip()


# ── OnlyOffice auth + document fetch chain (fs primary, API fallback) ─────────
_oo_token: str = ""
_oo_base: str = ""


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


def _xlsx_extract(data: bytes) -> tuple[str, list[str], list[str]]:
    """Unzip xlsx. Returns (normalised text = sharedStrings <t> concatenation
    plus sheet1 inline <t>, list of <v> cell values from sheet1.xml, list of
    <f> formula texts from sheet1.xml). Numeric cells never enter
    sharedStrings, so number probes must use the sheet <v> values."""
    import zipfile
    import io
    import html
    zf = zipfile.ZipFile(io.BytesIO(data))
    names = zf.namelist()
    shared = ""
    if "xl/sharedStrings.xml" in names:
        xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "replace")
        shared = " ".join(
            html.unescape(t) for t in re.findall(r"<t(?: [^>]*)?>(.*?)</t>", xml, re.S)
        )
    sheet_name = "xl/worksheets/sheet1.xml"
    if sheet_name not in names:
        sheets = sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n))
        sheet_name = sheets[0] if sheets else ""
    sheet_xml = zf.read(sheet_name).decode("utf-8", "replace") if sheet_name else ""
    inline = " ".join(
        html.unescape(t) for t in re.findall(r"<t(?: [^>]*)?>(.*?)</t>", sheet_xml, re.S)
    )
    values = [v.strip() for v in re.findall(r"<v(?: [^>]*)?>([^<]*)</v>", sheet_xml)]
    formulas = [html.unescape(f) for f in re.findall(r"<f(?: [^>]*)?>(.*?)</f>", sheet_xml, re.S)]
    text = " ".join((shared + " " + inline).split())
    return text, values, formulas


# ── Roundcube sent-mail parsing (header-level, no whole-file grep) ────────────
SENT_SUBJECT = ("Globex Industries Q2 2026 Quarterly Business Review"
                " - Proposed Meeting Date")
EXPECTED_TO = {"linda.park@globexindustries.com"}
EXPECTED_CC = {"emma.larsson@mail.local"}
EXPECTED_FROM_ADDR = "marcus.torres@mail.local"
EXPECTED_FROM_NAME = "Marcus Torres - Sales Operations"
BODY_PROBES = (
    "Wednesday, July 29, 2026 at 10:00 AM ET",
    "1. Globex Industries Q2 2026 Quarterly Business Review",
    "2. Q2 2026 Performance Metrics",
    "3. Q2 Performance Highlights",
    "4. Q3 2026 Proposed Initiatives",
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


def _prio_normal_ok(msg) -> bool:
    """Normal priority: no X-Priority header, or its numeric token == 3."""
    xp = (msg.get("X-Priority") or "").strip()
    return (not xp) or xp.split()[0] == "3"


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
    if _prio_normal_ok(msg):
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


# ── Shared state across Mattermost checks ─────────────────────────────────────
_ck9_post_id: str = ""


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_onlyoffice_spreadsheet() -> None:
    """Spreadsheet 'Globex_Industries_Q2_2026_QBR_Data' exists (exact title)."""
    try:
        result = onlyoffice_db(
            "SELECT id FROM files_file WHERE title IN "
            "('Globex_Industries_Q2_2026_QBR_Data', 'Globex_Industries_Q2_2026_QBR_Data.xlsx');"
        )
        passed = bool(result.strip())
        check("1. OnlyOffice spreadsheet exists", 1, passed,
              "found" if passed else "no exact-title match in files_file")
    except Exception as e:
        check("1. OnlyOffice spreadsheet exists", 1, False, f"exception: {e}")


def check_1b_spreadsheet_content() -> None:
    """xlsx probe: headers, months, numeric <v> cells, Average/Max/Min labels, formulas."""
    label = "1b. Spreadsheet content (headers/data/formulas)"
    try:
        data, src = _oo_file_bytes("Globex_Industries_Q2_2026_QBR_Data", "xlsx")
        if not data:
            check(label, 2, False, f"could not fetch xlsx: {src}")
            return
        text, values, formulas = _xlsx_extract(data)
        missing: list[str] = []
        for probe in ("Month", "Revenue", "Support Tickets", "SLA Compliance %", "NPS Score",
                      "April", "May", "June", "Average", "Max", "Min"):
            if probe not in text:
                missing.append(f"text {probe!r}")
        fvals = []
        for v in values:
            try:
                fvals.append(float(v))
            except ValueError:
                pass
        for target in ("187500", "203000", "221000", "54", "47", "39",
                       "97.8", "98.6", "99.2", "58", "63", "68"):
            t = float(target)
            if not any(abs(fv - t) < 1e-6 for fv in fvals) and target not in text:
                missing.append(f"value {target}")
        # Formulas: case-insensitive, strip _xlfn. prefix, tolerate $ in ranges.
        norm_formulas = [f.upper().replace("_XLFN.", "").replace("$", "") for f in formulas]
        for func in ("AVERAGE", "MAX", "MIN"):
            pat = re.compile(func + r"\([B-E]2:[B-E]4")
            if not any(pat.search(f) for f in norm_formulas):
                missing.append(f"no {func}(B2:B4)-style <f> formula (cols B-E)")
        passed = not missing
        detail = (f"source={src}, formulas={len(formulas)}" if passed
                  else "missing: " + "; ".join(missing[:8])
                  + (f" (+{len(missing) - 8} more)" if len(missing) > 8 else ""))
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_2_onlyoffice_presentation() -> None:
    """Presentation 'Globex_Industries_Q2_2026_QBR_Presentation' exists (exact title)."""
    try:
        result = onlyoffice_db(
            "SELECT id FROM files_file WHERE title IN "
            "('Globex_Industries_Q2_2026_QBR_Presentation', "
            "'Globex_Industries_Q2_2026_QBR_Presentation.pptx');"
        )
        passed = bool(result.strip())
        check("2. OnlyOffice presentation exists", 1, passed,
              "found" if passed else "no exact-title match in files_file")
    except Exception as e:
        check("2. OnlyOffice presentation exists", 1, False, f"exception: {e}")


def check_2b_presentation_content() -> None:
    """pptx probe: exactly 4 slides with per-slide text anchors."""
    label = "2b. Presentation slide content (pptx probe)"
    try:
        data, src = _oo_file_bytes("Globex_Industries_Q2_2026_QBR_Presentation", "pptx")
        if not data:
            check(label, 2, False, f"could not fetch pptx: {src}")
            return
        texts, _raws = _pptx_slide_texts(data)
        missing: list[str] = []
        if len(texts) != 4:
            missing.append(f"slide count={len(texts)} (expected 4)")
        t1 = texts.get(1, "")
        t2 = texts.get(2, "")
        t3 = texts.get(3, "")
        t4 = texts.get(4, "")
        for probe in ("Globex Industries Q2 2026 Quarterly Business Review",
                      "Prepared for Globex Industries Leadership Team"):
            if probe not in t1:
                missing.append(f"slide1 {probe[:44]!r}")
        if "Q2 2026 Performance Metrics" not in t2:
            missing.append("slide2 'Q2 2026 Performance Metrics'")
        if "187500" not in t2 and "187,500" not in t2:
            missing.append("slide2 revenue token (187500 or 187,500)")
        for probe in ("$203,833", "$221,000 (June)", "99.2%", "Peak NPS: 68"):
            if probe not in t3:
                missing.append(f"slide3 {probe!r}")
        for probe in ("Enterprise SSO integration",
                      "custom analytics dashboard with real-time metrics",
                      "dedicated technical account management pod"):
            if probe not in t4:
                missing.append(f"slide4 {probe[:44]!r}")
        passed = not missing
        detail = (f"source={src}, slides={sorted(texts)}" if passed
                  else "missing: " + "; ".join(missing[:6])
                  + (f" (+{len(missing) - 6} more)" if len(missing) > 6 else ""))
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_onlyoffice_shares() -> None:
    """Presentation shared with jun.chen access==1 (edit) AND amit.singh access==2 (view).

    Two independent strict assertions on the presentation file id. API primary
    (sharedTo.userName exact + access exact int); DB fallback joins core_user on
    the files_security subject GUID with exact username + security level."""
    try:
        fid = _oo_file_id([
            "Globex_Industries_Q2_2026_QBR_Presentation",
            "Globex_Industries_Q2_2026_QBR_Presentation.pptx",
        ])
        if not fid:
            check("3. OnlyOffice presentation shares", 2, False, "presentation not found")
            return

        jun_ok = amit_ok = False
        sources: list[str] = []
        if _oo_auth():
            try:
                resp = requests.get(
                    f"{_oo_base}/api/2.0/files/file/{fid}/share",
                    headers={"Authorization": _oo_token}, timeout=15,
                )
                shares = resp.json().get("response", []) or []
                # access: 1 = ReadWrite (edit), 2 = Read (view)
                jun_ok = any(
                    s.get("sharedTo", {}).get("userName", "") == "jun.chen"
                    and s.get("access", -1) == 1
                    for s in shares
                )
                amit_ok = any(
                    s.get("sharedTo", {}).get("userName", "") == "amit.singh"
                    and s.get("access", -1) == 2
                    for s in shares
                )
                sources.append(
                    "api shares=" + str([(s.get("sharedTo", {}).get("userName", "?"),
                                          s.get("access")) for s in shares])
                )
            except Exception as e:
                sources.append(f"api share query failed: {e}")
        else:
            sources.append("api auth failed")

        if not (jun_ok and amit_ok):
            try:
                if not jun_ok:
                    jun_db = onlyoffice_db(
                        "SELECT 1 FROM files_security fs "
                        "JOIN core_user cu ON fs.subject = cu.id "
                        "WHERE fs.entry_type = 2 "
                        f"AND fs.entry_id = '{fid}' "
                        "AND cu.username = 'jun.chen' AND fs.security = 1;"
                    )
                    jun_ok = bool(jun_db.strip())
                if not amit_ok:
                    amit_db = onlyoffice_db(
                        "SELECT 1 FROM files_security fs "
                        "JOIN core_user cu ON fs.subject = cu.id "
                        "WHERE fs.entry_type = 2 "
                        f"AND fs.entry_id = '{fid}' "
                        "AND cu.username = 'amit.singh' AND fs.security = 2;"
                    )
                    amit_ok = bool(amit_db.strip())
                sources.append("db fallback consulted")
            except Exception as e:
                sources.append(f"db fallback failed: {e}")

        passed = jun_ok and amit_ok
        check("3. OnlyOffice presentation shares", 2, passed,
              f"jun.chen_edit={jun_ok}, amit.singh_view={amit_ok}; "
              + "; ".join(sources)[:280])
    except Exception as e:
        check("3. OnlyOffice presentation shares", 2, False, f"exception: {e}")


def check_4_owncloud_folders() -> None:
    """Folder Globex_Industries_QBR_Q2_2026 with MetricsData and SlideDeck subfolders."""
    try:
        result = owncloud_db(
            "SELECT path FROM oc_filecache "
            "WHERE path LIKE 'files/Globex\\_Industries\\_QBR\\_Q2\\_2026%' "
            "AND mimetype = (SELECT id FROM oc_mimetypes WHERE mimetype='httpd/unix-directory')"
        )
        has_main = "files/Globex_Industries_QBR_Q2_2026" in result
        has_metrics = "MetricsData" in result
        has_slide = "SlideDeck" in result
        passed = has_main and has_metrics and has_slide
        detail = (f"main={'ok' if has_main else 'missing'}, "
                  f"MetricsData={'ok' if has_metrics else 'missing'}, "
                  f"SlideDeck={'ok' if has_slide else 'missing'}")
        check("4. ownCloud folder structure", 1, passed, detail)
    except Exception as e:
        check("4. ownCloud folder structure", 1, False, f"exception: {e}")


def check_5_owncloud_pdf() -> None:
    """PDF file exists in SlideDeck subfolder."""
    try:
        result = owncloud_db(
            "SELECT name FROM oc_filecache "
            "WHERE path LIKE 'files/Globex\\_Industries\\_QBR\\_Q2\\_2026/SlideDeck/%' "
            "AND name LIKE '%.pdf'"
        )
        passed = bool(result) and ".pdf" in result.lower()
        check("5. ownCloud PDF in SlideDeck", 1, passed,
              result if passed else "no PDF found in SlideDeck")
    except Exception as e:
        check("5. ownCloud PDF in SlideDeck", 1, False, f"exception: {e}")


def check_6_owncloud_metric_sources() -> None:
    """metric_sources.txt in MetricsData with correct data-source content."""
    try:
        exists = owncloud_db(
            "SELECT name FROM oc_filecache "
            "WHERE path = 'files/Globex_Industries_QBR_Q2_2026/MetricsData/metric_sources.txt'"
        )
        if not exists:
            check("6. ownCloud metric_sources.txt", 2, False, "file not found in filecache")
            return

        url = (f"http://{HOST}:{OWNCLOUD_PORT}/remote.php/dav/files/admin/"
               "Globex_Industries_QBR_Q2_2026/MetricsData/metric_sources.txt")
        resp = requests.get(url, auth=("admin", "admin"), timeout=15)
        content = resp.text

        has_hubspot = "HubSpot" in content
        has_intercom = "Intercom" in content
        has_sla = "SLA" in content
        has_nps = "AskNicely" in content

        passed = has_hubspot and has_intercom and has_sla and has_nps
        detail = (f"HubSpot={'ok' if has_hubspot else 'missing'}, "
                  f"Intercom={'ok' if has_intercom else 'missing'}, "
                  f"SLA={'ok' if has_sla else 'missing'}, "
                  f"AskNicely={'ok' if has_nps else 'missing'}")
        check("6. ownCloud metric_sources.txt", 2, passed, detail)
    except Exception as e:
        check("6. ownCloud metric_sources.txt", 2, False, f"exception: {e}")


def check_7_owncloud_shares() -> None:
    """admin group share rw on main folder, admin group share ro on SlideDeck."""
    try:
        # share_type=1 means group share
        # permissions: 1=read, 15=read+update+create+delete, 31=all
        shares = owncloud_db(
            "SELECT file_target, share_with, permissions FROM oc_share "
            "WHERE share_with = 'admin' AND share_type = 1 "
            "AND (file_target LIKE '%Globex\\_Industries\\_QBR\\_Q2\\_2026%' "
            "     OR file_target LIKE '%SlideDeck%')"
        )

        has_main_rw = False
        has_slide_ro = False

        for line in shares.split("\n"):
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 3:
                target = parts[0]
                try:
                    perms = int(parts[2].strip())
                except ValueError:
                    continue
                if "SlideDeck" in target and perms == 1:
                    has_slide_ro = True
                elif ("Globex_Industries_QBR_Q2_2026" in target
                      and "SlideDeck" not in target
                      and perms >= 15):
                    has_main_rw = True

        passed = has_main_rw and has_slide_ro
        detail = (f"main_rw={'ok' if has_main_rw else 'missing'}, "
                  f"slidedeck_ro={'ok' if has_slide_ro else 'missing'}")
        if not shares:
            detail = "no matching group shares found"
        check("7. ownCloud shares", 2, passed, detail)
    except Exception as e:
        check("7. ownCloud shares", 2, False, f"exception: {e}")


def check_8_mattermost_channel() -> None:
    """Private channel globex-qbr-q2-review in Product & Design with correct header/purpose."""
    try:
        result = mattermost_db(
            "SELECT c.name, c.type, c.header, c.purpose FROM channels c "
            "JOIN teams t ON c.teamid = t.id "
            "WHERE c.name = 'globex-qbr-q2-review' "
            "AND t.displayname = 'Product & Design'"
        )
        if not result:
            check("8. Mattermost channel exists", 1, False,
                  "channel not found in team 'Product & Design'")
            return

        parts = result.split("|")
        is_private = len(parts) >= 2 and parts[1].strip() == "P"
        has_header = ("Internal review channel for Globex Industries Q2 2026 QBR deliverables"
                      in result)
        has_purpose = ("Coordinate internal review and approval of the Globex Industries "
                       "Q2 2026 QBR presentation and supporting data before client delivery"
                       in result)

        passed = is_private and has_header and has_purpose
        detail = (f"private={'ok' if is_private else 'no'}, "
                  f"header={'ok' if has_header else 'wrong'}, "
                  f"purpose={'ok' if has_purpose else 'wrong'}")
        check("8. Mattermost channel exists", 1, passed, detail)
    except Exception as e:
        check("8. Mattermost channel exists", 1, False, f"exception: {e}")


def check_9_mattermost_review_message() -> None:
    """Review request root post with anchor + 2026-07-22 deadline + real SlideDeck private link."""
    global _ck9_post_id
    try:
        fid_out = owncloud_db(
            "SELECT fc.fileid FROM oc_filecache fc "
            "JOIN oc_storages s ON fc.storage = s.numeric_id "
            "WHERE s.id = 'home::admin' "
            "AND fc.path = 'files/Globex_Industries_QBR_Q2_2026/SlideDeck';"
        )
        fid = fid_out.splitlines()[0].strip() if fid_out.strip() else ""
        if not fid:
            check("9. Mattermost review message", 2, False,
                  "SlideDeck fileid not found in oc_filecache (cannot verify private link)")
            return

        rows = mattermost_db(
            "SELECT p.id || '|' || replace(replace(p.message, chr(10), ' '), chr(13), ' ') "
            "FROM posts p JOIN channels c ON p.channelid = c.id "
            "WHERE c.name = 'globex-qbr-q2-review' "
            "AND p.rootid = '' AND p.deleteat = 0 "
            "AND p.message LIKE '%please review the Globex Industries Q2 2026 QBR%' "
            "ORDER BY p.createat ASC;"
        )
        link_re = re.compile(rf"(/f/{re.escape(fid)}\b|fileid={re.escape(fid)}\b)")
        hit_id = ""
        link_seen = False
        n_rows = 0
        for row in rows.splitlines():
            if "|" not in row:
                continue
            pid, message = row.split("|", 1)
            message = " ".join(message.split())
            if not message:
                continue
            n_rows += 1
            has_link = bool(link_re.search(message))
            link_seen = link_seen or has_link
            if ("please review the Globex Industries Q2 2026 QBR package" in message
                    and "2026-07-22" in message and has_link):
                hit_id = pid.strip()
                break
        _ck9_post_id = hit_id
        check("9. Mattermost review message", 2, bool(hit_id),
              f"slidedeck_fileid={fid}, candidates={n_rows}, private_link_match={link_seen}")
    except Exception as e:
        check("9. Mattermost review message", 2, False, f"exception: {e}")


def check_10_mattermost_thread_reply() -> None:
    """Single thread reply (rootid = review post) tagging @christene with all anchors."""
    try:
        if not _ck9_post_id:
            check("10. Mattermost thread reply @christene", 2, False,
                  "review message (check 9) not identified; cannot bind rootid")
            return
        rows = mattermost_db(
            "SELECT replace(replace(p.message, chr(10), ' '), chr(13), ' ') "
            "FROM posts p "
            f"WHERE p.rootid = '{_ck9_post_id}' AND p.deleteat = 0;"
        )
        probes = ("@christene",
                  "SLA compliance figures and ticket volume counts",
                  "slides 2 and 3",
                  "sign-off")
        passed = False
        n_rows = 0
        for row in rows.splitlines():
            message = " ".join(row.split())
            if not message:
                continue
            n_rows += 1
            if all(p in message for p in probes):
                passed = True
                break
        check("10. Mattermost thread reply @christene", 2, passed,
              "single reply matched all anchors" if passed
              else f"no single reply on the review thread matched all anchors (replies={n_rows})")
    except Exception as e:
        check("10. Mattermost thread reply @christene", 2, False, f"exception: {e}")


def check_11_mattermost_reaction() -> None:
    """Thumbsup reaction on the original review request message."""
    try:
        result = mattermost_db(
            "SELECT r.emojiname FROM reactions r "
            "JOIN posts p ON r.postid = p.id "
            "JOIN channels c ON p.channelid = c.id "
            "WHERE c.name = 'globex-qbr-q2-review' "
            "AND p.rootid = '' "
            "AND p.message LIKE '%please review%' "
            "AND r.emojiname IN ('thumbsup', '+1')"
        )
        # Mattermost stores '+1' for the thumbsup emoji picked from the picker
        passed = "thumbsup" in result or "+1" in result
        check("11. Mattermost thumbsup reaction", 1, passed,
              "found" if passed else "no thumbsup/+1 reaction on review message")
    except Exception as e:
        check("11. Mattermost thumbsup reaction", 1, False, f"exception: {e}")


def check_12_roundcube_identity() -> None:
    """Identity 'Marcus Torres - Sales Operations': exact email/org, all 5 signature lines."""
    try:
        rows = roundcube_db(
            "SELECT email, organization, signature FROM identities "
            "WHERE name = 'Marcus Torres - Sales Operations' AND del = 0;"
        )
        if not rows:
            check("12. Roundcube identity", 2, False, "identity not found")
            return

        sig_lines = ("Marcus Torres", "Sales Operations Manager", "TechCorp",
                     "marcus.torres@mail.local", "+1-555-0187")
        passed = False
        detail = ""
        for row in rows.splitlines():
            parts = row.split("\t")
            if len(parts) < 3:
                continue
            email_v, org, sig = parts[0], parts[1], parts[2]
            email_ok = email_v == "marcus.torres@mail.local"
            org_ok = org == "Sales Operations - TechCorp"
            sig_missing = [line for line in sig_lines if line not in sig]
            detail = (f"email_ok={email_ok}, org_ok={org_ok}, "
                      f"sig_missing={sig_missing or 'none'}")
            if email_ok and org_ok and not sig_missing:
                passed = True
                break
        check("12. Roundcube identity", 2, passed, detail)
    except Exception as e:
        check("12. Roundcube identity", 2, False, f"exception: {e}")


def check_13_roundcube_email_sent() -> None:
    """Sent email: exact subject, To/Cc set equality, From identity, body probes, Normal priority."""
    try:
        st = _sent_email()
        msg = st["msg"]
        if msg is None:
            check("13. Roundcube email sent", 2, False,
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
        body_missing = [p for p in BODY_PROBES if p not in body]
        xp = (msg.get("X-Priority") or "").strip()
        prio_ok = _prio_normal_ok(msg)
        passed = to_ok and cc_ok and from_ok and not body_missing and prio_ok
        check("13. Roundcube email sent", 2, passed,
              f"to_ok={to_ok} ({sorted(to_set)}), cc_ok={cc_ok} ({sorted(cc_set)}), "
              f"from_ok={from_ok} ({name!r} <{addr}>), "
              f"body_missing={[p[:30] for p in body_missing] or 'none'}, "
              f"normal_priority={prio_ok} (X-Priority={xp or 'absent'})")
    except Exception as e:
        check("13. Roundcube email sent", 2, False, f"exception: {e}")


def check_14_roundcube_contact() -> None:
    """Contact Linda Park owned by james.whitfield with Globex Industries ORG in vcard."""
    try:
        rows = roundcube_db(
            "SELECT c.email, (c.vcard LIKE '%ORG%Globex Industries%') FROM contacts c "
            "JOIN users u ON c.user_id = u.user_id "
            "WHERE u.username = 'james.whitfield@mail.local' "
            "AND c.firstname = 'Linda' AND c.surname = 'Park' AND c.del = 0;"
        )
        if not rows:
            check("14. Roundcube contact Linda Park", 1, False,
                  "no Linda Park contact owned by james.whitfield@mail.local")
            return
        passed = False
        detail = ""
        for row in rows.splitlines():
            parts = row.split("\t")
            if len(parts) < 2:
                continue
            email_v, org_flag = parts[0], parts[1].strip()
            email_ok = "linda.park@globexindustries.com" in email_v.lower()
            org_ok = org_flag == "1"
            detail = f"email_ok={email_ok}, vcard_org_globex={org_ok}"
            if email_ok and org_ok:
                passed = True
                break
        check("14. Roundcube contact Linda Park", 1, passed, detail)
    except Exception as e:
        check("14. Roundcube contact Linda Park", 1, False, f"exception: {e}")


def check_15_default_addressbook_pref() -> None:
    """Record-only (0pt): default_addressbook preference present for james.whitfield."""
    try:
        prefs = roundcube_db(
            "SELECT preferences FROM users WHERE username = 'james.whitfield@mail.local';"
        )
        present = '"default_addressbook"' in prefs
        check("15. Default addressbook pref (record-only)", 0, present,
              ("pref present" if present else "pref absent")
              + "; 0pt record-only: Roundcube drops prefs equal to the built-in default, "
              "so a correct 'Personal Addresses' selection may legitimately not persist")
    except Exception as e:
        check("15. Default addressbook pref (record-only)", 0, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_onlyoffice_spreadsheet()
    check_1b_spreadsheet_content()
    check_2_onlyoffice_presentation()
    check_2b_presentation_content()
    check_3_onlyoffice_shares()
    check_4_owncloud_folders()
    check_5_owncloud_pdf()
    check_6_owncloud_metric_sources()
    check_7_owncloud_shares()
    check_8_mattermost_channel()
    check_9_mattermost_review_message()
    check_10_mattermost_thread_reply()
    check_11_mattermost_reaction()
    check_12_roundcube_identity()
    check_13_roundcube_email_sent()
    check_14_roundcube_contact()
    check_15_default_addressbook_pref()

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
