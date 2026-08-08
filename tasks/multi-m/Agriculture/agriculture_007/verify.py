#!/usr/bin/env python3
"""Verify Recipya visual selection -> exact Grocy deficit plan for agriculture_007."""

import atexit
import base64
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request


GROCY_CONTAINER = os.getenv("GROCY_CONTAINER")
RECIPYA_CONTAINER = os.getenv("RECIPYA_CONTAINER")
RECIPYA_DB = "/root/.config/Recipya/Database/recipya.db"
RECIPYA_IMAGES = "/root/.config/Recipya/Images"
GROCY_DB_CANDIDATES = [
    "/config/data/grocy.db",
    "/config/data/data/grocy.db",
    "/var/www/data/grocy.db",
]
MARKER_RE = re.compile(r"^AG007 \| Recipya #(\d+) \| (.+)$")

for _name, _value in [
    ("GROCY_CONTAINER", GROCY_CONTAINER),
    ("RECIPYA_CONTAINER", RECIPYA_CONTAINER),
]:
    if not _value:
        print(f"FATAL: {_name} not set", file=sys.stderr)
        sys.exit(1)


_checks: list[tuple[str, int, bool, str]] = []
_recipya_db_copy = ""
_grocy_db_path = ""
_marked_rows: list[dict] = []
_recipe: dict = {}
_ingredients: list[str] = []
_image_bytes = b""
_recipe_ok = False
_visual_ok = False
_marker_ok = False


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{suffix}", file=sys.stderr)


def docker_exec(container: str, *args: str, timeout: int = 30) -> tuple[int, str, str]:
    result = subprocess.run(
        ["docker", "exec", container, *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, result.stdout, result.stderr


def _cleanup() -> None:
    if _recipya_db_copy:
        try:
            os.unlink(_recipya_db_copy)
        except OSError:
            pass


atexit.register(_cleanup)


def recipya_query(sql: str) -> list[dict]:
    global _recipya_db_copy
    if not _recipya_db_copy:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        _recipya_db_copy = tmp.name
        tmp.close()
        result = subprocess.run(
            ["docker", "cp", f"{RECIPYA_CONTAINER}:{RECIPYA_DB}", _recipya_db_copy],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Recipya DB copy failed: {result.stderr.strip()}")
    connection = sqlite3.connect(_recipya_db_copy)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(sql).fetchall()]
    finally:
        connection.close()


def _find_grocy_db() -> str:
    global _grocy_db_path
    if _grocy_db_path:
        return _grocy_db_path
    for candidate in GROCY_DB_CANDIDATES:
        rc, _, _ = docker_exec(GROCY_CONTAINER, "test", "-f", candidate, timeout=5)
        if rc == 0:
            _grocy_db_path = candidate
            return candidate
    return GROCY_DB_CANDIDATES[0]


def grocy_query(sql: str) -> list[dict]:
    php = (
        '$db=new PDO("sqlite:' + _find_grocy_db() + '");'
        '$db->setAttribute(PDO::ATTR_ERRMODE,PDO::ERRMODE_EXCEPTION);'
        '$s=$db->query($argv[1]);'
        'echo json_encode($s->fetchAll(PDO::FETCH_ASSOC));'
    )
    rc, out, err = docker_exec(GROCY_CONTAINER, "php", "-r", php, "--", sql)
    if rc != 0:
        raise RuntimeError(f"Grocy query failed: {err.strip()[:300]}")
    return json.loads(out) if out.strip() else []


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def read_recipe_image(image_id: str) -> bytes:
    image_id = (image_id or "").strip()
    if not image_id or os.path.basename(image_id) != image_id:
        return b""
    names = [image_id]
    if not image_id.lower().endswith((".webp", ".jpg", ".jpeg", ".png", ".gif")):
        names.insert(0, image_id + ".webp")
    for name in names:
        rc, encoded, _ = docker_exec(
            RECIPYA_CONTAINER,
            "base64",
            f"{RECIPYA_IMAGES}/{name}",
        )
        if rc != 0 or not encoded.strip():
            continue
        try:
            data = base64.b64decode(re.sub(r"\s+", "", encoded), validate=True)
        except ValueError:
            continue
        if len(data) >= 1024:
            return data
    return b""


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def mindra_yes_no(prompt: str, image_bytes: bytes | None = None, timeout: int = 60) -> tuple[bool, str]:
    content: list[dict] = []
    if image_bytes:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{image_mime(image_bytes)};base64,"
                + base64.b64encode(image_bytes).decode("ascii")
            },
        })
    content.append({"type": "text", "text": prompt + "\nAnswer only YES or NO."})
    payload = {
        "model": os.getenv("MINDRA_MODEL", "gemini-3.0-flash-preview"),
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 32,
    }
    try:
        request = urllib.request.Request(
            os.getenv("MINDRA_BASE_URL", "https://api.mindracode.com/v1") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {os.getenv('MINDRA_API_KEY', '')}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return False, "judge response missing choices"
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return False, "judge response missing message"
        content_value = message.get("content")
        if not isinstance(content_value, str) or not content_value.strip():
            return False, "judge response missing message content"
        answer = content_value.strip().upper()
        return answer.startswith("YES"), answer[:120]
    except Exception as exc:
        return False, f"judge error: {exc}"


def load_context() -> None:
    global _marked_rows, _recipe, _ingredients, _image_bytes
    _marked_rows = grocy_query(
        "SELECT sl.id,sl.product_id,sl.amount,COALESCE(sl.note,'') AS note,"
        "COALESCE(p.name,'') AS product_name "
        "FROM shopping_list sl LEFT JOIN products p ON p.id=sl.product_id "
        "WHERE sl.note LIKE 'AG007%';"
    )
    markers = {row.get("note") or "" for row in _marked_rows}
    parsed = [MARKER_RE.fullmatch(marker) for marker in markers]
    if len(markers) != 1 or len(parsed) != 1 or parsed[0] is None:
        return
    recipe_id = int(parsed[0].group(1))
    recipe_name = html.unescape(parsed[0].group(2))
    rows = recipya_query(
        "SELECT r.id,r.name,r.image FROM recipes r "
        "JOIN user_recipe ur ON ur.recipe_id=r.id "
        "JOIN users u ON u.id=ur.user_id "
        f"WHERE r.id={recipe_id} AND LOWER(u.email)='admin@recipya.com'"
    )
    if len(rows) != 1 or html.unescape(rows[0]["name"]) != recipe_name:
        return
    _recipe = rows[0]
    _recipe["name"] = html.unescape(_recipe["name"])
    _ingredients = [html.unescape(row["name"]) for row in recipya_query(
        "SELECT i.name FROM ingredient_recipe ir "
        "JOIN ingredients i ON i.id=ir.ingredient_id "
        f"WHERE ir.recipe_id={recipe_id} ORDER BY ir.ingredient_order"
    )]
    _image_bytes = read_recipe_image(_recipe.get("image") or "")


def check_1_exact_admin_recipe() -> None:
    global _recipe_ok
    has_tomato = any(re.search(r"\btomato(?:es)?\b", value, re.IGNORECASE) for value in _ingredients)
    _recipe_ok = bool(
        _recipe and _ingredients and has_tomato and _image_bytes and len(_marked_rows) > 0
    )
    check(
        "1. exact admin-owned Recipya recipe with real cover",
        4,
        _recipe_ok,
        f"id={_recipe.get('id')} name='{_recipe.get('name')}' ingredients={len(_ingredients)} image_bytes={len(_image_bytes)}"
        if _recipe else "one exact AG007 marker did not resolve to an admin recipe",
    )


def check_2_actual_cover_is_tomato_dominant() -> None:
    global _visual_ok
    if not _recipe_ok:
        check("2. actual Recipya cover is tomato-dominant", 5, False, "gated: recipe invalid")
        return
    prompt = (
        "Inspect the actual cover image stored for this Recipya recipe. Does the visible dish "
        "clearly use tomato as a dominant ingredient or sauce base, rather than as a minor "
        "garnish or incidental component? The ingredient list must also support that reading.\n"
        f"Recipe: {_recipe['name']}\nIngredients: " + " | ".join(_ingredients)
    )
    _visual_ok, detail = mindra_yes_no(prompt, _image_bytes)
    check("2. actual Recipya cover is tomato-dominant", 5, _visual_ok, detail)


def check_3_exact_unique_marker_rows() -> None:
    global _marker_ok
    if not _visual_ok:
        check("3. exact unique Grocy task entries", 1, False, "gated: visual recipe invalid")
        return
    expected_note = f"AG007 | Recipya #{int(_recipe['id'])} | {_recipe['name']}"
    product_ids = [row.get("product_id") for row in _marked_rows]
    problems = []
    if not _marked_rows:
        problems.append("no task shopping entries")
    if any((row.get("note") or "") != expected_note for row in _marked_rows):
        problems.append("inexact or mixed task marker")
    if any(product_id is None for product_id in product_ids):
        problems.append("entry without a Grocy product")
    if len(product_ids) != len(set(product_ids)):
        problems.append("duplicate product entries")
    if any(abs(float(row.get("amount") or 0) - 1.0) > 1e-6 for row in _marked_rows):
        problems.append("every amount must equal 1")
    if any(re.search(r"\btomato(?:es)?\b", row.get("product_name") or "", re.IGNORECASE)
           for row in _marked_rows):
        problems.append("tomato appears in task shopping entries")
    _marker_ok = not problems
    check(
        "3. exact unique Grocy task entries",
        1,
        _marker_ok,
        f"entries={len(_marked_rows)}" if _marker_ok else "; ".join(problems),
    )


def check_4_exact_inventory_deficit_set() -> None:
    if not (_visual_ok and _marker_ok):
        check("4. exact auxiliary-ingredient deficit set", 10, False,
              "gated: visual/marker chain invalid")
        return
    products = grocy_query(
        "SELECT p.id,p.name,"
        "COALESCE((SELECT SUM(s.amount) FROM stock s WHERE s.product_id=p.id),0) AS stock "
        "FROM products p ORDER BY p.name,p.id"
    )
    shopping_products = [
        f"#{row.get('product_id')} {row.get('product_name') or ''}"
        for row in _marked_rows
    ]
    prompt = (
        "Evaluate an inventory comparison. Ignore recipe quantities and preparation adjectives. "
        "Treat singular/plural and ordinary culinary synonyms as the same ingredient. Exclude all "
        "tomato forms from the shopping requirement. A positive-stock product supplies an ingredient "
        "only if it is a direct usable source; a flavored/composite product that merely mentions the "
        "ingredient does not count. The marked row must reuse an existing suitable Grocy product when "
        "one exists; the final product list must not contain a newly created semantic duplicate. If no "
        "suitable product exists, the marked product name must equal the displayed Recipya ingredient "
        "text. After deduplicating ingredient identities, are the marked Grocy shopping products exactly "
        "the complete set of non-tomato recipe ingredients that lack a direct positive-stock source, "
        "with no stocked or unrelated extras, duplicates, or omissions?\n\n"
        "RECIPE INGREDIENTS:\n- " + "\n- ".join(_ingredients) +
        "\n\nALL GROCY PRODUCTS (ID | NAME | CURRENT STOCK):\n- " +
        "\n- ".join(
            f"#{row['id']} | {row['name']} | {float(row.get('stock') or 0):g}"
            for row in products
        ) +
        "\n\nMARKED SHOPPING PRODUCTS:\n- " + "\n- ".join(shopping_products)
    )
    passed, detail = mindra_yes_no(prompt, timeout=75)
    check("4. exact auxiliary-ingredient deficit set", 10, passed, detail)


def main() -> None:
    try:
        load_context()
    except Exception as exc:
        print(f"WARNING: context load failed: {exc}", file=sys.stderr)
    check_1_exact_admin_recipe()
    check_2_actual_cover_is_tomato_dominant()
    check_3_exact_unique_marker_rows()
    check_4_exact_inventory_deficit_set()
    total = sum(weight for _, weight, _, _ in _checks)
    earned = sum(weight for _, weight, passed, _ in _checks if passed)
    all_pass = bool(_checks) and all(passed for _, _, passed, _ in _checks)
    print(f"SCORE: {earned / total if total else 0:.3f}  PASS: {all_pass}  ({earned}/{total})", file=sys.stderr)
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
