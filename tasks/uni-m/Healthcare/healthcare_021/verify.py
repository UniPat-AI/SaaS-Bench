"""
Verifier for Healthcare-021-I2: Pediatric Well-Child Visit Workflow with Milestone Screening

Checks: 16 printed checks (2 zero-weight precondition gates), total weight 23.
Strategy: API for OpnForm; docker exec MariaDB for OpenEMR; docker exec MySQL +
filesystem (fs-first, API fallback) for OnlyOffice.

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

import requests

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

for _var in [
    "OPNFORM_PORT", "OPNFORM_CONTAINER",
    "OPENEMR_PORT", "OPENEMR_CONTAINER", "OPENEMR_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
]:
    if not os.environ.get(_var):
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

OPNFORM_BASE = f"http://{HOST}:{OPNFORM_PORT}"
OPENEMR_BASE = f"http://{HOST}:{OPENEMR_PORT}"
ONLYOFFICE_BASE = f"http://{HOST}:{ONLYOFFICE_PORT}"

# ── Date anchors & constants ─────────────────────────────────────────────────
# probed seed max, mw-openemr:latest 2026-08
ENCOUNTER_SEED_MAX = "2026-03-05"          # form_encounter MAX(date) = 2026-03-05 00:00:00
HISTORY_SEED_MAX = "2026-03-22 05:30:20"   # history_data MAX(date)

# units_of_measurement global = 1 (US): form_vitals stores lbs/inches/degF
# (verified empirically: 80 kg entered via UI -> weight=176.369810).
LBS_PER_KG = 2.20462262
CM_PER_INCH = 2.54
NUM_TOL = 0.05

OPNFORM_TITLE = "Early Childhood Developmental Screening Form Q2-2026"
XLSX_TITLE = "Pediatric Well-Child Visit Tracker Q2-2026"

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


def openemr_db(sql: str) -> str:
    """Query OpenEMR MariaDB. Raises on SQL error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "--default-character-set=utf8mb4", "-N", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_db(sql: str) -> str:
    """Query OnlyOffice MySQL. Raises on SQL error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-e", sql,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


def _num_ok(val: str, *expected: float) -> bool:
    """Numeric equality with tolerance against any expected value. No substrings."""
    try:
        v = float(val)
    except (TypeError, ValueError):
        return False
    return any(abs(v - e) < NUM_TOL for e in expected)


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _fnorm(name: str) -> str:
    """Normalize an OpnForm field name: strip a trailing '(kg)'/'(cm)' unit."""
    n = re.sub(r"\s*\((?:kg|cm)\)\s*$", "", (name or "").strip(), flags=re.I)
    return " ".join(n.lower().split())


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


# ── Patient / encounter resolution (convention B: no cross-pid unions) ────────
def get_patient_pids(fname: str, lname: str) -> list[str]:
    out = openemr_db(
        f"SELECT pid FROM patient_data WHERE fname='{fname}' AND lname='{lname}' "
        f"ORDER BY pid"
    )
    return [line.strip() for line in out.split("\n") if line.strip()]


def resolve_patient(fname: str, lname: str) -> tuple[str | None, str | None, str]:
    """Canonical (pid, encounter): among all same-name pids, the one owning the
    newest form_encounter after the seed anchor. No DOB disambiguation here —
    seed DOBs contradict the task's toddler ages (plan risk (e))."""
    pids = get_patient_pids(fname, lname)
    if not pids:
        return None, None, "patient not found"
    candidates: list[tuple[str, int, str]] = []
    for p in pids:
        out = openemr_db(
            f"SELECT date, encounter FROM form_encounter "
            f"WHERE pid='{p}' AND date > '{ENCOUNTER_SEED_MAX}' "
            f"ORDER BY date DESC, encounter DESC LIMIT 1"
        )
        if out:
            parts = out.split("\t")
            if len(parts) >= 2:
                candidates.append((parts[0], int(parts[1]), p))
    if not candidates:
        return None, None, (
            f"no encounter with date > {ENCOUNTER_SEED_MAX} for pids {pids}"
        )
    candidates.sort(reverse=True)
    date, enc, pid = candidates[0]
    return pid, str(enc), f"pid={pid} enc={enc} date={date}"


CONNIE: tuple[str | None, str | None, str] = (None, None, "not resolved")
LEKISHA: tuple[str | None, str | None, str] = (None, None, "not resolved")

# DB vitals values (floats) of each patient's anchored row; ck16b reconciles
# the spreadsheet against these live values.
VITALS_DB: dict[str, list[float] | None] = {"connie": None, "lekisha": None}


# ── OpnForm Checks ───────────────────────────────────────────────────────────
_opnform_form = None


def _opnform_login() -> dict:
    r = requests.post(
        f"{OPNFORM_BASE}/api/login",
        json={"email": "seeded_admin@example.com", "password": "mw-admin-123"},
        timeout=15,
    )
    r.raise_for_status()
    token = r.json().get("token", "")
    return {"Authorization": f"Bearer {token}"}


def _find_opnform_form(headers: dict) -> dict | None:
    global _opnform_form
    if _opnform_form is not None:
        return _opnform_form

    r = requests.get(f"{OPNFORM_BASE}/api/open/forms", headers=headers, timeout=15)
    r.raise_for_status()
    forms_list = r.json()
    if isinstance(forms_list, dict):
        forms_list = forms_list.get("data", forms_list.get("response", []))
    if isinstance(forms_list, list):
        for f in forms_list:
            if f.get("title") == OPNFORM_TITLE:
                _opnform_form = f
                return f
    return None


def check_1_opnform_form_exists() -> None:
    """0pt precondition gate: form exists with the exact title."""
    check("1. OpnForm form exists (gate)", 0, _opnform_form is not None,
          "found" if _opnform_form else "form not found")


# (name, type, required) — required set per description: fields 1-6, 11, 12.
FIELD_SPECS = [
    ("Child's Name", "text", True),
    ("Date of Birth", "date", True),
    ("Age in Months", "number", True),
    ("Parent/Guardian Name", "text", True),
    ("Age Group", "select", True),
    ("Developmental Milestones", "matrix", True),
    ("Milestones Achieved Count", "number", False),
    ("Parent Concern Level", "rating", False),
    ("Referral Requested by Parent", "checkbox", False),
    ("Referral Reason", "text", False),
    ("Current Weight (kg)", "number", True),
    ("Current Height (cm)", "number", True),
    ("Head Circumference (cm)", "number", False),
]


def _find_field(props: list, name: str) -> dict | None:
    want = _fnorm(name)
    for p in props:
        if isinstance(p, dict) and _fnorm(p.get("name", "")) == want:
            return p
    return None


def check_2_opnform_fields() -> None:
    """All 13 fields present with exact types, required set, rating max 5,
    plus a signature field."""
    label = "2. OpnForm required fields present"
    try:
        if not _opnform_form:
            check(label, 2, False, "gate failed: form not found")
            return
        props = _opnform_form.get("properties", [])
        if isinstance(props, str):
            props = json.loads(props)

        issues = []
        for name, ftype, required in FIELD_SPECS:
            f = _find_field(props, name)
            if not f:
                issues.append(f"{name}: missing")
                continue
            if f.get("type") != ftype:
                issues.append(f"{name}: type={f.get('type')}!={ftype}")
            if required and not _truthy(f.get("required")):
                issues.append(f"{name}: not required")
            if (ftype == "rating" and f.get("rating_max_value") is not None
                    and str(f.get("rating_max_value")) != "5"):
                # 5 is the rating default; UI only persists changed keys -> absent ok
                issues.append(f"{name}: rating_max_value={f.get('rating_max_value')}")
        if not any(isinstance(p, dict) and p.get("type") == "signature" for p in props):
            issues.append("signature field: missing")

        check(label, 2, not issues,
              "13/13 fields + types + required + rating max + signature OK"
              if not issues else "; ".join(issues)[:250])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_opnform_matrix() -> None:
    """Matrix field: rows 6/6 and columns 4/4 exact (superset, case-insensitive)."""
    label = "3. OpnForm matrix field config"
    try:
        if not _opnform_form:
            check(label, 2, False, "gate failed: form not found")
            return
        props = _opnform_form.get("properties", [])
        if isinstance(props, str):
            props = json.loads(props)
        matrix = next((p for p in props
                       if isinstance(p, dict) and p.get("type") == "matrix"), None)
        if not matrix:
            check(label, 2, False, "no matrix field found")
            return

        rows = [_norm_text(str(r)) for r in (matrix.get("rows") or [])]
        cols = [_norm_text(str(c)) for c in (matrix.get("columns") or [])]
        exp_rows = ["walks independently", "stacks three or more blocks",
                    "uses two-word phrases", "points to named body parts",
                    "follows simple instructions", "scribbles with crayon"]
        exp_cols = ["achieved", "emerging", "not yet", "unable to assess"]
        miss_rows = [r for r in exp_rows if r not in rows]
        miss_cols = [c for c in exp_cols if c not in cols]
        check(label, 2, not miss_rows and not miss_cols,
              f"rows {6 - len(miss_rows)}/6, cols {4 - len(miss_cols)}/4"
              + (f", missing: {(miss_rows + miss_cols)[:3]}"
                 if miss_rows or miss_cols else ""))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3b_opnform_logic_pagebreak() -> None:
    """Referral Reason conditional logic (show-block on the Referral checkbox)
    + page break with 'Continue to Growth Data' between fields 10 and 11."""
    label = "3b. OpnForm conditional logic + page break"
    try:
        if not _opnform_form:
            check(label, 2, False, "gate failed: form not found")
            return
        props = _opnform_form.get("properties", [])
        if isinstance(props, str):
            props = json.loads(props)

        issues = []
        reason = _find_field(props, "Referral Reason")
        checkbox = _find_field(props, "Referral Requested by Parent")
        weight_f = _find_field(props, "Current Weight (kg)")

        if not reason:
            issues.append("Referral Reason field missing")
        if not checkbox:
            issues.append("Referral checkbox field missing")
        if reason and checkbox:
            logic = reason.get("logic") or {}
            actions = logic.get("actions") or []
            if "show-block" not in actions:
                issues.append(f"actions={actions} lacks show-block")
            cb_id = str(checkbox.get("id"))
            leaves = _logic_leaves(logic.get("conditions"))
            leaf_ok = False
            for leaf in leaves:
                v = leaf.get("value") or {}
                if _leaf_trigger_id(leaf) != cb_id:
                    continue
                if v.get("operator") == "is_checked" or _truthy(v.get("value")):
                    leaf_ok = True
                    break
            if not leaf_ok:
                issues.append(f"no condition leaf on checkbox id={cb_id} "
                              f"with is_checked/true ({len(leaves)} leaves)")

        pb_ok = False
        pb_texts = []
        if reason and weight_f:
            r_idx = props.index(reason)
            w_idx = props.index(weight_f)
            for i, p in enumerate(props):
                if isinstance(p, dict) and p.get("type") == "nf-page-break":
                    txt = (p.get("next_btn_text") or "").strip()
                    pb_texts.append(f"@{i}:{txt[:30]}")
                    if txt == "Continue to Growth Data" and r_idx < i < w_idx:
                        pb_ok = True
            if not pb_ok:
                issues.append(
                    f"no page break with next_btn_text='Continue to Growth Data' "
                    f"between idx {r_idx} and {w_idx}; found {pb_texts[:3]}")
        elif weight_f is None:
            issues.append("Current Weight field missing (page-break position)")

        check(label, 2, not issues,
              "logic + page break OK" if not issues else "; ".join(issues)[:280])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_opnform_styling() -> None:
    """color=#2196F3, theme=default, border_radius=small, auto_save==true,
    visibility=public."""
    label = "4. OpnForm form styling"
    try:
        form = _opnform_form
        if not form:
            check(label, 1, False, "gate failed: form not found")
            return
        issues = []
        color = (form.get("color") or "").upper()
        if color != "#2196F3":
            issues.append(f"color={color}")
        if form.get("theme") != "default":
            issues.append(f"theme={form.get('theme')}")
        if form.get("border_radius") != "small":
            issues.append(f"border_radius={form.get('border_radius')}")
        if not _truthy(form.get("auto_save")):
            issues.append(f"auto_save={form.get('auto_save')!r}")
        if form.get("visibility") != "public":
            issues.append(f"visibility={form.get('visibility')}")
        check(label, 1, not issues,
              "; ".join(issues) if issues else "all correct")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── OpenEMR — Connie McLaughlin ──────────────────────────────────────────────
def check_5_connie_family_history() -> None:
    """Latest post-anchor history_data row contains BOTH family-history phrases."""
    label = "5. Connie family history"
    pid, _enc, why = CONNIE
    if not pid:
        check(label, 1, False, f"gate: {why}")
        return
    try:
        row = openemr_db(
            f"SELECT CONCAT_WS(' | ', history_mother, history_father, "
            f"history_siblings, history_offspring, history_spouse, "
            f"additional_history, relatives_cancer, relatives_diabetes, "
            f"relatives_heart_problems) FROM history_data "
            f"WHERE pid='{pid}' AND date > '{HISTORY_SEED_MAX}' "
            f"ORDER BY date DESC, id DESC LIMIT 1"
        )
        if not row:
            check(label, 1, False,
                  f"pid={pid}: no history_data row after {HISTORY_SEED_MAX}")
            return
        low = row.lower()
        missing = [p for p in ("maternal aunt with asthma",
                               "paternal grandmother with hypothyroidism")
                   if p not in low]
        check(label, 1, not missing,
              f"pid={pid}: " + (f"missing {missing}" if missing
                                else f"both phrases in row: {row[:100]}"))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def _vitals_row(pid: str, enc: str) -> list[str] | None:
    """Vitals row anchored via the forms registry to the patient's new encounter."""
    out = openemr_db(
        f"SELECT v.weight, v.height, v.head_circ, v.temperature "
        f"FROM form_vitals v JOIN forms f ON f.form_id = v.id "
        f"AND f.formdir='vitals' AND f.deleted=0 "
        f"WHERE f.pid='{pid}' AND f.encounter='{enc}' "
        f"ORDER BY v.id DESC LIMIT 1"
    )
    if not out:
        return None
    parts = out.split("\t")
    return parts if len(parts) >= 4 else None


def _check_vitals(label: str, weight: int, patient_key: str,
                  who: tuple[str | None, str | None, str],
                  kg: float, cm_h: float, cm_hc: float, temp_f: float) -> None:
    """Numeric-equality vitals check (tolerance 0.05, no substrings).
    UNIT DEGRADATION (pending calibration): units_of_measurement=1 (US) means
    the UI converts kg->lbs and cm->inches on save, but we could not run the
    single UI-entry probe the plan requires; accept EITHER the raw metric value
    from the description OR its US-converted value.
    # TODO(calibration): pin single canonical unit after one UI-entry probe
    """
    pid, enc, why = who
    if not pid:
        check(label, weight, False, f"gate: {why}")
        return
    try:
        parts = _vitals_row(pid, enc)
        if not parts:
            check(label, weight, False,
                  f"pid={pid} enc={enc}: no vitals form on this encounter")
            return
        try:
            VITALS_DB[patient_key] = [float(p) for p in parts[:4]]
        except (TypeError, ValueError):
            VITALS_DB[patient_key] = None
        issues = []
        if not _num_ok(parts[0], kg, kg * LBS_PER_KG):
            issues.append(f"weight={parts[0]}")
        if not _num_ok(parts[1], cm_h, cm_h / CM_PER_INCH):
            issues.append(f"height={parts[1]}")
        if not _num_ok(parts[2], cm_hc, cm_hc / CM_PER_INCH):
            issues.append(f"head_circ={parts[2]}")
        if not _num_ok(parts[3], temp_f):
            issues.append(f"temp={parts[3]}")
        check(label, weight, not issues,
              f"pid={pid} enc={enc}: "
              + ("; ".join(issues) if issues else "all match (tol 0.05)"))
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_6_connie_vitals() -> None:
    """Vitals: weight 11.2, height 84.1, head_circ 47.0, temp 98.2."""
    _check_vitals("6. Connie vitals", 2, "connie", CONNIE, 11.2, 84.1, 47.0, 98.2)


def _check_icd10(label: str, weight: int,
                 who: tuple[str | None, str | None, str]) -> None:
    """Z00.129 billing row bound to the anchored encounter with code_type ICD10."""
    pid, enc, why = who
    if not pid:
        check(label, weight, False, f"gate: {why}")
        return
    try:
        row = openemr_db(
            f"SELECT code, code_type FROM billing WHERE pid='{pid}' "
            f"AND encounter='{enc}' AND code='Z00.129' AND code_type='ICD10' "
            f"LIMIT 1"
        )
        check(label, weight, bool(row),
              f"pid={pid} enc={enc}: "
              + (f"found {row[:60]}" if row
                 else "no ICD10 Z00.129 billing row on encounter"))
    except Exception as e:
        check(label, weight, False, f"exception: {e}")


def check_8_connie_icd10() -> None:
    _check_icd10("8. Connie ICD-10 Z00.129", 1, CONNIE)


def check_9_connie_care_plan() -> None:
    """Care Plan form on the anchored encounter containing goal + instructions
    phrases within the same form (forms-registry join)."""
    label = "9. Connie care plan"
    pid, enc, why = CONNIE
    if not pid:
        check(label, 2, False, f"gate: {why}")
        return
    try:
        out = openemr_db(
            f"SELECT CONCAT_WS(' ', v.care_plan_type, v.description) "
            f"FROM form_care_plan v JOIN forms f ON f.form_id = v.id "
            f"AND f.formdir='care_plan' AND f.deleted=0 "
            f"WHERE f.pid='{pid}' AND f.encounter='{enc}'"
        )
        if not out:
            check(label, 2, False,
                  f"pid={pid} enc={enc}: no care_plan form on this encounter")
            return
        low = out.lower()
        missing = [p for p in ("language development", "18-month milestones",
                               "encourage two-word phrases") if p not in low]
        check(label, 2, not missing,
              f"pid={pid} enc={enc}: "
              + (f"missing {missing}" if missing
                 else f"all phrases in form: {out[:80]}"))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OpenEMR — Lekisha Bosco ──────────────────────────────────────────────────
def check_10_lekisha_vitals() -> None:
    """Vitals: weight 14.2, height 95.5, head_circ 49.1, temp 99.0."""
    _check_vitals("10. Lekisha vitals", 2, "lekisha", LEKISHA, 14.2, 95.5, 49.1, 99.0)


def check_11_lekisha_immunization() -> None:
    """Same-row MMR immunization: vaccine identifies as MMR, manufacturer Merck,
    lot LOT52918, subcutaneous route, administered within the run window."""
    label = "11. Lekisha MMR immunization"
    pid, _enc, why = LEKISHA
    if not pid:
        check(label, 2, False, f"gate: {why}")
        return
    try:
        out = openemr_db(
            f"SELECT cvx_code, manufacturer, lot_number, route, note "
            f"FROM immunizations WHERE patient_id='{pid}' "
            f"AND administered_date >= CURDATE() - INTERVAL 1 DAY"
        )
        if not out:
            check(label, 2, False,
                  f"pid={pid}: no immunization administered since yesterday")
            return
        best = ""
        for line in out.split("\n"):
            parts = line.split("\t", 4)
            if len(parts) < 5:
                continue
            cvx, manuf, lot, route, note = (p.strip() for p in parts)
            issues = []
            if cvx not in ("03", "94") and "mmr" not in note.lower():
                issues.append(f"not MMR (cvx={cvx}, note={note[:30]})")
            if manuf.lower() != "merck":
                issues.append(f"manufacturer={manuf}")
            if lot.upper() != "LOT52918":
                issues.append(f"lot={lot}")
            # route is varchar(100); the UI may store an option id like 'SC'
            if "subcutaneous" not in route.lower() and route.upper() not in ("SC", "SQ"):
                issues.append(f"route={route}")
            if not issues:
                check(label, 2, True,
                      f"pid={pid}: MMR row OK (cvx={cvx}, route={route})")
                return
            best = best or "; ".join(issues)
        check(label, 2, False, f"pid={pid}: {best[:250] or 'unparseable rows'}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_13_lekisha_icd10() -> None:
    _check_icd10("13. Lekisha ICD-10 Z00.129", 1, LEKISHA)


def check_14_lekisha_clinical_note() -> None:
    """form_clinical_notes row bound to the anchored encounter containing both
    note phrases in the same row."""
    label = "14. Lekisha clinical note"
    pid, enc, why = LEKISHA
    if not pid:
        check(label, 2, False, f"gate: {why}")
        return
    try:
        out = openemr_db(
            f"SELECT description FROM form_clinical_notes "
            f"WHERE pid='{pid}' AND encounter='{enc}'"
        )
        if not out:
            check(label, 2, False,
                  f"pid={pid} enc={enc}: no clinical note on this encounter")
            return
        for line in out.split("\n"):
            low = line.lower()
            if "32-month well-child visit" in low and "fine motor coordination" in low:
                check(label, 2, True, f"pid={pid} enc={enc}: {line[:100]}")
                return
        check(label, 2, False,
              f"pid={pid} enc={enc}: no single note row with both phrases; "
              f"got: {out[:120]}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OnlyOffice Checks ────────────────────────────────────────────────────────
_xlsx_file: tuple[str, str] | None = None  # (id, title)


def _resolve_tracker_file() -> tuple[str, str] | None:
    """files_file row whose title (extension stripped) equals the exact expected
    title; editor crash-recovery copies ('... Recovery') explicitly excluded."""
    out = onlyoffice_db(
        "SELECT id, title FROM files_file "
        f"WHERE title LIKE '{XLSX_TITLE}%' AND title NOT LIKE '%Recovery%' "
        "ORDER BY id DESC;"
    )
    for line in out.split("\n"):
        parts = line.split("\t")
        if len(parts) >= 2:
            fid, title = parts[0].strip(), parts[1].strip()
            if re.sub(r"\.(xlsx|xls|ods|csv)$", "", title, flags=re.I) == XLSX_TITLE:
                return fid, title
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
    s = requests.Session()
    try:
        resp = s.post(f"{ONLYOFFICE_BASE}/api/2.0/authentication",
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
        return s
    except Exception:
        return None


def _oo_bytes_from_api(file_id: str) -> bytes | None:
    s = _oo_auth_session()
    if not s:
        return None
    for url, params in (
        (f"{ONLYOFFICE_BASE}/products/files/httphandlers/filehandler.ashx",
         {"action": "download", "fileid": str(file_id)}),
        (f"{ONLYOFFICE_BASE}/api/2.0/files/file/{file_id}/download", None),
    ):
        try:
            r = s.get(url, params=params, timeout=30, allow_redirects=True)
            if r.status_code == 200 and len(r.content) > 100:
                return r.content
        except Exception:
            continue
    return None


def _oo_get_xlsx() -> tuple[bytes | None, str]:
    """(bytes|None, source detail). fs first, API fallback."""
    if not _xlsx_file:
        return None, "gate: spreadsheet not found in files_file"
    file_id, title = _xlsx_file
    data = _oo_bytes_from_fs(file_id)
    if data:
        return data, f"fs id={file_id}"
    data = _oo_bytes_from_api(file_id)
    if data:
        return data, f"api id={file_id}"
    return None, f"id={file_id} found but content unreadable (fs+api)"


# ── xlsx parsing (brief convention E) ────────────────────────────────────────
def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        s = s.replace(a, b)
    return s


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
    """Rows as lists of cell strings (same-row assertions run per row)."""
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
    out = []
    for name in zf.namelist():
        if re.match(r"xl/charts/chart\d*\.xml$", name):
            out.append(zf.read(name).decode("utf-8", errors="replace"))
    return out


def _sheet_has_chart(zf: zipfile.ZipFile, sheet_path: str) -> bool:
    """Soft probe: does this worksheet mount a drawing that references a chart?"""
    try:
        base = sheet_path.rsplit("/", 1)[-1]
        rels = zf.read(f"xl/worksheets/_rels/{base}.rels").decode(
            "utf-8", errors="replace")
        for tgt in re.findall(r'Target="([^"]*drawing[^"]*)"', rels):
            dname = tgt.rsplit("/", 1)[-1]
            try:
                drels = zf.read(f"xl/drawings/_rels/{dname}.rels").decode(
                    "utf-8", errors="replace")
            except KeyError:
                continue
            if "charts/chart" in drels:
                return True
    except KeyError:
        pass
    return False


def check_15_onlyoffice_spreadsheet_exists() -> None:
    """0pt precondition gate: spreadsheet with the exact title exists
    (extension-stripped equality; Recovery copies excluded)."""
    label = "15. OnlyOffice spreadsheet exists (gate)"
    if _xlsx_file:
        check(label, 0, True, f"id={_xlsx_file[0]} title={_xlsx_file[1][:60]}")
    else:
        check(label, 0, False,
              f"no files_file row with exact title '{XLSX_TITLE}' "
              f"(Recovery copies excluded)")


def check_16a_sheet_names() -> None:
    """Spreadsheet has exactly the 3 named sheets."""
    label = "16a. OnlyOffice sheet names"
    try:
        data, src = _oo_get_xlsx()
        if not data:
            check(label, 1, False, src)
            return
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = [_norm_text(n) for n in _xlsx_sheets(zf)]
        expected = ["growth vitals", "milestone status", "visit summary"]
        missing = [e for e in expected if e not in names]
        check(label, 1, not missing,
              f"{src}; sheets={names[:5]}"
              + (f", missing={missing}" if missing else " (3/3)"))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


MILESTONES = ["walks independently", "stacks three or more blocks",
              "uses two-word phrases", "points to named body parts",
              "follows simple instructions", "scribbles with crayon"]
STATUS_ENUM = {"achieved", "emerging", "not yet", "unable to assess"}
SHEET3_SPECS = [
    ("weight percentile", "45th", "35th"),
    ("height percentile", "50th", "40th"),
    ("head circumference percentile", "55th", "45th"),
    ("immunization given", "n/a", "mmr"),
    ("follow-up needed", "no - routine 24-month visit",
     "yes - fine motor evaluation at next visit"),
]


def _sheet1_patient_ok(rows: list[list[str]], patient: str, age_group: str,
                       key: str) -> str | None:
    """Return None if some row reconciles with the live DB vitals row, else a
    reason. DB values are whatever form_vitals actually stores; each must match
    a numeric cell within 0.05 (metric-normalized counterpart also accepted
    pending the unit calibration — see _check_vitals TODO)."""
    db = VITALS_DB.get(key)
    if not db:
        return f"{patient}: no DB vitals row to reconcile against"
    # weight may be stored in lbs, lengths in inches (US units) while the sheet
    # uses the kg/cm columns from the description.
    accepted = [
        (db[0], db[0] / LBS_PER_KG),   # weight
        (db[1], db[1] * CM_PER_INCH),  # height
        (db[2], db[2] * CM_PER_INCH),  # head circumference
        (db[3],),                      # temperature (already degF)
    ]
    p_norm = _norm_text(patient)
    ag_norm = _norm_text(age_group)
    best = f"{patient}: no Growth Vitals row"
    for row in rows:
        row_norm = _norm_text(" | ".join(row))
        if p_norm not in row_norm:
            continue
        if ag_norm not in row_norm:
            best = f"{patient}: row found but age group '{age_group}' missing"
            continue
        nums = []
        for c in row:
            try:
                nums.append(float(c.replace(",", "")))
            except ValueError:
                continue
        unmatched = [i for i, exp in enumerate(accepted)
                     if not any(any(abs(n - e) < NUM_TOL for e in exp)
                                for n in nums)]
        if not unmatched:
            return None
        best = (f"{patient}: cells {nums[:8]} miss DB vitals idx {unmatched} "
                f"(db={[round(v, 2) for v in db]})")
    return best


def check_16b_sheet_content() -> None:
    """Content probe: Sheet1 vitals cross-reconciled with live DB, Sheet2
    milestone rows enum-valid, Sheet3 metric rows/values, bar chart present."""
    label = "16b. OnlyOffice spreadsheet content + chart"
    try:
        data, src = _oo_get_xlsx()
        if not data:
            check(label, 2, False, src)
            return
        zf = zipfile.ZipFile(io.BytesIO(data))
        shared = _xlsx_shared_strings(zf)
        sheets = {_norm_text(k): v for k, v in _xlsx_sheets(zf).items()}
        issues = []

        # Sheet 1: Growth Vitals — cross-reconcile with live form_vitals rows.
        s1 = sheets.get("growth vitals")
        if not s1:
            issues.append("sheet 'Growth Vitals' missing")
        else:
            rows = _xlsx_rows(zf, s1, shared)
            for patient, ag, key in (
                ("Connie McLaughlin", "13-24 months", "connie"),
                ("Lekisha Bosco", "25-36 months", "lekisha"),
            ):
                r = _sheet1_patient_ok(rows, patient, ag, key)
                if r:
                    issues.append(r)

        # Sheet 2: Milestone Status — 6 rows, status cells enum-valid only
        # (the description's expected distribution is ambiguous; do not assert it).
        s2 = sheets.get("milestone status")
        if not s2:
            issues.append("sheet 'Milestone Status' missing")
        else:
            rows = _xlsx_rows(zf, s2, shared)
            for m in MILESTONES:
                row = next((r for r in rows
                            if any(_norm_text(c) == m for c in r)), None)
                if row is None:
                    issues.append(f"milestone row '{m}' missing")
                    continue
                statuses = [_norm_text(c) for c in row
                            if c.strip() and _norm_text(c) != m]
                if not statuses:
                    issues.append(f"'{m}': no status cells")
                elif any(s not in STATUS_ENUM for s in statuses):
                    bad = [s for s in statuses if s not in STATUS_ENUM]
                    issues.append(f"'{m}': invalid status {bad[:2]}")

        # Sheet 3: Visit Summary — 5 metric rows with both patients' values.
        s3 = sheets.get("visit summary")
        if not s3:
            issues.append("sheet 'Visit Summary' missing")
        else:
            rows = _xlsx_rows(zf, s3, shared)
            row_texts = [_norm_text(" | ".join(r)) for r in rows]
            for metric, v1, v2 in SHEET3_SPECS:
                if not any(metric in t and v1 in t and v2 in t
                           for t in row_texts):
                    issues.append(f"summary row '{metric}' ({v1}/{v2}) missing")

        # Chart: bar chart XML must exist (hard); mounting on the Visit Summary
        # sheet is informational only (plan risk (d)).
        charts = _xlsx_charts(zf)
        if not any("barChart" in c for c in charts):
            issues.append(f"no barChart in xl/charts ({len(charts)} chart xml)")
        mount = "yes" if (s3 and _sheet_has_chart(zf, s3)) else "no"

        check(label, 2, not issues,
              (f"{src}; chart_on_sheet3={mount} (info); "
               + ("; ".join(issues)[:250] if issues else "sheet1+2+3+chart OK")))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    global _opnform_form, CONNIE, LEKISHA, _xlsx_file

    # Resolutions (wrapped so an infra error never kills the SCORE line).
    try:
        _find_opnform_form(_opnform_login())
    except Exception as e:
        _opnform_form = None
        print(f"WARN: opnform resolution failed: {e}", file=sys.stderr)
    try:
        CONNIE = resolve_patient("Connie", "McLaughlin")
    except Exception as e:
        CONNIE = (None, None, f"resolution exception: {e}")
    try:
        LEKISHA = resolve_patient("Lekisha", "Bosco")
    except Exception as e:
        LEKISHA = (None, None, f"resolution exception: {e}")
    try:
        _xlsx_file = _resolve_tracker_file()
    except Exception as e:
        _xlsx_file = None
        print(f"WARN: onlyoffice resolution failed: {e}", file=sys.stderr)

    check_1_opnform_form_exists()
    check_2_opnform_fields()
    check_3_opnform_matrix()
    check_3b_opnform_logic_pagebreak()
    check_4_opnform_styling()
    check_5_connie_family_history()
    check_6_connie_vitals()
    check_8_connie_icd10()
    check_9_connie_care_plan()
    check_10_lekisha_vitals()
    check_11_lekisha_immunization()
    check_13_lekisha_icd10()
    check_14_lekisha_clinical_note()
    check_15_onlyoffice_spreadsheet_exists()
    check_16a_sheet_names()
    check_16b_sheet_content()

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
