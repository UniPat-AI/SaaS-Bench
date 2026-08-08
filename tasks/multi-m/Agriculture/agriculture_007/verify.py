"""
Verifier for agriculture_007: Cross-reference Recipya tomato recipe ingredients against Grocy stock,
add missing auxiliary ingredients to Grocy shopping list with recipe name in notes.

Checks: 8 weighted checks (13 pts total) across grocy + recipya.
Strategy: docker exec sqlite3 (recipya), docker exec php PDO (grocy — no sqlite3 CLI).

Required env vars:
  SERVER_HOSTNAME, GROCY_PORT, GROCY_CONTAINER, RECIPYA_PORT, RECIPYA_CONTAINER
"""

import os
import sys
import subprocess
import json
import re
import html as _html

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")
GROCY_PORT = os.getenv("GROCY_PORT")
GROCY_CONTAINER = os.getenv("GROCY_CONTAINER")
RECIPYA_PORT = os.getenv("RECIPYA_PORT")
RECIPYA_CONTAINER = os.getenv("RECIPYA_CONTAINER")

for _var, _val in [
    ("GROCY_PORT", GROCY_PORT),
    ("GROCY_CONTAINER", GROCY_CONTAINER),
    ("RECIPYA_PORT", RECIPYA_PORT),
    ("RECIPYA_CONTAINER", RECIPYA_CONTAINER),
]:
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

GROCY_DB = "/config/data/grocy.db"
RECIPYA_DB = "/root/.config/Recipya/Database/recipya.db"

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


def normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


# ── Data fetchers ─────────────────────────────────────────────────────────────
def get_recipya_tomato_recipes() -> list[dict]:
    sql = (
        "SELECT DISTINCT r.id, r.name FROM recipes r "
        "JOIN ingredient_recipe ir ON ir.recipe_id = r.id "
        "JOIN ingredients i ON i.id = ir.ingredient_id "
        "WHERE LOWER(r.name) LIKE '%tomato%' "
        "OR LOWER(r.name) LIKE '%cann%' "
        "OR LOWER(r.name) LIKE '%preserv%' "
        "OR LOWER(r.name) LIKE '%sauce%' "
        "OR LOWER(i.name) LIKE '%tomato%';"
    )
    return recipya_query(sql)


def get_recipe_ingredients(recipe_id: int) -> list[str]:
    sql = (
        f"SELECT i.name FROM ingredient_recipe ir "
        f"JOIN ingredients i ON i.id = ir.ingredient_id "
        f"WHERE ir.recipe_id = {recipe_id} "
        f"ORDER BY ir.ingredient_order;"
    )
    rows = recipya_query(sql)
    return [r["name"] for r in rows]


def get_all_recipya_recipe_names() -> dict[str, int]:
    rows = recipya_query("SELECT id, name FROM recipes;")
    # Recipya stores names HTML-escaped (e.g. "Penne Al&#39;Arrabbiato");
    # unescape so a plain "Penne Al'Arrabbiato" note still matches.
    return {_html.unescape(r["name"]): r["id"] for r in rows}


def get_grocy_shopping_list() -> list[dict]:
    sql = (
        "SELECT sl.id, sl.product_id, sl.amount, sl.note, "
        "COALESCE(p.name, '') AS product_name "
        "FROM shopping_list sl "
        "LEFT JOIN products p ON sl.product_id = p.id"
    )
    return grocy_query(sql)


def get_grocy_stock_product_names() -> set[str]:
    sql = (
        "SELECT DISTINCT p.name FROM stock s "
        "JOIN products p ON s.product_id = p.id "
        "WHERE s.amount > 0"
    )
    rows = grocy_query(sql)
    return {r["name"] for r in rows}


def get_grocy_product_names() -> set[str]:
    rows = grocy_query("SELECT name FROM products")
    return {r["name"] for r in rows}


def extract_recipe_name_from_note(note: str, known_recipes: dict[str, int]) -> str | None:
    if not note:
        return None
    for rname in sorted(known_recipes.keys(), key=len, reverse=True):
        if rname.lower() in note.lower():
            return rname
    return None


def ingredient_matches_product(ingredient: str, product: str) -> bool:
    ing_norm = normalize(ingredient)
    prod_norm = normalize(product)
    if not ing_norm or not prod_norm:
        return False
    return prod_norm in ing_norm or ing_norm in prod_norm


def is_tomato_ingredient(name: str) -> bool:
    return "tomato" in name.lower()


# ── Individual checks ─────────────────────────────────────────────────────────
def check_1_recipya_has_tomato_recipe() -> list[dict]:
    """Recipya has at least one recipe involving tomatoes."""
    try:
        recipes = get_recipya_tomato_recipes()
        names = [r["name"] for r in recipes[:5]]
        check("1. recipya_has_tomato_recipe", 1, len(recipes) > 0,
              f"found {len(recipes)}: {', '.join(names)}" if recipes else "no tomato recipes found")
        return recipes
    except Exception as e:
        check("1. recipya_has_tomato_recipe", 1, False, f"exception: {e}")
        return []


def check_2_shopping_list_has_recipe_note() -> list[dict]:
    """Grocy shopping list has entries with a note referencing a recipe name."""
    try:
        entries = get_grocy_shopping_list()
        with_notes = [e for e in entries if e.get("note") and e["note"].strip()]
        known = get_all_recipya_recipe_names()
        recipe_entries = []
        for e in with_notes:
            rname = extract_recipe_name_from_note(e["note"], known)
            if rname:
                e["_matched_recipe"] = rname
                recipe_entries.append(e)

        check("2. shopping_list_has_recipe_note", 2, len(recipe_entries) > 0,
              f"found {len(recipe_entries)} entries referencing a Recipya recipe"
              if recipe_entries else "no shopping list entries reference a known Recipya recipe name")
        return recipe_entries
    except Exception as e:
        check("2. shopping_list_has_recipe_note", 2, False, f"exception: {e}")
        return []


def check_3_recipe_note_matches_recipya(recipe_entries: list[dict]) -> str | None:
    """The recipe name in the shopping list note exists in Recipya."""
    try:
        if not recipe_entries:
            check("3. recipe_note_matches_recipya", 2, False,
                  "no recipe-referencing entries to validate")
            return None

        matched_names = list({e["_matched_recipe"] for e in recipe_entries})
        known = get_all_recipya_recipe_names()
        valid = [n for n in matched_names if n in known]
        invalid = [n for n in matched_names if n not in known]

        check("3. recipe_note_matches_recipya", 2, len(valid) > 0 and len(invalid) == 0,
              f"valid: {valid}; invalid: {invalid}" if invalid else f"recipe(s): {valid}")
        return valid[0] if valid else None
    except Exception as e:
        check("3. recipe_note_matches_recipya", 2, False, f"exception: {e}")
        return None


def check_4_items_are_recipe_ingredients(recipe_entries: list[dict], recipe_name: str | None) -> None:
    """Items on the shopping list are ingredients from the referenced recipe."""
    try:
        if not recipe_name or not recipe_entries:
            check("4. items_are_recipe_ingredients", 2, False,
                  "no valid recipe reference to cross-check")
            return

        known = get_all_recipya_recipe_names()
        recipe_id = known.get(recipe_name)
        if not recipe_id:
            check("4. items_are_recipe_ingredients", 2, False,
                  f"recipe '{recipe_name}' not found in Recipya")
            return

        ingredients = get_recipe_ingredients(recipe_id)
        ing_lower = [i.lower() for i in ingredients]

        relevant = [e for e in recipe_entries if e.get("_matched_recipe") == recipe_name]
        unmatched = []
        for e in relevant:
            pname = e.get("product_name") or e.get("note", "")
            matched = any(
                ingredient_matches_product(ing, pname) for ing in ingredients
            )
            if not matched:
                unmatched.append(pname)

        total = len(relevant)
        matched_count = total - len(unmatched)
        check("4. items_are_recipe_ingredients", 2,
              total > 0 and len(unmatched) == 0,
              f"{matched_count}/{total} matched"
              + (f"; unmatched: {', '.join(unmatched[:3])}" if unmatched else ""))
    except Exception as e:
        check("4. items_are_recipe_ingredients", 2, False, f"exception: {e}")


def check_5_missing_items_not_in_stock(recipe_entries: list[dict]) -> None:
    """Items added to shopping list are not already present in Grocy stock."""
    try:
        if not recipe_entries:
            check("5. missing_items_not_in_stock", 2, False,
                  "no recipe-linked shopping entries to validate")
            return

        stocked = get_grocy_stock_product_names()
        stocked_norm = {normalize(s) for s in stocked}
        in_stock = []
        for e in recipe_entries:
            pname = e.get("product_name", "")
            if pname and normalize(pname) in stocked_norm:
                in_stock.append(pname)

        check("5. missing_items_not_in_stock", 2, len(in_stock) == 0,
              f"already in stock: {', '.join(in_stock[:5])}" if in_stock
              else f"all {len(recipe_entries)} entries are genuinely missing from stock")
    except Exception as e:
        check("5. missing_items_not_in_stock", 2, False, f"exception: {e}")


def check_6_tomatoes_not_on_shopping_list(recipe_entries: list[dict]) -> None:
    """Tomatoes (overstocked) should NOT be among the entries added for the recipe.

    Only entries the agent added for this task (i.e. those whose note references
    a Recipya recipe) are considered — the seed shopping list already contains
    unrelated tomato-named products, which must not fail this check.
    """
    try:
        tomato_entries = []
        for e in recipe_entries:
            pname = (e.get("product_name") or "").lower()
            if is_tomato_ingredient(pname):
                tomato_entries.append(e.get("product_name", "unknown"))

        check("6. tomatoes_not_on_shopping_list", 1, len(tomato_entries) == 0,
              f"tomato products added for the recipe: {', '.join(tomato_entries[:3])}"
              if tomato_entries else "no tomato products among the recipe's shopping entries")
    except Exception as e:
        check("6. tomatoes_not_on_shopping_list", 1, False, f"exception: {e}")


def check_7_all_missing_covered(recipe_name: str | None, recipe_entries: list[dict]) -> None:
    """All recipe ingredients not in Grocy stock appear on the shopping list."""
    try:
        if not recipe_name:
            check("7. all_missing_covered", 2, False, "no valid recipe to cross-check")
            return

        known = get_all_recipya_recipe_names()
        recipe_id = known.get(recipe_name)
        if not recipe_id:
            check("7. all_missing_covered", 2, False,
                  f"recipe '{recipe_name}' not found in Recipya")
            return

        ingredients = get_recipe_ingredients(recipe_id)
        stocked = get_grocy_stock_product_names()
        all_products = get_grocy_product_names()
        stocked_norm = {normalize(s) for s in stocked}

        non_tomato_ingredients = [i for i in ingredients if not is_tomato_ingredient(i)]

        missing_from_stock = []
        for ing in non_tomato_ingredients:
            found_in_stock = False
            for sp in stocked:
                if ingredient_matches_product(ing, sp):
                    found_in_stock = True
                    break
            if not found_in_stock:
                missing_from_stock.append(ing)

        shop_product_names = [e.get("product_name", "") for e in recipe_entries]
        shop_notes = [(e.get("note") or "") for e in recipe_entries]
        shop_combined = shop_product_names + shop_notes

        not_on_list = []
        for ing in missing_from_stock:
            on_list = False
            for sp in shop_combined:
                if ingredient_matches_product(ing, sp):
                    on_list = True
                    break
            if not on_list:
                not_on_list.append(ing)

        if not missing_from_stock:
            check("7. all_missing_covered", 2, True,
                  "all non-tomato ingredients already in stock")
        else:
            check("7. all_missing_covered", 2, len(not_on_list) == 0,
                  f"missing from list: {', '.join(not_on_list[:5])}"
                  if not_on_list
                  else f"all {len(missing_from_stock)} missing ingredients are on shopping list")
    except Exception as e:
        check("7. all_missing_covered", 2, False, f"exception: {e}")


def check_8_no_duplicate_entries(recipe_entries: list[dict]) -> None:
    """No duplicate product entries on the shopping list for this recipe."""
    try:
        if not recipe_entries:
            check("8. no_duplicate_entries", 1, False,
                  "no recipe-linked shopping entries")
            return

        pids = [int(e["product_id"]) for e in recipe_entries if e.get("product_id")]
        pnames = [e.get("product_name", "") for e in recipe_entries if e.get("product_name")]
        dupes = [p for p in set(pids) if pids.count(p) > 1]
        dupe_names = [n for n in set(pnames) if pnames.count(n) > 1 and n]

        check("8. no_duplicate_entries", 1, len(dupes) == 0 and len(dupe_names) == 0,
              f"duplicate product_ids: {dupes}, names: {dupe_names}"
              if dupes or dupe_names else "")
    except Exception as e:
        check("8. no_duplicate_entries", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    tomato_recipes = check_1_recipya_has_tomato_recipe()
    recipe_entries = check_2_shopping_list_has_recipe_note()
    recipe_name = check_3_recipe_note_matches_recipya(recipe_entries)
    check_4_items_are_recipe_ingredients(recipe_entries, recipe_name)
    check_5_missing_items_not_in_stock(recipe_entries)
    check_6_tomatoes_not_on_shopping_list(recipe_entries)
    check_7_all_missing_covered(recipe_name, recipe_entries)
    check_8_no_duplicate_entries(recipe_entries)

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
