"""
Verifier for agriculture_011: Dual-scale field photo analysis — aphid severity + organic intervention logging

Checks: 12 weighted checks (23 total points) across farmos.
Strategy: docker exec PHP/PDO queries against farmOS SQLite DB + llm_judge / llm_judge_vision.

Required env vars:
  SERVER_HOSTNAME, FARMOS_PORT, FARMOS_CONTAINER
"""

import base64
import json
import os
import re
import subprocess
import sys

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")
FARMOS_PORT = os.getenv("FARMOS_PORT")
FARMOS_CONTAINER = os.getenv("FARMOS_CONTAINER")

_missing = []
for var in ["FARMOS_PORT", "FARMOS_CONTAINER"]:
    if not os.getenv(var):
        _missing.append(var)
if _missing:
    print(f"FATAL: {', '.join(_missing)} not set", file=sys.stderr)
    sys.exit(1)

SQLITE_PATH = "/opt/drupal/web/sites/default/files/.ht.sqlite"

_INPUTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "inputs"
)

INPUT_FILES: list[str] = [
    os.path.join(_INPUTS_DIR, "farmos_crop_043.jpg"),
    os.path.join(_INPUTS_DIR, "farmos_crop_044.jpg"),
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


def php_query(sql: str, timeout: int = 15) -> list[dict]:
    php_code = (
        "$db = new PDO('sqlite:" + SQLITE_PATH + "');"
        "$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);"
        "$r = $db->query($argv[1]);"
        "$rows = $r->fetchAll(PDO::FETCH_ASSOC);"
        "echo json_encode($rows);"
    )
    rc, stdout, stderr = docker_exec(
        FARMOS_CONTAINER, "php", "-r", php_code, "--", sql, timeout=timeout,
    )
    if rc != 0:
        raise RuntimeError(f"php query failed (rc={rc}): {stderr[:300]}")
    if not stdout.strip():
        return []
    return json.loads(stdout.strip())


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def llm_judge(content: str, condition: str, timeout: int = 30) -> tuple[bool, str]:
    api_base = os.getenv("MINDRA_BASE_URL", "https://api.mindracode.com/v1")
    api_key = os.getenv("MINDRA_API_KEY", "")
    prompt = (
        f"Does the following content satisfy this condition?\n"
        f"Condition: {condition}\n\n"
        f"Content:\n{content}\n\n"
        f"Answer only YES or NO."
    )
    try:
        import requests
        resp = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={"model": os.getenv("MINDRA_MODEL", "gemini-3.0-flash-preview"),
                  "messages": [{"role": "user", "content": prompt}],
                  "max_tokens": 512},
            timeout=timeout,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
        return answer.startswith("YES"), answer
    except Exception as e:
        return False, f"llm_judge error: {e}"


def llm_judge_vision(
    image_b64: str,
    mime: str,
    recorded_value: str,
    condition: str,
    timeout: int = 45,
) -> tuple[bool, str]:
    api_base = os.getenv("MINDRA_BASE_URL", "https://api.mindracode.com/v1")
    api_key = os.getenv("MINDRA_API_KEY", "")
    prompt = (
        f"You are given an image and a value that an AI agent extracted from it.\n"
        f"Recorded value: «{recorded_value}»\n"
        f"Condition: {condition}\n\n"
        f"Does the recorded value accurately match the information visible in the image, "
        f"satisfying the condition above?\n"
        f"Answer only YES or NO."
    )
    try:
        import requests
        resp = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            json={
                "model": os.getenv("MINDRA_MODEL", "gemini-3.0-flash-preview"),
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
                        {"type": "text", "text": prompt},
                    ],
                }],
                "max_tokens": 512,
            },
            timeout=timeout,
        )
        resp.raise_for_status()
        answer = resp.json()["choices"][0]["message"]["content"].strip().upper()
        return answer.startswith("YES"), answer
    except Exception as e:
        return False, f"llm_judge_vision error: {e}"


# ── Shared state ──────────────────────────────────────────────────────────────
_corn_asset_id: int | None = None
_obs_logs: list[dict] = []
_input_logs: list[dict] = []
_emergency_obs: dict | None = None
_followup_obs: dict | None = None
_treatment_input: dict | None = None


# ── Log loader ────────────────────────────────────────────────────────────────
def _load_logs_for_corn_asset() -> None:
    global _obs_logs, _input_logs, _emergency_obs, _followup_obs
    if _corn_asset_id is None:
        return
    all_logs = php_query(
        "SELECT l.id, l.type, l.name, l.timestamp, l.status, l.notes__value "
        "FROM log_field_data l "
        "JOIN log__asset la ON l.id = la.entity_id AND la.deleted = 0 "
        f"WHERE la.asset_target_id = {_corn_asset_id} "
        "ORDER BY l.timestamp ASC"
    )
    _obs_logs = [r for r in all_logs if r["type"] == "observation"]
    _input_logs = [r for r in all_logs if r["type"] == "input"]
    _emergency_obs = None
    _followup_obs = None


def _log_text(log: dict) -> str:
    return strip_html(
        (log.get("notes__value") or "") + " " + (log.get("name") or "")
    ).lower()


def _select_followup_observation() -> dict | None:
    """Select the task's follow-up, not an arbitrary first/last seeded log."""
    if _emergency_obs is None:
        return None
    emergency_id = int(_emergency_obs["id"])
    emergency_ts = int(_emergency_obs["timestamp"])
    input_ts = int(_treatment_input["timestamp"]) if _treatment_input else emergency_ts
    candidates = [
        obs for obs in _obs_logs
        if int(obs["id"]) != emergency_id
        and int(obs["timestamp"]) >= input_ts
        and int(obs["timestamp"]) > emergency_ts
    ]
    if not candidates:
        return None

    expected_ts = emergency_ts + 7 * 86400

    def rank(obs: dict) -> tuple[int, int]:
        text = _log_text(obs)
        has_reduction = any(value in text for value in ["70%", "70 %", "seventy percent"])
        has_monitoring = any(value in text for value in [
            "monitor", "continu", "watch", "follow", "re-appl", "reappl",
            "inspect", "7 day", "7-day", "one week", "1 week", "observe",
        ])
        return (0 if has_reduction and has_monitoring else 1,
                abs(int(obs["timestamp"]) - expected_ts))

    return min(candidates, key=rank)


# ── Individual checks ─────────────────────────────────────────────────────────
def check_0_input_files_exist() -> None:
    missing = [p for p in INPUT_FILES if not os.path.isfile(p)]
    if missing:
        check("0. input_files_exist", 1, False, "missing: " + ", ".join(missing))
    else:
        check("0. input_files_exist", 1, True)


def check_1_corn_plant_asset_exists() -> None:
    global _corn_asset_id
    try:
        rows = php_query(
            "SELECT id, name, type FROM asset_field_data "
            "WHERE type = 'plant' "
            "AND LOWER(TRIM(name)) = '2023 sweet corn planting 1' "
            "ORDER BY id DESC"
        )
        if rows:
            _corn_asset_id = int(rows[0]["id"])
            check("1. corn_plant_asset_exists", 1, True,
                  f"found: id={_corn_asset_id} name='{rows[0]['name']}'")
        else:
            check("1. corn_plant_asset_exists", 1, False,
                  "plant asset '2023 Sweet Corn Planting 1' not found")
    except Exception as e:
        check("1. corn_plant_asset_exists", 1, False, f"exception: {e}")


def check_2_emergency_observation_log() -> None:
    """Emergency Observation log on corn asset with 'High' severity and aphid keywords."""
    global _emergency_obs
    try:
        if _corn_asset_id is None:
            check("2. emergency_observation_severity_high", 2, False, "corn asset not found")
            return
        if not _obs_logs:
            check("2. emergency_observation_severity_high", 2, False,
                  "no observation logs on corn asset")
            return

        candidates = []
        for obs in _obs_logs:
            combined = _log_text(obs)
            if "high" in combined and any(
                kw in combined for kw in ["aphid", "pest", "infestation", "insect", "bug"]
            ):
                candidates.append(obs)

        def emergency_rank(obs: dict) -> tuple[int, int]:
            text = _log_text(obs)
            evidence_terms = ["emergency", "dense", "tassel", "leaf sheath", "shed skin"]
            return (sum(term in text for term in evidence_terms), int(obs["timestamp"]))

        best = max(candidates, key=emergency_rank) if candidates else None

        if best:
            _emergency_obs = best
            check("2. emergency_observation_severity_high", 2, True)
        else:
            check("2. emergency_observation_severity_high", 2, False,
                  "no observation log with 'High' severity + aphid keywords")
    except Exception as e:
        check("2. emergency_observation_severity_high", 2, False, f"exception: {e}")


def check_3_observation_photo_attached() -> None:
    """Obs1 has at least one image/file attachment."""
    try:
        if _emergency_obs is None:
            check("3. observation_photo_attached", 2, False, "emergency observation not found")
            return
        obs_id = int(_emergency_obs["id"])
        rows = php_query(
            "SELECT li.image_target_id "
            "FROM log__image li "
            f"WHERE li.entity_id = {obs_id} AND li.deleted = 0 "
            "LIMIT 1"
        )
        if rows:
            check("3. observation_photo_attached", 2, True)
            return
        rows_file = php_query(
            "SELECT lf.file_target_id, fm.filemime "
            "FROM log__file lf "
            "JOIN file_managed fm ON lf.file_target_id = fm.fid "
            f"WHERE lf.entity_id = {obs_id} AND lf.deleted = 0 "
            "AND (fm.filemime LIKE 'image/%' OR fm.filename LIKE '%.jpg' "
            "OR fm.filename LIKE '%.jpeg' OR fm.filename LIKE '%.png') "
            "LIMIT 1"
        )
        if rows_file:
            check("3. observation_photo_attached", 2, True)
        else:
            check("3. observation_photo_attached", 2, False,
                  "no image attached to emergency observation log")
    except Exception as e:
        check("3. observation_photo_attached", 2, False, f"exception: {e}")


def check_4_input_log_pyrethrin_omri() -> None:
    """Input log notes contain both 'Pyrethrin' and 'OMRI-2023-PY-001'."""
    global _treatment_input
    try:
        if _corn_asset_id is None:
            check("4. input_log_pyrethrin_omri", 2, False, "corn asset not found")
            return
        if not _input_logs:
            check("4. input_log_pyrethrin_omri", 2, False, "no input logs on corn asset")
            return
        candidates = []
        for inp in _input_logs:
            notes = _log_text(inp)
            has_pyrethrin = "pyrethrin" in notes
            has_cert = "omri-2023-py-001" in notes or "omri 2023 py 001" in notes
            if has_pyrethrin and has_cert:
                candidates.append(inp)
        if candidates:
            def treatment_rank(inp: dict) -> tuple[int, int]:
                text = _log_text(inp)
                detail_terms = ["200", "ml", "li shifu", "tractor-mounted boom sprayer"]
                return (sum(term in text for term in detail_terms), int(inp["timestamp"]))

            _treatment_input = max(candidates, key=treatment_rank)
            check("4. input_log_pyrethrin_omri", 2, True)
            return
        missing = []
        any_pyrethrin = any("pyrethrin" in strip_html(
            (i.get("notes__value") or "") + " " + (i.get("name") or "")
        ).lower() for i in _input_logs)
        any_omri = any("omri" in strip_html(
            (i.get("notes__value") or "") + " " + (i.get("name") or "")
        ).lower() for i in _input_logs)
        if not any_pyrethrin:
            missing.append("Pyrethrin")
        if not any_omri:
            missing.append("OMRI-2023-PY-001")
        check("4. input_log_pyrethrin_omri", 2, False,
              f"missing: {', '.join(missing)}" if missing else "values in different logs")
    except Exception as e:
        check("4. input_log_pyrethrin_omri", 2, False, f"exception: {e}")


def check_5_input_log_details() -> None:
    """Input log notes contain 200 mL/acre, Li Shifu, Tractor-Mounted Boom Sprayer."""
    try:
        if not _treatment_input:
            check("5. input_log_details", 2, False, "no input logs on corn asset")
            return
        inp = _treatment_input
        notes = strip_html(
            (inp.get("notes__value") or "") + " " + (inp.get("name") or "")
        ).lower()
        found_rate = "200" in notes and ("ml" in notes or "milliliter" in notes)
        found_operator = "li shifu" in notes or "li_shifu" in notes
        found_sprayer = "tractor-mounted boom sprayer" in notes

        if not found_operator:
            owner_rows = php_query(
                f"SELECT owner_target_id FROM log__owner "
                f"WHERE entity_id = {inp['id']} AND deleted = 0"
            )
            for ow in owner_rows:
                user_rows = php_query(
                    f"SELECT name FROM users_field_data "
                    f"WHERE uid = {ow['owner_target_id']}"
                )
                if any("li shifu" in (u.get("name") or "").lower() for u in user_rows):
                    found_operator = True
                    break

        missing = []
        if not found_rate:
            missing.append("200 mL/acre")
        if not found_operator:
            missing.append("Li Shifu")
        if not found_sprayer:
            missing.append("Tractor-Mounted Boom Sprayer")
        passed = not missing
        check("5. input_log_details", 2, passed,
              f"missing: {', '.join(missing)}" if missing else "")
    except Exception as e:
        check("5. input_log_details", 2, False, f"exception: {e}")


def check_6_log_chronological_order() -> None:
    """Obs1 <= Input <= Obs2 in timestamp order."""
    try:
        if _corn_asset_id is None:
            check("6. log_chronological_order", 2, False, "corn asset not found")
            return
        followup = _select_followup_observation()
        has_emergency = _emergency_obs is not None
        has_input = _treatment_input is not None
        if not has_emergency or not has_input or followup is None:
            detail_parts = []
            if not has_emergency:
                detail_parts.append("emergency observation not found")
            if not has_input:
                detail_parts.append("treatment input log not found")
            if followup is None:
                detail_parts.append("follow-up observation not found")
            check("6. log_chronological_order", 2, False, "; ".join(detail_parts))
            return

        obs1_ts = int(_emergency_obs["timestamp"])
        inp_ts = int(_treatment_input["timestamp"])
        obs2_ts = int(followup["timestamp"])
        order_ok = obs1_ts <= inp_ts <= obs2_ts
        check("6. log_chronological_order", 2, order_ok,
              "" if order_ok else
              f"expected Obs1({obs1_ts}) <= Input({inp_ts}) <= Obs2({obs2_ts})")
    except Exception as e:
        check("6. log_chronological_order", 2, False, f"exception: {e}")


def check_7_followup_dated_7_days() -> None:
    """Follow-up Observation is dated ~7 days after emergency observation (±2 day tolerance)."""
    global _followup_obs
    try:
        if _emergency_obs is None:
            check("7. followup_dated_7_days", 2, False,
                  "emergency observation not found")
            return
        candidate = _select_followup_observation()
        if candidate is None:
            check("7. followup_dated_7_days", 2, False,
                  "no follow-up observation after the emergency/treatment logs")
            return
        obs1_ts = int(_emergency_obs["timestamp"])
        obs2_ts = int(candidate["timestamp"])
        expected_ts = obs1_ts + 7 * 86400
        diff = abs(obs2_ts - expected_ts)
        tolerance = 2 * 86400
        if diff <= tolerance:
            _followup_obs = candidate
            actual_days = (obs2_ts - obs1_ts) / 86400
            check("7. followup_dated_7_days", 2, True, f"{actual_days:.1f} days apart")
        else:
            actual_days = (obs2_ts - obs1_ts) / 86400
            check("7. followup_dated_7_days", 2, False,
                  f"follow-up is {actual_days:.1f} days after emergency (expected ~7)")
    except Exception as e:
        check("7. followup_dated_7_days", 2, False, f"exception: {e}")


def check_8_followup_reduction_notes() -> None:
    """Follow-up Observation notes mention ~70% reduction and continued monitoring."""
    try:
        target = _followup_obs or _select_followup_observation()
        if target is None:
            check("8. followup_reduction_notes", 2, False, "follow-up observation not found")
            return
        notes = strip_html(
            (target.get("notes__value") or "") + " " + (target.get("name") or "")
        ).lower()
        has_reduction = any(kw in notes for kw in [
            "70%", "70 %", "seventy percent",
        ])
        has_monitoring = any(kw in notes for kw in [
            "monitor", "continu", "watch", "follow", "re-appl", "reappl",
            "inspect", "7 day", "7-day", "one week", "1 week", "observe",
        ])
        passed = has_reduction and has_monitoring
        detail_parts = []
        if not has_reduction:
            detail_parts.append("no ~70% reduction mentioned")
        if not has_monitoring:
            detail_parts.append("no continued monitoring mentioned")
        check("8. followup_reduction_notes", 2, passed,
              "; ".join(detail_parts) if detail_parts else "")
    except Exception as e:
        check("8. followup_reduction_notes", 2, False, f"exception: {e}")


def check_9_equipment_maintenance_log() -> None:
    """Equipment asset 'Tractor-Mounted Boom Sprayer' has a Maintenance log for post-spray cleaning."""
    try:
        rows = php_query(
            "SELECT id, name FROM asset_field_data "
            "WHERE type = 'equipment' "
            "AND LOWER(TRIM(name)) = 'tractor-mounted boom sprayer' "
            "LIMIT 1"
        )
        if not rows:
            check("9. equipment_maintenance_log", 2, False,
                  "equipment asset 'Tractor-Mounted Boom Sprayer' not found")
            return

        equip_id = int(rows[0]["id"])
        equip_name = rows[0]["name"]
        maint_rows = php_query(
            "SELECT l.id, l.type, l.name, l.notes__value "
            "FROM log_field_data l "
            "JOIN log__asset la ON l.id = la.entity_id AND la.deleted = 0 "
            f"WHERE la.asset_target_id = {equip_id} "
            "AND l.type = 'maintenance' "
            "ORDER BY l.id DESC LIMIT 5"
        )
        if not maint_rows:
            maint_rows = php_query(
                "SELECT l.id, l.type, l.name, l.notes__value "
                "FROM log_field_data l "
                "JOIN log__equipment le ON l.id = le.entity_id AND le.deleted = 0 "
                f"WHERE le.equipment_target_id = {equip_id} "
                "AND l.type = 'maintenance' "
                "ORDER BY l.id DESC LIMIT 5"
            )
        cleaning_logs = []
        for log in maint_rows:
            text = strip_html(
                (log.get("name") or "") + " " + (log.get("notes__value") or "")
            ).lower()
            if "water" in text and any(k in text for k in ["clean", "rinse", "wash"]):
                cleaning_logs.append(log)
        if cleaning_logs:
            check("9. equipment_maintenance_log", 2, True,
                  f"equipment='{equip_name}', cleaning_log={cleaning_logs[0]['id']}")
        else:
            check("9. equipment_maintenance_log", 2, False,
                  f"no water-cleaning maintenance log on equipment '{equip_name}'")
    except Exception as e:
        check("9. equipment_maintenance_log", 2, False, f"exception: {e}")


def check_10_cross_modal_obs1_vs_crop044() -> None:
    """Cross-modal: obs1 notes match close-up aphid image (crop_044.jpg)."""
    try:
        if _emergency_obs is None:
            check("10. cross_modal_obs1_vs_crop044", 2, False, "skipped: obs1 not found")
            return
        crop044_path = INPUT_FILES[1]
        if not os.path.isfile(crop044_path):
            check("10. cross_modal_obs1_vs_crop044", 2, False,
                  f"skipped: input file missing: {crop044_path}")
            return
        notes_clean = strip_html(_emergency_obs.get("notes__value") or "")
        if len(notes_clean) < 10:
            check("10. cross_modal_obs1_vs_crop044", 2, False,
                  "skipped: obs1 notes too short")
            return
        with open(crop044_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        passed, detail = llm_judge_vision(
            b64,
            "image/jpeg",
            notes_clean[:500],
            "The observation notes describe what is visible in this close-up crop image "
            "(pest damage, insect presence, or plant stress at the tassel/leaf area) and "
            "the severity assessment 'High' is consistent with the level of damage shown.",
        )
        check("10. cross_modal_obs1_vs_crop044", 2, passed, detail)
    except Exception as e:
        check("10. cross_modal_obs1_vs_crop044", 2, False, f"exception: {e}")


def check_11_llm_judge_dual_image_notes() -> None:
    """LLM judge: obs1 notes describe BOTH images (full-field + close-up) with severity 'High'."""
    try:
        if _emergency_obs is None:
            check("11. llm_judge_dual_image_notes", 3, False, "skipped: obs1 not found")
            return
        notes_clean = strip_html(_emergency_obs.get("notes__value") or "")
        if len(notes_clean) < 10:
            check("11. llm_judge_dual_image_notes", 3, False,
                  "skipped: obs1 notes too short")
            return
        condition = (
            "The notes describe observations from TWO different images or perspectives: "
            "(1) a full-field or wide-angle overview showing overall canopy/field condition "
            "where aphid density cannot be confirmed from distance, AND "
            "(2) a close-up image showing dense aphid/pest clustering at the tassel or "
            "leaf sheath area with visible damage or shed skins. "
            "The notes must conclude with a severity determination of 'High'. "
            "Both image observations (wide + close-up) must be referenced."
        )
        passed, detail = llm_judge(notes_clean, condition)
        check("11. llm_judge_dual_image_notes", 3, passed, detail)
    except Exception as e:
        check("11. llm_judge_dual_image_notes", 3, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_input_files_exist()
    check_1_corn_plant_asset_exists()
    try:
        _load_logs_for_corn_asset()
    except Exception as e:
        print(f"WARNING: log loading failed: {e}", file=sys.stderr)
    check_2_emergency_observation_log()
    check_3_observation_photo_attached()
    check_4_input_log_pyrethrin_omri()
    check_5_input_log_details()
    check_6_log_chronological_order()
    check_7_followup_dated_7_days()
    check_8_followup_reduction_notes()
    check_9_equipment_maintenance_log()
    check_10_cross_modal_obs1_vs_crop044()
    check_11_llm_judge_dual_image_notes()

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
