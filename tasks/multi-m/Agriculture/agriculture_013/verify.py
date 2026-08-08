"""
Verifier for agriculture_013: Dish photo analysis → Recipya recipe search/create → Grocy inventory check + recipe creation

Checks: 8 weighted checks (15 pts total) across grocy, recipya.
Strategy: docker exec sqlite3 for Recipya; docker exec php PDO for Grocy;
          llm_judge_vision for cross-modal consistency.

Required env vars:
  SERVER_HOSTNAME, GROCY_PORT, GROCY_CONTAINER, RECIPYA_PORT, RECIPYA_CONTAINER
"""

import base64
import json
import os
import subprocess
import sys

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

GROCY_PORT = os.getenv("GROCY_PORT")
GROCY_CONTAINER = os.getenv("GROCY_CONTAINER")
RECIPYA_PORT = os.getenv("RECIPYA_PORT")
RECIPYA_CONTAINER = os.getenv("RECIPYA_CONTAINER")

for var_name, var_val in [
    ("GROCY_PORT", GROCY_PORT),
    ("GROCY_CONTAINER", GROCY_CONTAINER),
    ("RECIPYA_PORT", RECIPYA_PORT),
    ("RECIPYA_CONTAINER", RECIPYA_CONTAINER),
]:
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

GROCY_DB = "/config/data/grocy.db"
RECIPYA_DB = "/root/.config/Recipya/Database/recipya.db"

_INPUTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "inputs"
)

INPUT_FILES = [
    os.path.join(_INPUTS_DIR, "recipya_recipe_545.jpg"),
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


_RECIPYA_DB_CACHE: str | None = None


def _get_recipya_db() -> str:
    global _RECIPYA_DB_CACHE
    if _RECIPYA_DB_CACHE and os.path.exists(_RECIPYA_DB_CACHE):
        return _RECIPYA_DB_CACHE
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_path = tmp.name
    tmp.close()
    r = subprocess.run(
        ["docker", "cp", f"{RECIPYA_CONTAINER}:{RECIPYA_DB}", tmp_path],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"docker cp failed (recipya db): {r.stderr.strip()}")
    _RECIPYA_DB_CACHE = tmp_path
    return tmp_path


def recipya_query(sql: str) -> list[dict]:
    import sqlite3 as _sqlite3
    db_path = _get_recipya_db()
    conn = _sqlite3.connect(db_path)
    conn.row_factory = _sqlite3.Row
    cur = conn.execute(sql)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def grocy_query(sql: str) -> list[dict]:
    php_code = (
        '$pdo = new PDO("sqlite:' + GROCY_DB + '");'
        "$pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);"
        "$stmt = $pdo->query(" + json.dumps(sql) + ");"
        "$rows = $stmt->fetchAll(PDO::FETCH_ASSOC);"
        "echo json_encode($rows);"
    )
    rc, out, err = docker_exec(GROCY_CONTAINER, "php", "-r", php_code)
    if rc != 0:
        raise RuntimeError(f"Grocy PHP query error: {err.strip()}")
    if not out.strip():
        return []
    return json.loads(out.strip())


def llm_judge_vision(
    image_path: str,
    recorded_value: str,
    condition: str,
    timeout: int = 45,
) -> tuple[bool, str]:
    import urllib.request

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
        payload = json.dumps({
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
        }).encode()
        req = urllib.request.Request(
            f"{api_base}/chat/completions",
            data=payload,
            headers={"Authorization": f"Bearer {api_key}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            answer = json.loads(resp.read())["choices"][0]["message"]["content"].strip().upper()
        return answer.startswith("YES"), answer
    except Exception as e:
        return False, f"llm_judge_vision error: {e}"


# ── Shared state (populated by earlier checks, used by later ones) ────────────
_recipya_recipe_name: str = ""
_recipya_recipe_id: int = -1
_recipya_ingredients: list[str] = []

DISH_KEYWORDS = [
    "ratatouille", "vegetable", "stew", "zucchini", "courgette",
    "tomato", "pepper", "provenc", "french", "aubergine", "eggplant",
]


def _find_grocy_recipe() -> dict | None:
    """Find the Grocy recipe created for this task by name match or keyword fallback."""
    recipes = grocy_query(
        "SELECT id, name, description, picture_file_name FROM recipes WHERE id > 0"
    )

    if _recipya_recipe_name:
        recipya_lower = _recipya_recipe_name.lower().strip()
        for r in recipes:
            if (r.get("name") or "").lower().strip() == recipya_lower:
                return r
        for r in recipes:
            gname = (r.get("name") or "").lower()
            if recipya_lower in gname or gname in recipya_lower:
                return r
        stop = {"a", "the", "of", "and", "with", "in", "on", "for", "to", "de", "la", "le"}
        recipya_words = set(recipya_lower.split()) - stop
        if recipya_words:
            for r in recipes:
                gwords = set((r.get("name") or "").lower().split()) - stop
                if recipya_words & gwords:
                    return r

    for r in recipes:
        name = (r.get("name") or "").lower()
        if any(kw in name for kw in DISH_KEYWORDS):
            return r

    return None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_0_input_files_exist() -> None:
    missing = [p for p in INPUT_FILES if not os.path.isfile(p)]
    if missing:
        check("0. input_files_exist", 1, False, "missing: " + ", ".join(missing))
    else:
        check("0. input_files_exist", 1, True)


def check_1_recipya_recipe_exists() -> None:
    """A recipe related to the dish photo exists in Recipya (matched or newly created)."""
    global _recipya_recipe_name, _recipya_recipe_id
    try:
        rows = recipya_query(
            "SELECT r.id, r.name FROM recipes r "
            "JOIN user_recipe ur ON ur.recipe_id = r.id "
            "ORDER BY r.id DESC;"
        )
        if not rows:
            check("1. recipya_recipe_exists", 2, False, "no recipes found in Recipya")
            return

        best_match = None
        for row in rows:
            name_lower = row["name"].lower()
            if any(kw in name_lower for kw in DISH_KEYWORDS):
                best_match = row
                break

        if not best_match:
            best_match = rows[0]

        _recipya_recipe_id = int(best_match["id"])
        _recipya_recipe_name = best_match["name"]
        check("1. recipya_recipe_exists", 2, True,
              f"recipe id={_recipya_recipe_id} name='{_recipya_recipe_name}'")
    except Exception as e:
        check("1. recipya_recipe_exists", 2, False, f"exception: {e}")


def check_2_recipya_recipe_min_ingredients() -> None:
    """The confirmed/created Recipya recipe has at least 5 ingredients."""
    global _recipya_ingredients
    try:
        if _recipya_recipe_id < 0:
            check("2. recipya_min_5_ingredients", 1, False, "no recipe identified in check 1")
            return
        rows = recipya_query(
            f"SELECT i.name FROM ingredient_recipe ir "
            f"JOIN ingredients i ON i.id = ir.ingredient_id "
            f"WHERE ir.recipe_id = {_recipya_recipe_id} "
            f"ORDER BY ir.ingredient_order;"
        )
        _recipya_ingredients = [r["name"] for r in rows]
        count = len(_recipya_ingredients)
        passed = count >= 5
        check("2. recipya_min_5_ingredients", 1, passed,
              f"{count} ingredients: {', '.join(_recipya_ingredients[:8])}")
    except Exception as e:
        check("2. recipya_min_5_ingredients", 1, False, f"exception: {e}")


def check_3_cross_modal_recipe_matches_photo() -> None:
    """llm_judge_vision: The Recipya recipe's ingredient list semantically matches
    the dish visible in the photo."""
    try:
        if not _recipya_ingredients:
            check("3. cross_modal_recipe_vs_photo", 3, False,
                  "skipped: no ingredients from check 2")
            return
        if not os.path.isfile(INPUT_FILES[0]):
            check("3. cross_modal_recipe_vs_photo", 3, False, "skipped: input file missing")
            return

        ingredient_str = ", ".join(_recipya_ingredients)
        recipe_desc = f"Recipe name: {_recipya_recipe_name}. Ingredients: {ingredient_str}"
        condition = (
            "The recipe's ingredient list is semantically consistent with the dish visible "
            "in the photo. The ingredients should be appropriate for the type of dish shown "
            "(a vegetable-based dish such as ratatouille, vegetable stew, or similar)."
        )
        passed, detail = llm_judge_vision(INPUT_FILES[0], recipe_desc, condition)
        check("3. cross_modal_recipe_vs_photo", 3, passed, detail)
    except Exception as e:
        check("3. cross_modal_recipe_vs_photo", 3, False, f"exception: {e}")


def check_4_grocy_shopping_list_bistrot() -> None:
    """Grocy shopping list contains at least 1 item with a note mentioning 'Bistrot Provençal'."""
    try:
        rows = grocy_query(
            "SELECT sl.id, sl.product_id, sl.amount, sl.note, "
            "COALESCE(p.name, '') AS product_name "
            "FROM shopping_list sl "
            "LEFT JOIN products p ON sl.product_id = p.id"
        )
        matching = []
        for item in rows:
            note = (item.get("note") or "").lower()
            if "bistrot" in note or "provençal" in note or "provencal" in note:
                matching.append(item)

        if matching:
            previews = [
                f"{m.get('product_name', '?')}: {(m.get('note') or '')[:60]}"
                for m in matching[:3]
            ]
            check("4. grocy_shopping_list_bistrot", 2, True,
                  f"{len(matching)} item(s): {'; '.join(previews)}")
        else:
            check("4. grocy_shopping_list_bistrot", 2, False,
                  f"no shopping list item mentions 'Bistrot Provençal' ({len(rows)} total items)")
    except Exception as e:
        check("4. grocy_shopping_list_bistrot", 2, False, f"exception: {e}")


def check_5_grocy_recipe_exists() -> None:
    """A Grocy recipe exists whose name matches the Recipya recipe name."""
    try:
        recipe = _find_grocy_recipe()
        if recipe:
            check("5. grocy_recipe_exists", 2, True,
                  f"id={recipe['id']} name='{recipe['name']}'")
        else:
            all_recipes = grocy_query("SELECT id, name FROM recipes WHERE id > 0")
            names = [r["name"] for r in all_recipes[:5]]
            check("5. grocy_recipe_exists", 2, False,
                  f"no match for '{_recipya_recipe_name}'; existing: {names}")
    except Exception as e:
        check("5. grocy_recipe_exists", 2, False, f"exception: {e}")


def check_6_grocy_recipe_has_ingredients() -> None:
    """The Grocy recipe has a non-empty ingredient list (recipes_pos)."""
    try:
        recipe = _find_grocy_recipe()
        if not recipe:
            check("6. grocy_recipe_has_ingredients", 2, False, "no Grocy recipe found")
            return

        recipe_id = recipe["id"]
        positions = grocy_query(
            f"SELECT rp.id, rp.product_id, rp.amount, rp.note, "
            f"COALESCE(p.name, '') AS product_name "
            f"FROM recipes_pos rp "
            f"LEFT JOIN products p ON rp.product_id = p.id "
            f"WHERE rp.recipe_id = {recipe_id}"
        )
        count = len(positions)
        if count > 0:
            names = [p.get("product_name") or p.get("note", "?") for p in positions[:5]]
            check("6. grocy_recipe_has_ingredients", 2, True,
                  f"recipe '{recipe['name']}' has {count} ingredient(s): {', '.join(names)}")
        else:
            check("6. grocy_recipe_has_ingredients", 2, False,
                  f"recipe '{recipe['name']}' (id={recipe_id}) has 0 ingredients")
    except Exception as e:
        check("6. grocy_recipe_has_ingredients", 2, False, f"exception: {e}")


def check_7_grocy_recipe_has_image() -> None:
    """The Grocy recipe has a non-empty picture_file_name (image attachment)."""
    try:
        recipe = _find_grocy_recipe()
        if not recipe:
            check("7. grocy_recipe_has_image", 2, False, "no Grocy recipe found")
            return

        pic = (recipe.get("picture_file_name") or "").strip()
        if pic:
            check("7. grocy_recipe_has_image", 2, True,
                  f"recipe '{recipe['name']}' has picture: {pic[:50]}")
        else:
            check("7. grocy_recipe_has_image", 2, False,
                  f"recipe '{recipe['name']}' (id={recipe['id']}) has no picture_file_name")
    except Exception as e:
        check("7. grocy_recipe_has_image", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_input_files_exist()
    check_1_recipya_recipe_exists()
    check_2_recipya_recipe_min_ingredients()
    check_3_cross_modal_recipe_matches_photo()
    check_4_grocy_shopping_list_bistrot()
    check_5_grocy_recipe_exists()
    check_6_grocy_recipe_has_ingredients()
    check_7_grocy_recipe_has_image()

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
