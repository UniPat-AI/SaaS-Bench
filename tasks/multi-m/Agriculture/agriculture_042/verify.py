"""
Verifier for agriculture_042: Spring plowing traceability — batch number VINO-2025-001
across FarmOS activity log, Grocy product, and e-label wine record.

Checks: 10 weighted checks (17 total points) across farmos, grocy, e-label.
Strategy: farmos via docker exec php/PDO (SQLite); grocy via docker exec sqlite3/php;
          e-label via docker exec sqlcmd.

Required env vars:
  SERVER_HOSTNAME, FARMOS_PORT, FARMOS_CONTAINER,
  GROCY_PORT, GROCY_CONTAINER,
  E_LABEL_PORT, E_LABEL_CONTAINER.
"""

import base64
import json
import os
import re
import subprocess
import sys

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

FARMOS_PORT = os.getenv("FARMOS_PORT")
FARMOS_CONTAINER = os.getenv("FARMOS_CONTAINER")
GROCY_PORT = os.getenv("GROCY_PORT")
GROCY_CONTAINER = os.getenv("GROCY_CONTAINER")
E_LABEL_PORT = os.getenv("E_LABEL_PORT")
E_LABEL_CONTAINER = os.getenv("E_LABEL_CONTAINER")

for _var_name, _var_val in [
    ("FARMOS_PORT", FARMOS_PORT),
    ("FARMOS_CONTAINER", FARMOS_CONTAINER),
    ("GROCY_PORT", GROCY_PORT),
    ("GROCY_CONTAINER", GROCY_CONTAINER),
    ("E_LABEL_PORT", E_LABEL_PORT),
    ("E_LABEL_CONTAINER", E_LABEL_CONTAINER),
]:
    if not _var_val:
        print(f"FATAL: {_var_name} not set", file=sys.stderr)
        sys.exit(1)

def _derive_elabel_db_container() -> str:
    explicit = os.getenv("E_LABEL_DB_CONTAINER")
    if explicit:
        return explicit
    candidates = [
        E_LABEL_CONTAINER.replace("-app", "-db"),
        E_LABEL_CONTAINER + "-db",
    ]
    for c in candidates:
        r = subprocess.run(
            ["docker", "inspect", c],
            capture_output=True, text=True, errors="replace", timeout=5,
        )
        if r.returncode == 0:
            return c
    return candidates[0]


E_LABEL_DB_CONTAINER = _derive_elabel_db_container()

FARMOS_SQLITE = "/opt/drupal/web/sites/default/files/.ht.sqlite"

GROCY_DB_CANDIDATES = [
    "/config/data/grocy.db",
    "/config/data/data/grocy.db",
    "/var/www/data/grocy.db",
]

EXPECTED_BATCH = "VINO-2025-001"

_INPUTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "inputs"
)

INPUT_FILES: list[str] = [
    os.path.join(_INPUTS_DIR, "farmos_crop_021.jpg"),
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


def farmos_sql(query: str) -> str:
    php_script = (
        '$db = new PDO("sqlite:' + FARMOS_SQLITE + '");'
        '$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);'
        '$r = $db->query(' + json.dumps(query) + ');'
        'while($row=$r->fetch(PDO::FETCH_NUM))'
        '{ echo implode("|",$row)."\\n"; }'
    )
    rc, stdout, stderr = docker_exec(
        FARMOS_CONTAINER, "php", "-r", php_script, timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"farmos php error (rc={rc}): {stderr.strip()}")
    return stdout.strip()


_grocy_db_path = ""


def _find_grocy_db() -> str:
    global _grocy_db_path
    if _grocy_db_path:
        return _grocy_db_path
    for path in GROCY_DB_CANDIDATES:
        rc, _, _ = docker_exec(GROCY_CONTAINER, "test", "-f", path)
        if rc == 0:
            _grocy_db_path = path
            return path
    _grocy_db_path = GROCY_DB_CANDIDATES[0]
    return _grocy_db_path


def grocy_sql(query: str) -> str:
    db = _find_grocy_db()
    rc, stdout, stderr = docker_exec(
        GROCY_CONTAINER,
        "sqlite3", "-separator", "|", db, query,
        timeout=15,
    )
    if rc == 0:
        return stdout.strip()
    php_script = (
        f'$db = new PDO("sqlite:{db}");'
        f'$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);'
        f'$r = $db->query("{query.replace(chr(34), chr(92)+chr(34))}");'
        f'while($row=$r->fetch(PDO::FETCH_NUM))'
        f'{{ echo implode("|",$row)."\\n"; }}'
    )
    rc2, stdout2, stderr2 = docker_exec(
        GROCY_CONTAINER, "php", "-r", php_script, timeout=15,
    )
    if rc2 != 0:
        raise RuntimeError(
            f"grocy query failed: sqlite3({stderr.strip()}) php({stderr2.strip()})"
        )
    return stdout2.strip()


def elabel_sql(query: str) -> str:
    full_query = f"SET NOCOUNT ON; {query}"
    for sqlcmd in ["/opt/mssql-tools18/bin/sqlcmd", "/opt/mssql-tools/bin/sqlcmd"]:
        rc, stdout, stderr = docker_exec(
            E_LABEL_DB_CONTAINER,
            sqlcmd,
            "-S", "localhost", "-U", "sa", "-P", "Elabel2024!Strong",
            "-d", "elabel", "-C", "-h", "-1", "-s", "|", "-W",
            "-Q", full_query,
            timeout=15,
        )
        if rc == 0:
            lines = [
                l for l in stdout.strip().split("\n")
                if l.strip()
                and not l.strip().startswith("Msg ")
                and "Changed database context" not in l
            ]
            return "\n".join(lines)
        stderr_lower = stderr.lower()
        if "no such file" not in stderr_lower and "not found" not in stderr_lower:
            raise RuntimeError(f"sqlcmd error (rc={rc}): {stderr.strip()}")
    raise RuntimeError("sqlcmd not found at known paths")


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    return text.strip()


def llm_judge_vision(
    image_path: str,
    recorded_value: str,
    condition: str,
    timeout: int = 45,
) -> tuple[bool, str]:
    api_base = os.getenv("MINDRA_BASE_URL", "https://api.mindracode.com/v1")
    api_key = os.getenv("MINDRA_API_KEY", "")

    ext = os.path.splitext(image_path)[1].lower().lstrip(".")
    mime = {
        "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
        "gif": "image/gif", "webp": "image/webp",
    }.get(ext, "image/jpeg")

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


# ── Cached state ──────────────────────────────────────────────────────────────
_farmos_log_id = 0
_farmos_notes = ""
_farmos_batch = ""
_grocy_product_id = 0
_grocy_batch = ""
_elabel_batch = ""

FARMOS_NOTES_COL = "notes__value"


# ── Individual checks ─────────────────────────────────────────────────────────
def check_0_input_files_exist() -> None:
    missing = [p for p in INPUT_FILES if not os.path.isfile(p)]
    if missing:
        check("0. input_files_exist", 1, False, "missing: " + ", ".join(missing))
    else:
        check("0. input_files_exist", 1, True)


def check_1_farmos_activity_log_exists() -> None:
    global _farmos_log_id
    try:
        rows = farmos_sql(
            "SELECT lfd.id, lfd.name "
            "FROM log_field_data lfd "
            "JOIN log__asset la ON la.entity_id = lfd.id "
            "JOIN asset_field_data afd ON afd.id = la.asset_target_id "
            "WHERE lfd.type = 'activity' "
            "AND (afd.name LIKE '%Vineyard Block 1%' "
            "OR afd.name LIKE '%vineyard%block%1%') "
            "ORDER BY lfd.id DESC LIMIT 1"
        )
        if not rows:
            rows = farmos_sql(
                f"SELECT id, name FROM log_field_data "
                f"WHERE type = 'activity' AND "
                f"({FARMOS_NOTES_COL} LIKE '%VINO-2025-001%' "
                f"OR name LIKE '%plow%' OR name LIKE '%Vineyard%Block%1%') "
                f"ORDER BY id DESC LIMIT 1"
            )
        if not rows:
            rows = farmos_sql(
                f"SELECT id, name FROM log_field_data "
                f"WHERE {FARMOS_NOTES_COL} LIKE '%VINO-2025-001%' "
                f"OR name LIKE '%VINO-2025-001%' "
                f"ORDER BY id DESC LIMIT 1"
            )
        if not rows:
            check("1. farmos_activity_log_exists", 2, False,
                  "no activity log for 'Vineyard Block 1' with batch VINO-2025-001")
            return
        parts = rows.split("\n")[0].split("|", 1)
        _farmos_log_id = int(parts[0])
        log_name = parts[1] if len(parts) > 1 else ""
        check("1. farmos_activity_log_exists", 2, True,
              f"log #{_farmos_log_id}: {log_name}")
    except Exception as e:
        check("1. farmos_activity_log_exists", 2, False, f"exception: {e}")


def check_2_farmos_batch_in_notes() -> None:
    global _farmos_batch, _farmos_notes
    try:
        if not _farmos_log_id:
            check("2. farmos_batch_VINO-2025-001", 2, False, "no activity log found")
            return
        raw = farmos_sql(
            f"SELECT {FARMOS_NOTES_COL} FROM log_field_data WHERE id = {_farmos_log_id}"
        )
        if not raw:
            raw = farmos_sql(
                f"SELECT name FROM log_field_data WHERE id = {_farmos_log_id}"
            )
        _farmos_notes = _strip_html(raw) if raw else ""

        has_batch = EXPECTED_BATCH in _farmos_notes
        if has_batch:
            _farmos_batch = EXPECTED_BATCH
        else:
            m = re.search(r"VINO-\d{4}-\d{3}", _farmos_notes, re.IGNORECASE)
            if m:
                _farmos_batch = m.group()

        passed = _farmos_batch == EXPECTED_BATCH
        detail = f"batch='{_farmos_batch}'" if _farmos_batch else "batch not found in notes"
        check("2. farmos_batch_VINO-2025-001", 2, passed, detail)
    except Exception as e:
        check("2. farmos_batch_VINO-2025-001", 2, False, f"exception: {e}")


def check_3_farmos_image_attached() -> None:
    try:
        if not _farmos_log_id:
            check("3. farmos_image_attached", 1, False, "no activity log found")
            return
        rows = farmos_sql(
            f"SELECT fm.fid, fm.uri, fm.filename "
            f"FROM log__image li "
            f"JOIN file_managed fm ON fm.fid = li.image_target_id "
            f"WHERE li.entity_id = {_farmos_log_id} LIMIT 1"
        )
        if not rows:
            rows = farmos_sql(
                f"SELECT fm.fid, fm.uri, fm.filename "
                f"FROM log__file lf "
                f"JOIN file_managed fm ON fm.fid = lf.file_target_id "
                f"WHERE lf.entity_id = {_farmos_log_id} "
                f"AND fm.filemime LIKE 'image/%' LIMIT 1"
            )
        if not rows:
            check("3. farmos_image_attached", 1, False,
                  "no image attached to activity log")
            return
        parts = rows.split("|")
        fid = parts[0].strip() if parts else ""
        filename = parts[2].strip() if len(parts) > 2 else ""
        check("3. farmos_image_attached", 1, True, f"fid={fid}, file={filename}")
    except Exception as e:
        check("3. farmos_image_attached", 1, False, f"exception: {e}")


def check_4_grocy_product_exists() -> None:
    global _grocy_product_id
    try:
        rows = grocy_sql(
            "SELECT id, name FROM products "
            "WHERE name LIKE '%Organic Estate Wine 2025%' "
            "OR name LIKE '%Organic%Estate%Wine%2025%' "
            "OR (LOWER(name) LIKE '%organic%estate%wine%' AND LOWER(name) LIKE '%2025%') "
            "LIMIT 1;"
        )
        if not rows:
            rows = grocy_sql(
                "SELECT id, name FROM products "
                "WHERE LOWER(name) LIKE '%organic%estate%wine%' "
                "OR LOWER(name) LIKE '%vino-2025%' "
                "LIMIT 1;"
            )
        if not rows:
            check("4. grocy_product_exists", 1, False,
                  "no product matching 'Organic Estate Wine 2025'")
            return
        parts = rows.split("\n")[0].split("|", 1)
        _grocy_product_id = int(parts[0])
        name = parts[1] if len(parts) > 1 else ""
        check("4. grocy_product_exists", 1, True,
              f"id={_grocy_product_id}, name='{name}'")
    except Exception as e:
        check("4. grocy_product_exists", 1, False, f"exception: {e}")


def check_5_grocy_batch() -> None:
    global _grocy_batch
    try:
        if not _grocy_product_id:
            check("5. grocy_batch_VINO-2025-001", 2, False, "product not found")
            return
        rows = grocy_sql(
            f"SELECT description FROM products WHERE id = {_grocy_product_id};"
        )
        desc = rows.strip() if rows else ""
        if EXPECTED_BATCH in desc:
            _grocy_batch = EXPECTED_BATCH
            check("5. grocy_batch_VINO-2025-001", 2, True,
                  f"batch in description: '{desc[:80]}'")
            return

        ufield_rows = grocy_sql(
            f"SELECT uv.value FROM userfield_values uv "
            f"JOIN userfields uf ON uf.id = uv.field_id "
            f"WHERE uf.entity = 'products' "
            f"AND uv.object_id = CAST({_grocy_product_id} AS TEXT) "
            f"AND (LOWER(uf.name) LIKE '%batch%' OR LOWER(uf.name) LIKE '%lot%') "
            f"LIMIT 1;"
        )
        if ufield_rows and EXPECTED_BATCH in ufield_rows:
            _grocy_batch = EXPECTED_BATCH
            check("5. grocy_batch_VINO-2025-001", 2, True,
                  f"batch in userfield: '{ufield_rows.strip()}'")
            return

        stock_rows = grocy_sql(
            f"SELECT stock_id FROM stock "
            f"WHERE product_id = {_grocy_product_id} "
            f"AND stock_id IS NOT NULL AND TRIM(stock_id) != '' LIMIT 1;"
        )
        if stock_rows and EXPECTED_BATCH in stock_rows:
            _grocy_batch = EXPECTED_BATCH
            check("5. grocy_batch_VINO-2025-001", 2, True,
                  f"batch in stock_id: '{stock_rows.strip()}'")
            return

        all_text = f"{desc} {ufield_rows or ''} {stock_rows or ''}"
        m = re.search(r"VINO-\d{4}-\d{3}", all_text, re.IGNORECASE)
        if m:
            _grocy_batch = m.group()
        check("5. grocy_batch_VINO-2025-001", 2, _grocy_batch == EXPECTED_BATCH,
              f"batch='{_grocy_batch}'" if _grocy_batch
              else "VINO-2025-001 not found in description, userfields, or stock")
    except Exception as e:
        check("5. grocy_batch_VINO-2025-001", 2, False, f"exception: {e}")


def check_6_elabel_wine_record_exists() -> None:
    try:
        rows = elabel_sql(
            "SELECT TOP 1 Id, Name FROM Product "
            "WHERE Name LIKE '%Organic Estate Wine 2025%' "
            "OR Name LIKE '%Organic%Estate%Wine%' "
            "OR Sku = 'VINO-2025-001' "
            "ORDER BY CreatedOn DESC;"
        )
        lines = [
            l for l in rows.split("\n")
            if l.strip() and not l.startswith("---")
            and "rows affected" not in l.lower()
        ]
        if not lines:
            check("6. elabel_wine_record_exists", 1, False,
                  "no wine record matching 'Organic Estate Wine 2025'")
            return
        parts = [p.strip() for p in lines[0].split("|")]
        pid = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        check("6. elabel_wine_record_exists", 1, True,
              f"id={pid}, name='{name}'")
    except Exception as e:
        check("6. elabel_wine_record_exists", 1, False, f"exception: {e}")


def check_7_elabel_batch() -> None:
    global _elabel_batch
    try:
        rows = elabel_sql(
            "SELECT TOP 1 Name, Sku, Brand, FBOAdditionalInfo "
            "FROM Product "
            "WHERE Name LIKE '%Organic Estate Wine 2025%' "
            "OR Name LIKE '%Organic%Estate%Wine%' "
            "OR Sku = 'VINO-2025-001' "
            "ORDER BY CreatedOn DESC;"
        )
        lines = [
            l for l in rows.split("\n")
            if l.strip() and not l.startswith("---")
            and "rows affected" not in l.lower()
        ]
        if not lines:
            check("7. elabel_batch_VINO-2025-001", 2, False,
                  "no wine record found in e-label")
            return
        parts = [p.strip() for p in lines[0].split("|")]
        name = parts[0] if parts else ""
        sku = parts[1] if len(parts) > 1 else ""
        full_text = " ".join(parts)

        if EXPECTED_BATCH in sku:
            _elabel_batch = EXPECTED_BATCH
        elif EXPECTED_BATCH in full_text:
            _elabel_batch = EXPECTED_BATCH
        else:
            m = re.search(r"VINO-\d{4}-\d{3}", full_text, re.IGNORECASE)
            if m:
                _elabel_batch = m.group()

        passed = _elabel_batch == EXPECTED_BATCH
        check("7. elabel_batch_VINO-2025-001", 2, passed,
              f"name='{name}', sku='{sku}', batch='{_elabel_batch}'")
    except Exception as e:
        check("7. elabel_batch_VINO-2025-001", 2, False, f"exception: {e}")


def check_8_cross_app_batch_consistency() -> None:
    try:
        batches: dict[str, str] = {}
        if _farmos_batch:
            batches["farmos"] = _farmos_batch
        if _grocy_batch:
            batches["grocy"] = _grocy_batch
        if _elabel_batch:
            batches["elabel"] = _elabel_batch

        if len(batches) < 2:
            check("8. cross_app_batch_consistency", 3, False,
                  f"need batch from >=2 systems, got: {batches}")
            return

        vals = list(batches.values())
        all_same = all(v == vals[0] for v in vals)
        all_correct = all_same and vals[0] == EXPECTED_BATCH
        all_three = len(batches) == 3
        check("8. cross_app_batch_consistency", 3,
              all_correct and all_three,
              f"all='{vals[0]}', systems={list(batches.keys())}" if all_correct
              else f"mismatch or incomplete: {batches}")
    except Exception as e:
        check("8. cross_app_batch_consistency", 3, False, f"exception: {e}")


def check_9_cross_modal_plowing_photo() -> None:
    try:
        img_path = INPUT_FILES[0] if INPUT_FILES else ""
        if not img_path or not os.path.isfile(img_path):
            check("9. cross_modal_plowing_photo", 2, False,
                  "skipped: input file missing")
            return
        condition = (
            "The image depicts a farm or agricultural scene — showing plowed fields, "
            "vineyard rows, tractors, soil preparation, or farming activity. "
            "It is consistent with a 'spring plowing complete' field photo."
        )
        passed, raw = llm_judge_vision(img_path, "spring plowing field photo", condition)
        check("9. cross_modal_plowing_photo", 2, passed, f"llm_judge_vision: {raw}")
    except Exception as e:
        check("9. cross_modal_plowing_photo", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_input_files_exist()
    check_1_farmos_activity_log_exists()
    check_2_farmos_batch_in_notes()
    check_3_farmos_image_attached()
    check_4_grocy_product_exists()
    check_5_grocy_batch()
    check_6_elabel_wine_record_exists()
    check_7_elabel_batch()
    check_8_cross_app_batch_consistency()
    check_9_cross_modal_plowing_photo()

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
