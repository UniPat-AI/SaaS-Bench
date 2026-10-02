"""
Verifier for Healthcare-048-I1: Immunization History Audit & Catch-Up Schedule

Checks: 12 checks (9a is a 0pt precondition gate) across openemr, opnform, onlyoffice.
Strategy: docker exec (MariaDB for OpenEMR, Postgres for OpnForm, MySQL for OnlyOffice)
plus fs-first xlsx retrieval from the OnlyOffice data dir.

Required env vars:
  SERVER_HOSTNAME,
  OPENEMR_PORT, OPENEMR_CONTAINER, OPENEMR_DB_CONTAINER,
  OPNFORM_PORT, OPNFORM_CONTAINER,
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
from datetime import date, timedelta

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

OPENEMR_PORT = os.getenv("OPENEMR_PORT")
OPENEMR_CONTAINER = os.getenv("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.getenv("OPENEMR_DB_CONTAINER")

OPNFORM_PORT = os.getenv("OPNFORM_PORT")
OPNFORM_CONTAINER = os.getenv("OPNFORM_CONTAINER")

ONLYOFFICE_PORT = os.getenv("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.getenv("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.getenv("ONLYOFFICE_DB_CONTAINER")

_required = {
    "OPENEMR_PORT": OPENEMR_PORT,
    "OPENEMR_CONTAINER": OPENEMR_CONTAINER,
    "OPENEMR_DB_CONTAINER": OPENEMR_DB_CONTAINER,
    "OPNFORM_PORT": OPNFORM_PORT,
    "OPNFORM_CONTAINER": OPNFORM_CONTAINER,
    "ONLYOFFICE_PORT": ONLYOFFICE_PORT,
    "ONLYOFFICE_CONTAINER": ONLYOFFICE_CONTAINER,
    "ONLYOFFICE_DB_CONTAINER": ONLYOFFICE_DB_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
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


def openemr_sql(query: str) -> str:
    """Run a MariaDB query against OpenEMR and return stdout. Raises on error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "--default-character-set=utf8mb4",
        "-u", "openemr", "-popenemr_pass", "-D", "openemr",
        "-N", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def opnform_sql(query: str) -> str:
    """Run a Postgres query against OpnForm (embedded in app container). Raises on error."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Run a MySQL query against OnlyOffice. Raises on error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice", "-N", "-e", query,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def _norm_text(s: str) -> str:
    """Lowercase, dash-normalize (em/en dash -> '-'), collapse whitespace."""
    s = s.replace("—", "-").replace("–", "-").replace("’", "'")
    return " ".join(s.lower().split())


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


# ── OpenEMR appointment checks ───────────────────────────────────────────────

# ── Probed seed baseline (mw-openemr:latest, probed 2026-08) — REFERENCE ONLY.
# ALL reconciliation truths (immunizations / allergies / counts) are re-queried
# live at verify time; the agent may add rows.
#   immunizations: 50 rows total. Cyrstal Labadie (pids 207/314): 0 rows;
#   Numbers Mohr: only pid 187 has 1 row (CVX 140 Influenza, 2025-03-16);
#   Adrianne Simonis (pid 189): 0 rows  ->  all 5 expected vaccines Missing for
#   all three patients (Number Missing = 5/5/5 on the seed).
#   allergies (lists, type='allergy', activity=1): Cyrstal(207)=Tetracycline;
#   Numbers=none; Adrianne(189)='Allergic disposition' + 'Peanut' x2
#   (duplicate Peanut rows -> assert "contains Peanut", never row counts).
#   providers (users): Pouros=55, Reinger=56, Dickinson=57.
#   duplicate-name pids: Cyrstal Labadie {207 (DOB 1954-12-18), 314 (NULL dup)};
#   Numbers Mohr {187 (1974-03-10), 333 (NULL dup)}; Adrianne Simonis {189}.

PATIENTS = [
    {
        "name": "Cyrstal Labadie",
        "date": "2026-05-12",
        "time": "09:30:00",
        "provider_lname": "Pouros",
        # Full comment from description, compared normalized (lowercase, collapsed ws)
        "comment": "Immunization catch-up visit - review MMR, Tdap, IPV, Varicella, Hep B",
        "label": "Cyrstal Labadie",
    },
    {
        "name": "Numbers Mohr",
        "date": "2026-05-13",
        "time": "10:00:00",
        "provider_lname": "Dickinson",
        "comment": "Catch-up immunization appointment for adult vaccines",
        "label": "Numbers Mohr",
    },
    {
        "name": "Adrianne Simonis",
        "date": "2026-05-14",
        "time": "14:00:00",
        "provider_lname": "Reinger",
        "comment": "Catch-up vaccination visit - prioritize MMR and Tdap",
        "label": "Adrianne Simonis",
    },
]

# Canonical pid per patient = the pc_pid that owns the anchored appointment
# (dup-name disambiguation; shared by ALL later checks incl. xlsx reconciliation).
CANON_PID: dict[str, str] = {}


def _patient_pids(fname: str, lname: str) -> list[str]:
    """All pids for a (possibly duplicated) patient name."""
    raw = openemr_sql(
        f"SELECT pid FROM patient_data WHERE fname='{fname}' AND lname='{lname}'"
    )
    return [ln.strip() for ln in raw.splitlines() if ln.strip()]


def _check_appointment(idx: int, p: dict) -> None:
    """Verify an OpenEMR calendar appointment for a given patient."""
    label = f"{idx}. Appointment for {p['label']}"
    try:
        fname, lname = p["name"].split(" ", 1)
        pids = _patient_pids(fname, lname)
        if not pids:
            check(label, 1, False, f"patient '{p['name']}' not found in patient_data")
            return

        prov_raw = openemr_sql(
            f"SELECT id FROM users WHERE lname='{p['provider_lname']}' LIMIT 1"
        )
        if not prov_raw:
            check(label, 1, False, f"provider '{p['provider_lname']}' not found")
            return
        prov_id = prov_raw.splitlines()[0].strip()

        # Resolve 'Office Visit' category id dynamically (never hardcode)
        cat_raw = openemr_sql(
            "SELECT pc_catid FROM openemr_postcalendar_categories "
            "WHERE pc_catname='Office Visit' LIMIT 1"
        )
        cat_id = cat_raw.splitlines()[0].strip() if cat_raw else ""

        pids_sql = ",".join(f"'{x}'" for x in pids)
        appt_raw = openemr_sql(
            f"SELECT pc_pid, pc_startTime, pc_aid, pc_catid, pc_hometext "
            f"FROM openemr_postcalendar_events "
            f"WHERE pc_pid IN ({pids_sql}) AND pc_eventDate='{p['date']}' "
            f"ORDER BY pc_eid DESC LIMIT 1"
        )
        if not appt_raw:
            check(label, 1, False,
                  f"no appointment on {p['date']} for pids {pids}")
            return

        parts = appt_raw.split("\t", 4)
        appt_pid = parts[0].strip() if len(parts) > 0 else ""
        appt_time = parts[1].strip() if len(parts) > 1 else ""
        appt_aid = parts[2].strip() if len(parts) > 2 else ""
        appt_catid = parts[3].strip() if len(parts) > 3 else ""
        appt_comment = parts[4] if len(parts) > 4 else ""

        # The hit pc_pid is this patient's canonical pid for all later checks.
        CANON_PID[p["name"]] = appt_pid

        issues = []
        if not appt_time.startswith(p["time"][:5]):
            issues.append(f"time={appt_time} expected {p['time'][:5]}")
        if str(appt_aid) != str(prov_id):
            issues.append(f"provider_id={appt_aid} expected {prov_id}")
        if not cat_id:
            issues.append("category 'Office Visit' not found in categories table")
        elif str(appt_catid) != str(cat_id):
            issues.append(f"catid={appt_catid} expected {cat_id}")
        if _norm_text(p["comment"]) not in _norm_text(appt_comment):
            issues.append(f"comment missing full text '{p['comment'][:60]}'")

        check(label, 1, not issues,
              ", ".join(issues) if issues else f"all fields match (pid={appt_pid})")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_1_appointment_cyrstal():
    """Appointment for Cyrstal Labadie on 2026-05-12."""
    _check_appointment(1, PATIENTS[0])


def check_2_appointment_numbers():
    """Appointment for Numbers Mohr on 2026-05-13."""
    _check_appointment(2, PATIENTS[1])


def check_3_appointment_adrianne():
    """Appointment for Adrianne Simonis on 2026-05-14."""
    _check_appointment(3, PATIENTS[2])


# ── OpnForm checks ───────────────────────────────────────────────────────────

FORM_TITLE = "Immunization Catch-Up Consent Form 2026"

CONSENT_OPTIONS = {
    "consent to all listed vaccines",
    "consent to selected vaccines only",
    "decline all vaccines",
}
EXPECTED_VACCINE_OPTIONS = {"mmr", "tdap", "ipv", "varicella", "hepatitis b"}


def _get_form_row() -> dict | None:
    """Fetch the OpnForm form row as a dict."""
    raw = opnform_sql(
        f"SELECT row_to_json(f) FROM forms f WHERE title = '{FORM_TITLE}' LIMIT 1"
    )
    if not raw:
        return None
    try:
        return json.loads(raw.split("\n")[0])
    except (json.JSONDecodeError, IndexError):
        return None


def _form_props(row: dict) -> list:
    props = row.get("properties") or []
    if isinstance(props, str):
        props = json.loads(props)
    return props


def _fields_by_name(props: list, keywords: tuple[str, ...]) -> list[dict]:
    out = []
    for prop in props:
        name = _norm_text(str(prop.get("name") or prop.get("label") or ""))
        if name and all(k in name for k in keywords):
            out.append(prop)
    return out


def _typed_field(props: list, keyword_sets: list[tuple[str, ...]],
                 ftype: str) -> tuple[dict | None, str]:
    """First field matching any keyword set AND the exact type.
    Returns (field, '') or (None, reason)."""
    cands: list[dict] = []
    for kws in keyword_sets:
        for f in _fields_by_name(props, kws):
            if f not in cands:
                cands.append(f)
    for f in cands:
        if (f.get("type") or "").lower() == ftype:
            return f, ""
    if cands:
        return None, f"type={cands[0].get('type')} expected {ftype}"
    return None, "not found"


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


def _logic_ok(field: dict, trigger_id: str,
              value_substr: str | None = None) -> tuple[bool, str]:
    """Field must have non-empty logic whose conditions reference trigger_id and
    whose actions include 'show-block'. Empty/absent logic FAILS."""
    logic = field.get("logic")
    if not isinstance(logic, dict) or not logic:
        return False, "no logic"
    leaves = _logic_leaves(logic.get("conditions"))
    if not leaves:
        return False, "logic has no condition leaves"
    if "show-block" not in (logic.get("actions") or []):
        return False, "actions missing show-block"
    hits = [lf for lf in leaves if _leaf_trigger_id(lf) == str(trigger_id)]
    if not hits:
        return False, "condition does not reference trigger field"
    if value_substr is not None:
        for lf in hits:
            val = (lf.get("value") or {}).get("value")
            if value_substr.lower() in str(val).lower():
                return True, ""
        return False, f"condition value lacks '{value_substr[:40]}'"
    return True, ""


def check_4_form_exists():
    """OpnForm form with correct title exists."""
    try:
        row = _get_form_row()
        check("4. OpnForm form exists", 1, row is not None,
              f"title='{FORM_TITLE}'" if row else "form not found")
    except Exception as e:
        check("4. OpnForm form exists", 1, False, f"exception: {e}")


def check_5_form_settings():
    """OpnForm form settings: color, theme, size, progress bar, submit text, visibility."""
    try:
        row = _get_form_row()
        if not row:
            check("5. OpnForm form settings", 2, False, "form not found")
            return

        issues = []
        if row.get("color", "").upper() != "#2563EB":
            issues.append(f"color={row.get('color')}")
        if row.get("theme") != "default":
            issues.append(f"theme={row.get('theme')}")
        if row.get("size") != "lg":
            issues.append(f"size={row.get('size')}")
        if not row.get("show_progress_bar"):
            issues.append("progress bar not enabled")
        if (row.get("submit_button_text") or "").strip() != "Submit Consent Form":
            issues.append(f"submit_text='{row.get('submit_button_text')}'")
        if row.get("visibility") != "public":
            issues.append(f"visibility={row.get('visibility')}")

        check("5. OpnForm form settings", 2, not issues,
              ", ".join(issues) if issues else "all settings correct")
    except Exception as e:
        check("5. OpnForm form settings", 2, False, f"exception: {e}")


def check_6_form_core_fields():
    """OpnForm required core fields exist with correct types (+ Consent options)."""
    try:
        row = _get_form_row()
        if not row:
            check("6. OpnForm core fields", 2, False, "form not found")
            return
        props = _form_props(row)

        issues = []
        specs = [
            ("Patient Name", [("patient", "name")], "text"),
            ("Date of Birth", [("date", "birth"), ("dob",)], "date"),
            ("Parent/Guardian Name", [("parent",), ("guardian",)], "text"),
            ("Vaccines Due", [("vaccine", "due")], "multi_select"),
            ("Consent Decision", [("consent", "decision")], "select"),
            ("Egg Allergy checkbox", [("egg",)], "checkbox"),
            ("Adverse Reaction checkbox", [("adverse",), ("reaction",)], "checkbox"),
        ]
        found: dict[str, dict] = {}
        for label, kwsets, ftype in specs:
            f, reason = _typed_field(props, kwsets, ftype)
            if not f:
                issues.append(f"{label}: {reason}")
            else:
                found[label] = f

        consent = found.get("Consent Decision")
        if consent:
            opts = _option_names(_prop_options(consent))
            missing = CONSENT_OPTIONS - opts
            if missing:
                issues.append(f"Consent Decision options missing {sorted(missing)}")

        check("6. OpnForm core fields", 2, not issues,
              "; ".join(issues)[:240] if issues
              else f"all 7 typed fields + consent options ok in {len(props)} properties")
    except Exception as e:
        check("6. OpnForm core fields", 2, False, f"exception: {e}")


def check_7_form_conditional_and_signature():
    """OpnForm conditional logics (x3), page break text, signature, and field attrs."""
    try:
        row = _get_form_row()
        if not row:
            check("7. OpnForm conditional/signature", 2, False, "form not found")
            return
        props = _form_props(row)
        issues = []

        egg_cb, _ = _typed_field(props, [("egg",)], "checkbox")
        adverse_cb, _ = _typed_field(props, [("adverse",), ("reaction",)], "checkbox")
        consent, _ = _typed_field(props, [("consent", "decision")], "select")

        # 1) Egg Allergy Severity shown only when egg checkbox is checked
        sev = (_fields_by_name(props, ("egg", "severity"))
               or _fields_by_name(props, ("severity",)))
        if not sev:
            issues.append("Egg Allergy Severity field missing")
        elif not egg_cb:
            issues.append("severity logic unverifiable: egg checkbox missing")
        else:
            ok, why = _logic_ok(sev[0], str(egg_cb.get("id")))
            if not ok:
                issues.append(f"severity logic: {why}")

        # 2) Previous Reaction Details shown only when adverse checkbox is checked
        det = (_fields_by_name(props, ("reaction", "details"))
               or _fields_by_name(props, ("previous", "reaction")))
        if not det:
            issues.append("Previous Reaction Details field missing")
        elif not adverse_cb:
            issues.append("details logic unverifiable: adverse checkbox missing")
        else:
            ok, why = _logic_ok(det[0], str(adverse_cb.get("id")))
            if not ok:
                issues.append(f"details logic: {why}")

        # 3) Declined Vaccines shown only when Consent Decision =
        #    'Consent to Selected Vaccines Only'; options superset of 5 vaccines
        declined = _fields_by_name(props, ("declined",))
        if not declined:
            issues.append("Declined Vaccines field missing")
        else:
            dfield = declined[0]
            if (dfield.get("type") or "").lower() != "multi_select":
                issues.append(f"Declined Vaccines type={dfield.get('type')} expected multi_select")
            else:
                dopts = _option_names(_prop_options(dfield))
                dmiss = EXPECTED_VACCINE_OPTIONS - dopts
                if dmiss:
                    issues.append(f"Declined Vaccines options missing {sorted(dmiss)}")
            if not consent:
                issues.append("declined logic unverifiable: Consent Decision select missing")
            else:
                ok, why = _logic_ok(dfield, str(consent.get("id")),
                                    "Consent to Selected Vaccines Only")
                if not ok:
                    issues.append(f"declined logic: {why}")

        # 4) Page break with next button text 'Sign Consent'
        breaks = [f for f in props if (f.get("type") or "").lower() == "nf-page-break"]
        if not breaks:
            issues.append("no nf-page-break found")
        elif not any(_norm_text(str(f.get("next_btn_text") or "")) == "sign consent"
                     for f in breaks):
            issues.append("no page break with next_btn_text 'Sign Consent'")

        # 5) Signature field
        if not any("signature" in (f.get("type") or "").lower()
                   or "signature" in _norm_text(str(f.get("name") or ""))
                   for f in props):
            issues.append("no signature field found")

        # 6) Vaccines Due min_selection == 1
        vdue, reason = _typed_field(props, [("vaccine", "due")], "multi_select")
        if not vdue:
            issues.append(f"Vaccines Due: {reason}")
        else:
            ms = vdue.get("multi_select") if isinstance(vdue.get("multi_select"), dict) else {}
            if str(ms.get("min_selection")) != "1":
                issues.append(f"Vaccines Due min_selection={ms.get('min_selection')} expected 1")

        # 7) Preferred Appointment Date disable_past_dates == true
        pref, reason = _typed_field(props, [("preferred", "appointment"), ("preferred",)], "date")
        if not pref:
            issues.append(f"Preferred Appointment Date: {reason}")
        elif not _truthy(pref.get("disable_past_dates")):
            issues.append(f"disable_past_dates={pref.get('disable_past_dates')} expected true")

        check("7. OpnForm conditional/signature", 2, not issues,
              "; ".join(issues)[:240] if issues
              else "3 conditional logics + page-break 'Sign Consent' + signature + attrs ok")
    except Exception as e:
        check("7. OpnForm conditional/signature", 2, False, f"exception: {e}")


def _prop_options(prop: dict) -> list:
    """Extract a property's option list. OpnForm stores select options nested
    at prop['multi_select']['options'] / prop['select']['options'], with
    top-level 'options'/'choices' as legacy locations."""
    for candidate in (
        prop.get("options"),
        prop.get("choices"),
        (prop.get("multi_select") or {}).get("options") if isinstance(prop.get("multi_select"), dict) else None,
        (prop.get("select") or {}).get("options") if isinstance(prop.get("select"), dict) else None,
    ):
        if isinstance(candidate, list) and candidate:
            return candidate
    return []


def _option_names(raw_opts: list) -> set[str]:
    if raw_opts and isinstance(raw_opts[0], dict):
        return {(o.get("name") or o.get("value") or "").lower() for o in raw_opts}
    return {str(o).lower() for o in raw_opts}


def check_8_form_vaccines_options():
    """OpnForm Vaccines Due multi-select has correct options (MMR, Tdap, IPV, Varicella, Hepatitis B)."""
    try:
        row = _get_form_row()
        if not row:
            check("8. OpnForm vaccine options", 2, False, "form not found")
            return

        props = row.get("properties") or []
        if isinstance(props, str):
            props = json.loads(props)

        expected_vaccines = {"mmr", "tdap", "ipv", "varicella", "hepatitis b"}

        vaccine_field = None
        for prop in props:
            name = (prop.get("name") or prop.get("label") or "").lower()
            if "vaccine" in name and "due" in name:
                vaccine_field = prop
                break
        if not vaccine_field:
            # Fallback: find any multi-select with vaccine-related options
            for prop in props:
                ptype = (prop.get("type") or "").lower()
                if "multi" in ptype or "select" in ptype or "checkbox" in ptype:
                    opt_names = _option_names(_prop_options(prop))
                    if "mmr" in opt_names or "tdap" in opt_names:
                        vaccine_field = prop
                        break

        if not vaccine_field:
            check("8. OpnForm vaccine options", 2, False, "Vaccines Due field not found")
            return

        # Extract option values (including nested multi_select/select paths)
        raw_opts = _prop_options(vaccine_field)
        found = _option_names(raw_opts) if raw_opts else set()

        missing = expected_vaccines - found
        check("8. OpnForm vaccine options", 2, not missing,
              f"missing options: {missing}" if missing else f"all 5 vaccine options present in {len(raw_opts)} options")
    except Exception as e:
        check("8. OpnForm vaccine options", 2, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

SPREADSHEET_TITLE = "Immunization_Audit_CatchUp_2026Q2"

# Expected vaccine schedule and live-classification name patterns
# (matched against CVX-joined code_text + note, normalized).
EXPECTED_VACCINES: list[tuple[str, str]] = [
    ("MMR", r"mmr|measles.*mumps"),
    ("Tdap", r"tdap|tetanus.*pertussis"),
    ("IPV", r"\bipv\b|polio"),
    ("Varicella", r"varicella"),
    ("Hepatitis B", r"hepatitis\s*b|\bhep\s*b\b"),
]

# Generic (non-specific) allergy list titles not required verbatim in the
# Sheet2 allergy cell (binding: Adrianne asserts "contains Peanut" only).
_GENERIC_ALLERGY_TITLES = {"allergic disposition"}
_EMPTYISH = {"", "none", "n/a", "na", "no", "-", "no known allergies", "nka", "nkda"}


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


# ── xlsx parsing helpers ─────────────────────────────────────────────────────

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
    """Rows as lists of cell strings, placed by column ref (r="B2") so that
    skipped empty cells do not shift indices. Same-row assertions must run per
    row — never on the whole-document text."""
    if shared is None:
        shared = _xlsx_shared_strings(zf)
    xml = zf.read(sheet_path).decode("utf-8", errors="replace")
    rows = []
    for rowxml in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, flags=re.DOTALL):
        cells: list[str] = []
        for cm in re.finditer(r"<c\b([^>]*?)(?:/>|>(.*?)</c>)", rowxml, flags=re.DOTALL):
            attrs, inner = cm.group(1), cm.group(2) or ""
            rm = re.search(r'\br="([A-Z]+)\d+"', attrs)
            if rm:
                col = 0
                for ch in rm.group(1):
                    col = col * 26 + (ord(ch) - 64)
                col -= 1
            else:
                col = len(cells)
            while len(cells) <= col:
                cells.append("")
            tm = re.search(r'\bt="([^"]+)"', attrs)
            ctype = tm.group(1) if tm else ""
            if ctype == "s":
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                try:
                    idx = int(v.group(1)) if v else -1
                except ValueError:
                    idx = -1
                cells[col] = shared[idx] if 0 <= idx < len(shared) else ""
            elif ctype in ("inlineStr", "str"):
                ts = re.findall(r"<t(?:\s[^>]*)?>(.*?)</t>", inner, flags=re.DOTALL)
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells[col] = (_xml_unescape("".join(ts)) if ts
                              else (_xml_unescape(v.group(1)) if v else ""))
            else:
                v = re.search(r"<v>(.*?)</v>", inner, flags=re.DOTALL)
                cells[col] = v.group(1).strip() if v else ""
        rows.append(cells)
    return rows


def _xlsx_charts(zf: zipfile.ZipFile) -> list[str]:
    """XML text of each xl/charts/chart*.xml member."""
    out = []
    for name in zf.namelist():
        if re.match(r"xl/charts/chart\d*\.xml$", name):
            out.append(zf.read(name).decode("utf-8", errors="replace"))
    return out


def _cell(row: list[str], i: int) -> str:
    return row[i] if 0 <= i < len(row) else ""


def _col_idx(header: list[str], all_of: tuple[str, ...],
             none_of: tuple[str, ...] = ()) -> int:
    for i, cell in enumerate(header):
        c = _norm_text(cell)
        if c and all(k in c for k in all_of) and not any(k in c for k in none_of):
            return i
    return -1


def _cell_date(cell: str) -> date | None:
    """Parse a cell as a date: ISO / y-m-d / m/d/y strings, or an Excel serial
    number (days since 1899-12-30). Returns None if it is not a date."""
    s = (cell or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
    try:
        f = float(s)
    except ValueError:
        return None
    if 20000 <= f <= 80000:
        return date(1899, 12, 30) + timedelta(days=int(f))
    return None


# ── Live reconciliation truths (re-queried at verify time; never hardcoded) ──

_truths_cache: dict | None = None


def _immunization_truth(pids: list[str]) -> dict[str, tuple[str, set[str]]]:
    """Classify each expected vaccine Completed/Missing from the LIVE
    immunizations table (CVX-joined code_text or note pattern match).
    Returns {vaccine: (status, {administered ISO dates})}."""
    ids = ",".join(f"'{x}'" for x in pids)
    raw = openemr_sql(
        "SELECT IFNULL(c.code_text,''), IFNULL(i.note,''), "
        "IFNULL(DATE(i.administered_date),'') "
        "FROM immunizations i LEFT JOIN codes c ON c.code = i.cvx_code "
        "AND c.code_type IN (SELECT ct_id FROM code_types WHERE ct_key='CVX') "
        f"WHERE i.patient_id IN ({ids})"
    )
    recs: list[tuple[str, str]] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            recs.append((_norm_text(parts[0] + " " + parts[1]), parts[2].strip()))
    truth: dict[str, tuple[str, set[str]]] = {}
    for vname, pat in EXPECTED_VACCINES:
        dates = {d for text, d in recs
                 if re.search(pat, text) and d and d.upper() != "NULL"}
        matched = any(re.search(pat, text) for text, _ in recs)
        truth[vname] = ("Completed", dates) if matched else ("Missing", set())
    return truth


def _allergy_titles(pids: list[str]) -> list[str]:
    ids = ",".join(f"'{x}'" for x in pids)
    raw = openemr_sql(
        "SELECT DISTINCT title FROM lists "
        f"WHERE type='allergy' AND activity=1 AND pid IN ({ids})"
    )
    return [t.strip() for t in raw.splitlines() if t.strip()]


def _load_truths() -> dict:
    """Per-patient live truths keyed on the canonical pid (appointment-hit pid;
    falls back to the full same-name pid set if no appointment was found)."""
    global _truths_cache
    if _truths_cache is None:
        t = {}
        for p in PATIENTS:
            fname, lname = p["name"].split(" ", 1)
            if p["name"] in CANON_PID:
                pids = [CANON_PID[p["name"]]]
            else:
                pids = _patient_pids(fname, lname)
            if not pids:
                raise RuntimeError(f"no pid resolvable for {p['name']}")
            t[p["name"]] = {
                "pids": pids,
                "vaccines": _immunization_truth(pids),
                "allergies": _allergy_titles(pids),
            }
        _truths_cache = t
    return _truths_cache


def _missing_count(truths: dict, name: str) -> int:
    return sum(1 for s, _ in truths[name]["vaccines"].values() if s == "Missing")


def _allergy_cell_ok(cell_norm: str, titles: list[str]) -> bool:
    """Binding: Cyrstal cell contains Tetracycline; Adrianne contains Peanut
    (generic 'Allergic disposition' not required); Numbers None/empty/'none'.
    Driven by the LIVE lists query, never by row counts."""
    if not titles:
        return cell_norm in _EMPTYISH
    specific = [t for t in titles if _norm_text(t) not in _GENERIC_ALLERGY_TITLES]
    need = specific or titles
    return all(_norm_text(t) in cell_norm for t in need)


# ── OnlyOffice check functions ───────────────────────────────────────────────

def check_9a_spreadsheet_gate():
    """0pt gate: spreadsheet exists with exact title (suffix tolerated, Recovery
    excluded) AND has both required sheet names. Returns (zf, sheets) or None."""
    label = "9a. OnlyOffice spreadsheet + sheet names (gate)"
    try:
        found = _oo_find_file_id([
            SPREADSHEET_TITLE,
            SPREADSHEET_TITLE + ".xlsx",
            SPREADSHEET_TITLE + "%",
        ])
        if not found:
            check(label, 0, False, "file not found in files_file")
            return None
        file_id, title = found
        if not title.lower().startswith(SPREADSHEET_TITLE.lower()):
            check(label, 0, False, f"title mismatch: '{title[:60]}'")
            return None
        data = _oo_bytes_from_fs(file_id)
        src = "fs"
        if not data:
            data = _oo_bytes_from_api(file_id)
            src = "api"
        if not data:
            check(label, 0, False,
                  f"id={file_id} found but content unreadable (fs+api)")
            return None
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
            sheets = {_norm_text(k): v for k, v in _xlsx_sheets(zf).items()}
        except Exception as e:
            check(label, 0, False, f"not a readable xlsx: {e}")
            return None
        missing = [s for s in ("immunization records", "catch-up summary")
                   if s not in sheets]
        if missing:
            check(label, 0, False,
                  f"missing sheets {missing}; have {sorted(sheets)[:6]}")
            return None
        check(label, 0, True, f"{src} id={file_id} title='{title[:50]}' sheets ok")
        return (zf, sheets)
    except Exception as e:
        check(label, 0, False, f"exception: {e}")
        return None


def check_9b_sheet1_reconciliation(ctx):
    """Sheet1 rows reconciled against the LIVE immunizations table per canonical
    pid: patient x expected vaccine exactly one row, Status matches truth,
    Missing rows have no administered date, Completed rows carry the DB date,
    Catch-Up Appointment Date equals the ck1-3 appointment date. Extra
    historical rows (e.g. Numbers' Influenza) are tolerated, not scored.
    DOB cells are deliberately NOT asserted (dup-profile dependent)."""
    label = "9b. Sheet1 immunization reconciliation"
    if not ctx:
        check(label, 2, False, "gate 9a failed: spreadsheet/sheets unavailable")
        return
    try:
        zf, sheets = ctx
        truths = _load_truths()
        rows = _xlsx_rows(zf, sheets["immunization records"])
        hdr_idx = -1
        for i, r in enumerate(rows):
            joined = _norm_text(" | ".join(r))
            if "patient name" in joined and "status" in joined:
                hdr_idx = i
                break
        if hdr_idx < 0:
            check(label, 2, False, "Sheet1 header row (Patient Name + Status) not found")
            return
        hdr = rows[hdr_idx]
        cols = {
            "name": _col_idx(hdr, ("patient", "name")),
            "vaccine": _col_idx(hdr, ("vaccine",)),
            "adm": _col_idx(hdr, ("administered",)),
            "status": _col_idx(hdr, ("status",)),
            "appt": _col_idx(hdr, ("appointment",)),
        }
        missing_cols = [k for k, v in cols.items() if v < 0]
        if missing_cols:
            check(label, 2, False, f"Sheet1 header missing columns: {missing_cols}")
            return
        data = rows[hdr_idx + 1:]
        issues = []
        for p in PATIENTS:
            fn, ln = _norm_text(p["name"]).split(" ", 1)
            appt_date = date.fromisoformat(p["date"])
            vtruth = truths[p["name"]]["vaccines"]
            for vname, pat in EXPECTED_VACCINES:
                matches = [
                    r for r in data
                    if fn in _norm_text(_cell(r, cols["name"]))
                    and ln in _norm_text(_cell(r, cols["name"]))
                    and re.search(pat, _norm_text(_cell(r, cols["vaccine"])))
                ]
                if len(matches) != 1:
                    issues.append(f"{ln}/{vname}: {len(matches)} rows")
                    continue
                r = matches[0]
                status, ddates = vtruth[vname]
                scell = _norm_text(_cell(r, cols["status"]))
                if status.lower() not in scell:
                    issues.append(f"{ln}/{vname}: status '{scell[:20]}' != {status}")
                adm = _cell_date(_cell(r, cols["adm"]))
                if status == "Missing" and adm is not None:
                    issues.append(f"{ln}/{vname}: Missing row has date {adm}")
                if status == "Completed" and adm not in {date.fromisoformat(d) for d in ddates}:
                    issues.append(f"{ln}/{vname}: date {adm} not in DB {sorted(ddates)}")
                if _cell_date(_cell(r, cols["appt"])) != appt_date:
                    issues.append(f"{ln}/{vname}: catch-up date != {p['date']}")
        counts = "/".join(str(_missing_count(truths, p["name"])) for p in PATIENTS)
        check(label, 2, not issues,
              "; ".join(issues)[:240] if issues
              else f"15 patient x vaccine rows reconciled vs live DB (missing {counts})")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9c_sheet2_summary(ctx):
    """Sheet2 SAME-ROW per patient: name, missing-vaccine list consistent with
    live truth, Number Missing == truth count, allergy cell vs live lists query,
    Appointment Scheduled=Yes, appointment date, provider surname."""
    label = "9c. Sheet2 catch-up summary rows"
    if not ctx:
        check(label, 2, False, "gate 9a failed: spreadsheet/sheets unavailable")
        return
    try:
        zf, sheets = ctx
        truths = _load_truths()
        rows = _xlsx_rows(zf, sheets["catch-up summary"])
        hdr_idx = -1
        for i, r in enumerate(rows):
            joined = _norm_text(" | ".join(r))
            if "patient name" in joined and "missing" in joined:
                hdr_idx = i
                break
        if hdr_idx < 0:
            check(label, 2, False, "Sheet2 header row (Patient Name + Missing) not found")
            return
        hdr = rows[hdr_idx]
        cols = {
            "name": _col_idx(hdr, ("patient", "name")),
            "missing": _col_idx(hdr, ("missing", "vaccine")),
            "num": _col_idx(hdr, ("number",)),
            "allergy": _col_idx(hdr, ("allerg",)),
            "sched": _col_idx(hdr, ("scheduled",)),
            "apptdate": _col_idx(hdr, ("appointment", "date"), ("scheduled",)),
            "provider": _col_idx(hdr, ("provider",)),
        }
        missing_cols = [k for k, v in cols.items() if v < 0]
        if missing_cols:
            check(label, 2, False, f"Sheet2 header missing columns: {missing_cols}")
            return
        data = rows[hdr_idx + 1:]
        issues = []
        for p in PATIENTS:
            fn, ln = _norm_text(p["name"]).split(" ", 1)
            cand = [
                r for r in data
                if fn in _norm_text(_cell(r, cols["name"]))
                and ln in _norm_text(_cell(r, cols["name"]))
            ]
            if len(cand) != 1:
                issues.append(f"{ln}: {len(cand)} summary rows")
                continue
            r = cand[0]
            vtruth = truths[p["name"]]["vaccines"]
            miss_names = [v for v, _ in EXPECTED_VACCINES if vtruth[v][0] == "Missing"]
            mcell = _norm_text(_cell(r, cols["missing"]))
            if miss_names:
                for vname, pat in EXPECTED_VACCINES:
                    if vtruth[vname][0] == "Missing" and not re.search(pat, mcell):
                        issues.append(f"{ln}: missing list lacks {vname}")
            elif mcell not in _EMPTYISH:
                issues.append(f"{ln}: missing list should be empty, got '{mcell[:30]}'")
            try:
                num_ok = float(_cell(r, cols["num"]).strip()) == float(len(miss_names))
            except ValueError:
                num_ok = False
            if not num_ok:
                issues.append(f"{ln}: Number Missing '{_cell(r, cols['num'])[:10]}' != {len(miss_names)}")
            if not _allergy_cell_ok(_norm_text(_cell(r, cols["allergy"])),
                                    truths[p["name"]]["allergies"]):
                issues.append(f"{ln}: allergy cell '{_cell(r, cols['allergy'])[:30]}' vs live {truths[p['name']]['allergies']}")
            if "yes" not in _norm_text(_cell(r, cols["sched"])):
                issues.append(f"{ln}: Appointment Scheduled != Yes")
            if _cell_date(_cell(r, cols["apptdate"])) != date.fromisoformat(p["date"]):
                issues.append(f"{ln}: appointment date != {p['date']}")
            if p["provider_lname"].lower() not in _norm_text(_cell(r, cols["provider"])):
                issues.append(f"{ln}: provider missing '{p['provider_lname']}'")
        check(label, 2, not issues,
              "; ".join(issues)[:240] if issues
              else "3 summary rows match live truths (same-row scoped)")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9d_bar_chart(ctx):
    """Bar chart exists AND (numCache values include the three live Number
    Missing truths OR the chart references the 'Catch-Up Summary' sheet)."""
    label = "9d. Sheet2 bar chart"
    if not ctx:
        check(label, 1, False, "gate 9a failed: spreadsheet/sheets unavailable")
        return
    try:
        zf, _sheets = ctx
        truths = _load_truths()
        counts = [_missing_count(truths, p["name"]) for p in PATIENTS]
        charts = _xlsx_charts(zf)
        if not charts:
            check(label, 1, False, "no xl/charts/chart*.xml member in workbook")
            return
        bars = [x for x in charts if "barChart" in x]
        if not bars:
            check(label, 1, False, f"{len(charts)} chart(s) found but none contain barChart")
            return
        vals: list[float] = []
        for x in bars:
            vals = []
            for v in re.findall(r"<c:v>([^<]*)</c:v>", x):
                try:
                    vals.append(float(v))
                except ValueError:
                    pass
            ok_num = all(
                sum(1 for v in vals if abs(v - t) < 1e-6) >= counts.count(t)
                for t in set(counts)
            )
            ok_ref = "Catch-Up Summary" in x
            if ok_num or ok_ref:
                check(label, 1, True,
                      f"barChart ok (numCache match={ok_num}, sheet ref={ok_ref}, truth {counts})")
                return
        check(label, 1, False,
              f"barChart present but numCache values {vals[:12]} lack truth {counts} "
              f"and no 'Catch-Up Summary' reference")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_appointment_cyrstal()
    check_2_appointment_numbers()
    check_3_appointment_adrianne()
    check_4_form_exists()
    check_5_form_settings()
    check_6_form_core_fields()
    check_7_form_conditional_and_signature()
    check_8_form_vaccines_options()
    ctx = check_9a_spreadsheet_gate()
    check_9b_sheet1_reconciliation(ctx)
    check_9c_sheet2_summary(ctx)
    check_9d_bar_chart(ctx)

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
