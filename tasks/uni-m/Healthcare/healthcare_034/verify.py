#!/usr/bin/env python3
"""
Verifier for Healthcare-034-I1: New Patient Orientation Workflow for Maria Gonzalez

Checks: 18 checks (2 zero-weight gates) across opnform, openemr, onlyoffice.
Strategy: docker exec (DB) for OpnForm and OpenEMR; filesystem-first with API
fallback for OnlyOffice document content.

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

OPNFORM_PORT = os.getenv("OPNFORM_PORT")
OPNFORM_CONTAINER = os.getenv("OPNFORM_CONTAINER")

OPENEMR_PORT = os.getenv("OPENEMR_PORT")
OPENEMR_CONTAINER = os.getenv("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.getenv("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.getenv("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.getenv("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.getenv("ONLYOFFICE_DB_CONTAINER")

for _v in [
    "OPNFORM_PORT", "OPNFORM_CONTAINER",
    "OPENEMR_PORT", "OPENEMR_CONTAINER", "OPENEMR_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
]:
    if not os.getenv(_v):
        print(f"FATAL: {_v} not set", file=sys.stderr)
        sys.exit(1)

# probed seed max, mw-openemr:latest 2026-08 (form_encounter: 842 rows,
# MAX(date)='2026-03-05 00:00:00'; `date > '2026-03-05'` excludes the seed max)
SEED_ENCOUNTER_MAX_DATE = "2026-03-05"


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


def opnform_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def openemr_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "--default-character-set=utf8mb4", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(sql: str) -> str:
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice", "--default-character-set=utf8mb4", "-N", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def _num_eq(val: str, expected: float, tol: float = 0.5) -> bool:
    """Numeric compare tolerant of DB float formatting (e.g. '76.000000')."""
    try:
        return abs(float(str(val).strip()) - expected) <= tol
    except (ValueError, TypeError):
        return False


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


# ── Canonical patient pid (name + DOB, exactly one row — no LIMIT 1) ─────────
_patient_pid: str | None = None
_pid_error: str = ""


def pid() -> str:
    global _patient_pid, _pid_error
    if _patient_pid is None:
        _patient_pid = ""
        try:
            out = openemr_sql(
                "SELECT pid FROM patient_data "
                "WHERE fname='Maria' AND lname='Gonzalez' AND DOB='1985-07-22';"
            )
            rows = [r.strip() for r in out.splitlines() if r.strip()]
            if len(rows) == 1:
                _patient_pid = rows[0]
            elif not rows:
                _pid_error = "no Maria Gonzalez (DOB 1985-07-22) patient row"
            else:
                _pid_error = f"{len(rows)} duplicate Maria Gonzalez patient rows"
        except Exception as e:
            _pid_error = f"pid lookup exception: {e}"
    return _patient_pid


# ── Anchored encounters (date > seed max, via form_encounter) ────────────────
_enc_cache: list[str] | None = None
_enc_error: str = ""


def anchored_encounters() -> list[str]:
    global _enc_cache, _enc_error
    if _enc_cache is None:
        _enc_cache = []
        p = pid()
        if not p:
            _enc_error = _pid_error or "no patient"
        else:
            try:
                out = openemr_sql(
                    f"SELECT encounter FROM form_encounter "
                    f"WHERE pid={p} AND date > '{SEED_ENCOUNTER_MAX_DATE}';"
                )
                _enc_cache = [r.strip() for r in out.splitlines() if r.strip()]
                if not _enc_cache:
                    _enc_error = (
                        f"no encounter dated after {SEED_ENCOUNTER_MAX_DATE} for pid {p}"
                    )
            except Exception as e:
                _enc_error = f"encounter lookup exception: {e}"
    return _enc_cache


# ── OpnForm: cached form row + field helpers ─────────────────────────────────
_form_data: dict | None = None


def get_form() -> dict:
    global _form_data
    if _form_data is None:
        out = opnform_sql(
            "SELECT row_to_json(f) FROM forms f "
            "WHERE title = 'New Patient Orientation Questionnaire 2026' LIMIT 1;"
        )
        _form_data = json.loads(out) if out else {}
    return _form_data


def get_props() -> list:
    props = get_form().get("properties", [])
    if isinstance(props, str):
        props = json.loads(props)
    return props if isinstance(props, list) else []


def _norm_name(s) -> str:
    return " ".join(str(s).lower().split()).rstrip("?").strip()


def _field_by_name(props: list, name: str) -> dict | None:
    want = _norm_name(name)
    for p in props:
        if isinstance(p, dict) and _norm_name(p.get("name", "")) == want:
            return p
    return None


def _opt_names(field: dict, kind: str) -> set[str]:
    opts = (field.get(kind) or {}).get("options") or []
    return {str(o.get("name", "")).strip().lower() for o in opts if isinstance(o, dict)}


def _num_attr_eq(field: dict, key: str, expected: float) -> bool:
    v = field.get(key)
    if v is None:
        return False
    try:
        return abs(float(str(v).strip()) - expected) < 1e-9
    except (ValueError, TypeError):
        return False


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


# 16 named input fields from the task description (nf-video asserted separately).
EXPECTED_FIELDS: list[tuple[str, str]] = [
    ("Patient Full Name", "text"),
    ("Date of Birth", "date"),
    ("Preferred Language", "select"),
    ("How Did You Hear About Us", "select"),
    ("Referring Website", "url"),
    ("Health Goals", "multi_select"),
    ("Current Health Self-Assessment", "scale"),
    ("Do you have a primary care provider currently", "checkbox"),
    ("Current Provider Name", "text"),
    ("I have watched the orientation video", "checkbox"),
    ("Chronic Conditions", "multi_select"),
    ("Other Conditions", "text"),
    ("Last Physical Exam Date", "date"),
    ("Currently Taking Medications", "checkbox"),
    ("Medication List", "text"),
    ("Preferred Contact Email", "email"),
]

# conditional field -> trigger field (by name)
CONDITIONAL_FIELDS: list[tuple[str, str]] = [
    ("Referring Website", "How Did You Hear About Us"),
    ("Current Provider Name", "Do you have a primary care provider currently"),
    ("Other Conditions", "Chronic Conditions"),
    ("Medication List", "Currently Taking Medications"),
]


# ── Check 1 (gate): OpnForm form exists ──────────────────────────────────────
def check_1_form_exists() -> None:
    """Gate: form titled 'New Patient Orientation Questionnaire 2026' exists."""
    try:
        f = get_form()
        check("1. OpnForm form exists (gate)", 0, bool(f),
              f"title={f.get('title', '')}" if f else "not found")
    except Exception as e:
        check("1. OpnForm form exists (gate)", 0, False, f"exception: {e}")


# ── Check 2: OpnForm form settings ───────────────────────────────────────────
def check_2_form_settings() -> None:
    """Settings: public, editable submissions + button text, redirect URL,
    re-fillable off, theme default, focused presentation, auto-focus."""
    try:
        f = get_form()
        if not f:
            check("2. OpnForm form settings", 2, False, "gate: form not found")
            return

        issues = []
        if f.get("visibility") != "public":
            issues.append(f"visibility={f.get('visibility')}")
        if not f.get("editable_submissions"):
            issues.append("editable_submissions off")
        rurl = f.get("redirect_url") or ""
        if "clinic.example.com/orientation/thank-you" not in rurl:
            issues.append(f"redirect_url={rurl!r}")
        if f.get("re_fillable"):
            issues.append("re_fillable should be off")
        if f.get("theme") != "default":
            issues.append(f"theme={f.get('theme')}")
        if f.get("presentation_style") != "focused":
            issues.append(f"presentation_style={f.get('presentation_style')}")
        if not _truthy(f.get("auto_focus")):
            issues.append(f"auto_focus={f.get('auto_focus')}")
        btn = (f.get("editable_submissions_button_text") or "").strip()
        if btn != "Update My Information":
            issues.append(f"editable_submissions_button_text={btn!r}")

        check("2. OpnForm form settings", 2, not issues,
              "correct" if not issues else "; ".join(issues)[:160])
    except Exception as e:
        check("2. OpnForm form settings", 2, False, f"exception: {e}")


# ── Check 3a: OpnForm field inventory + types ────────────────────────────────
def check_3a_field_types() -> None:
    """All 16 named input fields present with exact types, plus an nf-video
    element (type only — its URL attribute name is unverified)."""
    try:
        if not get_form():
            check("3a. OpnForm field types", 1, False, "gate: form not found")
            return
        props = get_props()
        if not props:
            check("3a. OpnForm field types", 1, False, "properties empty/not a list")
            return

        issues = []
        for name, want_type in EXPECTED_FIELDS:
            fld = _field_by_name(props, name)
            if fld is None:
                issues.append(f"missing:{name}")
            elif (fld.get("type") or "") != want_type:
                issues.append(f"{name}:type={fld.get('type')}!={want_type}")
        if not any(p.get("type") == "nf-video" for p in props if isinstance(p, dict)):
            issues.append("no nf-video element")

        check("3a. OpnForm field types", 1, not issues,
              f"16 named fields + nf-video ({len(props)} total)"
              if not issues else "; ".join(issues)[:160])
    except Exception as e:
        check("3a. OpnForm field types", 1, False, f"exception: {e}")


# ── Check 3b: OpnForm option sets + numeric specs ────────────────────────────
def check_3b_field_specs() -> None:
    """Option sets (superset), min_selection/allow_creation, scale 1-10 step 1,
    disable_future_dates."""
    try:
        if not get_form():
            check("3b. OpnForm field specs", 1, False, "gate: form not found")
            return
        props = get_props()
        issues = []

        lang = _field_by_name(props, "Preferred Language")
        if not lang:
            issues.append("no Preferred Language")
        else:
            miss = {"english", "spanish", "mandarin", "portuguese"} - _opt_names(lang, "select")
            if miss:
                issues.append(f"language opts missing {sorted(miss)}")

        hear = _field_by_name(props, "How Did You Hear About Us")
        if not hear:
            issues.append("no How Did You Hear")
        else:
            miss = {"physician referral", "insurance directory", "online search",
                    "friend/family", "other"} - _opt_names(hear, "select")
            if miss:
                issues.append(f"hear opts missing {sorted(miss)}")

        goals = _field_by_name(props, "Health Goals")
        if not goals:
            issues.append("no Health Goals")
        else:
            miss = {"weight management", "chronic pain relief", "mental health support",
                    "preventive care", "medication management",
                    "smoking cessation"} - _opt_names(goals, "multi_select")
            if miss:
                issues.append(f"goals opts missing {sorted(miss)}")
            ms = (goals.get("multi_select") or {})
            if not _num_eq(str(ms.get("min_selection", goals.get("min_selection"))), 1, tol=0):
                issues.append(f"min_selection={ms.get('min_selection', goals.get('min_selection'))}")
            if not _truthy(ms.get("allow_creation", goals.get("allow_creation"))):
                issues.append("allow_creation not true")

        chronic = _field_by_name(props, "Chronic Conditions")
        if not chronic:
            issues.append("no Chronic Conditions")
        else:
            miss = {"diabetes", "hypertension", "asthma", "heart disease", "depression",
                    "anxiety", "none"} - _opt_names(chronic, "multi_select")
            if miss:
                issues.append(f"chronic opts missing {sorted(miss)}")

        scale = _field_by_name(props, "Current Health Self-Assessment")
        if not scale:
            issues.append("no scale field")
        else:
            # min/step equal the OpnForm type defaults; the UI only persists
            # changed keys (plan D risk note), so ABSENT counts as compliant.
            # max was changed away from the default and must be explicit.
            if scale.get("scale_min_value") is not None and not _num_attr_eq(scale, "scale_min_value", 1):
                issues.append(f"scale_min={scale.get('scale_min_value')}")
            if not _num_attr_eq(scale, "scale_max_value", 10):
                issues.append(f"scale_max={scale.get('scale_max_value')}")
            if scale.get("scale_step_value") is not None and not _num_attr_eq(scale, "scale_step_value", 1):
                issues.append(f"scale_step={scale.get('scale_step_value')}")

        exam = _field_by_name(props, "Last Physical Exam Date")
        if not exam:
            issues.append("no Last Physical Exam Date")
        elif not _truthy(exam.get("disable_future_dates")):
            issues.append(f"disable_future_dates={exam.get('disable_future_dates')}")

        check("3b. OpnForm field specs", 1, not issues,
              "options + scale 1-10/1 + no-future-dates correct"
              if not issues else "; ".join(issues)[:160])
    except Exception as e:
        check("3b. OpnForm field specs", 1, False, f"exception: {e}")


# ── Check 3c: OpnForm conditional visibility logic ───────────────────────────
def check_3c_conditional_logic() -> None:
    """4 conditional fields: non-empty logic referencing the trigger field id,
    with 'show-block' in actions (operator tree intentionally not asserted)."""
    try:
        if not get_form():
            check("3c. OpnForm conditional logic", 1, False, "gate: form not found")
            return
        props = get_props()
        issues = []
        for cond_name, trig_name in CONDITIONAL_FIELDS:
            fld = _field_by_name(props, cond_name)
            trig = _field_by_name(props, trig_name)
            if fld is None:
                issues.append(f"missing:{cond_name}")
                continue
            if trig is None:
                issues.append(f"missing trigger:{trig_name}")
                continue
            trig_id = str(trig.get("id") or "")
            logic = fld.get("logic") or {}
            conditions = logic.get("conditions") if isinstance(logic, dict) else None
            if not conditions:
                issues.append(f"{cond_name}: empty logic")
                continue
            leaves = _logic_leaves(conditions)
            if not any(_leaf_trigger_id(lf) == trig_id for lf in leaves):
                issues.append(f"{cond_name}: no leaf references trigger id")
            if "show-block" not in (logic.get("actions") or []):
                issues.append(f"{cond_name}: actions={logic.get('actions')}")

        check("3c. OpnForm conditional logic", 1, not issues,
              "4 conditional fields wired to their triggers"
              if not issues else "; ".join(issues)[:160])
    except Exception as e:
        check("3c. OpnForm conditional logic", 1, False, f"exception: {e}")


# ── Check 4: Patient demographics ────────────────────────────────────────────
def check_4_demographics() -> None:
    """Exactly one Maria Gonzalez (DOB 1985-07-22), Female, Spanish."""
    try:
        out = openemr_sql(
            "SELECT sex, language FROM patient_data "
            "WHERE fname='Maria' AND lname='Gonzalez' AND DOB='1985-07-22';"
        )
        rows = [r for r in out.splitlines() if r.strip()]
        if not rows:
            check("4. Patient demographics", 2, False,
                  "no Maria Gonzalez (DOB 1985-07-22) row")
            return

        issues = []
        if len(rows) != 1:
            issues.append(f"{len(rows)} rows (duplicate records)")
        cols = rows[0].split("\t")
        if len(cols) < 2:
            issues.append(f"cols={len(cols)}")
        else:
            if "female" not in cols[0].lower():
                issues.append(f"sex={cols[0]}")
            if "spanish" not in cols[1].lower() and "spa" not in cols[1].lower():
                issues.append(f"lang={cols[1]}")

        check("4. Patient demographics", 2, not issues,
              "unique row, Female, Spanish" if not issues else "; ".join(issues))
    except Exception as e:
        check("4. Patient demographics", 2, False, f"exception: {e}")


# ── Check 5: Patient contact info ────────────────────────────────────────────
def check_5_contact() -> None:
    """Address 45 Oakwood Avenue / Springfield / MA / 01103, phone, email."""
    try:
        p = pid()
        if not p:
            check("5. Contact info", 1, False, f"gate: {_pid_error}")
            return

        out = openemr_sql(
            f"SELECT street, city, state, postal_code, phone_home, phone_cell, email "
            f"FROM patient_data WHERE pid={p};"
        )
        c = out.split("\t")
        if len(c) < 7:
            check("5. Contact info", 1, False, f"unexpected cols={len(c)}")
            return

        street, city, state, postal = c[0].lower(), c[1].strip().lower(), c[2].strip().lower(), c[3].strip()
        phones = f"{c[4]} {c[5]}"
        email = c[6].strip().lower()
        issues = []
        if "oakwood" not in street:
            issues.append(f"street={c[0][:30]}")
        if city != "springfield":
            issues.append(f"city={c[1]}")
        if state not in ("ma", "massachusetts"):
            issues.append(f"state={c[2]}")
        if postal != "01103":
            issues.append(f"postal={postal}")
        if "413-555-0198" not in phones and "4135550198" not in phones:
            issues.append("phone")
        if "maria.gonzalez@example.com" not in email:
            issues.append(f"email={email[:40]}")

        check("5. Contact info", 1, not issues,
              "address/phone/email correct" if not issues else "; ".join(issues)[:160])
    except Exception as e:
        check("5. Contact info", 1, False, f"exception: {e}")


# ── Check 6: Primary provider ────────────────────────────────────────────────
def check_6_provider() -> None:
    """Primary provider is Dr. Elizbeth Dickinson."""
    try:
        p = pid()
        if not p:
            check("6. Primary provider", 1, False, f"gate: {_pid_error}")
            return

        out = openemr_sql(
            f"SELECT u.fname, u.lname FROM users u "
            f"WHERE u.id IN (SELECT providerID FROM patient_data WHERE pid={p}) "
            f"AND u.id > 0;"
        )
        ok = "dickinson" in out.lower()
        check("6. Primary provider", 1, ok,
              out.strip()[:80] if out.strip() else "none assigned")
    except Exception as e:
        check("6. Primary provider", 1, False, f"exception: {e}")


# ── Check 7: Patient history ─────────────────────────────────────────────────
def check_7_history() -> None:
    """Per-category history: tobacco 'never', alcohol social/1-2, past medical
    asthma+appendectomy+allerg, family Diabetes+Hypertension+Breast cancer."""
    try:
        p = pid()
        if not p:
            check("7. Patient history", 2, False, f"gate: {_pid_error}")
            return

        out = openemr_sql(
            f"SELECT tobacco, alcohol, history_father, history_mother, "
            f"history_siblings, additional_history, "
            f"usertext11, usertext12, usertext13, usertext14, usertext15 "
            f"FROM history_data WHERE pid={p} ORDER BY id DESC LIMIT 1;"
        )
        if not out.strip():
            check("7. Patient history", 2, False, "no history_data row")
            return

        c = (out.splitlines()[0].split("\t") + [""] * 11)[:11]
        tobacco, alcohol = c[0].lower(), c[1].lower()
        hist_union = " ".join(c[2:]).replace("\\n", " ").lower()

        issues = []
        for probe in ("asthma", "appendectomy", "allerg"):
            if probe not in hist_union:
                issues.append(f"past_medical:{probe}")
        for probe in ("diabetes", "hypertension", "breast cancer"):
            if probe not in hist_union:
                issues.append(f"family:{probe}")
        if "never" not in tobacco:
            issues.append(f"tobacco={tobacco[:30]}")
        if "social" not in alcohol and "1-2" not in alcohol:
            issues.append(f"alcohol={alcohol[:30]}")

        check("7. Patient history", 2, not issues,
              "all categories present" if not issues else f"missing: {issues}"[:160])
    except Exception as e:
        check("7. Patient history", 2, False, f"exception: {e}")


# ── Check 8: Issue — Essential Hypertension I10 ──────────────────────────────
def check_8_hypertension() -> None:
    """Active medical problem titled Essential Hypertension with ICD-10 I10."""
    try:
        p = pid()
        if not p:
            check("8. Issue: Hypertension I10", 1, False, f"gate: {_pid_error}")
            return

        out = openemr_sql(
            f"SELECT title, diagnosis, activity FROM lists "
            f"WHERE pid={p} AND type='medical_problem' AND activity=1 "
            f"AND title LIKE '%Essential Hypertension%' AND diagnosis LIKE '%I10%';"
        )
        ok = bool(out.strip()) and "I10" in out
        check("8. Issue: Hypertension I10", 1, ok,
              out.strip().splitlines()[0][:80] if out.strip()
              else "no active 'Essential Hypertension' issue with I10")
    except Exception as e:
        check("8. Issue: Hypertension I10", 1, False, f"exception: {e}")


# ── Check 9: Issue — Type 2 Diabetes E11.9 ───────────────────────────────────
def check_9_diabetes() -> None:
    """Active medical problem titled Type 2 Diabetes ... without complications, E11.9."""
    try:
        p = pid()
        if not p:
            check("9. Issue: Diabetes E11.9", 1, False, f"gate: {_pid_error}")
            return

        out = openemr_sql(
            f"SELECT title, diagnosis, activity FROM lists "
            f"WHERE pid={p} AND type='medical_problem' AND activity=1 "
            f"AND title LIKE '%Type 2 Diabetes%without complications%' "
            f"AND diagnosis LIKE '%E11.9%';"
        )
        ok = bool(out.strip()) and "E11.9" in out
        check("9. Issue: Diabetes E11.9", 1, ok,
              out.strip().splitlines()[0][:80] if out.strip()
              else "no active 'Type 2 Diabetes...without complications' issue with E11.9")
    except Exception as e:
        check("9. Issue: Diabetes E11.9", 1, False, f"exception: {e}")


# ── Check 10: Encounter vitals (anchored) ────────────────────────────────────
def check_10_vitals() -> None:
    """Vitals on a new (post-seed) encounter: BP 138/88, pulse 76, temp 98.4,
    height 64, weight 168; BMI printed as info (expect ~28.8)."""
    try:
        p = pid()
        if not p:
            check("10. Encounter vitals", 2, False, f"gate: {_pid_error}")
            return
        encs = anchored_encounters()
        if not encs:
            check("10. Encounter vitals", 2, False, f"gate: {_enc_error}")
            return

        out = openemr_sql(
            f"SELECT fv.bps, fv.bpd, fv.pulse, fv.temperature, fv.height, fv.weight, fv.BMI "
            f"FROM form_vitals fv "
            f"JOIN forms f ON f.form_id = fv.id AND f.formdir = 'vitals' AND f.deleted = 0 "
            f"WHERE f.pid = {p} AND f.encounter IN ({','.join(encs)}) "
            f"ORDER BY fv.id DESC LIMIT 1;"
        )
        if not out.strip():
            check("10. Encounter vitals", 2, False, "no vitals on anchored encounter")
            return

        c = out.strip().split("\t")
        issues = []
        bmi = ""
        if len(c) >= 7:
            bmi = c[6].strip()
            # form_vitals stores numeric columns as floats (e.g. pulse
            # '76.000000'), so compare numerically instead of as strings.
            if not _num_eq(c[0], 138):
                issues.append(f"sys={c[0].strip()}")
            if not _num_eq(c[1], 88):
                issues.append(f"dia={c[1].strip()}")
            if not _num_eq(c[2], 76):
                issues.append(f"pulse={c[2].strip()}")
            if not _num_eq(c[3], 98.4, tol=0.1):
                issues.append(f"temp={c[3].strip()}")
            if not _num_eq(c[4], 64):
                issues.append(f"ht={c[4].strip()}")
            if not _num_eq(c[5], 168):
                issues.append(f"wt={c[5].strip()}")
        else:
            issues.append(f"unexpected cols={len(c)}")

        bmi_info = f"info: BMI={bmi} (expect ~28.8)"
        check("10. Encounter vitals", 2, not issues,
              f"BP 138/88, pulse 76, temp 98.4, ht 64, wt 168; {bmi_info}"
              if not issues else f"{'; '.join(issues)}; {bmi_info}")
    except Exception as e:
        check("10. Encounter vitals", 2, False, f"exception: {e}")


# ── Check 11: Care Plan (anchored, per care_plan_type) ───────────────────────
def check_11_care_plan() -> None:
    """Care Plan on the anchored encounter: full goal + instruction anchors,
    preferring the 'goal'/'instructions' rows, union fallback."""
    try:
        p = pid()
        if not p:
            check("11. Care Plan", 2, False, f"gate: {_pid_error}")
            return
        encs = anchored_encounters()
        if not encs:
            check("11. Care Plan", 2, False, f"gate: {_enc_error}")
            return

        out = openemr_sql(
            f"SELECT fcp.care_plan_type, fcp.description FROM form_care_plan fcp "
            f"JOIN forms f ON f.form_id = fcp.id AND f.formdir = 'care_plan' AND f.deleted = 0 "
            f"WHERE f.pid = {p} AND f.encounter IN ({','.join(encs)});"
        )
        if not out.strip():
            check("11. Care Plan", 2, False, "no care_plan rows on anchored encounter")
            return

        goal_parts, instr_parts, union_parts = [], [], []
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 1)
            ctype = parts[0].strip().lower()
            desc = (parts[1] if len(parts) > 1 else "").replace("\\n", " ").lower()
            union_parts.append(desc)
            if ctype == "goal":
                goal_parts.append(desc)
            elif ctype == "instructions":
                instr_parts.append(desc)

        union = " ".join(union_parts)
        goal_src = " ".join(goal_parts) or union
        instr_src = " ".join(instr_parts) or union

        issues = []
        for probe in ("achieve bp below 130/80", "hba1c below 7.0", "6 months"):
            if probe not in goal_src:
                issues.append(f"goal:{probe!r}")
        for probe in ("dash diet", "monitor bp daily", "follow up in 4 weeks"):
            if probe not in instr_src:
                issues.append(f"instr:{probe!r}")

        check("11. Care Plan", 2, not issues,
              "goal + instructions anchors present"
              if not issues else f"missing: {issues}"[:160])
    except Exception as e:
        check("11. Care Plan", 2, False, f"exception: {e}")


# ── Check 12: Clinical Instructions (anchored, AND of anchors) ───────────────
def check_12_clinical_instructions() -> None:
    """Clinical Instructions on the anchored encounter contain all four anchors."""
    try:
        p = pid()
        if not p:
            check("12. Clinical Instructions", 1, False, f"gate: {_pid_error}")
            return
        encs = anchored_encounters()
        if not encs:
            check("12. Clinical Instructions", 1, False, f"gate: {_enc_error}")
            return

        out = openemr_sql(
            f"SELECT fci.instruction FROM form_clinical_instructions fci "
            f"JOIN forms f ON f.form_id = fci.id "
            f"AND f.formdir = 'clinical_instructions' AND f.deleted = 0 "
            f"WHERE f.pid = {p} AND f.encounter IN ({','.join(encs)});"
        )
        if not out.strip():
            check("12. Clinical Instructions", 1, False,
                  "no clinical_instructions rows on anchored encounter")
            return

        lo = " ".join(re.sub(r"<[^>]+>", " ", out.replace("\\n", " ")).lower().split())
        missing = [probe for probe in (
            "orientation questionnaire", "patient portal",
            "lab work within 2 weeks", "follow-up appointment in 4 weeks",
        ) if probe not in lo]

        check("12. Clinical Instructions", 1, not missing,
              "all 4 anchors present" if not missing else f"missing: {missing}"[:160])
    except Exception as e:
        check("12. Clinical Instructions", 1, False, f"exception: {e}")


# ── Check 13: Fee Sheet Z00.00 (ICD10, anchored encounter) ───────────────────
def check_13_fee_sheet() -> None:
    """billing row: code Z00.00, code_type ICD10, active, on anchored encounter."""
    try:
        p = pid()
        if not p:
            check("13. Fee Sheet Z00.00", 1, False, f"gate: {_pid_error}")
            return
        encs = anchored_encounters()
        if not encs:
            check("13. Fee Sheet Z00.00", 1, False, f"gate: {_enc_error}")
            return

        out = openemr_sql(
            f"SELECT code, code_type, encounter FROM billing "
            f"WHERE pid = {p} AND code = 'Z00.00' AND code_type = 'ICD10' "
            f"AND activity = 1 AND encounter IN ({','.join(encs)});"
        )
        ok = "Z00.00" in out
        check("13. Fee Sheet Z00.00", 1, ok,
              out.strip().splitlines()[0][:80] if ok
              else "no active ICD10 Z00.00 billing row on anchored encounter")
    except Exception as e:
        check("13. Fee Sheet Z00.00", 1, False, f"exception: {e}")


# ── OnlyOffice document retrieval (fs-first, API fallback) ───────────────────
OO_TITLE_PATTERNS = [
    "Welcome Letter - Maria Gonzalez%",
    "Welcome Letter%Maria Gonzalez%",
    "%Welcome%Gonzalez%",
]


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
    """Read content.docx for a file id from the portal data dir (docker exec).
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


_oo_doc: tuple[bytes | None, str] | None = None


def get_welcome_doc() -> tuple[bytes | None, str]:
    """(bytes|None, source detail). fs first, API fallback. Cached."""
    global _oo_doc
    if _oo_doc is not None:
        return _oo_doc
    try:
        found = _oo_find_file_id(OO_TITLE_PATTERNS)
    except Exception as e:
        _oo_doc = (None, f"files_file lookup failed: {e}")
        return _oo_doc
    if not found:
        _oo_doc = (None, "document not found in files_file")
        return _oo_doc
    file_id, title = found
    data = _oo_bytes_from_fs(file_id)
    if data:
        _oo_doc = (data, f"fs id={file_id} title={title[:60]}")
        return _oo_doc
    data = _oo_bytes_from_api(file_id)
    if data:
        _oo_doc = (data, f"api id={file_id} title={title[:60]}")
        return _oo_doc
    _oo_doc = (None, f"id={file_id} found but content unreadable (fs+api)")
    return _oo_doc


# ── docx parsing helpers ──────────────────────────────────────────────────────
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


def _docx_text(data: bytes) -> str:
    """Plain text of word/document.xml. <w:t> runs inside one paragraph are
    concatenated with NO separator; paragraphs joined with newlines."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paras = []
    for p in xml.split("</w:p>"):
        runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, flags=re.DOTALL)
        if runs:
            paras.append(_xml_unescape("".join(runs)))
    return "\n".join(paras)


def _docx_tables(data: bytes) -> list[list[str]]:
    """Per <w:tbl>: list of row texts (cells joined with ' | ')."""
    zf = zipfile.ZipFile(io.BytesIO(data))
    xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    tables = []
    for tbl in re.findall(r"<w:tbl(?:\s[^>]*)?>.*?</w:tbl>", xml, flags=re.DOTALL):
        rows = []
        for tr in re.findall(r"<w:tr(?:\s[^>]*)?>.*?</w:tr>", tbl, flags=re.DOTALL):
            cells = []
            for tc in tr.split("</w:tc>"):
                runs = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", tc, flags=re.DOTALL)
                if runs:
                    cells.append(_xml_unescape("".join(runs)))
            if cells:
                rows.append(" | ".join(cells))
        tables.append(rows)
    return tables


# ── Check 14 (gate): OnlyOffice document exists in DB ────────────────────────
def check_14_doc_exists() -> None:
    """Gate: 'Welcome Letter - Maria Gonzalez' row exists in files_file."""
    try:
        found = _oo_find_file_id(OO_TITLE_PATTERNS)
        check("14. OnlyOffice doc exists (gate)", 0, bool(found),
              f"id={found[0]} title={found[1][:60]}" if found
              else "not found in files_file")
    except Exception as e:
        check("14. OnlyOffice doc exists (gate)", 0, False, f"exception: {e}")


# ── Check 15a: welcome letter table structure ────────────────────────────────
def check_15a_doc_structure() -> None:
    """Health Profile Summary table: one <w:tbl> contains BP 138/88, a 28.x BMI,
    and both issue names."""
    try:
        data, src = get_welcome_doc()
        if not data:
            check("15a. Welcome letter table", 1, False, f"gate: {src}")
            return

        tables = _docx_tables(data)
        ok = False
        for rows in tables:
            txt = _norm_text(" | ".join(rows))
            if ("138/88" in txt and re.search(r"28\.\d", txt)
                    and "essential hypertension" in txt
                    and "type 2 diabetes" in txt):
                ok = True
                break

        check("15a. Welcome letter table", 1, ok,
              f"table with 138/88 + BMI 28.x + both issues ({src})" if ok
              else f"{len(tables)} table(s), none with 138/88 + 28.x BMI + both issues ({src})")
    except Exception as e:
        check("15a. Welcome letter table", 1, False, f"exception: {e}")


# ── Check 15b: welcome letter content ────────────────────────────────────────
def check_15b_doc_content() -> None:
    """Welcome letter body: greeting, clinic, provider, goal sentence, next
    steps, resources, closing."""
    try:
        data, src = get_welcome_doc()
        if not data:
            check("15b. Welcome letter content", 2, False, f"gate: {src}")
            return

        text = _norm_text(_docx_text(data))
        probes = [
            ("greeting", "dear maria gonzalez"),
            ("clinic", "springfield community health clinic"),
            ("provider", "dickinson"),
            ("goal", "achieve bp below 130/80 and hba1c below 7.0 within 6 months"),
            ("dash_diet", "dash diet"),
            ("step_portal", "portal registration within 7 days"),
            ("step_labs", "cbc, cmp, hba1c, lipid panel"),
            ("step_followup", "follow-up appointment in 4 weeks"),
            ("phone", "413-555-0100"),
            ("portal_url", "portal.springfieldclinic.example.com"),
            ("after_hours", "413-555-0911"),
            ("closing", "we look forward to partnering with you on your health "
                        "journey and supporting your path to better wellness"),
        ]
        missing = [lbl for lbl, probe in probes if probe not in text]

        check("15b. Welcome letter content", 2, not missing,
              f"all {len(probes)} content anchors present ({src})"
              if not missing else f"missing: {missing} ({src})"[:160])
    except Exception as e:
        check("15b. Welcome letter content", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_form_exists()
    check_2_form_settings()
    check_3a_field_types()
    check_3b_field_specs()
    check_3c_conditional_logic()
    check_4_demographics()
    check_5_contact()
    check_6_provider()
    check_7_history()
    check_8_hypertension()
    check_9_diabetes()
    check_10_vitals()
    check_11_care_plan()
    check_12_clinical_instructions()
    check_13_fee_sheet()
    check_14_doc_exists()
    check_15a_doc_structure()
    check_15b_doc_content()

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
