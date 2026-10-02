#!/usr/bin/env python3
"""
Verifier for Healthcare-013-I5: Set Up Telehealth Intake Workflow and Visit Summary

Checks: 16 weighted checks (total 19pt) across opnform, openemr, onlyoffice.
Strategy: docker exec (DB queries) for OpnForm and OpenEMR; fs-first (docker exec)
with API fallback for OnlyOffice document content.

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

OPNFORM_PORT = os.getenv("OPNFORM_PORT")
OPNFORM_CONTAINER = os.getenv("OPNFORM_CONTAINER")

OPENEMR_PORT = os.getenv("OPENEMR_PORT")
OPENEMR_CONTAINER = os.getenv("OPENEMR_CONTAINER")
OPENEMR_DB_CONTAINER = os.getenv("OPENEMR_DB_CONTAINER")

ONLYOFFICE_PORT = os.getenv("ONLYOFFICE_PORT")
ONLYOFFICE_CONTAINER = os.getenv("ONLYOFFICE_CONTAINER")
ONLYOFFICE_DB_CONTAINER = os.getenv("ONLYOFFICE_DB_CONTAINER")

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

# ── Constants ─────────────────────────────────────────────────────────────────
# probed seed max, mw-openemr:latest 2026-08
SEED_ENCOUNTER_MAX = "2026-03-05"
# probed seed max, mw-openemr:latest 2026-08
SEED_HISTORY_MAX = "2026-03-22 05:30:20"

FORM_TITLE = "Digital Pre-Visit Telehealth Intake Survey"
DOC_TITLE = "Telehealth Visit Summary - Riley Gleichner - 2026-05-21"
EMAIL_TARGET = "intake-triage@worcesterwellness.example.com"

# (name, type, required) — exact field inventory from description.md.
# The nf-text block is the 11th field; it is gated in check 2c.
EXPECTED_FIELDS = [
    ("Patient Full Name", "text", True),
    ("Date of Birth", "date", True),
    ("Phone Number", "phone_number", True),
    ("Visit Reason", "select", True),
    ("Symptom Severity", "rating", True),
    ("Symptom Duration in Days", "number", True),
    ("Taking Medications for This Condition", "checkbox", False),
    ("Current Medications List", "text", False),
    ("Upload Prior Lab Results", "files", False),
    ("I Confirm Information is Accurate", "checkbox", True),
]

VISIT_REASON_OPTIONS = [
    "Hypertension Monitoring",
    "Joint Pain Evaluation",
    "Sleep Disturbance Review",
    "Urgent Minor Injury Consult",
]


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


def opnform_db(sql: str) -> str:
    """Query OpnForm PostgreSQL (embedded in app container). Raises on error."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER, "psql", "-U", "forge", "-d", "forge",
        "-t", "-A", "-c", sql,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def openemr_db(sql: str) -> str:
    """Query OpenEMR MariaDB. Raises on error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER, "mysql", "-u", "openemr", "-popenemr_pass",
        "--default-character-set=utf8mb4", "openemr",
        "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_db(sql: str) -> str:
    """Query OnlyOffice MySQL 8.0. Raises on error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER, "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "--default-character-set=utf8mb4", "onlyoffice",
        "-N", "-B", "-e", sql,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def _norm_name(s) -> str:
    return " ".join(str(s or "").split()).lower()


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


_props_cache: dict = {}


def _load_props():
    """Fetch and cache the form's properties JSON array (list of field dicts).

    Returns None if the form is not found. Raises on DB error / bad JSON.
    """
    if "props" in _props_cache:
        return _props_cache["props"]
    raw = opnform_db(
        f"SELECT properties FROM forms WHERE title = '{FORM_TITLE}' "
        "AND deleted_at IS NULL LIMIT 1;"
    )
    props = json.loads(raw) if raw else None
    if props is not None and not isinstance(props, list):
        raise RuntimeError(f"properties is not a JSON array: {str(props)[:120]}")
    _props_cache["props"] = props
    return props


def _field_by_name(props: list, name: str):
    target = _norm_name(name)
    for f in props:
        if _norm_name(f.get("name")) == target:
            return f
    return None


def _resolve_patient_and_encounter() -> tuple[str | None, str | None, str]:
    """Resolve canonical pid (DOB-disambiguated) and the new anchored encounter.

    Returns (pid, encounter, detail). pid 284 (DOB NULL duplicate) is only
    scanned to make FAIL details explanatory — never used for scoring.
    """
    try:
        pid = openemr_db(
            "SELECT pid FROM patient_data WHERE fname='Riley' AND lname='Gleichner' "
            "AND DOB='1965-10-02';"
        ).strip()
        if not pid or "\n" in pid:
            others = ""
            try:
                others = openemr_db(
                    "SELECT pid, DOB FROM patient_data "
                    "WHERE fname='Riley' AND lname='Gleichner';"
                ).replace("\n", " / ").replace("\t", ":")
            except Exception:
                pass
            return None, None, (
                "canonical pid not resolved (fname=Riley lname=Gleichner "
                f"DOB=1965-10-02); Riley Gleichner rows: {others[:120]}"
            )
        enc = openemr_db(
            f"SELECT encounter FROM form_encounter WHERE pid={pid} "
            f"AND date > '{SEED_ENCOUNTER_MAX}' "
            "ORDER BY date DESC, encounter DESC LIMIT 1;"
        ).strip()
        if not enc:
            extra = ""
            try:
                dup = openemr_db(
                    "SELECT DISTINCT fe.pid FROM form_encounter fe "
                    "JOIN patient_data p ON p.pid=fe.pid "
                    "WHERE p.fname='Riley' AND p.lname='Gleichner' "
                    f"AND fe.pid<>{pid} AND fe.date > '{SEED_ENCOUNTER_MAX}';"
                ).strip()
                if dup:
                    extra = (
                        f"; a new encounter exists under duplicate pid "
                        f"{dup.replace(chr(10), ',')} (wrong chart opened?)"
                    )
            except Exception:
                pass
            return pid, None, (
                f"no new encounter (form_encounter.date > '{SEED_ENCOUNTER_MAX}') "
                f"for pid {pid}{extra}"
            )
        return pid, enc, f"pid={pid} enc={enc}"
    except Exception as e:
        return None, None, f"exception: {e}"


# ── OpnForm checks ───────────────────────────────────────────────────────────

def check_1_opnform_form_settings() -> None:
    """Form exists with correct title, theme, size, dark_mode, visibility, auto_focus, redirect_url."""
    try:
        row = opnform_db(
            "SELECT title, theme, size, dark_mode, visibility, auto_focus, redirect_url "
            f"FROM forms WHERE title = '{FORM_TITLE}' "
            "AND deleted_at IS NULL LIMIT 1;"
        )
        if not row:
            check("1. OpnForm form settings", 2, False, "form not found")
            return
        parts = row.split("|")
        if len(parts) < 7:
            check("1. OpnForm form settings", 2, False, f"unexpected format: {row[:200]}")
            return
        title, theme, size, dark_mode, visibility, auto_focus, redirect_url = (
            p.strip() for p in parts[:7]
        )
        issues = []
        if theme != "notion":
            issues.append(f"theme={theme}")
        if size != "md":
            issues.append(f"size={size}")
        if dark_mode != "light":
            issues.append(f"dark_mode={dark_mode}")
        if visibility != "public":
            issues.append(f"visibility={visibility}")
        if auto_focus not in ("t", "1", "true"):
            issues.append(f"auto_focus={auto_focus}")
        expected_redirect = "https://worcesterwellness.example.com/telehealth/submission-received"
        if redirect_url != expected_redirect:
            issues.append(f"redirect_url mismatch")
        check("1. OpnForm form settings", 2, not issues,
              "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("1. OpnForm form settings", 2, False, f"exception: {e}")


def check_2a_opnform_field_inventory() -> None:
    """All 10 named input fields present with exact types; 7 required fields required==true."""
    try:
        props = _load_props()
        if props is None:
            check("2a. OpnForm field inventory", 1, False, "form not found")
            return
        issues = []
        for name, ftype, req in EXPECTED_FIELDS:
            f = _field_by_name(props, name)
            if f is None:
                issues.append(f"missing field '{name}'")
                continue
            if (f.get("type") or "") != ftype:
                issues.append(f"'{name}' type={f.get('type')} expected {ftype}")
            if req and not _truthy(f.get("required")):
                issues.append(f"'{name}' not required")
        check("2a. OpnForm field inventory", 1, not issues,
              f"{len(props)} fields; 10 named fields + 7 required ok" if not issues
              else "; ".join(issues)[:300])
    except Exception as e:
        check("2a. OpnForm field inventory", 1, False, f"exception: {e}")


def check_2b_opnform_options_rating() -> None:
    """Visit Reason select options superset of the 4 required; Symptom Severity rating_max_value==5."""
    try:
        props = _load_props()
        if props is None:
            check("2b. OpnForm options + rating", 1, False, "form not found")
            return
        issues = []
        vr = _field_by_name(props, "Visit Reason")
        if vr is None or vr.get("type") != "select":
            issues.append("Visit Reason select field not found")
        else:
            opts = {
                _norm_name(o.get("name"))
                for o in ((vr.get("select") or {}).get("options") or [])
                if isinstance(o, dict)
            }
            missing = [o for o in VISIT_REASON_OPTIONS if _norm_name(o) not in opts]
            if missing:
                issues.append(f"options missing: {', '.join(missing)}")
        sev = _field_by_name(props, "Symptom Severity")
        if sev is None or sev.get("type") != "rating":
            issues.append("Symptom Severity rating field not found")
        else:
            # 5 is the OpnForm rating default; the UI only persists changed
            # keys, so ABSENT counts as compliant (plan D risk note).
            rmax = sev.get("rating_max_value")
            if rmax is None:
                rmax_ok = True
            else:
                try:
                    rmax_ok = int(float(rmax)) == 5
                except (TypeError, ValueError):
                    rmax_ok = False
            if not rmax_ok:
                issues.append(f"rating_max_value={rmax} expected 5 or absent-default")
        check("2b. OpnForm options + rating", 1, not issues,
              "options superset + rating max 5 ok" if not issues
              else "; ".join(issues)[:300])
    except Exception as e:
        check("2b. OpnForm options + rating", 1, False, f"exception: {e}")


def check_2c_opnform_files_and_text_block() -> None:
    """Files field: max_file_size 5, allowed types pdf/jpg/png; nf-text block content fragments."""
    try:
        props = _load_props()
        if props is None:
            check("2c. OpnForm files field + text block", 1, False, "form not found")
            return
        issues = []
        ff = _field_by_name(props, "Upload Prior Lab Results")
        if ff is None or ff.get("type") != "files":
            ff = next((f for f in props if f.get("type") == "files"), None)
        if ff is None:
            issues.append("files field not found")
        else:
            mfs = ff.get("max_file_size")
            try:
                mfs_ok = int(float(mfs)) == 5
            except (TypeError, ValueError):
                mfs_ok = False
            if not mfs_ok:
                issues.append(f"max_file_size={mfs} expected 5")
            # Seed data uses allowed_extensions; the validator/bundle use
            # allowed_file_types — accept pdf/jpg/png present in EITHER key.
            allowed = " ".join(
                str(ff.get(k)) for k in ("allowed_file_types", "allowed_extensions")
                if ff.get(k)
            ).lower()
            missing_types = [t for t in ("pdf", "jpg", "png") if t not in allowed]
            if missing_types:
                issues.append(
                    f"allowed types missing {','.join(missing_types)} "
                    f"(got: {allowed[:80] or 'none'})"
                )
        texts = [f for f in props if f.get("type") == "nf-text"]
        if not texts:
            issues.append("nf-text block not found")
        else:
            fragments = ("telehealth service advisory", "call 911", "hipaa")
            block_ok = False
            for tf in texts:
                plain = _norm_name(re.sub(r"<[^>]+>", " ", str(tf.get("content") or "")))
                if all(fr in plain for fr in fragments):
                    block_ok = True
                    break
            if not block_ok:
                issues.append(
                    "nf-text content missing one of: Telehealth Service Advisory / "
                    "call 911 / HIPAA"
                )
        check("2c. OpnForm files field + text block", 1, not issues,
              "files config + advisory text ok" if not issues
              else "; ".join(issues)[:300])
    except Exception as e:
        check("2c. OpnForm files field + text block", 1, False, f"exception: {e}")


def check_2d_opnform_conditional_logic() -> None:
    """Current Medications List shown conditionally when the medications checkbox is checked."""
    try:
        props = _load_props()
        if props is None:
            check("2d. OpnForm conditional logic", 1, False, "form not found")
            return
        meds = _field_by_name(props, "Current Medications List")
        cb = _field_by_name(props, "Taking Medications for This Condition")
        if meds is None:
            check("2d. OpnForm conditional logic", 1, False,
                  "Current Medications List field not found")
            return
        if cb is None:
            check("2d. OpnForm conditional logic", 1, False,
                  "Taking Medications for This Condition checkbox not found")
            return
        logic = meds.get("logic") or {}
        conditions = logic.get("conditions") if isinstance(logic, dict) else None
        actions = logic.get("actions") if isinstance(logic, dict) else None
        if not conditions:
            check("2d. OpnForm conditional logic", 1, False,
                  "field has no conditional logic (logic.conditions empty)")
            return
        cb_id = str(cb.get("id"))
        leaves = _logic_leaves(conditions)
        trig_ok = any(
            _leaf_trigger_id(leaf) == cb_id
            and (leaf.get("value") or {}).get("operator") == "is_checked"
            for leaf in leaves
        )
        act_ok = isinstance(actions, list) and "show-block" in actions
        issues = []
        if not trig_ok:
            issues.append(
                f"no condition leaf with trigger id={cb_id} operator=is_checked "
                f"({len(leaves)} leaves)"
            )
        if not act_ok:
            issues.append(f"actions={actions} missing 'show-block'")
        check("2d. OpnForm conditional logic", 1, not issues,
              "show-block on checkbox is_checked" if not issues
              else "; ".join(issues)[:300])
    except Exception as e:
        check("2d. OpnForm conditional logic", 1, False, f"exception: {e}")


def check_3_opnform_email_integration() -> None:
    """Email integration: integration_id='email', status='active', data.send_to exact."""
    try:
        rows = opnform_db(
            "SELECT fi.integration_id || '|||' || fi.status || '|||' || "
            "COALESCE(fi.data::text, '') FROM form_integrations fi "
            "JOIN forms f ON fi.form_id = f.id "
            f"WHERE f.title = '{FORM_TITLE}' "
            "AND f.deleted_at IS NULL AND fi.deleted_at IS NULL;"
        )
        if not rows:
            check("3. OpnForm email integration", 1, False, "no integrations found")
            return
        found = False
        seen = []
        for line in rows.splitlines():
            parts = line.split("|||", 2)
            if len(parts) < 3:
                continue
            integration_id, status, data_txt = (p.strip() for p in parts)
            seen.append(f"{integration_id}/{status}")
            if integration_id != "email" or status != "active":
                continue
            try:
                data = json.loads(data_txt) if data_txt else {}
            except json.JSONDecodeError:
                continue
            send_to = data.get("send_to")
            if send_to == EMAIL_TARGET or (
                isinstance(send_to, list) and send_to == [EMAIL_TARGET]
            ):
                found = True
                break
        check("3. OpnForm email integration", 1, found,
              f"active email integration send_to={EMAIL_TARGET}" if found
              else f"no active email integration with exact send_to; rows: {'; '.join(seen)[:200]}")
    except Exception as e:
        check("3. OpnForm email integration", 1, False, f"exception: {e}")


# ── OpenEMR checks ───────────────────────────────────────────────────────────

def check_4_openemr_patient_phone(pid: str | None, gate_detail: str) -> None:
    """Patient Riley Gleichner (pid DOB-locked) has phone (774) 555-0388."""
    if not pid:
        check("4. OpenEMR patient phone", 1, False, f"patient gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            "SELECT phone_home, phone_cell, phone_biz, phone_contact "
            f"FROM patient_data WHERE pid={pid};"
        )
        if not row:
            check("4. OpenEMR patient phone", 1, False, f"pid {pid} row not found")
            return
        target = "(774) 555-0388"
        digits = "7745550388"
        raw = row.replace("-", "").replace(" ", "").replace("(", "").replace(")", "")
        found = target in row or digits in raw
        check("4. OpenEMR patient phone", 1, found,
              "phone correct" if found else f"phones: {row[:200]}")
    except Exception as e:
        check("4. OpenEMR patient phone", 1, False, f"exception: {e}")


def check_5_openemr_social_history(pid: str | None, gate_detail: str) -> None:
    """New history row: tobacco 'Former smoker, quit 5 years'; alcohol 'Moderate'/'1-2 drinks' — per-column."""
    if not pid:
        check("5. OpenEMR social history", 1, False, f"patient gate failed: {gate_detail}")
        return
    try:
        tobacco = openemr_db(
            f"SELECT tobacco FROM history_data WHERE pid={pid} "
            f"AND date > '{SEED_HISTORY_MAX}' ORDER BY date DESC, id DESC LIMIT 1;"
        )
        alcohol = openemr_db(
            f"SELECT alcohol FROM history_data WHERE pid={pid} "
            f"AND date > '{SEED_HISTORY_MAX}' ORDER BY date DESC, id DESC LIMIT 1;"
        )
        if not tobacco and not alcohol:
            check("5. OpenEMR social history", 1, False,
                  f"no history_data row with date > '{SEED_HISTORY_MAX}' for pid {pid}")
            return
        issues = []
        tob_low = tobacco.lower()
        alc_low = alcohol.lower()
        if "former smoker" not in tob_low or "quit 5 years" not in tob_low:
            issues.append(f"tobacco mismatch: {tobacco[:80]}")
        if "moderate" not in alc_low and "1-2 drinks" not in alc_low:
            issues.append(f"alcohol mismatch: {alcohol[:80]}")
        check("5. OpenEMR social history", 1, not issues,
              "tobacco+alcohol correct (per-column)" if not issues
              else "; ".join(issues)[:250])
    except Exception as e:
        check("5. OpenEMR social history", 1, False, f"exception: {e}")


def check_6_openemr_vitals(pid: str | None, enc: str | None, gate_detail: str) -> None:
    """Vitals on the new encounter (via forms registry): BP 138/88, pulse 74, temp 98.5, weight 215."""
    if not pid or not enc:
        check("6. OpenEMR vitals", 2, False, f"encounter gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            "SELECT v.bps, v.bpd, v.pulse, v.temperature, v.weight FROM form_vitals v "
            "JOIN forms f ON f.form_id = v.id AND f.formdir='vitals' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY v.id DESC LIMIT 1;"
        )
        if not row:
            check("6. OpenEMR vitals", 2, False,
                  f"no vitals form on encounter {enc}")
            return
        parts = row.split("\t")
        if len(parts) < 5:
            check("6. OpenEMR vitals", 2, False, f"unexpected format: {row[:200]}")
            return
        bps, bpd, pulse, temp, weight = (p.strip() for p in parts[:5])
        issues = []
        if bps != "138":
            issues.append(f"bps={bps}")
        if bpd != "88":
            issues.append(f"bpd={bpd}")
        try:
            if abs(float(pulse) - 74) > 0.5:
                issues.append(f"pulse={pulse}")
        except ValueError:
            issues.append(f"pulse={pulse}")
        try:
            if abs(float(temp) - 98.5) > 0.2:
                issues.append(f"temp={temp}")
        except ValueError:
            issues.append(f"temp={temp}")
        try:
            if abs(float(weight) - 215) > 0.5:
                issues.append(f"weight={weight}")
        except ValueError:
            issues.append(f"weight={weight}")
        check("6. OpenEMR vitals", 2, not issues,
              "all vitals correct on new encounter" if not issues else "; ".join(issues))
    except Exception as e:
        check("6. OpenEMR vitals", 2, False, f"exception: {e}")


def check_7_openemr_ros(pid: str | None, enc: str | None, gate_detail: str) -> None:
    """Active ROS form on the new encounter (form_ros has only varchar(3) checkbox
    columns — the narrative text cannot land in this table, so anchored existence
    is the verifiable ceiling; checked values printed for review)."""
    if not pid or not enc:
        check("7. OpenEMR ROS", 1, False, f"encounter gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            "SELECT r.fatigue, r.fever, r.chills, r.weight_change, "
            "r.cough, r.shortness_of_breath, r.wheezing, r.sinus_problems "
            "FROM form_ros r "
            "JOIN forms f ON f.form_id = r.id AND f.formdir='ros' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} AND r.activity=1 "
            "ORDER BY r.id DESC LIMIT 1;"
        )
        if not row:
            check("7. OpenEMR ROS", 1, False,
                  f"no active ROS form on encounter {enc}")
            return
        vals = ",".join(p.strip() for p in row.split("\t"))
        check("7. OpenEMR ROS", 1, True,
              f"ROS form on new encounter; fatigue,fever,chills,wt_chg,cough,sob,"
              f"wheeze,sinus={vals[:100]}")
    except Exception as e:
        check("7. OpenEMR ROS", 1, False, f"exception: {e}")


def check_8_openemr_soap(pid: str | None, enc: str | None, gate_detail: str) -> None:
    """SOAP note on the new encounter with correct S/O/A/P text."""
    if not pid or not enc:
        check("8. OpenEMR SOAP", 2, False, f"encounter gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            "SELECT s.subjective, s.objective, s.assessment, s.plan FROM form_soap s "
            "JOIN forms f ON f.form_id = s.id AND f.formdir='soap' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY s.id DESC LIMIT 1;"
        )
        if not row:
            check("8. OpenEMR SOAP", 2, False,
                  f"no SOAP form on encounter {enc}")
            return
        parts = row.split("\t")
        if len(parts) < 4:
            check("8. OpenEMR SOAP", 2, False, f"unexpected format: {row[:200]}")
            return
        subj, obj, assess, plan = (p.strip().lower() for p in parts[:4])
        issues = []
        if "hypertension" not in subj or "lisinopril" not in subj:
            issues.append("subjective mismatch")
        if "telehealth" not in obj:
            issues.append("objective mismatch")
        if "essential hypertension" not in assess or "suboptimal control" not in assess:
            issues.append("assessment mismatch")
        if ("lisinopril" not in plan or "20mg" not in plan
                or "basic metabolic panel" not in plan or "6 weeks" not in plan):
            issues.append("plan mismatch")
        check("8. OpenEMR SOAP", 2, not issues,
              "SOAP correct on new encounter" if not issues else "; ".join(issues))
    except Exception as e:
        check("8. OpenEMR SOAP", 2, False, f"exception: {e}")


def check_9_openemr_icd10(pid: str | None, enc: str | None, gate_detail: str) -> None:
    """ICD-10 code I10 billed on the new encounter."""
    if not pid or not enc:
        check("9. OpenEMR ICD-10 I10", 1, False, f"encounter gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            f"SELECT code FROM billing WHERE pid={pid} AND encounter={enc} "
            "AND code='I10' AND code_type='ICD10' AND activity=1 LIMIT 1;"
        )
        found = bool(row and "I10" in row)
        check("9. OpenEMR ICD-10 I10", 1, found,
              "I10 (ICD10) billed on new encounter" if found
              else f"no active ICD10 I10 billing row on encounter {enc}")
    except Exception as e:
        check("9. OpenEMR ICD-10 I10", 1, False, f"exception: {e}")


def check_10_openemr_clinical_instructions(pid: str | None, enc: str | None,
                                           gate_detail: str) -> None:
    """Clinical instructions on the new encounter: lisinopril 20mg + BP monitoring + specifics."""
    if not pid or not enc:
        check("10. OpenEMR clinical instructions", 1, False,
              f"encounter gate failed: {gate_detail}")
        return
    try:
        row = openemr_db(
            "SELECT ci.instruction FROM form_clinical_instructions ci "
            "JOIN forms f ON f.form_id = ci.id "
            "AND f.formdir='clinical_instructions' AND f.deleted=0 "
            f"WHERE f.pid={pid} AND f.encounter={enc} ORDER BY ci.id DESC LIMIT 1;"
        )
        if not row:
            check("10. OpenEMR clinical instructions", 1, False,
                  f"no clinical instructions form on encounter {enc}")
            return
        low = row.lower()
        issues = []
        if "lisinopril 20mg" not in low:
            issues.append("missing 'lisinopril 20mg'")
        if "blood pressure" not in low:
            issues.append("missing 'blood pressure'")
        if not any(s in low for s in ("twice daily", "160/100", "2,000mg")):
            issues.append("missing all of 'twice daily'/'160/100'/'2,000mg'")
        check("10. OpenEMR clinical instructions", 1, not issues,
              "instructions correct on new encounter" if not issues
              else f"{'; '.join(issues)}; text: {row[:120]}")
    except Exception as e:
        check("10. OpenEMR clinical instructions", 1, False, f"exception: {e}")


def check_11_openemr_appointment(pid: str | None, gate_detail: str) -> None:
    """Follow-up appointment on 2026-07-02 at 15:45 with Krystyna Reinger (all rows scanned)."""
    if not pid:
        check("11. OpenEMR appointment", 2, False, f"patient gate failed: {gate_detail}")
        return
    try:
        out = openemr_db(
            "SELECT e.pc_eventDate, e.pc_startTime, u.fname, u.lname "
            "FROM openemr_postcalendar_events e "
            "LEFT JOIN users u ON e.pc_aid = u.id "
            f"WHERE e.pc_pid='{pid}' AND e.pc_eventDate='2026-07-02';"
        )
        if not out:
            check("11. OpenEMR appointment", 2, False,
                  f"no appointment on 2026-07-02 for pid {pid}")
            return
        rows_seen = []
        matched = None
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) < 4:
                continue
            date_val, time_val, ufname, ulname = parts[:4]
            provider = f"{ufname} {ulname}".strip().lower()
            rows_seen.append(f"{time_val} {ufname} {ulname}")
            if (date_val == "2026-07-02" and time_val.startswith("15:45")
                    and "krystyna" in provider and "reinger" in provider):
                matched = line
                break
        check("11. OpenEMR appointment", 2, matched is not None,
              "appointment 2026-07-02 15:45 Krystyna Reinger found" if matched
              else f"no row matches 15:45 + Krystyna Reinger; rows: {'; '.join(rows_seen)[:180]}")
    except Exception as e:
        check("11. OpenEMR appointment", 2, False, f"exception: {e}")


# ── OnlyOffice checks ────────────────────────────────────────────────────────

def check_12_onlyoffice_document_exists() -> tuple[str, str] | None:
    """0pt gate: document with the full required title exists (tolerating a .docx
    suffix, excluding editor Recovery copies). Returns (id, title) for check 13."""
    try:
        row = onlyoffice_db(
            "SELECT id, title FROM files_file "
            f"WHERE title LIKE '{DOC_TITLE}%' AND title NOT LIKE '%Recovery%' "
            "ORDER BY id DESC LIMIT 1;"
        )
        if not row:
            check("12. OnlyOffice document exists", 0, False,
                  f"no files_file row with title '{DOC_TITLE}'")
            return None
        parts = row.split("\t")
        if len(parts) < 2:
            check("12. OnlyOffice document exists", 0, False,
                  f"unexpected format: {row[:200]}")
            return None
        file_id, title = parts[0].strip(), parts[1].strip()
        check("12. OnlyOffice document exists", 0, True,
              f"id={file_id} title={title[:80]}")
        return file_id, title
    except Exception as e:
        check("12. OnlyOffice document exists", 0, False, f"exception: {e}")
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


def _xml_unescape(s: str) -> str:
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"&#x([0-9a-fA-F]+);", lambda m: chr(int(m.group(1), 16)), s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                 ("&quot;", '"'), ("&apos;", "'")):
        s = s.replace(a, b)
    return s


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


def check_13_onlyoffice_document_content(file_info: tuple[str, str] | None) -> None:
    """Document contains key clinical content (fs-first fetch, API fallback)."""
    if not file_info:
        check("13. OnlyOffice document content", 1, False,
              "gate failed: document title not found (check 12)")
        return
    try:
        file_id, title = file_info
        data = _oo_bytes_from_fs(file_id)
        src = f"fs id={file_id}"
        if not data:
            data = _oo_bytes_from_api(file_id)
            src = f"api id={file_id}"
        if not data:
            check("13. OnlyOffice document content", 1, False,
                  f"id={file_id} found but content unreadable (fs+api)")
            return
        content = _docx_text(data)
        low = content.lower()

        key_phrases = [
            ("Worcester Wellness Associates", "clinic name"),
            ("(774) 555-0388", "patient phone"),
            ("1965-10-02", "patient DOB"),
            ("138/88", "BP"),
            ("2026-07-02", "follow-up date"),
            ("Krystyna Reinger", "follow-up provider"),
            ("(774) 555-0500", "clinic phone"),
            ("support@worcesterwellness.example.com", "clinic email"),
            ("Telehealth Visit Summary", "summary label"),
            ("98.5", "temperature"),
            ("215", "weight"),
        ]
        missing = [lbl for phrase, lbl in key_phrases if phrase not in content]
        if not any(t in content for t in ("3:45", "15:45")):
            missing.append("appointment time 3:45/15:45")
        if "lisinopril" not in low:
            missing.append("lisinopril")

        # Visit Reason value is not explicitly mandated by the description —
        # informational only, never gated.
        vr_found = next(
            (o for o in VISIT_REASON_OPTIONS if o.lower() in low), None)
        info = f"info: visit_reason={vr_found or 'not detected'}"

        check("13. OnlyOffice document content", 1, not missing,
              f"all key content found ({src}; {info})" if not missing
              else f"missing: {', '.join(missing)} ({src}; {info})")
    except Exception as e:
        check("13. OnlyOffice document content", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_opnform_form_settings()
    check_2a_opnform_field_inventory()
    check_2b_opnform_options_rating()
    check_2c_opnform_files_and_text_block()
    check_2d_opnform_conditional_logic()
    check_3_opnform_email_integration()

    pid, enc, gate_detail = _resolve_patient_and_encounter()

    check_4_openemr_patient_phone(pid, gate_detail)
    check_5_openemr_social_history(pid, gate_detail)
    check_6_openemr_vitals(pid, enc, gate_detail)
    check_7_openemr_ros(pid, enc, gate_detail)
    check_8_openemr_soap(pid, enc, gate_detail)
    check_9_openemr_icd10(pid, enc, gate_detail)
    check_10_openemr_clinical_instructions(pid, enc, gate_detail)
    check_11_openemr_appointment(pid, gate_detail)

    file_info = check_12_onlyoffice_document_exists()
    check_13_onlyoffice_document_content(file_info)

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
