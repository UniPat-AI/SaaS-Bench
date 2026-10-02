#!/usr/bin/env python3
"""
Verifier for Healthcare-001-I5: Launch diabetes program across OpnForm, OpenEMR, OnlyOffice

Checks: 15 weighted checks (total 21 pts) across opnform, openemr, onlyoffice.
Strategy: docker exec DB queries + OnlyOffice content.xlsx bytes (fs-first, API fallback).

Required env vars:
  SERVER_HOSTNAME, OPNFORM_PORT, OPNFORM_CONTAINER,
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
OPENEMR_DB = os.environ.get("OPENEMR_DB_CONTAINER")
ONLYOFFICE_PORT = os.environ.get("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.environ.get("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB = os.environ.get("ONLYOFFICE_DB_CONTAINER")

_REQUIRED = [
    "OPNFORM_PORT", "OPNFORM_CONTAINER",
    "OPENEMR_PORT", "OPENEMR_CONTAINER", "OPENEMR_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
]
for _v in _REQUIRED:
    if not os.environ.get(_v):
        print(f"FATAL: {_v} not set", file=sys.stderr)
        sys.exit(1)

# ── Slot values (from expected_output / slot_values) ──────────────────────────
FORM_TITLE = "Diabetes Self-Management Follow-Up Form"
SPREADSHEET_TITLE = "Diabetes Program Multi-Site Tracker June 2026"
PATIENT_1_FNAME, PATIENT_1_LNAME = "Cyrstal", "Labadie"
PATIENT_2_FNAME, PATIENT_2_LNAME = "Julianne", "Mueller"
P1_BPS, P1_BPD = "144", "89"
P2_BPS, P2_BPD = "156", "98"
# OpenEMR stores form_vitals.weight in lbs (units_of_measurement=1); only the
# lbs-converted value counts — raw kg (80/104) means the unit was never set.
P1_WEIGHT_KG, P1_WEIGHT_LBS = 80.0, 176.3698
P2_WEIGHT_KG, P2_WEIGHT_LBS = 104.0, 229.2808
WEIGHT_TOL_LBS = 0.1

# probed seed max, mw-openemr:latest 2026-08 (form_encounter MAX(date)=2026-03-05 00:00:00)
SEED_MAX_ENCOUNTER_DATE = "2026-03-05"

CARE_GOAL_SUBSTRS = ["hba1c below 6.8", "within 6 months"]
CARE_INSTR_BASE = "fasting glucose each morning"
CARE_INSTR_ANY = ["25 minutes", "telehealth nursing check-ins"]
SOAP_ASSESS_SUBSTRS = ["type 2 diabetes mellitus", "stage 1 hypertension",
                       "fair glycemic control"]
SOAP_PLAN_SUBSTRS = ["metformin 1000mg bid", "empagliflozin 10mg", "lisinopril 10mg"]

XLSX_HEADERS = ["Patient Name", "Encounter Date", "Systolic BP", "Diastolic BP",
                "Weight (kg)", "HbA1c", "Adherence Score", "Care Plan Goal"]
SYMPTOM_OPTIONS = {"fatigue", "polyuria", "blurred vision", "numbness", "none"}

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers (docker exec) ─────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def opnform_psql(sql: str) -> str:
    """Query OpnForm's embedded PostgreSQL (forge/forge)."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", sql,
        timeout=20,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def openemr_sql(sql: str) -> str:
    """Query OpenEMR MariaDB."""
    rc, out, err = docker_exec(
        OPENEMR_DB,
        "mysql", "-u", "openemr", "-popenemr_pass",
        "--default-character-set=utf8mb4",
        "-D", "openemr", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(sql: str) -> str:
    """Query OnlyOffice MySQL."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice", "-N", "-B", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


# ── OpnForm parsing helpers ──────────────────────────────────────────────────
_form_fields_cache: list | None = None


def _get_form_fields() -> list:
    """Parsed forms.properties (JSON array) for FORM_TITLE. Raises if missing."""
    global _form_fields_cache
    if _form_fields_cache is None:
        raw = opnform_psql(
            f"SELECT properties FROM forms WHERE title = '{FORM_TITLE}' "
            "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not raw:
            raise RuntimeError("form not found")
        _form_fields_cache = json.loads(raw)
    return _form_fields_cache


def _find_field(fields: list, name_sub: str, ftype: str | None = None) -> dict | None:
    for f in fields:
        if ftype and f.get("type") != ftype:
            continue
        if name_sub in (f.get("name") or "").lower():
            return f
    return None


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


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


# ── OnlyOffice retrieval helpers (fs-first, API fallback) ────────────────────
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
    """Read content.docx/xlsx for a file id from the portal data dir (docker
    exec). Retries to tolerate OnlyOffice save/conversion delay."""
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
        base_url = f"http://{HOST}:{ONLYOFFICE_PORT}"
        s = requests.Session()
        resp = s.post(f"{base_url}/api/2.0/authentication",
                      json={"userName": "admin@onlyoffice.local",
                            "password": "NewAdmin123!"},
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


# ── docx/xlsx parsing helpers ────────────────────────────────────────────────
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


def _row_has_num(row: list[str], want: float, want_str: str) -> bool:
    """Cell matches if float-equal (tol 0.01) or want_str is a substring of a
    non-numeric cell (e.g. embedded in goal text)."""
    for cell in row:
        c = cell.strip()
        if not c:
            continue
        try:
            if abs(float(c) - want) <= 0.01:
                return True
        except ValueError:
            if want_str in c:
                return True
    return False


# ── OpenEMR anchoring ────────────────────────────────────────────────────────
def resolve_anchor(fname: str, lname: str) -> tuple[str, str, str] | None:
    """Canonical (pid, encounter, date): among all same-name pids (duplicate
    charts: Cyrstal Labadie 207/314, Julianne Mueller 203/248), the pid owning
    the newest form_encounter dated after the seed maximum."""
    row = openemr_sql(
        "SELECT pd.pid, fe.encounter, fe.date FROM patient_data pd "
        "JOIN form_encounter fe ON fe.pid = pd.pid "
        f"WHERE pd.fname='{fname}' AND pd.lname='{lname}' "
        f"AND fe.date > '{SEED_MAX_ENCOUNTER_DATE}' "
        "ORDER BY fe.date DESC, fe.encounter DESC LIMIT 1"
    )
    if not row:
        return None
    parts = row.split("\t")
    if len(parts) < 2:
        return None
    return (parts[0].strip(), parts[1].strip(),
            parts[2].strip() if len(parts) > 2 else "")


# ── OpnForm checks ───────────────────────────────────────────────────────────

def check_1_form_exists() -> None:
    """Form exists (exact title, not deleted) and visibility is public."""
    try:
        row = opnform_psql(
            f"SELECT visibility FROM forms WHERE title = '{FORM_TITLE}' "
            "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not row:
            check("1. OpnForm form exists & published", 2, False, "form not found")
            return
        passed = row.strip().lower() == "public"
        check("1. OpnForm form exists & published", 2, passed, f"visibility={row}")
    except Exception as e:
        check("1. OpnForm form exists & published", 2, False, f"exception: {e}")


def check_2a_field_names_types_required() -> None:
    """Named fields exist with correct types; date/both numbers/scale required."""
    label = "2a. OpnForm field names/types/required"
    try:
        fields = _get_form_fields()
        date_f = next((f for f in fields if f.get("type") == "date"), None)
        fbg = _find_field(fields, "fasting blood glucose", "number")
        hba1c = _find_field(fields, "hba1c", "number")
        adherence = _find_field(fields, "medication adherence", "scale")
        symptoms = _find_field(fields, "current symptoms", "multi_select")
        barriers = _find_field(fields, "barriers to adherence", "text")
        found = {
            "date": date_f is not None, "fbg": fbg is not None,
            "hba1c": hba1c is not None, "adherence": adherence is not None,
            "symptoms": symptoms is not None, "barriers": barriers is not None,
        }
        req = {
            "date": date_f is not None and _truthy(date_f.get("required")),
            "fbg": fbg is not None and _truthy(fbg.get("required")),
            "hba1c": hba1c is not None and _truthy(hba1c.get("required")),
            "adherence": adherence is not None and _truthy(adherence.get("required")),
        }
        passed = all(found.values()) and all(req.values())
        missing = [k for k, v in found.items() if not v]
        not_req = [k for k, v in req.items() if not v]
        check(label, 1, passed,
              f"missing={missing or 'none'}, not_required={not_req or 'none'}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_2b_scale_bounds_and_options() -> None:
    """Scale 1-10 on Medication Adherence; Current Symptoms options superset.
    prefill_today on the date field is observed (soft), not gated."""
    label = "2b. OpnForm scale bounds & symptom options"
    try:
        fields = _get_form_fields()
        adherence = _find_field(fields, "medication adherence", "scale")
        symptoms = _find_field(fields, "current symptoms", "multi_select")
        date_f = next((f for f in fields if f.get("type") == "date"), None)

        min_ok = max_ok = False
        smin = smax = None
        if adherence:
            smin = adherence.get("scale_min_value")
            smax = adherence.get("scale_max_value")
            min_ok = smin is None or str(smin) == "1"  # UI default is 1 when unset
            max_ok = smax is not None and str(smax) == "10"

        opts_ok = False
        opt_names: list[str] = []
        if symptoms:
            opt_names = [str(o.get("name", ""))
                         for o in (symptoms.get("multi_select") or {}).get("options", [])]
            opts_ok = SYMPTOM_OPTIONS.issubset({n.strip().lower() for n in opt_names})

        prefill = date_f.get("prefill_today") if date_f else None
        passed = bool(adherence) and bool(symptoms) and min_ok and max_ok and opts_ok
        check(label, 1, passed,
              f"scale_min={smin}, scale_max={smax}, options={opt_names[:8]}, "
              f"prefill_today={prefill} (info)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_3_conditional_logic() -> None:
    """Barriers to Adherence logic: Medication Adherence less_than 5 -> show-block."""
    label = "3. OpnForm conditional logic on Barriers"
    try:
        fields = _get_form_fields()
        barriers = _find_field(fields, "barrier")
        adherence = _find_field(fields, "medication adherence", "scale")
        if not barriers:
            check(label, 2, False, "no field with 'barrier' in name")
            return
        if not adherence or not adherence.get("id"):
            check(label, 2, False, "Medication Adherence scale field not found")
            return
        adh_id = str(adherence.get("id"))
        logic = barriers.get("logic") or {}
        leaves = _logic_leaves(logic.get("conditions")) if isinstance(logic, dict) else []
        leaf_ok = False
        for leaf in leaves:
            v = leaf.get("value") or {}
            if (_leaf_trigger_id(leaf) == adh_id
                    and v.get("operator") == "less_than"
                    and str(v.get("value")) == "5"):
                leaf_ok = True
                break
        actions = logic.get("actions") if isinstance(logic, dict) else None
        action_ok = isinstance(actions, list) and "show-block" in actions
        check(label, 2, leaf_ok and action_ok,
              f"leaves={len(leaves)}, trigger_match={leaf_ok}, actions={actions}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OpenEMR checks ───────────────────────────────────────────────────────────

def check_encounter_gate(num: int, fname: str, lname: str) -> tuple[str, str, str] | None:
    """0pt gate: anchored new encounter (date > seed max) found for the patient."""
    label = f"{num}. Anchored new encounter for {fname} {lname} (gate)"
    try:
        anchor = resolve_anchor(fname, lname)
        if not anchor:
            check(label, 0, False,
                  f"no form_encounter with date > '{SEED_MAX_ENCOUNTER_DATE}' "
                  "on any same-name pid")
            return None
        pid, enc, dt = anchor
        check(label, 0, True, f"pid={pid}, encounter={enc}, date={dt}")
        return anchor
    except Exception as e:
        check(label, 0, False, f"exception: {e}")
        return None


def check_vitals(num: int, name: str, anchor: tuple[str, str, str] | None,
                 bps_want: str, bpd_want: str, wt_lbs: float, wt_kg: float) -> None:
    """Vitals on the anchored encounter: exact BP strings; weight must be the
    lbs-converted value (raw kg rejected)."""
    label = f"{num}. Vitals for {name}"
    if not anchor:
        check(label, 2, False, "gate failed: no anchored new encounter")
        return
    pid, enc, _ = anchor
    try:
        row = openemr_sql(
            "SELECT v.bps, v.bpd, v.weight FROM form_vitals v "
            "JOIN forms f ON f.form_id = v.id AND f.formdir='vitals' "
            f"AND f.deleted=0 AND f.pid={pid} AND f.encounter={enc} "
            "ORDER BY v.date DESC LIMIT 1"
        )
        if not row:
            check(label, 2, False, "no vitals form on anchored encounter")
            return
        parts = row.split("\t")
        bps = parts[0].strip() if len(parts) > 0 else ""
        bpd = parts[1].strip() if len(parts) > 1 else ""
        wt = parts[2].strip() if len(parts) > 2 else ""
        try:
            wtf = float(wt)
        except ValueError:
            wtf = None
        wt_ok = wtf is not None and abs(wtf - wt_lbs) <= WEIGHT_TOL_LBS
        raw_kg = wtf is not None and abs(wtf - wt_kg) <= WEIGHT_TOL_LBS
        note = " raw-kg rejected, unit not set to kg" if (raw_kg and not wt_ok) else ""
        passed = bps == bps_want and bpd == bpd_want and wt_ok
        check(label, 2, passed,
              f"bps={bps}, bpd={bpd}, wt={wt} (want {wt_lbs} lbs){note}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_soap(num: int, name: str, anchor: tuple[str, str, str] | None) -> None:
    """SOAP note on the anchored encounter: full assessment + plan phrases."""
    label = f"{num}. SOAP note for {name}"
    if not anchor:
        check(label, 2, False, "gate failed: no anchored new encounter")
        return
    pid, enc, _ = anchor
    try:
        a_txt = openemr_sql(
            "SELECT s.assessment FROM form_soap s "
            "JOIN forms f ON f.form_id = s.id AND f.formdir='soap' "
            f"AND f.deleted=0 AND f.pid={pid} AND f.encounter={enc}"
        ).lower()
        p_txt = openemr_sql(
            "SELECT s.plan FROM form_soap s "
            "JOIN forms f ON f.form_id = s.id AND f.formdir='soap' "
            f"AND f.deleted=0 AND f.pid={pid} AND f.encounter={enc}"
        ).lower()
        if not a_txt and not p_txt:
            check(label, 2, False, "no SOAP form on anchored encounter")
            return
        a_missing = [s for s in SOAP_ASSESS_SUBSTRS if s not in a_txt]
        p_missing = [s for s in SOAP_PLAN_SUBSTRS if s not in p_txt]
        check(label, 2, not a_missing and not p_missing,
              f"assess_missing={a_missing or 'none'}, plan_missing={p_missing or 'none'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_careplan(num: int, name: str, anchor: tuple[str, str, str] | None) -> None:
    """Care plan on the anchored encounter: goal + full instructions phrases."""
    label = f"{num}. Care plan for {name}"
    if not anchor:
        check(label, 2, False, "gate failed: no anchored new encounter")
        return
    pid, enc, _ = anchor
    try:
        cp_txt = openemr_sql(
            "SELECT CONCAT_WS(' ', COALESCE(cp.description,''), COALESCE(cp.codetext,''), "
            "COALESCE(cp.reason_description,''), COALESCE(cp.note_related_to,'')) "
            "FROM form_care_plan cp "
            "JOIN forms f ON f.form_id = cp.id AND f.formdir='care_plan' "
            f"AND f.deleted=0 AND f.pid={pid} AND f.encounter={enc}"
        ).lower()
        ci_txt = openemr_sql(
            "SELECT ci.instruction FROM form_clinical_instructions ci "
            "JOIN forms f ON f.form_id = ci.id AND f.formdir='clinical_instructions' "
            f"AND f.deleted=0 AND f.pid={pid} AND f.encounter={enc}"
        ).lower()
        combined = cp_txt + " " + ci_txt
        goal_missing = [s for s in CARE_GOAL_SUBSTRS if s not in cp_txt]
        instr_ok = (CARE_INSTR_BASE in combined
                    and any(s in combined for s in CARE_INSTR_ANY))
        check(label, 2, not goal_missing and instr_ok,
              f"goal_missing={goal_missing or 'none'}, instr_ok={instr_ok}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

def check_12_spreadsheet() -> bytes | None:
    """0pt gate: spreadsheet located by exact title (extension tolerated,
    Recovery copies excluded). Returns the xlsx bytes for ck13/ck14."""
    label = "12. OnlyOffice spreadsheet located (gate)"
    try:
        found = _oo_find_file_id([f"{SPREADSHEET_TITLE}%"])
        if not found:
            check(label, 0, False, "exact-title spreadsheet not found in files_file")
            return None
        file_id, title = found
        data = _oo_bytes_from_fs(file_id)
        src = "fs"
        if not data:
            data = _oo_bytes_from_api(file_id)
            src = "api"
        if not data:
            check(label, 0, True,
                  f"id={file_id} title={title[:60]} (content unreadable fs+api)")
            return None
        check(label, 0, True, f"{src} id={file_id} title={title[:60]}")
        return data
    except Exception as e:
        check(label, 0, False, f"exception: {e}")
        return None


def check_13_xlsx_content(data: bytes | None) -> None:
    """Header row has all 8 labels; each patient's row holds their own values
    and the care-plan goal; placeholder values present in the data rows."""
    label = "13. Spreadsheet headers & patient rows"
    if data is None:
        check(label, 2, False, "gate failed: spreadsheet not located or unreadable")
        return
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        shared = _xlsx_shared_strings(zf)
        sheets = _xlsx_sheets(zf)
        if not sheets:
            check(label, 2, False, "no worksheets found in xlsx")
            return
        # Pick the sheet whose rows contain the 'Patient Name' header; else first.
        rows: list[list[str]] = []
        header_row: list[str] | None = None
        for path in sheets.values():
            cand = _xlsx_rows(zf, path, shared)
            hdr = next((r for r in cand
                        if any(_norm_text(c) == "patient name" for c in r)), None)
            if hdr is not None:
                rows, header_row = cand, hdr
                break
        if header_row is None:
            rows = _xlsx_rows(zf, next(iter(sheets.values())), shared)
            header_row = rows[0] if rows else []
        hdr_norm = {_norm_text(c) for c in header_row}
        hdr_missing = [h for h in XLSX_HEADERS if _norm_text(h) not in hdr_norm]

        def _patient_row(full_name: str) -> list[str] | None:
            want = _norm_text(full_name)
            return next((r for r in rows
                         if any(want in _norm_text(c) for c in r)), None)

        p1_row = _patient_row(f"{PATIENT_1_FNAME} {PATIENT_1_LNAME}")
        p2_row = _patient_row(f"{PATIENT_2_FNAME} {PATIENT_2_LNAME}")
        goal_norm = "hba1c below 6.8"
        p1_ok = p2_ok = False
        if p1_row:
            p1_ok = (all(_row_has_num(p1_row, v, s)
                         for v, s in ((144.0, "144"), (89.0, "89"), (80.0, "80")))
                     and goal_norm in _norm_text(" | ".join(p1_row)))
        if p2_row:
            p2_ok = (all(_row_has_num(p2_row, v, s)
                         for v, s in ((156.0, "156"), (98.0, "98"), (104.0, "104")))
                     and goal_norm in _norm_text(" | ".join(p2_row)))
        union = (p1_row or []) + (p2_row or [])
        ph_missing = [s for v, s in ((6.8, "6.8"), (8.7, "8.7"), (9.0, "9"), (4.0, "4"))
                      if not _row_has_num(union, v, s)]
        passed = (not hdr_missing and p1_row is not None and p2_row is not None
                  and p1_ok and p2_ok and not ph_missing)
        check(label, 2, passed,
              f"hdr_missing={hdr_missing or 'none'}, p1_row={'ok' if p1_ok else bool(p1_row)}, "
              f"p2_row={'ok' if p2_ok else bool(p2_row)}, placeholders_missing={ph_missing or 'none'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_charts(data: bytes | None) -> None:
    """At least 2 chart XML members each containing a barChart element."""
    label = "14. Spreadsheet bar charts"
    if data is None:
        check(label, 1, False, "gate failed: spreadsheet not located or unreadable")
        return
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
        charts = _xlsx_charts(zf)
        bar = [c for c in charts if "barChart" in c]
        pts = [len(re.findall(r"<c:pt[^>]*>\s*<c:v>(.*?)</c:v>", c)) for c in bar]
        check(label, 1, len(bar) >= 2,
              f"charts={len(charts)}, bar_charts={len(bar)}, numCache_pts={pts} (info)")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # OpnForm (4 checks)
    check_1_form_exists()
    check_2a_field_names_types_required()
    check_2b_scale_bounds_and_options()
    check_3_conditional_logic()

    # OpenEMR — Patient 1 (gate + 3 checks)
    anchor1 = check_encounter_gate(4, PATIENT_1_FNAME, PATIENT_1_LNAME)
    check_vitals(5, f"{PATIENT_1_FNAME} {PATIENT_1_LNAME}", anchor1,
                 P1_BPS, P1_BPD, P1_WEIGHT_LBS, P1_WEIGHT_KG)
    check_soap(6, f"{PATIENT_1_FNAME} {PATIENT_1_LNAME}", anchor1)
    check_careplan(7, f"{PATIENT_1_FNAME} {PATIENT_1_LNAME}", anchor1)

    # OpenEMR — Patient 2 (gate + 3 checks)
    anchor2 = check_encounter_gate(8, PATIENT_2_FNAME, PATIENT_2_LNAME)
    check_vitals(9, f"{PATIENT_2_FNAME} {PATIENT_2_LNAME}", anchor2,
                 P2_BPS, P2_BPD, P2_WEIGHT_LBS, P2_WEIGHT_KG)
    check_soap(10, f"{PATIENT_2_FNAME} {PATIENT_2_LNAME}", anchor2)
    check_careplan(11, f"{PATIENT_2_FNAME} {PATIENT_2_LNAME}", anchor2)

    # OnlyOffice (gate + 2 checks)
    xlsx_data = check_12_spreadsheet()
    check_13_xlsx_content(xlsx_data)
    check_14_charts(xlsx_data)

    # Summary
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
