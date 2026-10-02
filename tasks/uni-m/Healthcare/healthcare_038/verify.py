"""
Verifier for Healthcare-038-I1: HAI Surveillance — Exposure Form, Infection Documentation, Monthly Report

Checks: 17 weighted checks (incl. 0pt precondition gates) across opnform, openemr, onlyoffice.
Strategy: docker exec (DB queries) for all three sites; OnlyOffice xlsx fetched from the
portal data dir (fs-first, API fallback) and parsed with zipfile.

Required env vars:
  SERVER_HOSTNAME,
  OPNFORM_PORT, OPNFORM_CONTAINER,
  OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  ONLYOFFICE_PORT, ONLYOFFICE_CONTAINER, ONLYOFFICE_DB_CONTAINER
"""

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPNFORM_PORT = os.environ.get("OPNFORM_PORT")
OPNFORM_CONTAINER = os.environ.get("OPNFORM_CONTAINER")

OPENEMR_PORT = os.environ.get("OPENEMR_PORT")
OPENEMR_CONTAINER = os.environ.get("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.environ.get("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.environ.get("ONLYOFFICE_DB_CONTAINER")

_required = {
    "OPNFORM_PORT": OPNFORM_PORT,
    "OPNFORM_CONTAINER": OPNFORM_CONTAINER,
    "OPENEMR_PORT": OPENEMR_PORT,
    "OPENEMR_CONTAINER": OPENEMR_CONTAINER,
    "OPENEMR_DB_CONTAINER": OPENEMR_DB_CONTAINER,
    "ONLYOFFICE_PORT": ONLYOFFICE_PORT,
    "ONLYOFFICE_CONTAINER": ONLYOFFICE_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": ONLYOFFICE_DB_CONTAINER,
}
for _k, _v in _required.items():
    if not _v:
        print(f"FATAL: {_k} not set", file=sys.stderr)
        sys.exit(1)

FORM_TITLE = "HAI Staff Exposure Incident Report - March 2026"
# probed seed max, mw-openemr:latest 2026-08 (form_encounter MAX(date)='2026-03-05 00:00:00')
ENCOUNTER_ANCHOR = "2026-03-05"

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


def _one(s: object, limit: int = 160) -> str:
    """Single-line, truncated detail text."""
    return " ".join(str(s).split())[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def opnform_sql(query: str) -> str:
    """Query OpnForm's embedded Postgres (forge DB, forge user)."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql error (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def openemr_sql(query: str) -> str:
    """Query OpenEMR's MariaDB."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql error (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Query OnlyOffice's MySQL 8.0."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql error (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


# ── OpnForm property helpers ──────────────────────────────────────────────────
def _norm(s: object) -> str:
    return " ".join(str(s).lower().split())


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


def _find_field(props: list[dict], name: str) -> dict | None:
    tgt = _norm(name)
    for p in props:
        if _norm(p.get("name", "")) == tgt:
            return p
    for p in props:
        if tgt in _norm(p.get("name", "")):
            return p
    return None


def _option_names(field: dict, key: str) -> set[str]:
    opts = ((field.get(key) or {}).get("options")) or []
    return {_norm(o.get("name", "")) for o in opts if isinstance(o, dict)}


def _logic_leaves(node) -> list[dict]:
    """Recursively collect condition leaves ({'value': {...}} dicts) from an
    OpnForm logic condition tree."""
    leaves: list[dict] = []
    if isinstance(node, dict):
        if isinstance(node.get("value"), dict) and (
            "operator" in node["value"] or "property_meta" in node["value"]
        ):
            leaves.append(node)
        for v in node.values():
            leaves.extend(_logic_leaves(v))
    elif isinstance(node, list):
        for item in node:
            leaves.extend(_logic_leaves(item))
    return leaves


def _leaf_trigger_id(leaf: dict) -> str:
    v = leaf.get("value") or {}
    pm = v.get("property_meta") or {}
    return str(leaf.get("identifier") or pm.get("id") or "")


# ── OnlyOffice document retrieval + xlsx parsing ──────────────────────────────
def _oo_find_file_id(like_patterns: list[str]) -> tuple[str, str] | None:
    """Locate a files_file row by trying LIKE patterns in order. Excludes
    editor crash-recovery copies. Returns (id, title) or None."""
    for pat in like_patterns:
        row = onlyoffice_sql(
            "SELECT id, title FROM files_file "
            f"WHERE title LIKE '{pat}' AND title NOT LIKE '%Recovery%' "
            "ORDER BY id DESC LIMIT 1;"
        )
        if row:
            parts = row.split("\t")
            if len(parts) >= 2:
                return parts[0].strip(), parts[1].strip()
    return None


def _oo_bytes_from_fs(file_id: str, retries: int = 3, delay: float = 5.0) -> bytes | None:
    """Read content.xlsx for a file id from the portal data dir (docker exec).
    Retries to tolerate OnlyOffice save/conversion delay."""
    for attempt in range(retries):
        rc, out, _ = docker_exec(
            ONLYOFFICE_CONTAINER, "bash", "-c",
            f"find /var/www/onlyoffice/Data -type f -path '*file_{file_id}/*content.*' "
            "2>/dev/null | sort -V | tail -1",
            timeout=25,
        )
        path = out.strip().splitlines()[0].strip() if out.strip() else ""
        if path:
            r = subprocess.run(
                ["docker", "exec", ONLYOFFICE_CONTAINER, "cat", path],
                capture_output=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def _oo_auth_session():
    try:
        import requests
    except Exception:
        return None
    base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
    s = requests.Session()
    try:
        resp = s.post(f"{base_url}/api/2.0/authentication",
                      json={"userName": "admin@onlyoffice.local", "password": "NewAdmin123!"},
                      timeout=15)
        if resp.status_code not in (200, 201):
            return None
        token = resp.json().get("response", {}).get("token", "")
        if not token:
            return None
        s.headers.update({"Authorization": f"Bearer {token}"})
        s.cookies.set("asc_auth_key", token)
        s.base_url = base_url  # type: ignore[attr-defined]
        return s
    except Exception:
        return None


def _oo_bytes_from_api(file_id: str) -> bytes | None:
    s = _oo_auth_session()
    if not s:
        return None
    base = s.base_url  # type: ignore[attr-defined]
    for url, params in (
        (f"{base}/products/files/httphandlers/filehandler.ashx",
         {"action": "download", "fileid": str(file_id)}),
        (f"{base}/api/2.0/files/file/{file_id}/download", None),
    ):
        try:
            r = s.get(url, params=params, timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100:
                return r.content
        except Exception:
            continue
    return None


def _oo_get_document(like_patterns: list[str]) -> tuple[bytes | None, str]:
    """(bytes|None, source detail). fs first, API fallback."""
    try:
        found = _oo_find_file_id(like_patterns)
    except Exception as e:
        return None, f"files_file lookup failed: {_one(e)}"
    if not found:
        return None, "document not found in files_file"
    file_id, title = found
    data = _oo_bytes_from_fs(file_id)
    if data:
        return data, f"fs id={file_id} title={title[:60]}"
    data = _oo_bytes_from_api(file_id)
    if data:
        return data, f"api id={file_id} title={title[:60]}"
    return None, f"id={file_id} found but content unreadable (fs+api)"


def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        s = s.replace(a, b)
    return s


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    try:
        xml = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
    except KeyError:
        return []
    out = []
    for si in re.findall(r"<si>(.*?)</si>", xml, flags=re.DOTALL):
        ts = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", si, flags=re.DOTALL)
        out.append(_xml_unescape("".join(ts)))
    return out


def _xlsx_sheets(zf: zipfile.ZipFile) -> dict[str, str]:
    """sheet name -> worksheet member path."""
    wb = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
    rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
    rid_to_target = {}
    for rel in re.finditer(r"<Relationship\b[^>]*/?>", rels):
        tag = rel.group(0)
        rid = re.search(r'\bId="([^"]+)"', tag)
        tgt = re.search(r'\bTarget="([^"]+)"', tag)
        if rid and tgt:
            t = tgt.group(1)
            if not t.startswith("xl/") and not t.startswith("/"):
                t = "xl/" + t.lstrip("./")
            rid_to_target[rid.group(1)] = t.lstrip("/")
    sheets = {}
    for m in re.finditer(r"<sheet\b[^>]*/?>", wb):
        tag = m.group(0)
        nm = re.search(r'\bname="([^"]*)"', tag)
        rid = re.search(r'\br:id="([^"]*)"', tag)
        if nm and rid and rid.group(1) in rid_to_target:
            sheets[_xml_unescape(nm.group(1))] = rid_to_target[rid.group(1)]
    return sheets


def _xlsx_rows(zf: zipfile.ZipFile, sheet_path: str,
               shared: list[str] | None = None) -> list[list[str]]:
    """Rows as lists of cell strings: shared strings resolved, inline strings
    and raw numeric <v> values included."""
    if shared is None:
        shared = _xlsx_shared_strings(zf)
    xml = zf.read(sheet_path).decode("utf-8", errors="replace")
    rows = []
    for rowxml in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, flags=re.DOTALL):
        cells = []
        for cm in re.finditer(r"<c\b([^>]*)>(.*?)</c>", rowxml, flags=re.DOTALL):
            attrs, inner = cm.group(1), cm.group(2)
            tm = re.search(r'\bt="([^"]+)"', attrs)
            ctype = tm.group(1) if tm else ""
            if ctype == "s":
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                try:
                    idx = int(v.group(1)) if v else -1
                except ValueError:
                    idx = -1
                cells.append(shared[idx] if 0 <= idx < len(shared) else "")
            elif ctype in ("inlineStr", "str"):
                ts = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", inner, flags=re.DOTALL)
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells.append(_xml_unescape("".join(ts)) if ts
                             else (_xml_unescape(v.group(1)) if v else ""))
            else:
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells.append(v.group(1).strip() if v else "")
        rows.append(cells)
    return rows


def _xlsx_charts(zf: zipfile.ZipFile) -> list[str]:
    """XML text of each xl/charts/chart*.xml member."""
    out = []
    for name in zf.namelist():
        if re.match(r"xl/charts/chart\d*\.xml$", name):
            out.append(zf.read(name).decode("utf-8", errors="replace"))
    return out


# ── Shared resolution state (populated in main before checks run) ────────────
_FORM: dict = {"id": "", "props": None, "err": ""}

_PT: dict[str, dict] = {
    "carolyne": {
        "label": "Carolyne Schuster", "fname": "Carolyne", "lname": "Schuster",
        "anchors": ["purulent wound drainage", "right forearm IV site",
                    "Contact precautions initiated"],
        "pid": "", "encs": set(), "note_ok": False, "detail": "", "err": "",
    },
    "hipolito": {
        "label": "Hipolito Heller", "fname": "Hipolito", "lname": "Heller",
        "anchors": ["watery diarrhea", "Clostridioides difficile toxin",
                    "metronidazole 500mg TID"],
        "pid": "", "encs": set(), "note_ok": False, "detail": "", "err": "",
    },
}

_BILLING_CODE = {"carolyne": "", "hipolito": ""}   # code actually found in billing
_LISTS_FOUND = {"carolyne": False, "hipolito": False}
_LAB_ORDER_COUNT: int | None = None                 # live CBC order count (13d reconciliation)

_WB: dict = {"zf": None, "sheets": {}, "shared": [], "ok": False, "detail": ""}


def _resolve_form() -> None:
    try:
        row = opnform_sql(
            f"SELECT id FROM forms WHERE title = '{FORM_TITLE}' "
            "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1;"
        )
        _FORM["id"] = row.splitlines()[0].strip() if row else ""
        if _FORM["id"]:
            praw = opnform_sql(f"SELECT properties FROM forms WHERE id = {_FORM['id']};")
            _FORM["props"] = json.loads(praw)
    except Exception as e:
        _FORM["err"] = f"exception: {_one(e)}"


def _resolve_one_patient(fname: str, lname: str,
                         note_anchors: list[str]) -> tuple[str, set, bool, str]:
    """pid-set + anchored-encounter disambiguation (no LIMIT 1). Returns
    (canonical_pid, anchored_encounters, note_matched, detail)."""
    pids = [ln.strip() for ln in openemr_sql(
        f"SELECT pid FROM patient_data WHERE fname = '{fname}' AND lname = '{lname}';"
    ).splitlines() if ln.strip()]
    fallback: tuple[str, set] | None = None
    for pid in pids:
        encs = {e.strip() for e in openemr_sql(
            f"SELECT encounter FROM form_encounter "
            f"WHERE pid = {pid} AND date > '{ENCOUNTER_ANCHOR}';"
        ).splitlines() if e.strip()}
        if not encs:
            continue
        if fallback is None:
            fallback = (pid, encs)
        enc_list = ",".join(sorted(encs, key=int))
        rows = openemr_sql(
            f"SELECT encounter, description FROM form_clinical_notes "
            f"WHERE pid = {pid} AND activity = 1 AND encounter IN ({enc_list});"
        )
        for line in rows.splitlines():
            parts = line.split("\t", 1)
            desc = parts[1] if len(parts) > 1 else ""
            low = desc.lower()
            if all(a.lower() in low for a in note_anchors):
                return pid, encs, True, f"pid={pid} note matched in encounter {parts[0]}"
    if fallback:
        return fallback[0], fallback[1], False, (
            f"pid={fallback[0]} has post-anchor encounters but no clinical note "
            f"matched all anchors; candidate pids={pids}")
    return "", set(), False, (
        f"no candidate pid in {pids} has an encounter after {ENCOUNTER_ANCHOR}")


def _resolve_patients() -> None:
    for key, p in _PT.items():
        try:
            pid, encs, ok, detail = _resolve_one_patient(p["fname"], p["lname"], p["anchors"])
            p["pid"], p["encs"], p["note_ok"], p["detail"] = pid, encs, ok, detail
        except Exception as e:
            p["err"] = f"exception: {_one(e)}"


def _pt_gate(p: dict) -> str:
    """Empty string if the patient's canonical pid + anchored encounters are usable,
    else a single-line failure explanation."""
    if p["err"]:
        return p["err"]
    if not p["pid"] or not p["encs"]:
        return p["detail"] or "canonical pid/anchored encounters unresolved"
    return ""


def _prepare_workbook() -> None:
    data, src = _oo_get_document([
        "HAI Surveillance Report - March 2026%",
        "HAI Surveillance Report%March 2026%",
        "%HAI Surveillance Report%March 2026%",
        "%HAI Surveillance%2026%",
    ])
    if not data:
        _WB["detail"] = src
        return
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        sheets = _xlsx_sheets(zf)
        shared = _xlsx_shared_strings(zf)
    except Exception as e:
        _WB["detail"] = f"{src}; xlsx parse error: {_one(e)}"
        return
    _WB.update(zf=zf, sheets=sheets, shared=shared, detail=src)
    required = {"infection cases", "staff exposures", "monthly incidence"}
    _WB["ok"] = required <= {_norm_text(n) for n in sheets}


def _sheet_rows(name: str) -> list[list[str]] | None:
    for nm, path in _WB["sheets"].items():
        if _norm_text(nm) == _norm_text(name):
            return _xlsx_rows(_WB["zf"], path, _WB["shared"])
    return None


# ── Individual checks ─────────────────────────────────────────────────────────

# ---------- OpnForm ----------

def check_1_opnform_form_exists() -> None:
    """0pt gate: form 'HAI Staff Exposure Incident Report - March 2026' exists."""
    if _FORM["err"]:
        check("1. OpnForm form exists with correct title (gate)", 0, False, _FORM["err"])
        return
    found = bool(_FORM["id"]) and isinstance(_FORM["props"], list)
    check("1. OpnForm form exists with correct title (gate)", 0, found,
          f"id={_FORM['id']}" if found else "form not found")


def check_2_opnform_form_settings() -> None:
    """Form theme='simple', color='#1E6091', presentation_style='classic',
    visibility='public', auto_save=true, submit_button_text='Submit Exposure Report'."""
    label = "2. OpnForm form settings"
    if not _FORM["id"]:
        check(label, 2, False, f"gate failed (check 1): {_FORM['err'] or 'form not found'}")
        return
    try:
        row = opnform_sql(
            "SELECT theme, color, presentation_style, visibility, auto_save, submit_button_text "
            f"FROM forms WHERE id = {_FORM['id']};"
        )
        parts = row.split("|")
        if len(parts) < 6:
            check(label, 2, False, f"unexpected row format: {_one(row)!r}")
            return
        theme, color, pres, vis, auto_save, submit_btn = [p.strip() for p in parts[:6]]
        errors = []
        if theme != "simple":
            errors.append(f"theme={theme!r}")
        if color.upper() != "#1E6091":
            errors.append(f"color={color!r}")
        if pres != "classic":
            errors.append(f"presentation_style={pres!r}")
        if vis != "public":
            errors.append(f"visibility={vis!r}")
        if auto_save not in ("t", "1", "true", "True"):
            errors.append(f"auto_save={auto_save!r}")
        if submit_btn != "Submit Exposure Report":
            errors.append(f"submit_button_text={submit_btn!r}")
        check(label, 2, not errors, "; ".join(errors) if errors else "")
    except Exception as e:
        check(label, 2, False, f"exception: {_one(e)}")


def check_3_opnform_closed_message() -> None:
    """closed_text matches expected message."""
    label = "3. OpnForm closed message"
    expected = ("This exposure reporting form is currently closed. "
                "For urgent exposures, please contact Employee Health Services "
                "directly at ext. 4357.")
    if not _FORM["id"]:
        check(label, 1, False, f"gate failed (check 1): {_FORM['err'] or 'form not found'}")
        return
    try:
        row = opnform_sql(f"SELECT closed_text FROM forms WHERE id = {_FORM['id']};")
        passed = expected in row
        check(label, 1, passed, f"got: {_one(row, 80)!r}..." if not passed else "")
    except Exception as e:
        check(label, 1, False, f"exception: {_one(e)}")


def check_4_opnform_field_properties() -> None:
    """Per-field property assertions: Employee ID char limit, Date of Exposure
    disable_future_dates, four select option sets, PPE multi_select options,
    rating max 5, page-break with next button text."""
    label = "4. OpnForm field properties"
    if not isinstance(_FORM["props"], list):
        check(label, 2, False, f"gate failed (check 1): {_FORM['err'] or 'form not found'}")
        return
    try:
        props = _FORM["props"]
        errors: list[str] = []

        emp = _find_field(props, "Employee ID")
        if not emp:
            errors.append("'Employee ID' field missing")
        else:
            if str(emp.get("max_char_limit")) != "10":
                errors.append(f"Employee ID max_char_limit={emp.get('max_char_limit')!r}")
            if not _truthy(emp.get("show_char_limit")):
                errors.append(f"Employee ID show_char_limit={emp.get('show_char_limit')!r}")

        doe = _find_field(props, "Date of Exposure")
        if not doe:
            errors.append("'Date of Exposure' field missing")
        elif not _truthy(doe.get("disable_future_dates")):
            errors.append(f"Date of Exposure disable_future_dates={doe.get('disable_future_dates')!r}")

        select_specs = [
            ("Location of Exposure", {"emergency department", "intensive care unit",
                                      "medical-surgical ward", "operating room"}),
            ("Exposure Type", {"needlestick", "splash/spray", "airborne", "contact", "other"}),
            ("Source Patient Known", {"yes", "no", "unknown"}),
            ("Post-Exposure Prophylaxis Initiated", {"yes", "no", "not applicable"}),
        ]
        for fname, wanted in select_specs:
            f = _find_field(props, fname)
            if not f:
                errors.append(f"'{fname}' select missing")
                continue
            have = _option_names(f, "select")
            missing = wanted - have
            if missing:
                errors.append(f"'{fname}' options missing {sorted(missing)}")

        ppe = _find_field(props, "PPE Worn at Time of Exposure")
        if not ppe:
            errors.append("'PPE Worn at Time of Exposure' field missing")
        else:
            wanted = {"gloves", "gown", "n95 mask", "surgical mask",
                      "eye protection", "face shield", "none"}
            have = _option_names(ppe, "multi_select")
            missing = wanted - have
            if missing:
                errors.append(f"PPE multi_select options missing {sorted(missing)}")

        rating = _find_field(props, "Incident Severity Assessment")
        if not rating:
            errors.append("'Incident Severity Assessment' rating missing")
        elif rating.get("rating_max_value") is not None and str(rating.get("rating_max_value")) != "5":
            # 5 is the rating default; UI only persists changed keys -> absent ok
            errors.append(f"rating_max_value={rating.get('rating_max_value')!r}")

        breaks = [p for p in props if p.get("type") == "nf-page-break"]
        if not breaks:
            errors.append("no nf-page-break field")
        elif not any((b.get("next_btn_text") or "").strip() == "Proceed to Supervisor Review"
                     for b in breaks):
            errors.append(
                f"page-break next_btn_text={[(b.get('next_btn_text') or '') for b in breaks]!r}")

        check(label, 2, not errors, _one("; ".join(errors), 300) if errors else
              f"all field properties verified ({len(props)} fields)")
    except Exception as e:
        check(label, 2, False, f"exception: {_one(e)}")


def check_5_opnform_conditional_fields() -> None:
    """Conditional logic: 'Other Exposure Details' triggered by Exposure Type='Other',
    'Source Patient Identifier' triggered by Source Patient Known='Yes'; both show-block."""
    label = "5. OpnForm conditional fields"
    if not isinstance(_FORM["props"], list):
        check(label, 2, False, f"gate failed (check 1): {_FORM['err'] or 'form not found'}")
        return
    try:
        props = _FORM["props"]
        errors: list[str] = []
        specs = [
            ("Other Exposure Details", "Exposure Type", "Other"),
            ("Source Patient Identifier", "Source Patient Known", "Yes"),
        ]
        for dep_name, trig_name, needle in specs:
            dep = _find_field(props, dep_name)
            trig = _find_field(props, trig_name)
            if not dep:
                errors.append(f"'{dep_name}' field missing")
                continue
            if not trig:
                errors.append(f"trigger field '{trig_name}' missing")
                continue
            logic = dep.get("logic") if isinstance(dep.get("logic"), dict) else None
            if not logic:
                errors.append(f"'{dep_name}' has no logic")
                continue
            actions = logic.get("actions") or []
            if "show-block" not in actions:
                errors.append(f"'{dep_name}' actions={actions!r} lack show-block")
            trig_id = str(trig.get("id"))
            hit = False
            for leaf in _logic_leaves(logic.get("conditions")):
                if _leaf_trigger_id(leaf) != trig_id:
                    continue
                val = (leaf.get("value") or {}).get("value")
                txt = val if isinstance(val, str) else json.dumps(val)
                if needle.lower() in (txt or "").lower():
                    hit = True
                    break
            if not hit:
                errors.append(
                    f"'{dep_name}' has no condition on '{trig_name}' (id={trig_id}) "
                    f"with value containing {needle!r}")
        check(label, 2, not errors, _one("; ".join(errors), 300) if errors else "")
    except Exception as e:
        check(label, 2, False, f"exception: {_one(e)}")


# ---------- OpenEMR ----------

def check_6_openemr_carolyne_encounter_notes() -> None:
    """Carolyne Schuster: new (post-anchor) encounter carries a Clinical Notes form
    containing the three long text anchors."""
    label = "6. OpenEMR Carolyne encounter + clinical notes"
    p = _PT["carolyne"]
    if p["err"]:
        check(label, 1, False, p["err"])
        return
    check(label, 1, p["note_ok"], _one(p["detail"]))


def check_7_openemr_hipolito_encounter_notes() -> None:
    """Hipolito Heller: new (post-anchor) encounter carries a Clinical Notes form
    containing the three long text anchors."""
    label = "7. OpenEMR Hipolito encounter + clinical notes"
    p = _PT["hipolito"]
    if p["err"]:
        check(label, 2, False, p["err"])
        return
    check(label, 2, p["note_ok"], _one(p["detail"]))


def _check_billing(label: str, weight: int, key: str, code: str) -> None:
    p = _PT[key]
    gate = _pt_gate(p)
    if gate:
        check(label, weight, False, f"patient resolution failed: {_one(gate)}")
        return
    try:
        enc_list = ",".join(sorted(p["encs"], key=int))
        row = openemr_sql(
            f"SELECT code FROM billing "
            f"WHERE pid = {p['pid']} AND code_type = 'ICD10' AND code = '{code}' "
            f"AND activity = 1 AND encounter IN ({enc_list}) LIMIT 1;"
        )
        if row:
            _BILLING_CODE[key] = row.strip()
        check(label, weight, bool(row),
              f"pid={p['pid']} encounters={{{enc_list}}}" if row else
              f"{code} not in billing for pid={p['pid']} on post-anchor encounters {{{enc_list}}}")
    except Exception as e:
        check(label, weight, False, f"exception: {_one(e)}")


def check_8_openemr_carolyne_icd10() -> None:
    """Carolyne Schuster: billing has ICD-10 A49.02 on a post-anchor encounter."""
    _check_billing("8. OpenEMR Carolyne ICD-10 A49.02 (anchored encounter)", 1,
                   "carolyne", "A49.02")


def check_9_openemr_hipolito_icd10() -> None:
    """Hipolito Heller: billing has ICD-10 A04.7 on a post-anchor encounter."""
    _check_billing("9. OpenEMR Hipolito ICD-10 A04.7 (anchored encounter)", 1,
                   "hipolito", "A04.7")


_PROBLEM_SPECS = {
    "carolyne": ("%MRSA%bloodstream%", "%A49.02%"),
    "hipolito": ("%difficile%enterocolitis%", "%A04.7%"),
}


def _check_problem(label: str, key: str) -> None:
    """0pt gate: active medical problem with matching title AND diagnosis code."""
    p = _PT[key]
    gate = _pt_gate(p)
    if gate:
        check(label, 0, False, f"patient resolution failed: {_one(gate)}")
        return
    title_like, diag_like = _PROBLEM_SPECS[key]
    try:
        row = openemr_sql(
            f"SELECT id, title FROM lists "
            f"WHERE pid = {p['pid']} AND type = 'medical_problem' AND activity = 1 "
            f"AND title LIKE '{title_like}' AND diagnosis LIKE '{diag_like}' LIMIT 1;"
        )
        _LISTS_FOUND[key] = bool(row)
        check(label, 0, bool(row),
              _one(row) if row else
              f"no active problem for pid={p['pid']} matching title {title_like!r} "
              f"with diagnosis {diag_like!r}")
    except Exception as e:
        check(label, 0, False, f"exception: {_one(e)}")


def check_10_openemr_carolyne_problem() -> None:
    _check_problem("10. OpenEMR Carolyne medical problem + A49.02 diagnosis (gate)",
                   "carolyne")


def check_11_openemr_hipolito_problem() -> None:
    _check_problem("11. OpenEMR Hipolito medical problem + A04.7 diagnosis (gate)",
                   "hipolito")


def check_11b_openemr_issue_encounter_links() -> None:
    """Both problems are linked (issue_encounter) to the patient's post-anchor encounter."""
    label = "11b. OpenEMR problems linked to new encounters (issue_encounter)"
    errors: list[str] = []
    try:
        for key in ("carolyne", "hipolito"):
            p = _PT[key]
            gate = _pt_gate(p)
            if gate:
                errors.append(f"{p['label']}: patient resolution failed: {gate}")
                continue
            if not _LISTS_FOUND[key]:
                errors.append(f"{p['label']}: gate failed (check 10/11): problem row not found")
                continue
            title_like, diag_like = _PROBLEM_SPECS[key]
            enc_list = ",".join(sorted(p["encs"], key=int))
            cnt = openemr_sql(
                f"SELECT COUNT(*) FROM issue_encounter ie "
                f"JOIN lists l ON l.id = ie.list_id "
                f"WHERE ie.pid = {p['pid']} AND l.pid = {p['pid']} "
                f"AND l.type = 'medical_problem' AND l.activity = 1 "
                f"AND l.title LIKE '{title_like}' AND l.diagnosis LIKE '{diag_like}' "
                f"AND ie.encounter IN ({enc_list});"
            )
            if not cnt or int(cnt) < 1:
                errors.append(f"{p['label']}: problem not linked to encounters {{{enc_list}}}")
        check(label, 1, not errors, _one("; ".join(errors), 300) if errors else "")
    except Exception as e:
        check(label, 1, False, f"exception: {_one(e)}")


def check_12_openemr_procedure_order() -> None:
    """Carolyne Schuster: CBC Procedure Order with priority High, provider
    Dr. Krystyna Reinger (lab_id=ppid), clinical notes anchors, on a post-anchor
    encounter of the canonical pid."""
    label = "12. OpenEMR procedure order CBC (provider/priority/notes/encounter)"
    global _LAB_ORDER_COUNT
    p = _PT["carolyne"]
    gate = _pt_gate(p)
    if gate:
        check(label, 2, False, f"patient resolution failed: {_one(gate)}")
        return
    try:
        ppid = openemr_sql(
            "SELECT ppid FROM procedure_providers "
            "WHERE name = 'Dr. Krystyna Reinger' LIMIT 1;"
        ).strip()
        if not ppid:
            check(label, 2, False,
                  "procedure_providers row 'Dr. Krystyna Reinger' not found (fixture missing)")
            return
        enc_list = ",".join(sorted(p["encs"], key=int))
        cnt = openemr_sql(
            f"SELECT COUNT(DISTINCT po.procedure_order_id) FROM procedure_order po "
            f"JOIN procedure_order_code poc ON poc.procedure_order_id = po.procedure_order_id "
            f"WHERE po.patient_id = {p['pid']} "
            f"AND poc.procedure_name LIKE '%Complete Blood Count%' "
            f"AND po.encounter_id IN ({enc_list});"
        )
        _LAB_ORDER_COUNT = int(cnt) if cnt else 0
        rows = openemr_sql(
            f"SELECT po.order_priority, po.lab_id, po.encounter_id, po.clinical_hx "
            f"FROM procedure_order po "
            f"JOIN procedure_order_code poc ON poc.procedure_order_id = po.procedure_order_id "
            f"WHERE po.patient_id = {p['pid']} "
            f"AND poc.procedure_name LIKE '%Complete Blood Count%';"
        )
        matched = False
        last_issues = "no CBC procedure order rows for canonical pid"
        for line in rows.splitlines():
            parts = line.split("\t")
            if len(parts) < 4:
                continue
            prio, lab, enc = parts[0].strip(), parts[1].strip(), parts[2].strip()
            hx = "\t".join(parts[3:]).lower()
            probs = []
            if prio.lower() not in ("high", "high priority"):
                probs.append(f"priority={prio!r}")
            if lab != ppid:
                probs.append(f"lab_id={lab!r} != Reinger ppid={ppid}")
            if "monitor wbc response" not in hx:
                probs.append("clinical_hx missing 'monitor WBC response'")
            if "q48h until resolution" not in hx:
                probs.append("clinical_hx missing 'q48h until resolution'")
            if enc not in p["encs"]:
                probs.append(f"encounter_id={enc} not in post-anchor encounters")
            if not probs:
                matched = True
                break
            last_issues = "; ".join(probs)
        check(label, 2, matched,
              f"pid={p['pid']} lab={ppid} count={_LAB_ORDER_COUNT}" if matched
              else _one(last_issues, 300))
    except Exception as e:
        check(label, 2, False, f"exception: {_one(e)}")


# ---------- OnlyOffice ----------

def check_13a_onlyoffice_workbook_gate() -> None:
    """0pt gate: spreadsheet found+readable and all 3 sheet names present."""
    label = "13a. OnlyOffice spreadsheet exists with 3 sheets (gate)"
    sheet_names = ", ".join(sorted(_WB["sheets"])) if _WB["sheets"] else "none"
    check(label, 0, _WB["ok"],
          f"{_one(_WB['detail'])}; sheets: {_one(sheet_names, 100)}")


def check_13b_onlyoffice_infection_cases() -> None:
    """Sheet1 'Infection Cases': same-row cell sets for both patients, ICD codes
    cross-checked against live billing rows found by checks 8/9."""
    label = "13b. OnlyOffice Sheet1 infection case rows"
    if not _WB["ok"]:
        check(label, 2, False, f"gate failed (check 13a): {_one(_WB['detail'])}")
        return
    try:
        rows = _sheet_rows("Infection Cases") or []
        texts = [_norm_text(" | ".join(r)) for r in rows]
        c_tokens = ["a49.02", "2026-03-05", "mrsa", "icu", "vancomycin", "pending"]
        h_tokens = ["a04.7", "c. diff", "ward 4", "metronidazole", "recovered"]
        c_row = next((t for t in texts if all(tok in t for tok in c_tokens)), None)
        h_row = next((t for t in texts if all(tok in t for tok in h_tokens)), None)
        errors = []
        if not c_row:
            errors.append("no row with all Carolyne cells "
                          "{A49.02, 2026-03-05, MRSA, ICU, Vancomycin, Pending}")
        if not h_row:
            errors.append("no row with all Heller cells "
                          "{A04.7, C. diff, Ward 4, Metronidazole, Recovered}")
        if c_row and (not _BILLING_CODE["carolyne"]
                      or _norm_text(_BILLING_CODE["carolyne"]) not in c_row):
            errors.append(f"Carolyne row ICD does not match live billing "
                          f"code {_BILLING_CODE['carolyne']!r} (check 8)")
        if h_row and (not _BILLING_CODE["hipolito"]
                      or _norm_text(_BILLING_CODE["hipolito"]) not in h_row):
            errors.append(f"Heller row ICD does not match live billing "
                          f"code {_BILLING_CODE['hipolito']!r} (check 9)")
        check(label, 2, not errors, _one("; ".join(errors), 300) if errors else "")
    except Exception as e:
        check(label, 2, False, f"exception: {_one(e)}")


def check_13c_onlyoffice_staff_exposures() -> None:
    """Sheet2 'Staff Exposures': the two exposure rows with their full value sets."""
    label = "13c. OnlyOffice Sheet2 staff exposure rows"
    if not _WB["ok"]:
        check(label, 1, False, f"gate failed (check 13a): {_one(_WB['detail'])}")
        return
    try:
        rows = _sheet_rows("Staff Exposures") or []
        specs = [
            ("EMP00421", ["emp00421", "2026-03-12", "needlestick",
                          "emergency department", "partial", "yes", "2026-03-19"], "4"),
            ("EMP00587", ["emp00587", "2026-03-18", "splash/spray",
                          "intensive care unit", "full", "not applicable", "2026-03-25"], "2"),
        ]
        errors = []
        for tag, tokens, rating_cell in specs:
            found = False
            for r in rows:
                text = _norm_text(" | ".join(r))
                if all(tok in text for tok in tokens) and \
                        any(c.strip() == rating_cell for c in r):
                    found = True
                    break
            if not found:
                errors.append(f"no row with full {tag} value set (incl. rating {rating_cell})")
        check(label, 1, not errors, _one("; ".join(errors), 300) if errors else "")
    except Exception as e:
        check(label, 1, False, f"exception: {_one(e)}")


def check_13d_onlyoffice_monthly_incidence() -> None:
    """Sheet3 'Monthly Incidence': 7 metric labels, key values (row-scoped),
    lab-order count reconciled against live DB, bar chart present."""
    label = "13d. OnlyOffice Sheet3 metrics + bar chart"
    if not _WB["ok"]:
        check(label, 1, False, f"gate failed (check 13a): {_one(_WB['detail'])}")
        return
    try:
        rows = _sheet_rows("Monthly Incidence") or []
        texts = [_norm_text(" | ".join(r)) for r in rows]
        labels = ["surveillance period", "total hai cases", "infection type 1",
                  "infection type 2", "staff exposures reported", "incidence rate",
                  "lab orders submitted"]
        errors = []
        missing = [l for l in labels if not any(l in t for t in texts)]
        if missing:
            errors.append(f"metric labels missing: {missing}")

        def _row_for(lbl: str) -> list[str] | None:
            for r in rows:
                if lbl in _norm_text(" | ".join(r)):
                    return r
            return None

        for lbl, want in (("total hai cases", "2"), ("staff exposures reported", "2")):
            r = _row_for(lbl)
            if r is not None and not any(c.strip() == want for c in r):
                errors.append(f"'{lbl}' value != {want}")

        r = _row_for("lab orders submitted")
        if r is not None:
            if _LAB_ORDER_COUNT is None:
                errors.append("lab orders live count unavailable (check 12 unresolved)")
            elif not any(c.strip() == str(_LAB_ORDER_COUNT) for c in r):
                errors.append(f"'lab orders submitted' != live DB count {_LAB_ORDER_COUNT}")

        r = _row_for("incidence rate")
        if r is not None and "2.4 per 1000" not in _norm_text(" | ".join(r)).replace(",", ""):
            errors.append("'incidence rate' row lacks '2.4 per 1,000'")

        r = _row_for("surveillance period")
        if r is not None:
            t = _norm_text(" | ".join(r))
            if "2026-03-05" not in t or "2026-03-31" not in t:
                errors.append("'surveillance period' row lacks 2026-03-05 / 2026-03-31")

        charts = _xlsx_charts(_WB["zf"])
        if not any("barChart" in c for c in charts):
            errors.append(f"no bar chart (charts found: {len(charts)})")

        check(label, 1, not errors, _one("; ".join(errors), 300) if errors else "")
    except Exception as e:
        check(label, 1, False, f"exception: {_one(e)}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    _resolve_form()
    _resolve_patients()
    _prepare_workbook()

    check_1_opnform_form_exists()
    check_2_opnform_form_settings()
    check_3_opnform_closed_message()
    check_4_opnform_field_properties()
    check_5_opnform_conditional_fields()
    check_6_openemr_carolyne_encounter_notes()
    check_7_openemr_hipolito_encounter_notes()
    check_8_openemr_carolyne_icd10()
    check_9_openemr_hipolito_icd10()
    check_10_openemr_carolyne_problem()
    check_11_openemr_hipolito_problem()
    check_11b_openemr_issue_encounter_links()
    check_12_openemr_procedure_order()
    check_13a_onlyoffice_workbook_gate()
    check_13b_onlyoffice_infection_cases()
    check_13c_onlyoffice_staff_exposures()
    check_13d_onlyoffice_monthly_incidence()

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
