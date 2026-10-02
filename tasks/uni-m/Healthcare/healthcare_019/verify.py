"""
Verifier for Healthcare-019-I3: Colonoscopy Informed Consent Workflow for Three Patients

Checks: 19 weighted checks across opnform, openemr, onlyoffice.
Strategy: docker exec (DB queries) for all three apps; xlsx content parsed
from the OnlyOffice data dir (fs-first, API fallback).

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
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

# probed seed max, mw-openemr:latest 2026-08: form_encounter MAX(date)='2026-03-05 00:00:00'
SEED_ENCOUNTER_MAX_DATE = "2026-03-05"

FORM_TITLE = "Colonoscopy Informed Consent Form 2026"
WEBHOOK_URL = "https://hooks.clinicops.example.com/opnform/colonoscopy-consent"
TRACKER_TITLE = "Colonoscopy Consent and Procedure Tracker 2026"
SHEET1_NAME = "Consent Documentation Log"
SHEET2_NAME = "GI Procedure Schedule"

# key phrases from the required Clinical Instructions text (description.md)
INSTRUCTION_PHRASES = [
    "clear liquid diet 24 hours",
    "bowel prep solution",
    "npo 4 hours",
    "blood thinners 5 days",
    "cannot drive for 24 hours",
]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


def _one_line(s: str, limit: int = 120) -> str:
    return " ".join(str(s).split())[:limit]


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def opnform_sql_raw(query: str, sep: str | None = None) -> str:
    """Query OpnForm Postgres directly via psql. Raises on non-zero rc."""
    sep_arg = f" -F '{sep}'" if sep else ""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "bash", "-c",
        f"PGPASSWORD=forge psql -U forge -d forge -t -A{sep_arg} -c \"{query}\"",
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def openemr_sql(query: str) -> str:
    """Query OpenEMR MariaDB. Raises on non-zero rc."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Query OnlyOffice MySQL. Raises on non-zero rc."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql",
        "-u", "onlyoffice_user", "-ponlyoffice_pass", "-D", "onlyoffice",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql failed (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


def _strip_tags(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html or "")


# ── OpnForm form/properties resolution ────────────────────────────────────────
def _load_form() -> tuple[str, list | None, str]:
    """Returns (form_id, properties list | None, detail)."""
    form_id = opnform_sql_raw(
        f"SELECT id FROM forms WHERE title='{FORM_TITLE}' AND deleted_at IS NULL "
        "ORDER BY id DESC LIMIT 1"
    )
    if not form_id:
        return "", None, "form not found"
    # properties may contain newlines: SELECT it alone, in its own query
    raw = opnform_sql_raw(f"SELECT properties FROM forms WHERE id={form_id}")
    props = json.loads(raw)
    if not isinstance(props, list):
        return form_id, None, "properties is not a JSON array"
    return form_id, props, f"form_id={form_id} fields={len(props)}"


def _find_field(props: list, name: str) -> dict | None:
    target = _norm_text(name)
    for f in props:
        if isinstance(f, dict) and _norm_text(str(f.get("name", ""))) == target:
            return f
    return None


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


# ── OpnForm Checks ───────────────────────────────────────────────────────────
def check_1_opnform_form_settings():
    """Form exists with correct title, visibility, theme, size, color."""
    try:
        row = opnform_sql_raw(
            "SELECT title, visibility, theme, size, color FROM forms "
            f"WHERE title='{FORM_TITLE}' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not row:
            check("1. OpnForm form exists with correct settings", 2, False, "form not found")
            return
        parts = row.split("|")
        visibility = parts[1] if len(parts) > 1 else ""
        theme = parts[2] if len(parts) > 2 else ""
        size = parts[3] if len(parts) > 3 else ""
        color = parts[4] if len(parts) > 4 else ""
        issues = []
        if visibility != "public":
            issues.append(f"visibility={visibility}")
        if theme != "minimal":
            issues.append(f"theme={theme}")
        if size != "lg":
            issues.append(f"size={size}")
        if color.lower() != "#16a34a":
            issues.append(f"color={color}")
        check("1. OpnForm form exists with correct settings", 2, not issues,
              _one_line("; ".join(issues)))
    except Exception as e:
        check("1. OpnForm form exists with correct settings", 2, False, f"exception: {_one_line(e)}")


def check_2_opnform_form_options():
    """Form has re_fillable, correct button text, search indexing disabled."""
    try:
        row = opnform_sql_raw(
            "SELECT re_fillable, re_fill_button_text, can_be_indexed FROM forms "
            f"WHERE title='{FORM_TITLE}' AND deleted_at IS NULL ORDER BY id DESC LIMIT 1"
        )
        if not row:
            check("2. OpnForm form options (re-fillable, indexing)", 1, False, "form not found")
            return
        parts = row.split("|")
        re_fillable = parts[0] if len(parts) > 0 else ""
        re_fill_text = parts[1] if len(parts) > 1 else ""
        can_be_indexed = parts[2] if len(parts) > 2 else ""
        issues = []
        # re_fillable: postgres boolean is 't'/'f' or '1'/'0'
        if re_fillable not in ("t", "1", "true"):
            issues.append(f"re_fillable={re_fillable}")
        if re_fill_text != "Complete Another Consent":
            issues.append(f"re_fill_button_text={re_fill_text}")
        if can_be_indexed not in ("f", "0", "false"):
            issues.append(f"can_be_indexed={can_be_indexed}")
        check("2. OpnForm form options (re-fillable, indexing)", 1, not issues,
              _one_line("; ".join(issues)))
    except Exception as e:
        check("2. OpnForm form options (re-fillable, indexing)", 1, False, f"exception: {_one_line(e)}")


# (name, expected type, required must be true)
_FIELD_SPECS = [
    ("Patient Full Name", "text", True),
    ("Date of Birth", "date", True),
    ("I understand the procedure described above", "checkbox", True),
    ("Acknowledged Risks", "multi_select", True),
    ("Alternative Treatment Discussed", "select", True),
    ("Alternative Treatment Details", "text", False),
    ("I consent to the procedure", "checkbox", True),
    ("Consent Date", "date", True),
    ("Witness Name", "text", True),
]

_RISK_OPTIONS = [
    "Bowel Perforation",
    "Post-Polypectomy Bleeding",
    "Sedation Reaction",
    "Cardiopulmonary Complications",
]

_NF_TEXT_PHRASES = [
    "endoscopic examination",
    "bowel perforation",
    "early detection of colorectal cancer",
]


def check_3a_field_structure(props: list | None, props_detail: str):
    """Every input field's name/type/required per description; Acknowledged Risks
    options superset + min_selection==4; 3 nf-text contents; nf-divider."""
    label = "3a. OpnForm field structure (types, required, options, text blocks)"
    try:
        if props is None:
            check(label, 2, False, f"form properties unavailable: {props_detail}")
            return
        issues = []
        for name, ftype, req in _FIELD_SPECS:
            f = _find_field(props, name)
            if f is None:
                issues.append(f"missing field '{name}'")
                continue
            if str(f.get("type", "")) != ftype:
                issues.append(f"'{name}' type={f.get('type')} (want {ftype})")
            if req and not _truthy(f.get("required")):
                issues.append(f"'{name}' not required")
        # signature field (name unspecified in description): assert by type
        if not any(isinstance(f, dict) and f.get("type") == "signature" for f in props):
            issues.append("no signature field")
        # Acknowledged Risks: option superset + min_selection==4
        ms = _find_field(props, "Acknowledged Risks")
        if ms is not None:
            ms_cfg = ms.get("multi_select") if isinstance(ms.get("multi_select"), dict) else {}
            opt_names = {_norm_text(str(o.get("name", ""))) for o in ms_cfg.get("options", [])
                         if isinstance(o, dict)}
            missing = [o for o in _RISK_OPTIONS if _norm_text(o) not in opt_names]
            if missing:
                issues.append(f"risk options missing: {missing}")
            min_sel = ms.get("min_selection")
            ok_min = False
            try:
                ok_min = int(str(min_sel)) == 4
            except (TypeError, ValueError):
                ok_min = False
            if not ok_min:
                issues.append(f"min_selection={min_sel!r} (want 4)")
        # 3 nf-text blocks with the signature phrases (tag-stripped, case-insensitive)
        nf_texts = [_norm_text(_strip_tags(str(f.get("content", ""))))
                    for f in props if isinstance(f, dict) and f.get("type") == "nf-text"]
        if len(nf_texts) < 3:
            issues.append(f"nf-text blocks: {len(nf_texts)}/3")
        for phrase in _NF_TEXT_PHRASES:
            if not any(phrase in t for t in nf_texts):
                issues.append(f"no nf-text contains '{phrase}'")
        # divider
        if not any(isinstance(f, dict) and f.get("type") == "nf-divider" for f in props):
            issues.append("no nf-divider")
        check(label, 2, not issues, _one_line("; ".join(issues)))
    except Exception as e:
        check(label, 2, False, f"exception: {_one_line(e)}")


def check_3b_field_behaviors(props: list | None, props_detail: str):
    """Consent checkbox toggle switch; Consent Date prefill/no-future; page-break text."""
    label = "3b. OpnForm field behaviors (toggle, date prefill, page-break text)"
    try:
        if props is None:
            check(label, 1, False, f"form properties unavailable: {props_detail}")
            return
        issues = []
        consent = _find_field(props, "I consent to the procedure")
        if consent is None:
            issues.append("'I consent to the procedure' missing")
        elif not _truthy(consent.get("use_toggle_switch")):
            issues.append(f"use_toggle_switch={consent.get('use_toggle_switch')!r}")
        cdate = _find_field(props, "Consent Date")
        if cdate is None:
            issues.append("'Consent Date' missing")
        else:
            if not _truthy(cdate.get("prefill_today")):
                issues.append(f"prefill_today={cdate.get('prefill_today')!r}")
            if not _truthy(cdate.get("disable_future_dates")):
                issues.append(f"disable_future_dates={cdate.get('disable_future_dates')!r}")
        breaks = [f for f in props if isinstance(f, dict) and f.get("type") == "nf-page-break"]
        if not breaks:
            issues.append("no nf-page-break")
        elif not any(str(b.get("next_btn_text", "")) == "Proceed to Signature" for b in breaks):
            issues.append(f"next_btn_text={breaks[0].get('next_btn_text')!r} (want 'Proceed to Signature')")
        check(label, 1, not issues, _one_line("; ".join(issues)))
    except Exception as e:
        check(label, 1, False, f"exception: {_one_line(e)}")


def check_3c_conditional_logic(props: list | None, props_detail: str):
    """'Alternative Treatment Details' shown only when 'Yes - chose alternative'."""
    label = "3c. OpnForm conditional logic (Alternative Treatment Details)"
    try:
        if props is None:
            check(label, 1, False, f"form properties unavailable: {props_detail}")
            return
        details_f = _find_field(props, "Alternative Treatment Details")
        trigger_f = _find_field(props, "Alternative Treatment Discussed")
        if details_f is None or trigger_f is None:
            check(label, 1, False, "conditional field or trigger field missing")
            return
        logic = details_f.get("logic")
        if not isinstance(logic, dict) or not logic:
            check(label, 1, False, "field has no logic configured")
            return
        actions = logic.get("actions") or []
        has_show = "show-block" in actions
        trigger_id = str(trigger_f.get("id", ""))
        leaves = _logic_leaves(logic.get("conditions"))
        leaf_ok = any(
            _leaf_trigger_id(leaf) == trigger_id
            and str((leaf.get("value") or {}).get("value")) == "Yes - chose alternative"
            for leaf in leaves
        )
        issues = []
        if not has_show:
            issues.append(f"actions={actions!r} (want show-block)")
        if not leaf_ok:
            issues.append(f"no condition leaf on trigger id={trigger_id} == 'Yes - chose alternative' ({len(leaves)} leaves)")
        check(label, 1, not issues, _one_line("; ".join(issues)))
    except Exception as e:
        check(label, 1, False, f"exception: {_one_line(e)}")


def _json_string_values(node) -> list[str]:
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for v in node.values():
            out.extend(_json_string_values(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_json_string_values(v))
    return out


def check_4_opnform_webhook(form_id: str):
    """Active webhook integration on THIS form with the exact URL."""
    label = "4. OpnForm webhook integration"
    try:
        if not form_id:
            check(label, 1, False, "form not found")
            return
        rows = opnform_sql_raw(
            "SELECT integration_id, status, data FROM form_integrations "
            f"WHERE form_id={form_id}",
            sep="|||",
        )
        found = False
        seen = []
        for line in rows.splitlines():
            parts = line.split("|||")
            if len(parts) < 3:
                continue
            integration_id, status, data = parts[0].strip(), parts[1].strip(), parts[2]
            seen.append(f"{integration_id}/{status}")
            if integration_id != "webhook" or status != "active":
                continue
            try:
                values = _json_string_values(json.loads(data))
                url_ok = WEBHOOK_URL in values
            except (ValueError, TypeError):
                url_ok = WEBHOOK_URL in data
            if url_ok:
                found = True
                break
        check(label, 1, found,
              "" if found else _one_line(f"no active webhook with exact URL; integrations={seen}"))
    except Exception as e:
        check(label, 1, False, f"exception: {_one_line(e)}")


# ── OpenEMR (pid, encounter) resolution ──────────────────────────────────────
def resolve_patient_group(fname: str, lname: str, pa_number: str) -> tuple[str, str, str]:
    """Resolve the canonical (pid, encounter) for a patient group.

    Candidates = every new encounter (date > seed max) across ALL pids sharing
    the name (Connie McLaughlin has duplicate pids 168/346). Prefer the
    candidate whose form_misc_billing_options row carries this patient's PA
    number; otherwise the most recent new encounter.
    Returns (pid, encounter, detail); pid=='' means unresolved.
    """
    try:
        out = openemr_sql(
            f"SELECT pid FROM patient_data WHERE fname='{fname}' AND lname='{lname}'"
        )
        pids = [p.strip() for p in out.splitlines() if p.strip() and p.strip() != "NULL"]
        if not pids:
            return "", "", "patient not found"
        candidates: list[tuple[str, str, str]] = []  # (date, pid, encounter)
        for pid in pids:
            rows = openemr_sql(
                f"SELECT encounter, date FROM form_encounter "
                f"WHERE pid={pid} AND date > '{SEED_ENCOUNTER_MAX_DATE}'"
            )
            for line in rows.splitlines():
                parts = line.split("\t")
                enc = parts[0].strip() if parts else ""
                dt = parts[1].strip() if len(parts) > 1 else ""
                if enc and enc != "NULL":
                    candidates.append((dt, pid, enc))
        if not candidates:
            return "", "", f"no encounter after {SEED_ENCOUNTER_MAX_DATE} for pid(s) {','.join(pids)}"
        candidates.sort(reverse=True)
        for _, pid, enc in candidates:
            cnt = openemr_sql(
                f"SELECT COUNT(*) FROM form_misc_billing_options "
                f"WHERE pid={pid} AND encounter={enc} AND prior_auth_number='{pa_number}'"
            )
            if cnt and cnt != "0":
                return pid, enc, f"pid={pid} enc={enc} (PA-matched)"
        _, pid, enc = candidates[0]
        return pid, enc, f"pid={pid} enc={enc} (latest new encounter, no PA match)"
    except Exception as e:
        return "", "", f"exception: {_one_line(e)}"


def _check_prior_auth(label: str, weight: int, grp: tuple[str, str, str], pa: str):
    pid, enc, gdetail = grp
    try:
        if not pid:
            check(label, weight, False, f"group unresolved: {gdetail}")
            return
        rows = openemr_sql(
            f"SELECT prior_auth_number FROM form_misc_billing_options "
            f"WHERE pid={pid} AND encounter={enc}"
        )
        vals = [l.strip() for l in rows.splitlines() if l.strip()]
        passed = pa in vals
        check(label, weight, passed,
              f"pid={pid} enc={enc}" if passed else _one_line(f"pid={pid} enc={enc} prior_auth rows={vals!r}"))
    except Exception as e:
        check(label, weight, False, f"exception: {_one_line(e)}")


def _check_clinical_instructions(label: str, weight: int, grp: tuple[str, str, str]):
    """Clinical Instructions on the anchored encounter (via forms registry),
    containing >=4/5 required key phrases."""
    pid, enc, gdetail = grp
    try:
        if not pid:
            check(label, weight, False, f"group unresolved: {gdetail}")
            return
        rows = openemr_sql(
            f"SELECT ci.instruction FROM form_clinical_instructions ci "
            f"JOIN forms f ON f.form_id = ci.id AND f.formdir='clinical_instructions' "
            f"AND f.deleted=0 WHERE f.pid={pid} AND f.encounter={enc}"
        )
        if not rows:
            check(label, weight, False, f"pid={pid} enc={enc}: no clinical instructions form on encounter")
            return
        text = _norm_text(rows)
        hits = [p for p in INSTRUCTION_PHRASES if p in text]
        passed = len(hits) >= 4
        missing = [p for p in INSTRUCTION_PHRASES if p not in text]
        check(label, weight, passed,
              f"phrases {len(hits)}/5" if passed else _one_line(f"phrases {len(hits)}/5, missing: {missing}"))
    except Exception as e:
        check(label, weight, False, f"exception: {_one_line(e)}")


def _check_cpt(label: str, weight: int, grp: tuple[str, str, str]):
    """billing row on the anchored encounter: code 45378, code_type CPT4, active."""
    pid, enc, gdetail = grp
    try:
        if not pid:
            check(label, weight, False, f"group unresolved: {gdetail}")
            return
        cnt = openemr_sql(
            f"SELECT COUNT(*) FROM billing WHERE pid={pid} AND encounter={enc} "
            f"AND code='45378' AND code_type='CPT4' AND activity=1"
        )
        passed = bool(cnt) and cnt != "0"
        check(label, weight, passed,
              f"pid={pid} enc={enc}" if passed else f"pid={pid} enc={enc}: no active CPT4 45378 on encounter")
    except Exception as e:
        check(label, weight, False, f"exception: {_one_line(e)}")


# ── OpenEMR Checks — Hettie Torphy ───────────────────────────────────────────
def check_7_hettie_medical_problem(grp: tuple[str, str, str]):
    """Single active problem row: title Rectal bleeding AND diagnosis K62.5 (same row)."""
    label = "7. Hettie Torphy medical problem K62.5"
    pid, _, gdetail = grp
    try:
        if not pid:
            check(label, 2, False, f"group unresolved: {gdetail}")
            return
        cnt = openemr_sql(
            f"SELECT COUNT(*) FROM lists WHERE pid={pid} AND type='medical_problem' "
            f"AND title LIKE '%Rectal bleeding%' AND diagnosis LIKE '%K62.5%' AND activity=1"
        )
        passed = bool(cnt) and cnt != "0"
        check(label, 2, passed,
              f"pid={pid}" if passed else f"pid={pid}: no active same-row title+K62.5 problem")
    except Exception as e:
        check(label, 2, False, f"exception: {_one_line(e)}")


def check_8_hettie_fee_sheet(grp: tuple[str, str, str]):
    """Fee Sheet on the anchored encounter: ICD10 K62.5 AND CPT4 45378, active."""
    label = "8. Hettie Torphy fee sheet (K62.5 + CPT 45378)"
    pid, enc, gdetail = grp
    try:
        if not pid:
            check(label, 2, False, f"group unresolved: {gdetail}")
            return
        rows = openemr_sql(
            f"SELECT code, code_type FROM billing WHERE pid={pid} AND encounter={enc} "
            f"AND activity=1 AND code IN ('45378','K62.5')"
        )
        pairs = set()
        for line in rows.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                pairs.add((parts[0].strip(), parts[1].strip()))
        issues = []
        if ("K62.5", "ICD10") not in pairs:
            issues.append("missing ICD10 K62.5")
        if ("45378", "CPT4") not in pairs:
            issues.append("missing CPT4 45378")
        check(label, 2, not issues,
              f"pid={pid} enc={enc}" if not issues else _one_line(f"pid={pid} enc={enc}: {'; '.join(issues)}; got={sorted(pairs)}"))
    except Exception as e:
        check(label, 2, False, f"exception: {_one_line(e)}")


# ── OpenEMR Checks — Connie McLaughlin ────────────────────────────────────────
def check_15_connie_onset_date(grp: tuple[str, str, str]):
    """onset_date 2026-04-08 on the SAME misc-billing row carrying PA-2026-GI-3."""
    label = "15. Connie McLaughlin onset date 2026-04-08"
    pid, enc, gdetail = grp
    try:
        if not pid:
            check(label, 2, False, f"group unresolved: {gdetail}")
            return
        rows = openemr_sql(
            f"SELECT onset_date FROM form_misc_billing_options "
            f"WHERE pid={pid} AND encounter={enc} AND prior_auth_number='PA-2026-GI-3'"
        )
        vals = [l.strip() for l in rows.splitlines() if l.strip()]
        passed = any(v == "2026-04-08" or v.startswith("2026-04-08 ") for v in vals)
        check(label, 2, passed,
              f"pid={pid} enc={enc}" if passed else _one_line(f"pid={pid} enc={enc} onset_date on PA row={vals!r}"))
    except Exception as e:
        check(label, 2, False, f"exception: {_one_line(e)}")


# ── OnlyOffice document retrieval + xlsx parsing ─────────────────────────────
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
    import requests

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
    try:
        s = _oo_auth_session()
    except Exception:
        return None
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
        return None, f"files_file lookup failed: {_one_line(e)}"
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


# ── OnlyOffice Checks ─────────────────────────────────────────────────────────
def check_16_onlyoffice_sheets(wb_data: bytes | None, wb_src: str):
    """Workbook exists with exactly the two required sheet names."""
    label = "16. OnlyOffice workbook sheets (Consent Documentation Log + GI Procedure Schedule)"
    try:
        if not wb_data:
            check(label, 1, False, f"workbook unavailable: {wb_src}")
            return
        zf = zipfile.ZipFile(io.BytesIO(wb_data))
        names = {_norm_text(n) for n in _xlsx_sheets(zf)}
        want = {_norm_text(SHEET1_NAME), _norm_text(SHEET2_NAME)}
        passed = names == want
        check(label, 1, passed,
              wb_src if passed else _one_line(f"sheets={sorted(names)} (want {sorted(want)}); {wb_src}"))
    except Exception as e:
        check(label, 1, False, f"exception: {_one_line(e)}; {wb_src}")


_SHEET1_PATIENT_PA = [
    ("Hettie Torphy", "PA-2026-GI-1"),
    ("Zack Nikolaus", "PA-2026-GI-2"),
    ("Connie McLaughlin", "PA-2026-GI-3"),
]

_SHEET2_NOTES = [
    ("Hettie Torphy", "New problem Rectal bleeding, unspecified added"),
    ("Connie McLaughlin", "Onset date 2026-04-08 set"),
]


def check_17_onlyoffice_content(wb_data: bytes | None, wb_src: str):
    """Per-row workbook content: Sheet1 patient/PA/CPT rows; Sheet2 procedure + notes."""
    label = "17. OnlyOffice workbook content (per-row tracker data)"
    try:
        if not wb_data:
            check(label, 2, False, f"workbook unavailable: {wb_src}")
            return
        zf = zipfile.ZipFile(io.BytesIO(wb_data))
        sheets = _xlsx_sheets(zf)
        by_norm = {_norm_text(n): p for n, p in sheets.items()}
        issues = []
        shared = _xlsx_shared_strings(zf)

        def _sheet_rows(name: str) -> list[str] | None:
            path = by_norm.get(_norm_text(name))
            if not path:
                return None
            return [_norm_text(" | ".join(r)) for r in _xlsx_rows(zf, path, shared) if any(r)]

        rows1 = _sheet_rows(SHEET1_NAME)
        if rows1 is None:
            issues.append(f"sheet '{SHEET1_NAME}' missing")
        elif not rows1:
            issues.append(f"sheet '{SHEET1_NAME}' empty")
        else:
            for pname, pa in _SHEET1_PATIENT_PA:
                if not any(_norm_text(pname) in r and _norm_text(pa) in r for r in rows1):
                    issues.append(f"no Sheet1 row with '{pname}' + '{pa}'")
            if not any("45378" in r for r in rows1):
                issues.append("no Sheet1 row contains '45378'")

        rows2 = _sheet_rows(SHEET2_NAME)
        if rows2 is None:
            issues.append(f"sheet '{SHEET2_NAME}' missing")
        elif not rows2:
            issues.append(f"sheet '{SHEET2_NAME}' empty")
        else:
            if not any(_norm_text("Diagnostic Colonoscopy with Possible Polypectomy") in r
                       for r in rows2):
                issues.append("procedure name not found in Sheet2")
            for pname, note in _SHEET2_NOTES:
                if not any(_norm_text(pname) in r and _norm_text(note) in r for r in rows2):
                    issues.append(f"no Sheet2 row with '{pname}' + note")
        check(label, 2, not issues,
              wb_src if not issues else _one_line("; ".join(issues)))
    except Exception as e:
        check(label, 2, False, f"exception: {_one_line(e)}; {wb_src}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # OpnForm
    check_1_opnform_form_settings()
    check_2_opnform_form_options()
    try:
        form_id, props, props_detail = _load_form()
    except Exception as e:
        form_id, props, props_detail = "", None, f"exception: {_one_line(e)}"
    check_3a_field_structure(props, props_detail)
    check_3b_field_behaviors(props, props_detail)
    check_3c_conditional_logic(props, props_detail)
    check_4_opnform_webhook(form_id)

    # OpenEMR: resolve canonical (pid, encounter) per patient group
    hettie = resolve_patient_group("Hettie", "Torphy", "PA-2026-GI-1")
    zack = resolve_patient_group("Zack", "Nikolaus", "PA-2026-GI-2")
    connie = resolve_patient_group("Connie", "McLaughlin", "PA-2026-GI-3")

    _check_prior_auth("5. Hettie Torphy prior auth PA-2026-GI-1", 1, hettie, "PA-2026-GI-1")
    _check_clinical_instructions("6. Hettie Torphy clinical instructions", 1, hettie)
    check_7_hettie_medical_problem(hettie)
    check_8_hettie_fee_sheet(hettie)
    _check_prior_auth("9. Zack Nikolaus prior auth PA-2026-GI-2", 1, zack, "PA-2026-GI-2")
    _check_clinical_instructions("10. Zack Nikolaus clinical instructions", 1, zack)
    _check_cpt("11. Zack Nikolaus CPT 45378", 1, zack)
    _check_prior_auth("12. Connie McLaughlin prior auth PA-2026-GI-3", 1, connie, "PA-2026-GI-3")
    _check_clinical_instructions("13. Connie McLaughlin clinical instructions", 1, connie)
    _check_cpt("14. Connie McLaughlin CPT 45378", 1, connie)
    check_15_connie_onset_date(connie)

    # OnlyOffice
    wb_data, wb_src = _oo_get_document(
        [f"{TRACKER_TITLE}%", f"%{TRACKER_TITLE}%"]
    )
    check_16_onlyoffice_sheets(wb_data, wb_src)
    check_17_onlyoffice_content(wb_data, wb_src)

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
