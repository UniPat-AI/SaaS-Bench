"""
Verifier for Healthcare-045-I3: Q4 2026 Patient Access & Safety Experience Survey Deployment

Checks: 15 checks (12 tightened legacy + 3 new), weighted total 21.
Strategy: docker exec (DB) for OpnForm and OpenEMR; docker-exec fs probe
(files_file id -> portal data dir) with REST API fallback for OnlyOffice.

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
from datetime import date as _date

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

_required = [
    "OPNFORM_PORT", "OPNFORM_CONTAINER",
    "OPENEMR_PORT", "OPENEMR_CONTAINER", "OPENEMR_DB_CONTAINER",
    "ONLYOFFICE_PORT", "ONLYOFFICE_CONTAINER", "ONLYOFFICE_DB_CONTAINER",
]
for _v in _required:
    if not os.environ.get(_v):
        print(f"FATAL: {_v} not set", file=sys.stderr)
        sys.exit(1)


# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)

class CheckFail(RuntimeError):
    """Expected verification failure (not an infra error) — renders as a plain
    FAIL detail instead of the 'exception:' prefix that verify_runner promotes
    to ERROR status."""


def _exc_detail(e: Exception) -> str:
    return str(e) if isinstance(e, CheckFail) else _exc_detail(e)



# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15) -> tuple[int, str, str]:
    r = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.returncode, r.stdout, r.stderr


def opnform_sql(query: str) -> str:
    """Query OpnForm PostgreSQL (embedded in app container). Raises on error."""
    rc, out, err = docker_exec(
        OPNFORM_CONTAINER,
        "psql", "-U", "forge", "-d", "forge", "-t", "-A", "-c", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"opnform psql error (rc={rc}): {err.strip()[-300:]}")
    return out.strip()


def openemr_sql(query: str) -> str:
    """Query OpenEMR MariaDB. Raises on error."""
    rc, out, err = docker_exec(
        OPENEMR_DB_CONTAINER,
        "mysql", "-u", "openemr", "-popenemr_pass", "openemr",
        "--default-character-set=utf8mb4", "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"openemr mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


def onlyoffice_sql(query: str) -> str:
    """Query OnlyOffice MySQL. Raises on SQL error."""
    rc, out, err = docker_exec(
        ONLYOFFICE_DB_CONTAINER,
        "mysql", "-u", "onlyoffice_user", "-ponlyoffice_pass",
        "-D", "onlyoffice", "--default-character-set=utf8mb4",
        "-N", "-B", "-e", query,
        timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"onlyoffice mysql rc={rc}: {err.strip()[-300:]}")
    return out.strip()


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


def _truthy(v) -> bool:
    return v is True or v == 1 or (isinstance(v, str) and v.lower() == "true")


def _num_eq(v, expected: float) -> bool:
    """int-vs-str tolerant numeric equality; absent (None) -> False."""
    try:
        return v is not None and abs(float(v) - expected) < 1e-9
    except (TypeError, ValueError):
        return False


def _json_values(node):
    """Yield all scalar values from a nested JSON structure."""
    if isinstance(node, dict):
        for v in node.values():
            yield from _json_values(v)
    elif isinstance(node, list):
        for v in node:
            yield from _json_values(v)
    else:
        yield node


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


# ── OpnForm: cached form lookup ───────────────────────────────────────────────
FORM_TITLE = "Q4 2026 Patient Access & Safety Experience Survey"
_form_cache: dict | None = None


def _get_form() -> dict:
    global _form_cache
    if _form_cache is not None:
        return _form_cache
    row = opnform_sql(
        f"SELECT row_to_json(f) FROM forms f WHERE f.title = '{FORM_TITLE}' LIMIT 1;"
    )
    if not row:
        raise RuntimeError("form gate failed: form not found in forms table")
    _form_cache = json.loads(row)
    return _form_cache


def _get_form_props() -> list[dict]:
    form = _get_form()
    props = form.get("properties", [])
    if isinstance(props, str):
        props = json.loads(props)
    return props


def _find_field(props: list[dict], type_: str, *name_frags: str) -> dict | None:
    """First field of the given type whose (dash-normalized) name contains all frags."""
    for p in props:
        nm = _norm_text(p.get("name") or "")
        if p.get("type") == type_ and all(f in nm for f in name_frags):
            return p
    return None


def _select_options(p: dict) -> list[str]:
    sel = p.get("select") or {}
    opts = sel.get("options") or []
    return [str(o.get("name", "")) for o in opts if isinstance(o, dict)]


# ── Check 1 (0pt gate) ───────────────────────────────────────────────────────
def check_1_form_exists() -> None:
    """Gate: form exists with correct title (existence alone earns nothing)."""
    try:
        form = _get_form()
        ok = form.get("title") == FORM_TITLE
        check("1. OpnForm: form exists (gate)", 0, ok, f"title={form.get('title')!r}")
    except Exception as e:
        check("1. OpnForm: form exists (gate)", 0, False, _exc_detail(e))


# ── Check 2 ───────────────────────────────────────────────────────────────────
def check_2_form_settings() -> None:
    """Form settings: theme minimal, color #10B981, size md, border small,
    focused presentation, confetti, re-fillable + button text, submitted text,
    no indexing."""
    try:
        form = _get_form()
        issues = []

        if form.get("theme") != "minimal":
            issues.append(f"theme={form.get('theme')!r}")
        # Hex colors are stored lowercased (e.g. '#10b981') — compare case-insensitively
        if (form.get("color") or "").lower() != "#10b981":
            issues.append(f"color={form.get('color')!r}")
        if form.get("size") != "md":
            issues.append(f"size={form.get('size')!r}")
        if form.get("border_radius") != "small":
            issues.append(f"border_radius={form.get('border_radius')!r}")
        if form.get("presentation_style") != "focused":
            issues.append(f"presentation_style={form.get('presentation_style')!r}")
        if not form.get("confetti_on_submission"):
            issues.append("confetti off")
        if not form.get("re_fillable"):
            issues.append("re_fillable off")
        if form.get("re_fill_button_text") != "Submit Another Survey":
            issues.append(f"re_fill_button_text={form.get('re_fill_button_text')!r}")

        expected_ty = "Thank you for helping us enhance your care journey!"
        submitted = str(form.get("submitted_text", ""))
        if expected_ty not in submitted:
            issues.append("submitted_text mismatch")

        if _truthy(form.get("can_be_indexed")):
            issues.append("indexing not disabled (can_be_indexed truthy)")

        check("2. OpnForm: form settings", 2, not issues,
              "all correct" if not issues else "; ".join(issues))
    except Exception as e:
        check("2. OpnForm: form settings", 2, False, _exc_detail(e))


# ── Check 3 ───────────────────────────────────────────────────────────────────
def check_3_field_count_and_types() -> None:
    """Form has >=14 fields and ALL required field types present (missing==0)."""
    try:
        props = _get_form_props()
        types_found = {p.get("type", "") for p in props}
        count = len(props)

        # NOTE: 'nf-page-break' is deliberately ABSENT from this needed set and
        # must STAY absent. The page-break client block is available_in:
        # ["classic"] only, while this task requires presentation_style
        # 'focused' — a focused-presentation form CANNOT contain a page break
        # (verified against the mw-opnform image). Do NOT "re-tighten" by
        # adding it here: that would make the check unpassable.
        needed = {"date", "text", "rating", "scale", "select", "matrix",
                  "slider", "checkbox", "phone_number", "email", "signature"}
        # Also accept alternate names
        alt = {"phone_number": "phone"}
        missing = [t for t in needed
                   if t not in types_found and alt.get(t, "") not in types_found]

        ok = count >= 14 and len(missing) == 0
        detail = f"count={count}, types={sorted(types_found)}"
        if missing:
            detail += f", missing={missing}"
        check("3. OpnForm: field count & types", 2, ok, detail)
    except Exception as e:
        check("3. OpnForm: field count & types", 2, False, _exc_detail(e))


# ── Check 3b (NEW) ────────────────────────────────────────────────────────────
def check_3b_field_specs() -> None:
    """Per-field numeric/option specs demanded verbatim by the description."""
    try:
        props = _get_form_props()
        issues = []

        rating = _find_field(props, "rating", "satisfaction")
        if not rating:
            issues.append("rating 'Overall Visit Satisfaction' not found")
        elif rating.get("rating_max_value") is not None and not _num_eq(rating.get("rating_max_value"), 5):
            # 5 is the rating default; UI only persists changed keys -> absent ok
            issues.append(f"rating_max_value={rating.get('rating_max_value')!r} (want 5)")

        # Description demands min 1 / max 10 / step 1. The OpnForm UI only
        # persists keys the user changes (plan D risk note): min/step equal
        # the type defaults, so ABSENT counts as compliant for them; max was
        # changed away from the default and must be explicit.
        scale = _find_field(props, "scale", "wait time")
        if not scale:
            issues.append("scale 'Wait Time Acceptability' not found")
        else:
            if not _num_eq(scale.get("scale_max_value"), 10):
                issues.append(f"scale_max_value={scale.get('scale_max_value')!r} (want 10)")
            for key, exp in (("scale_min_value", 1), ("scale_step_value", 1)):
                if scale.get(key) is not None and not _num_eq(scale.get(key), exp):
                    issues.append(f"{key}={scale.get(key)!r} (want {exp} or absent-default)")

        slider = _find_field(props, "slider", "recommend")
        if not slider:
            issues.append("slider 'Likelihood to Recommend' not found")
        else:
            if not _num_eq(slider.get("slider_max_value"), 10):
                issues.append(f"slider_max_value={slider.get('slider_max_value')!r} (want 10)")
            for key, exp in (("slider_min_value", 0), ("slider_step_value", 1)):
                if slider.get(key) is not None and not _num_eq(slider.get(key), exp):
                    issues.append(f"{key}={slider.get(key)!r} (want {exp} or absent-default)")

        # Option sets: superset check (all required names present; extras allowed)
        vtype = _find_field(props, "select", "visit type")
        if not vtype:
            issues.append("select 'Visit Type' not found")
        else:
            opts = {_norm_text(o) for o in _select_options(vtype)}
            want = {"new patient", "follow-up", "urgent care", "telehealth"}
            miss = sorted(want - opts)
            if miss:
                issues.append(f"Visit Type options missing {miss}")

        followup = _find_field(props, "select", "follow-up")
        if not followup:
            issues.append("select 'Would You Like a Follow-Up' not found")
        else:
            opts = {_norm_text(o) for o in _select_options(followup)}
            want = {"yes - phone call", "yes - email", "no"}
            miss = sorted(want - opts)
            if miss:
                issues.append(f"Follow-Up options missing {miss}")

        cb = _find_field(props, "checkbox", "problem")
        if not cb:
            issues.append("checkbox 'Experienced a Problem During Visit' not found")
        elif not _truthy(cb.get("use_toggle_switch")):
            issues.append(f"use_toggle_switch={cb.get('use_toggle_switch')!r}")

        vdate = _find_field(props, "date", "visit date")
        if not vdate:
            issues.append("date 'Visit Date' not found")
        elif not _truthy(vdate.get("disable_future_dates")):
            issues.append(f"disable_future_dates={vdate.get('disable_future_dates')!r}")

        check("3b. OpnForm: field specs", 2, not issues,
              "all field specs correct" if not issues else "; ".join(issues)[:220])
    except Exception as e:
        check("3b. OpnForm: field specs", 2, False, _exc_detail(e))


# ── Check 4 ───────────────────────────────────────────────────────────────────
def check_4_matrix_field() -> None:
    """Matrix field has 4 service-area rows and 4 rating columns."""
    try:
        props = _get_form_props()
        matrix = next((p for p in props if p.get("type") == "matrix"), None)
        if not matrix:
            check("4. OpnForm: matrix field", 2, False, "no matrix field found")
            return

        rows = matrix.get("rows", [])
        columns = matrix.get("columns", [])
        row_names = [r.get("name", r) if isinstance(r, dict) else str(r) for r in rows]
        col_names = [c.get("name", c) if isinstance(c, dict) else str(c) for c in columns]
        rn_lower = [r.lower() for r in row_names]
        cn_lower = [c.lower() for c in col_names]

        exp_rows = ["wayfinding", "medical records", "specialist referral", "patient safety"]
        exp_cols = ["poor", "fair", "good", "excellent"]

        rows_ok = all(any(er in r for r in rn_lower) for er in exp_rows)
        cols_ok = all(any(ec in c for c in cn_lower) for ec in exp_cols)

        check("4. OpnForm: matrix field", 2, rows_ok and cols_ok,
              f"rows={row_names}, cols={col_names}")
    except Exception as e:
        check("4. OpnForm: matrix field", 2, False, _exc_detail(e))


# ── Check 5 ───────────────────────────────────────────────────────────────────
def _cond_ok(field: dict, trigger_id: str | None, value_frag: str | None) -> tuple[bool, str]:
    """Field logic must have >=1 condition leaf matching the expected trigger
    field id and/or value fragment, plus a show/hide-block action. Empty logic
    FAILS (the 'field exists is enough' leniency is exactly what was removed)."""
    logic = field.get("logic")
    if not isinstance(logic, dict) or not logic:
        return False, "no logic"
    actions = logic.get("actions") or []
    leaves = _logic_leaves(logic.get("conditions"))
    if not leaves:
        return False, "no condition leaves"
    # OpnForm stores conditional visibility as either show-block or hide-block
    # (the UI's "hide unless"/"show when" duals) — both are legitimate configs.
    if not ({"show-block", "hide-block"} & set(actions)):
        return False, f"actions={actions}"
    for leaf in leaves:
        if trigger_id is not None and _leaf_trigger_id(leaf) != str(trigger_id):
            continue
        if value_frag is not None:
            leaf_val = json.dumps((leaf.get("value") or {}).get("value"))
            if value_frag not in _norm_text(leaf_val):
                continue
        return True, "ok"
    return False, "no leaf matches expected trigger/value"


def check_5_conditional_fields() -> None:
    """Conditional fields: parse each logic tree — Problem Description triggered
    by the problem checkbox id; Contact Email on 'Yes - Email'; Contact Phone
    on 'Yes - Phone Call'; all with a show-block action."""
    try:
        props = _get_form_props()
        issues = []

        problem_cb = _find_field(props, "checkbox", "problem")
        cb_id = str(problem_cb.get("id")) if problem_cb else None
        if cb_id is None:
            issues.append("problem checkbox not found (trigger id unknown)")

        def by_name(*frags):
            for p in props:
                nm = _norm_text(p.get("name") or "")
                if all(f in nm for f in frags):
                    return p
            return None

        specs = [
            ("Problem Description", by_name("problem", "description"), cb_id, None),
            ("Contact Email", by_name("contact", "email"), None, "yes - email"),
            ("Contact Phone", by_name("contact", "phone"), None, "yes - phone call"),
        ]
        for label, fld, trig, frag in specs:
            if fld is None:
                issues.append(f"{label}: field not found")
                continue
            ok, why = _cond_ok(fld, trig, frag)
            if not ok:
                issues.append(f"{label}: {why}")

        check("5. OpnForm: conditional fields", 2, not issues,
              "3/3 conditional rules verified" if not issues else "; ".join(issues)[:220])
    except Exception as e:
        check("5. OpnForm: conditional fields", 2, False, _exc_detail(e))


# ── Check 6 ───────────────────────────────────────────────────────────────────
def check_6_form_public() -> None:
    """Form visibility is public."""
    try:
        form = _get_form()
        vis = form.get("visibility", "")
        check("6. OpnForm: public visibility", 1, vis == "public",
              f"visibility={vis!r}")
    except Exception as e:
        check("6. OpnForm: public visibility", 1, False, _exc_detail(e))


# ── Check 7 ───────────────────────────────────────────────────────────────────
def check_7_email_notification() -> None:
    """A real Email Notification integration row: form_integrations only —
    integration_id='email', status='active', recipient in parsed data JSON."""
    try:
        form = _get_form()
        form_id = int(form.get("id"))
        target = "patient.access@clinic.local"

        out = opnform_sql(
            f"SELECT row_to_json(fi) FROM form_integrations fi WHERE fi.form_id = {form_id};"
        )
        rows = [json.loads(line) for line in out.splitlines() if line.strip()]
        seen = []
        found = False
        for r in rows:
            seen.append(f"{r.get('integration_id')}/{r.get('status')}")
            if r.get("integration_id") != "email" or r.get("status") != "active":
                continue
            data = r.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except ValueError:
                    data = {}
            # recipient may live in send_to or another recipient-like key —
            # search all scalar values of the parsed data JSON
            if any(isinstance(v, str) and target in v for v in _json_values(data)):
                found = True
                break

        check("7. OpnForm: email notification", 2, found,
              f"active email integration -> {target}" if found
              else f"no active email integration with {target}; rows={seen}")
    except Exception as e:
        check("7. OpnForm: email notification", 2, False, _exc_detail(e))


# ── Check 8: OpenEMR ─────────────────────────────────────────────────────────
def check_8_office_note() -> None:
    """OpenEMR office note: launch text + '2027 Annual Quality & Safety Plan'
    anchor ('&' may be stored as '&amp;' — compare after unescape+normalize)."""
    try:
        result = openemr_sql(
            "SELECT body FROM onotes WHERE body LIKE "
            "'%Q4 2026 Patient Access%Safety Experience Survey Program%' LIMIT 1;"
        )
        body = _norm_text(_xml_unescape(result or ""))
        anchors = [
            "q4 2026 patient access & safety experience survey program officially launched today",
            "2027 annual quality & safety plan",
        ]
        missing = [a for a in anchors if _norm_text(a) not in body]
        ok = bool(result) and not missing
        detail = f"found={bool(result)}, len={len(result) if result else 0}"
        if result and missing:
            detail += f", missing anchors={missing}"
        check("8. OpenEMR: office note", 2, ok, detail)
    except Exception as e:
        check("8. OpenEMR: office note", 2, False, _exc_detail(e))


# ── Check 8b (NEW, 0pt observation) ──────────────────────────────────────────
def check_8b_batch_email() -> None:
    """Batch Communication email row in batchcom.

    WEIGHT 0 — observation only: the batchcom table and its columns
    (patient_id, sent_by, msg_type, msg_subject, msg_text, msg_date_sent; seed
    0 rows) are verified, but whether OpenEMR's Batch Communication Tool
    actually writes a row on send has NOT been smoke-tested. Promote to a
    weighted check only after a UI smoke test confirms the write behavior.
    """
    try:
        out = openemr_sql(
            "SELECT CONCAT_WS('|||', msg_subject, "
            "REPLACE(REPLACE(msg_text, CHAR(10), ' '), CHAR(13), ' ')) FROM batchcom;"
        )
        nrows = 0
        ok = False
        for line in out.splitlines():
            if not line.strip():
                continue
            nrows += 1
            subj, _, body = line.partition("|||")
            s, b = _norm_text(subj), _norm_text(body)
            # subject anchored in two pieces to tolerate the em dash
            if ("your voice matters" in s
                    and "access & safety experience" in s
                    and "patient access committee" in b
                    and "quality improvement initiatives" in b):
                ok = True
                break
        check("8b. OpenEMR: batch email (observation)", 0, ok,
              f"matching batchcom row found (rows={nrows})" if ok
              else f"no matching batchcom row (rows={nrows})")
    except Exception as e:
        check("8b. OpenEMR: batch email (observation)", 0, False, _exc_detail(e))


# ── OnlyOffice document retrieval (fs-first, API fallback) ───────────────────
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
        return None, f"files_file lookup failed: {e}"
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


_xlsx_cache: tuple[bytes | None, str] | None = None


def _get_xlsx() -> tuple[bytes | None, str]:
    global _xlsx_cache
    if _xlsx_cache is None:
        _xlsx_cache = _oo_get_document(
            ["Q4 2026 Patient Access%Safety Experience Survey Analysis%"]
        )
    return _xlsx_cache


# ── xlsx parsing ──────────────────────────────────────────────────────────────
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


def _xlsx_row_maps(zf: zipfile.ZipFile, sheet_path: str,
                   shared: list[str] | None = None) -> list[dict[str, str]]:
    """Rows as {column letter: cell string} dicts: shared strings resolved,
    inline/str strings and raw numeric <v> values included. Column letters are
    kept so column-scoped assertions (e.g. Priority non-empty) survive omitted
    empty cells. Same-row assertions must run per row — never on the whole
    document text."""
    if shared is None:
        shared = _xlsx_shared_strings(zf)
    xml = zf.read(sheet_path).decode("utf-8", errors="replace")
    rows = []
    for rowxml in re.findall(r"<row\b[^>]*>(.*?)</row>", xml, flags=re.DOTALL):
        cells: dict[str, str] = {}
        seq = 0
        for cm in re.finditer(r"<c\b([^>]*)>(.*?)</c>", rowxml, flags=re.DOTALL):
            attrs, inner = cm.group(1), cm.group(2)
            rm = re.search(r'\br="([A-Z]+)\d+"', attrs)
            col = rm.group(1) if rm else f"#{seq}"
            seq += 1
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


_wb_cache: dict | None = None


def _get_workbook() -> dict:
    """Cached {zf, sheets, shared, src}; raises RuntimeError with the gate
    detail when the spreadsheet could not be retrieved."""
    global _wb_cache
    if _wb_cache is None:
        data, src = _get_xlsx()
        if data is None:
            _wb_cache = {"err": f"spreadsheet gate failed: {src}"}
        else:
            zf = zipfile.ZipFile(io.BytesIO(data))
            _wb_cache = {"zf": zf, "sheets": _xlsx_sheets(zf),
                         "shared": _xlsx_shared_strings(zf), "src": src}
    if "err" in _wb_cache:
        raise RuntimeError(_wb_cache["err"])
    return _wb_cache


def _sheet_path(name: str) -> str:
    wb = _get_workbook()
    for nm, p in wb["sheets"].items():
        if _norm_text(nm) == _norm_text(name):
            return p
    raise CheckFail(f"sheet {name!r} not found (sheets={list(wb['sheets'])})")


def _sheet_row_maps(name: str) -> list[dict[str, str]]:
    wb = _get_workbook()
    return _xlsx_row_maps(wb["zf"], _sheet_path(name), wb["shared"])


def _sheet_xml(name: str) -> str:
    wb = _get_workbook()
    return wb["zf"].read(_sheet_path(name)).decode("utf-8", errors="replace")


def _excel_serial(iso: str) -> int:
    """Excel 1900-system date serial for an ISO date string."""
    return (_date.fromisoformat(iso) - _date(1899, 12, 30)).days


def _float_eq(cell: str, val: float, tol: float = 1e-9) -> bool:
    try:
        return abs(float(cell) - val) <= tol
    except ValueError:
        return False


# ── Check 9 (0pt gate) ───────────────────────────────────────────────────────
def check_9_spreadsheet_exists() -> None:
    """Gate: OnlyOffice spreadsheet located and readable (existence alone earns
    nothing; content checks 10-13 carry the weight)."""
    try:
        data, src = _get_xlsx()
        check("9. OnlyOffice: spreadsheet exists (gate)", 0, data is not None, src)
    except Exception as e:
        check("9. OnlyOffice: spreadsheet exists (gate)", 0, False, _exc_detail(e))


# ── Check 10 ──────────────────────────────────────────────────────────────────
def check_10_sheet_names() -> None:
    """Spreadsheet has 3 sheets: Raw Responses, Quality Metrics, Safety Action Plan."""
    try:
        wb = _get_workbook()
        names = list(wb["sheets"])
        norm = {_norm_text(n) for n in names}
        expected = ["Raw Responses", "Quality Metrics", "Safety Action Plan"]
        missing = [e for e in expected if _norm_text(e) not in norm]
        detail = f"sheets={names}" + (f", missing={missing}" if missing else "")
        check("10. OnlyOffice: sheet names", 1, not missing, detail)
    except Exception as e:
        check("10. OnlyOffice: sheet names", 1, False, _exc_detail(e))


# ── Check 11 ──────────────────────────────────────────────────────────────────
# (resp#, date, provider surname, satisfaction, wait, visit type, nps, problem)
SAMPLE_ROWS = [
    ("1", "2026-10-02", "pouros", "5", "10", "telehealth", "10", "no"),
    ("2", "2026-10-04", "dickinson", "3", "5", "follow-up", "6", "yes"),
    ("3", "2026-10-06", "kuhic", "4", "8", "new patient", "8", "no"),
    ("4", "2026-10-09", "reinger", "2", "4", "urgent care", "4", "yes"),
    ("5", "2026-10-13", "hartmann", "4", "7", "follow-up", "9", "no"),
]


def _sample_item_match(cells: list[str], idx: int, item: str) -> bool:
    """cells are normalized. idx 1 = date (ISO text or Excel serial);
    idx 2/5 = provider/visit-type (substring); rest = exact cell match
    (numeric-tolerant, so '2' never free-rides on '2026-10-09')."""
    if idx == 1:
        serial = str(_excel_serial(item))
        return any(c == item or c.startswith(item) or c == serial for c in cells)
    if idx in (2, 5):
        return any(item in c for c in cells)
    for c in cells:
        if c == item:
            return True
        try:
            if _float_eq(c, float(item)):
                return True
        except ValueError:
            pass
    return False


def check_11_raw_response_data() -> None:
    """Sheet 1: all 5 sample rows asserted same-row (full value tuple each);
    providers 5/5."""
    try:
        maps = _sheet_row_maps("Raw Responses")
        rows = [[_norm_text(v) for v in m.values()] for m in maps]
        matched, missing = [], []
        for exp in SAMPLE_ROWS:
            hit = any(
                all(_sample_item_match(r, i, it) for i, it in enumerate(exp))
                for r in rows if r
            )
            (matched if hit else missing).append(f"row{exp[0]}/{exp[2]}")
        ok = len(matched) == 5
        detail = f"sample rows matched {len(matched)}/5, providers {len(matched)}/5"
        if missing:
            detail += f", missing={missing}"
        check("11. OnlyOffice: raw response data", 1, ok, detail)
    except Exception as e:
        check("11. OnlyOffice: raw response data", 1, False, _exc_detail(e))


# ── Check 12 ──────────────────────────────────────────────────────────────────
AREA_ROWS = [  # (area fragment, avg rating, action keyword)
    ("wayfinding", 3.0, "signage"),
    ("medical records", 2.8, "portal"),
    ("specialist referral", 3.4, "referral coordinator"),
    ("patient safety", 4.3, "medication reconciliation"),
]


def check_12_action_plan() -> None:
    """Sheet 3: 4/4 service-area rows, each same-row with its rating and
    improvement-action keyword, and a non-empty Priority cell."""
    try:
        maps = _sheet_row_maps("Safety Action Plan")
        norm_maps = [{k: _norm_text(v) for k, v in m.items()} for m in maps]

        pcol = None  # Priority column letter, from the header row
        for m in norm_maps:
            for col, v in m.items():
                if v.startswith("priority"):
                    pcol = col
                    break
            if pcol:
                break

        issues = []
        if pcol is None:
            issues.append("Priority header column not found")
        for area, rating, action_kw in AREA_ROWS:
            m = next((m for m in norm_maps if any(area in v for v in m.values())), None)
            if m is None:
                issues.append(f"{area}: row not found")
                continue
            cells = list(m.values())
            if not any(_float_eq(c, rating) for c in cells):
                issues.append(f"{area}: rating {rating} missing")
            if not any(action_kw in c for c in cells):
                issues.append(f"{area}: action '{action_kw}' missing")
            if pcol is not None and not m.get(pcol, "").strip():
                issues.append(f"{area}: Priority empty")

        check("12. OnlyOffice: action plan", 1, not issues,
              "4/4 area rows with rating, action, priority" if not issues
              else "; ".join(issues)[:220])
    except Exception as e:
        check("12. OnlyOffice: action plan", 1, False, _exc_detail(e))


# ── Check 13 (NEW) ────────────────────────────────────────────────────────────
def check_13_quality_metrics() -> None:
    """Sheet 2 'Quality Metrics': real formulas (COUNTA / 3x AVERAGE / COUNTIF
    referencing 'Raw Responses') + cached-value reconciliation. Pasted plain
    values (no <f> elements) fail the formula assertions by design."""
    try:
        xml = _sheet_xml("Quality Metrics")
        formulas = [_xml_unescape(f)
                    for f in re.findall(r"<f\b[^>]*>(.*?)</f>", xml, flags=re.DOTALL)]
        fu = [f.upper() for f in formulas]
        issues = []
        if not any("COUNTA(" in f for f in fu):
            issues.append("no COUNTA formula")
        n_avg = sum(1 for f in fu if "AVERAGE(" in f)
        if n_avg < 3:
            issues.append(f"AVERAGE formulas {n_avg}/3")
        if not any("COUNTIF(" in f for f in fu):
            issues.append("no COUNTIF formula")
        # sheet ref may be quoted ('Raw Responses'!A2) or normalized — loose match
        if not any(re.search(r"raw[\s_]*responses", f, flags=re.IGNORECASE)
                   for f in formulas):
            issues.append("no 'Raw Responses' reference in formulas")

        maps = _sheet_row_maps("Quality Metrics")
        rows = [[_norm_text(v) for v in m.values()] for m in maps]

        def metric(*frags) -> float | None:
            """Numeric Value of the first row whose text contains all frags
            (cached <v> lives next to the <f> in the same cell)."""
            for r in rows:
                joined = " | ".join(r)
                if all(f in joined for f in frags):
                    for c in r:
                        try:
                            return float(c)
                        except ValueError:
                            continue
                    return None
            return None

        for frags, exp, name in (
            (("total responses",), 5.0, "Total Responses"),
            (("average", "satisfaction"), 3.6, "Avg Satisfaction"),
            (("average", "wait"), 6.8, "Avg Wait"),
            (("nps",), 7.4, "Avg NPS"),
            (("problems reported",), 2.0, "Problems Reported"),
        ):
            v = metric(*frags)
            if v is None or abs(v - exp) > 0.05:
                issues.append(f"{name}={v} (want {exp})")

        # Daily Encounter Volume truth queried LIVE at verify time (agents may
        # add encounters) — never hardcode the seed count.
        live = int(openemr_sql(
            "SELECT COUNT(*) FROM form_encounter "
            "WHERE date >= '2026-10-01' AND date < '2026-10-16';"
        ))
        dev = metric("encounter volume")
        if dev is None or abs(dev - live) > 0.05:
            issues.append(f"Daily Encounter Volume={dev} (want live count {live})")

        # Follow-Up Requested Count: truth not derivable from the sample data
        # (description flaw) -> label + numeric value only, no exact number.
        if metric("follow-up requested") is None:
            issues.append("Follow-Up Requested row missing or value non-numeric")

        check("13. OnlyOffice: quality metrics (Sheet 2)", 3, not issues,
              f"formulas={len(formulas)}, live_encounters={live}, all reconciled"
              if not issues else "; ".join(issues)[:220])
    except Exception as e:
        check("13. OnlyOffice: quality metrics (Sheet 2)", 3, False, _exc_detail(e))


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_form_exists()
    check_2_form_settings()
    check_3_field_count_and_types()
    check_3b_field_specs()
    check_4_matrix_field()
    check_5_conditional_fields()
    check_6_form_public()
    check_7_email_notification()
    check_8_office_note()
    check_8b_batch_email()
    check_9_spreadsheet_exists()
    check_10_sheet_names()
    check_11_raw_response_data()
    check_12_action_plan()
    check_13_quality_metrics()

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
