"""
Verifier for agriculture_016: Neem oil input log on garlic plant asset + maintenance log on equipment.

Checks: 10 weighted checks across farmos.
Strategy: docker exec php + SQLite inside farmos app container.

Required env vars:
  SERVER_HOSTNAME, FARMOS_PORT, FARMOS_CONTAINER.
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

for _var_name, _var_val in [
    ("FARMOS_PORT", FARMOS_PORT),
    ("FARMOS_CONTAINER", FARMOS_CONTAINER),
]:
    if not _var_val:
        print(f"FATAL: {_var_name} not set", file=sys.stderr)
        sys.exit(1)

SQLITE_DB = "/opt/drupal/web/sites/default/files/.ht.sqlite"

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


def farmos_sql(query: str) -> list[dict]:
    b64_sql = base64.b64encode(query.encode()).decode()
    php_code = (
        "$sql = base64_decode('" + b64_sql + "');"
        "$db = new SQLite3('" + SQLITE_DB + "');"
        "$r = $db->query($sql);"
        "if ($r === false) { echo json_encode([]); exit; }"
        "$rows = [];"
        "while ($row = $r->fetchArray(SQLITE3_ASSOC)) $rows[] = $row;"
        "echo json_encode($rows);"
    )
    rc, stdout, stderr = docker_exec(
        FARMOS_CONTAINER, "php", "-r", php_code, timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"php query error (rc={rc}): {stderr.strip()}")
    return json.loads(stdout) if stdout.strip() else []


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


# ── Cached state ──────────────────────────────────────────────────────────────
_input_log_id = 0
_input_log_notes = ""
_input_log_name = ""
_maint_log_id = 0
_maint_log_notes = ""
_maint_log_name = ""


# ── Individual checks ─────────────────────────────────────────────────────────
def check_1_input_log_on_plant_asset() -> None:
    """An input log exists that is linked to a plant asset (garlic)."""
    global _input_log_id, _input_log_name
    try:
        rows = farmos_sql(
            "SELECT lfd.id, lfd.name "
            "FROM log_field_data lfd "
            "JOIN log__asset la ON la.entity_id = lfd.id "
            "JOIN asset_field_data afd ON afd.id = la.asset_target_id "
            "WHERE lfd.type = 'input' "
            "AND afd.type = 'plant' "
            "AND LOWER(TRIM(afd.name)) = 'certified garlic plot' "
            "ORDER BY lfd.id DESC LIMIT 1"
        )
        if not rows:
            check("1. input_log_on_plant_asset", 2, False,
                  "no input log linked to plant asset 'Certified Garlic Plot'")
            return
        _input_log_id = int(rows[0]["id"])
        _input_log_name = rows[0].get("name", "")
        check("1. input_log_on_plant_asset", 2, True,
              f"log #{_input_log_id}: {_input_log_name}")
    except Exception as e:
        check("1. input_log_on_plant_asset", 2, False, f"exception: {e}")


def check_2_input_log_morning_note() -> None:
    """Input log notes contain 'applied during cooler morning hours to avoid leaf burn'."""
    global _input_log_notes
    try:
        if not _input_log_id:
            check("2. input_log_morning_note", 1, False, "no input log found")
            return
        rows = farmos_sql(
            f"SELECT notes__value FROM log_field_data WHERE id = {_input_log_id}"
        )
        raw = rows[0]["notes__value"] if rows and rows[0].get("notes__value") else ""
        _input_log_notes = _strip_html(raw)
        notes_lower = _input_log_notes.lower()
        passed = "cooler morning hours" in notes_lower or (
            "morning" in notes_lower and "leaf burn" in notes_lower
        )
        check("2. input_log_morning_note", 1, passed,
              "" if passed else f"note text (len={len(_input_log_notes)}) missing morning hours phrase")
    except Exception as e:
        check("2. input_log_morning_note", 1, False, f"exception: {e}")


def check_3_input_log_omri_cert() -> None:
    """Input log notes contain OMRI certification 'OMRI-2024-NO-007'."""
    try:
        if not _input_log_id:
            check("3. input_log_omri_cert", 1, False, "no input log found")
            return
        combined = (_input_log_notes + " " + _input_log_name).lower()
        passed = "omri-2024-no-007" in combined or "omri 2024 no 007" in combined
        if not passed:
            passed = "omri" in combined and "2024" in combined and "007" in combined
        check("3. input_log_omri_cert", 1, passed,
              "" if passed else "OMRI-2024-NO-007 not found in notes or name")
    except Exception as e:
        check("3. input_log_omri_cert", 1, False, f"exception: {e}")


def check_4_input_log_neem_oil() -> None:
    """Input log notes or name reference neem oil."""
    try:
        if not _input_log_id:
            check("4. input_log_neem_oil", 1, False, "no input log found")
            return
        combined = (_input_log_notes + " " + _input_log_name).lower()
        passed = "neem" in combined
        check("4. input_log_neem_oil", 1, passed,
              "" if passed else "no 'neem' reference in log name or notes")
    except Exception as e:
        check("4. input_log_neem_oil", 1, False, f"exception: {e}")


def check_5_input_log_rate_150() -> None:
    """Input log references rate of 150 mL/acre."""
    try:
        if not _input_log_id:
            check("5. input_log_rate_150", 1, False, "no input log found")
            return
        combined = (_input_log_notes + " " + _input_log_name).lower()
        passed = "150" in combined
        if not passed:
            qty_rows = farmos_sql(
                f"SELECT q.label, q.value__numerator, q.value__denominator "
                f"FROM log__quantity lq "
                f"JOIN quantity q ON q.id = lq.quantity_target_id "
                f"WHERE lq.entity_id = {_input_log_id}"
            )
            for row in qty_rows:
                try:
                    num = float(row.get("value__numerator", 0) or 0)
                    den = float(row.get("value__denominator", 1) or 1)
                    val = num / den if den else 0
                    if abs(val - 150) < 1:
                        passed = True
                        break
                except (ValueError, ZeroDivisionError):
                    pass
        check("5. input_log_rate_150", 1, passed,
              "" if passed else "rate 150 not found in notes or quantities")
    except Exception as e:
        check("5. input_log_rate_150", 1, False, f"exception: {e}")


def check_6_input_log_equipment_ref() -> None:
    """Input log references equipment 'Backpack Sprayer #2'."""
    try:
        if not _input_log_id:
            check("6. input_log_equipment_ref", 2, False, "no input log found")
            return
        equip_rows = farmos_sql(
            f"SELECT a.name FROM log__equipment le "
            f"JOIN asset_field_data a ON a.id = le.equipment_target_id "
            f"WHERE le.entity_id = {_input_log_id}"
        )
        if equip_rows:
            names = " ".join(r.get("name", "") for r in equip_rows).lower()
            passed = any(
                (r.get("name") or "").strip().lower() == "backpack sprayer #2"
                for r in equip_rows
            )
            check("6. input_log_equipment_ref", 2, passed,
                  f"equipment: {names}" if passed else f"equipment found but not sprayer: {names}")
            return

        check("6. input_log_equipment_ref", 2, False,
              "input log is not linked to equipment asset 'Backpack Sprayer #2'")
    except Exception as e:
        check("6. input_log_equipment_ref", 2, False, f"exception: {e}")


def check_7_maintenance_log_on_equipment() -> None:
    """A maintenance log exists linked to an equipment asset."""
    global _maint_log_id, _maint_log_name
    try:
        rows = farmos_sql(
            "SELECT lfd.id, lfd.name "
            "FROM log_field_data lfd "
            "JOIN log__asset la ON la.entity_id = lfd.id "
            "JOIN asset_field_data afd ON afd.id = la.asset_target_id "
            "WHERE lfd.type = 'maintenance' "
            "AND afd.type = 'equipment' "
            "AND LOWER(TRIM(afd.name)) = 'backpack sprayer #2' "
            "ORDER BY lfd.id DESC LIMIT 1"
        )
        if not rows:
            check("7. maintenance_log_on_equipment", 2, False,
                  "no maintenance log linked to 'Backpack Sprayer #2'")
            return
        _maint_log_id = int(rows[0]["id"])
        _maint_log_name = rows[0].get("name", "")
        check("7. maintenance_log_on_equipment", 2, True,
              f"log #{_maint_log_id}: {_maint_log_name}")
    except Exception as e:
        check("7. maintenance_log_on_equipment", 2, False, f"exception: {e}")


def check_8_maintenance_on_sprayer() -> None:
    """Maintenance log is linked to 'Backpack Sprayer #2' equipment asset."""
    try:
        if not _maint_log_id:
            check("8. maintenance_on_sprayer", 1, False, "no maintenance log found")
            return
        asset_rows = farmos_sql(
            f"SELECT a.name, a.type FROM log__asset la "
            f"JOIN asset_field_data a ON a.id = la.asset_target_id "
            f"WHERE la.entity_id = {_maint_log_id}"
        )
        if not asset_rows:
            check("8. maintenance_on_sprayer", 1, False,
                  "maintenance log has no linked assets")
            return
        names = " ".join(r.get("name", "") for r in asset_rows).lower()
        passed = any(
            (r.get("name") or "").strip().lower() == "backpack sprayer #2"
            for r in asset_rows
        )
        check("8. maintenance_on_sprayer", 1, passed,
              f"assets: {names}" if passed else f"linked to wrong asset: {names}")
    except Exception as e:
        check("8. maintenance_on_sprayer", 1, False, f"exception: {e}")


def check_9_maintenance_triple_rinse() -> None:
    """Maintenance log notes contain 'triple-rinse'."""
    global _maint_log_notes
    try:
        if not _maint_log_id:
            check("9. maintenance_triple_rinse", 1, False, "no maintenance log found")
            return
        rows = farmos_sql(
            f"SELECT notes__value FROM log_field_data WHERE id = {_maint_log_id}"
        )
        raw = rows[0]["notes__value"] if rows and rows[0].get("notes__value") else ""
        _maint_log_notes = _strip_html(raw)
        combined = (_maint_log_notes + " " + _maint_log_name).lower()
        passed = "triple-rinse" in combined or "triple rinse" in combined
        check("9. maintenance_triple_rinse", 1, passed,
              "" if passed else f"'triple-rinse' not found (notes len={len(_maint_log_notes)})")
    except Exception as e:
        check("9. maintenance_triple_rinse", 1, False, f"exception: {e}")


def check_10_maintenance_clean_water() -> None:
    """Maintenance log notes mention 'clean water'."""
    try:
        if not _maint_log_id:
            check("10. maintenance_clean_water", 1, False, "no maintenance log found")
            return
        combined = (_maint_log_notes + " " + _maint_log_name).lower()
        passed = "clean water" in combined
        check("10. maintenance_clean_water", 1, passed,
              "" if passed else "'clean water' not found in maintenance notes")
    except Exception as e:
        check("10. maintenance_clean_water", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_input_log_on_plant_asset()
    check_2_input_log_morning_note()
    check_3_input_log_omri_cert()
    check_4_input_log_neem_oil()
    check_5_input_log_rate_150()
    check_6_input_log_equipment_ref()
    check_7_maintenance_log_on_equipment()
    check_8_maintenance_on_sprayer()
    check_9_maintenance_triple_rinse()
    check_10_maintenance_clean_water()

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
