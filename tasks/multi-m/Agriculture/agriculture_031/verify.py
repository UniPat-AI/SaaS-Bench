"""
Verifier for agriculture_031: Chef dish photo → Recipya recipe lookup/create →
Grocy product+stock → FarmOS harvest log OMRI annotation → update Grocy product
description.

Checks: 10 weighted checks across recipya, grocy, farmos.
Strategy: host-side SQLite via docker cp (recipya, grocy), authenticated FarmOS
JSON:API session (web login; anonymous access sees no log data), llm_judge_vision.

Required env vars:
  SERVER_HOSTNAME, RECIPYA_PORT, RECIPYA_CONTAINER,
  GROCY_PORT, GROCY_CONTAINER,
  FARMOS_PORT, FARMOS_CONTAINER
"""

import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

RECIPYA_PORT = os.environ.get("RECIPYA_PORT")
RECIPYA_CONTAINER = os.environ.get("RECIPYA_CONTAINER")
GROCY_PORT = os.environ.get("GROCY_PORT")
GROCY_CONTAINER = os.environ.get("GROCY_CONTAINER")
FARMOS_PORT = os.environ.get("FARMOS_PORT")
FARMOS_CONTAINER = os.environ.get("FARMOS_CONTAINER")

for var in ("RECIPYA_PORT", "RECIPYA_CONTAINER", "GROCY_PORT", "GROCY_CONTAINER",
            "FARMOS_PORT", "FARMOS_CONTAINER"):
    if not os.environ.get(var):
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

RECIPYA_DB = "/root/.config/Recipya/Database/recipya.db"
GROCY_DB = "/config/data/grocy.db"

_INPUTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "inputs"
)

INPUT_FILES = [
    os.path.join(_INPUTS_DIR, "recipya_recipe_006.jpg"),
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


def _sqlite_via_docker_cp(
    container: str, db_path: str, query: str, timeout: int = 20
) -> str:
    """Query a SQLite DB inside a container that has no sqlite3 CLI.

    Copies the DB file (plus -wal/-shm when present) to a host temp dir via
    ``docker cp`` and runs the query with the host Python's sqlite3 module.
    Output mimics ``sqlite3 -separator "|"``: columns joined by "|", NULL as
    empty string, one row per line, no header.
    """
    with tempfile.TemporaryDirectory(prefix="verify_sqlite_") as tmpdir:
        local_db = os.path.join(tmpdir, os.path.basename(db_path))
        r = subprocess.run(
            ["docker", "cp", f"{container}:{db_path}", local_db],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"docker cp {container}:{db_path} failed: {r.stderr.strip()}"
            )
        # Best effort: copy WAL/SHM so un-checkpointed data is visible too.
        for suffix in ("-wal", "-shm"):
            subprocess.run(
                ["docker", "cp", f"{container}:{db_path}{suffix}", local_db + suffix],
                capture_output=True, text=True, errors="replace", timeout=timeout,
            )
        try:
            conn = sqlite3.connect(local_db)
            try:
                rows = conn.execute(query).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as e:
            raise RuntimeError(f"sqlite3 query on {db_path} failed: {e}")
    lines = []
    for row in rows:
        lines.append("|".join(
            "" if v is None
            else v.decode("utf-8", "replace") if isinstance(v, bytes)
            else str(v)
            for v in row
        ))
    return "\n".join(lines).strip()


def recipya_sql(query: str) -> str:
    try:
        return _sqlite_via_docker_cp(RECIPYA_CONTAINER, RECIPYA_DB, query)
    except Exception as e:
        raise RuntimeError(f"recipya sql error: {e}")


def grocy_sql(query: str) -> str:
    try:
        return _sqlite_via_docker_cp(GROCY_CONTAINER, GROCY_DB, query)
    except Exception as e:
        raise RuntimeError(f"grocy sql error: {e}")


def _farmos_session() -> "requests.Session":
    """Authenticated FarmOS session.

    FarmOS JSON:API hides log/asset data from anonymous and HTTP-basic
    requests, so authenticate through the web-login form and reuse the
    resulting session cookie.
    """
    global _FARMOS_SESSION
    if _FARMOS_SESSION is not None:
        return _FARMOS_SESSION
    s = requests.Session()
    base = f"http://{HOST}:{FARMOS_PORT}"
    try:
        login_page = s.get(f"{base}/user/login", timeout=15)
        m = re.search(r'name="form_build_id" value="([^"]+)"', login_page.text)
        s.post(
            f"{base}/user/login",
            data={
                "name": os.getenv("FARMOS_USER", "admin"),
                "pass": os.getenv("FARMOS_PASS", "admin123456"),
                "form_build_id": m.group(1) if m else "",
                "form_id": "user_login_form",
                "op": "Log in",
            },
            timeout=15,
        )
    except Exception:
        pass
    _FARMOS_SESSION = s
    return s


_FARMOS_SESSION: "requests.Session | None" = None


def farmos_api_get(path: str) -> dict:
    url = f"http://{HOST}:{FARMOS_PORT}{path}"
    resp = _farmos_session().get(url, timeout=15)
    resp.raise_for_status()
    return resp.json()


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
        resp = requests.post(
            f"{api_base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
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
    image_path: str,
    recorded_value: str,
    condition: str,
    timeout: int = 45,
) -> tuple[bool, str]:
    api_base = os.getenv("MINDRA_BASE_URL", "https://api.mindracode.com/v1")
    api_key = os.getenv("MINDRA_API_KEY", "")
    ext = os.path.splitext(image_path)[1].lower()
    mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "gif": "image/gif",
            "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    if not os.path.isfile(image_path):
        return False, f"image not found: {image_path}"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    prompt = (
        f"You are given an image and a value that an AI agent extracted from it.\n"
        f"Recorded value: «{recorded_value}»\n"
        f"Condition: {condition}\n\n"
        f"Does the recorded value accurately match the information visible in the image, "
        f"satisfying the condition above?\n"
        f"Answer only YES or NO."
    )
    try:
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
                         "image_url": {"url": f"data:{mime};base64,{b64}"}},
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


# ── Individual checks ─────────────────────────────────────────────────────────

def check_0_input_files_exist() -> None:
    missing = [p for p in INPUT_FILES if not os.path.isfile(p)]
    if missing:
        check("0. input_files_exist", 1, False, "missing: " + ", ".join(missing))
    else:
        check("0. input_files_exist", 1, True)


def check_1_recipe_exists_in_recipya() -> str | None:
    """The recipe 'Beef and Broccoli Stir-Fry' (created per the task) must exist in Recipya."""
    try:
        rows = recipya_sql(
            "SELECT id, name FROM recipes "
            "WHERE LOWER(TRIM(name)) = 'beef and broccoli stir-fry';"
        )
        if rows:
            first = rows.split("\n")[0]
            rid, rname = first.split("|", 1)
            check("1. recipe_exists_in_recipya", 2, True, f"id={rid} name={rname}")
            return rid
        near = recipya_sql(
            "SELECT id, name FROM recipes WHERE LOWER(name) LIKE '%broccoli%';"
        )
        check("1. recipe_exists_in_recipya", 2, False,
              "recipe 'Beef and Broccoli Stir-Fry' not found"
              + (f"; other broccoli recipes: {near.split(chr(10))[:3]}" if near else ""))
        return None
    except Exception as e:
        check("1. recipe_exists_in_recipya", 2, False, f"exception: {e}")
        return None


def check_2_cross_modal_recipe_matches_image(recipe_name: str | None) -> None:
    """The recipe found in Recipya should match the dish visible in the photo."""
    if not recipe_name:
        check("2. cross_modal_recipe_matches_image", 2, False,
              "skipped: no recipe found to validate")
        return
    if not os.path.isfile(INPUT_FILES[0]):
        check("2. cross_modal_recipe_matches_image", 2, False,
              "skipped: input file missing")
        return
    try:
        passed, raw = llm_judge_vision(
            INPUT_FILES[0],
            recipe_name,
            "The dish visible in the photo is consistent with a recipe named "
            f"'{recipe_name}'. The photo should show a dish that could reasonably "
            "be described by this recipe name.",
        )
        check("2. cross_modal_recipe_matches_image", 2, passed, raw)
    except Exception as e:
        check("2. cross_modal_recipe_matches_image", 2, False, f"exception: {e}")


def check_3_broccoli_ingredient_in_recipe(recipe_id: str | None) -> None:
    """Broccoli must be an ingredient of the identified recipe."""
    if not recipe_id:
        check("3. broccoli_ingredient_in_recipe", 1, False,
              "skipped: no recipe found")
        return
    try:
        rows = recipya_sql(
            f"SELECT i.name FROM ingredient_recipe ir "
            f"JOIN ingredients i ON i.id = ir.ingredient_id "
            f"WHERE ir.recipe_id = {recipe_id} AND LOWER(i.name) LIKE '%broccoli%';"
        )
        if rows:
            check("3. broccoli_ingredient_in_recipe", 1, True, rows.split("\n")[0])
        else:
            all_ing = recipya_sql(
                f"SELECT i.name FROM ingredient_recipe ir "
                f"JOIN ingredients i ON i.id = ir.ingredient_id "
                f"WHERE ir.recipe_id = {recipe_id};"
            )
            check("3. broccoli_ingredient_in_recipe", 1, False,
                  f"broccoli not found; ingredients: {all_ing[:200]}")
    except Exception as e:
        check("3. broccoli_ingredient_in_recipe", 1, False, f"exception: {e}")


def check_4_broccoli_product_in_grocy() -> str | None:
    """The product named exactly 'Broccoli' (created per the task) must exist in Grocy."""
    try:
        rows = grocy_sql(
            "SELECT id, name FROM products WHERE LOWER(TRIM(name)) = 'broccoli';"
        )
        if rows:
            first = rows.split("\n")[0]
            pid, pname = first.split("|", 1)
            check("4. broccoli_product_in_grocy", 1, True, f"id={pid} name={pname}")
            return pid
        check("4. broccoli_product_in_grocy", 1, False,
              "no product named exactly 'Broccoli' found in grocy")
        return None
    except Exception as e:
        check("4. broccoli_product_in_grocy", 1, False, f"exception: {e}")
        return None


def check_5_broccoli_in_grocy_stock(product_id: str | None) -> None:
    """Broccoli must have stock entries in Grocy."""
    if not product_id:
        check("5. broccoli_in_grocy_stock", 1, False,
              "skipped: no broccoli product found")
        return
    try:
        rows = grocy_sql(
            f"SELECT SUM(amount) FROM stock WHERE product_id = {product_id};"
        )
        amount = float(rows) if rows and rows != "" else 0.0
        if amount > 0:
            check("5. broccoli_in_grocy_stock", 1, True, f"stock amount={amount}")
        else:
            check("5. broccoli_in_grocy_stock", 1, False,
                  f"stock amount is 0 or empty for product {product_id}")
    except Exception as e:
        check("5. broccoli_in_grocy_stock", 1, False, f"exception: {e}")


TARGET_LOG_NAME = "2024 broccoli harvest — north field east bed (side shoots)"
EXPECTED_OMRI = "OMRI-ORG-2024-1187"


def check_6_farmos_harvest_log_exists() -> dict | None:
    """The broccoli harvest log named in the task must exist in FarmOS."""
    try:
        data = farmos_api_get("/api/log/harvest")
        logs = data.get("data", [])
        broccoli_logs = []
        for log in logs:
            attrs = log.get("attributes", {})
            name = (attrs.get("name") or "").lower()
            notes_val = ""
            notes = attrs.get("notes")
            if isinstance(notes, dict):
                notes_val = (notes.get("value") or "").lower()
            elif isinstance(notes, str):
                notes_val = notes.lower()
            if "broccoli" in name or "broccoli" in notes_val:
                broccoli_logs.append(log)
        if broccoli_logs:
            named = [
                log for log in broccoli_logs
                if (log.get("attributes", {}).get("name") or "").strip().lower()
                == TARGET_LOG_NAME
            ]
            if named:
                latest = named[0]
            else:
                broccoli_logs.sort(
                    key=lambda x: x.get("attributes", {}).get("timestamp", ""),
                    reverse=True,
                )
                latest = broccoli_logs[0]
            log_name = latest.get("attributes", {}).get("name", "?")
            check("6. farmos_harvest_log_exists", 2, True,
                  f"found: {log_name}")
            return latest
        check("6. farmos_harvest_log_exists", 2, False,
              "no harvest log mentioning broccoli found in FarmOS")
        return None
    except Exception as e:
        check("6. farmos_harvest_log_exists", 2, False, f"exception: {e}")
        return None


def check_7_farmos_omri_cert_in_log(harvest_log: dict | None) -> str | None:
    """The FarmOS harvest log notes must contain the OMRI certification
    number given in the task (OMRI-ORG-2024-1187)."""
    if not harvest_log:
        check("7. farmos_omri_cert_number", 2, False,
              "skipped: no harvest log found")
        return None
    try:
        attrs = harvest_log.get("attributes", {})
        notes = attrs.get("notes")
        notes_text = ""
        if isinstance(notes, dict):
            notes_text = notes.get("value") or ""
        elif isinstance(notes, str):
            notes_text = notes
        name = attrs.get("name") or ""
        search_text = f"{name} {notes_text}"
        if EXPECTED_OMRI in search_text:
            check("7. farmos_omri_cert_number", 2, True,
                  f"OMRI cert: {EXPECTED_OMRI}")
            return EXPECTED_OMRI
        check("7. farmos_omri_cert_number", 2, False,
              f"{EXPECTED_OMRI} not found in log notes (first 200 chars: {search_text[:200]})")
        return None
    except Exception as e:
        check("7. farmos_omri_cert_number", 2, False, f"exception: {e}")
        return None


def check_8_grocy_description_has_recipe_id(
    product_id: str | None, recipe_id: str | None
) -> None:
    """Grocy product description must contain the Recipya Recipe ID."""
    if not product_id:
        check("8. grocy_desc_has_recipe_id", 2, False,
              "skipped: no broccoli product in grocy")
        return
    if not recipe_id:
        check("8. grocy_desc_has_recipe_id", 2, False,
              "skipped: no recipe ID found")
        return
    try:
        desc = grocy_sql(
            f"SELECT description FROM products WHERE id = {product_id};"
        )
        if not desc:
            check("8. grocy_desc_has_recipe_id", 2, False,
                  "product description is empty")
            return
        if recipe_id in desc or f"Recipe ID" in desc or f"recipe" in desc.lower():
            id_found = recipe_id in desc
            if id_found:
                check("8. grocy_desc_has_recipe_id", 2, True,
                      f"recipe ID {recipe_id} found in description")
            else:
                passed_llm, reason = llm_judge(
                    desc,
                    f"The text contains a reference to Recipya Recipe ID {recipe_id}.",
                )
                check("8. grocy_desc_has_recipe_id", 2, passed_llm,
                      f"llm_judge: {reason}")
        else:
            check("8. grocy_desc_has_recipe_id", 2, False,
                  f"recipe ID {recipe_id} not found in description: {desc[:200]}")
    except Exception as e:
        check("8. grocy_desc_has_recipe_id", 2, False, f"exception: {e}")


def check_9_grocy_description_has_omri_cert(
    product_id: str | None, omri_cert: str | None
) -> None:
    """Grocy product description must contain the OMRI certification number from FarmOS."""
    if not product_id:
        check("9. grocy_desc_has_omri_cert", 3, False,
              "skipped: no broccoli product in grocy")
        return
    if not omri_cert:
        check("9. grocy_desc_has_omri_cert", 3, False,
              "skipped: no OMRI cert found in FarmOS")
        return
    try:
        desc = grocy_sql(
            f"SELECT description FROM products WHERE id = {product_id};"
        )
        if not desc:
            check("9. grocy_desc_has_omri_cert", 3, False,
                  "product description is empty")
            return
        omri_digits = re.sub(r'[^A-Za-z0-9]', '', omri_cert)
        desc_normalized = re.sub(r'[^A-Za-z0-9]', '', desc)
        if omri_digits in desc_normalized or omri_cert in desc:
            check("9. grocy_desc_has_omri_cert", 3, True,
                  f"OMRI cert '{omri_cert}' found in description")
        elif "omri" in desc.lower():
            passed_llm, reason = llm_judge(
                desc,
                f"The text contains the OMRI certification number '{omri_cert}' "
                f"or an equivalent reference to it.",
            )
            check("9. grocy_desc_has_omri_cert", 3, passed_llm,
                  f"llm_judge: {reason}")
        else:
            check("9. grocy_desc_has_omri_cert", 3, False,
                  f"OMRI cert '{omri_cert}' not found in description: {desc[:200]}")
    except Exception as e:
        check("9. grocy_desc_has_omri_cert", 3, False, f"exception: {e}")


def check_10_cross_modal_grocy_desc_consistent(product_id: str | None) -> None:
    """The updated Grocy description must be consistent with data derivable from the dish image."""
    if not product_id:
        check("10. cross_modal_grocy_desc", 2, False,
              "skipped: no broccoli product in grocy")
        return
    if not os.path.isfile(INPUT_FILES[0]):
        check("10. cross_modal_grocy_desc", 2, False,
              "skipped: input file missing")
        return
    try:
        desc = grocy_sql(
            f"SELECT description FROM products WHERE id = {product_id};"
        )
        if not desc or len(desc) < 5:
            check("10. cross_modal_grocy_desc", 2, False,
                  f"product description too short or empty: '{desc}'")
            return
        passed, raw = llm_judge_vision(
            INPUT_FILES[0],
            desc,
            "The product description references information that is consistent with "
            "the dish shown in the photo. The photo shows a dish that uses broccoli "
            "as a primary green vegetable, and the description contains traceability "
            "data (recipe ID, certification number) that could have been derived from "
            "a workflow starting with this dish photo.",
        )
        check("10. cross_modal_grocy_desc", 2, passed, raw)
    except Exception as e:
        check("10. cross_modal_grocy_desc", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_input_files_exist()
    recipe_id = check_1_recipe_exists_in_recipya()
    recipe_name = None
    if recipe_id:
        try:
            recipe_name = recipya_sql(
                f"SELECT name FROM recipes WHERE id = {recipe_id};"
            )
        except Exception:
            pass
    check_2_cross_modal_recipe_matches_image(recipe_name)
    check_3_broccoli_ingredient_in_recipe(recipe_id)
    grocy_product_id = check_4_broccoli_product_in_grocy()
    check_5_broccoli_in_grocy_stock(grocy_product_id)
    harvest_log = check_6_farmos_harvest_log_exists()
    omri_cert = check_7_farmos_omri_cert_in_log(harvest_log)
    check_8_grocy_description_has_recipe_id(grocy_product_id, recipe_id)
    check_9_grocy_description_has_omri_cert(grocy_product_id, omri_cert)
    check_10_cross_modal_grocy_desc_consistent(grocy_product_id)

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
