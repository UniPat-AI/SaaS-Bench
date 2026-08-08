"""
Verifier for agriculture_021: Cross-app batch audit — check the receiving
office's delivery manifest (Grocy product -> batch number) against FarmOS
harvest log names; flag products whose batch number matches no harvest log.

Checks: 6 weighted checks (12 total points) across grocy, farmos.
Strategy: grocy=docker exec PHP PDO (SQLite); farmos=docker exec PHP PDO (SQLite)

Required env vars:
  SERVER_HOSTNAME, FARMOS_PORT, FARMOS_CONTAINER, GROCY_PORT, GROCY_CONTAINER
"""

import json
import html
import os
import re
import subprocess
import sys

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

FARMOS_PORT = os.getenv("FARMOS_PORT")
FARMOS_CONTAINER = os.getenv("FARMOS_CONTAINER")
GROCY_PORT = os.getenv("GROCY_PORT")
GROCY_CONTAINER = os.getenv("GROCY_CONTAINER")

for _var_name, _var_val in [
    ("FARMOS_PORT", FARMOS_PORT),
    ("FARMOS_CONTAINER", FARMOS_CONTAINER),
    ("GROCY_PORT", GROCY_PORT),
    ("GROCY_CONTAINER", GROCY_CONTAINER),
]:
    if not _var_val:
        print(f"FATAL: {_var_name} not set", file=sys.stderr)
        sys.exit(1)

FARMOS_SQLITE = "/opt/drupal/web/sites/default/files/.ht.sqlite"

GROCY_DB_CANDIDATES = [
    "/config/data/grocy.db",
    "/config/data/data/grocy.db",
    "/var/www/data/grocy.db",
]

AUDIT_FLAG = "AUDIT FLAG: Missing FarmOS harvest log"

# Delivery manifest from the task description: exact Grocy product name ->
# expected FarmOS harvest log name. Match status is derived from the live DB.
MANIFEST = {
    "Sliced Beets": "2024 Beet Harvest — North Field Center Bed",
    "Strawberries": "2024 Strawberry Harvest — Peak Week June 15",
    "Whole Kernel Corn": "2024 Sweet Corn Harvest — South Field 1",
    "Cherry Tomatoes By Sainsburys": "2024 Cherry Tomato Harvest — North Field West Bed 1",
    "Organic Peas & Shoestring Carrots": "2024 Carrot Harvest — North Field Center Bed 1",
    "Organic Green Beans": "2024 Green Bean Harvest — North Field East Bed 1",
    "Chestnut Mushrooms": "2024 Chestnut Mushroom Harvest — West Greenhouse 1",
    "Shreds Iceberg": "2024 Iceberg Lettuce Harvest — North Field East Bed 2",
}

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


def grocy_sql_json(query: str) -> list[dict]:
    db = _find_grocy_db()
    php_script = (
        '$db = new PDO("sqlite:' + db + '");'
        '$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);'
        '$r = $db->query(' + json.dumps(query) + ');'
        '$rows = $r->fetchAll(PDO::FETCH_ASSOC);'
        'echo json_encode($rows);'
    )
    rc, stdout, stderr = docker_exec(
        GROCY_CONTAINER, "php", "-r", php_script, timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"grocy php error (rc={rc}): {stderr.strip()}")
    if not stdout.strip():
        return []
    return json.loads(stdout.strip())


def farmos_sql_json(query: str) -> list[dict]:
    php_script = (
        '$db = new PDO("sqlite:' + FARMOS_SQLITE + '");'
        '$db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);'
        '$r = $db->query(' + json.dumps(query) + ');'
        '$rows = $r->fetchAll(PDO::FETCH_ASSOC);'
        'echo json_encode($rows);'
    )
    rc, stdout, stderr = docker_exec(
        FARMOS_CONTAINER, "php", "-r", php_script, timeout=15,
    )
    if rc != 0:
        raise RuntimeError(f"farmos php error (rc={rc}): {stderr.strip()}")
    if not stdout.strip():
        return []
    return json.loads(stdout.strip())


# ── Cached state ──────────────────────────────────────────────────────────────
_products_by_name: dict[str, dict] | None = None
_farmos_harvest_names: set[str] | None = None


def _load_manifest_products() -> dict[str, dict]:
    """Grocy products referenced by the manifest, keyed by exact name."""
    global _products_by_name
    if _products_by_name is not None:
        return _products_by_name
    names = list(MANIFEST)
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    rows = grocy_sql_json(
        "SELECT id, name, COALESCE(description, '') AS description "
        f"FROM products WHERE name IN ({quoted})"
    )
    _products_by_name = {r["name"]: r for r in rows}
    return _products_by_name


def _load_farmos_harvest_names() -> set[str]:
    global _farmos_harvest_names
    if _farmos_harvest_names is not None:
        return _farmos_harvest_names
    rows = farmos_sql_json(
        "SELECT name FROM log_field_data WHERE type = 'harvest'"
    )
    _farmos_harvest_names = {r["name"].strip() for r in rows if r.get("name")}
    return _farmos_harvest_names


def _manifest_truth() -> tuple[set[str], set[str]]:
    harvest_names = _load_farmos_harvest_names()
    matched = {name for name, batch in MANIFEST.items() if batch in harvest_names}
    return matched, set(MANIFEST) - matched


def _visible_text_before_flag(description: str) -> str:
    prefix = re.split(re.escape(AUDIT_FLAG), description, maxsplit=1, flags=re.IGNORECASE)[0]
    prefix = re.sub(r"<[^>]+>", " ", html.unescape(prefix))
    return re.sub(r"\s+", " ", prefix).strip()


# ── Individual checks ─────────────────────────────────────────────────────────
def check_1_manifest_products_exist() -> None:
    """All 8 manifest products exist in Grocy (exact name)."""
    try:
        products = _load_manifest_products()
        missing = [n for n in MANIFEST
                   if n not in products]
        check("1. manifest_products_exist", 1, not missing,
              f"found {len(products)}/8 manifest products" if not missing
              else f"missing products: {'; '.join(missing)}")
    except Exception as e:
        check("1. manifest_products_exist", 1, False, f"exception: {e}")


def check_2_farmos_logs_match_manifest() -> None:
    """FarmOS harvest logs are retrievable and consistent with the manifest:
    every matched batch number exists verbatim, no unmatched one does."""
    try:
        names = _load_farmos_harvest_names()
        if not names:
            check("2. farmos_logs_match_manifest", 1, False,
                  "no harvest logs found in farmos")
            return
        matched, unmatched = _manifest_truth()
        check("2. farmos_logs_match_manifest", 1, True,
              f"{len(names)} harvest logs; derived {len(matched)} matched and "
              f"{len(unmatched)} unmatched manifest entries")
    except Exception as e:
        check("2. farmos_logs_match_manifest", 1, False, f"exception: {e}")


def check_3_audit_recall_and_precision() -> None:
    """Every manifest product whose batch number has no FarmOS harvest log
    carries the exact audit flag in its description."""
    try:
        products = _load_manifest_products()
        matched, unmatched = _manifest_truth()
        missing_flag = []
        for name in unmatched:
            p = products.get(name)
            if not p:
                missing_flag.append(f"{name} (product not found)")
            elif AUDIT_FLAG not in p["description"]:
                missing_flag.append(name)
        wrongly_flagged = [
            name for name in matched
            if products.get(name) and AUDIT_FLAG.casefold() in products[name]["description"].casefold()
        ]
        problems = missing_flag + [f"{name} (matched but flagged)" for name in wrongly_flagged]
        check("3. audit_recall_and_precision", 4, not problems,
              f"exactly the {len(unmatched)} unmatched products are flagged"
              if not problems else f"targeting errors: {'; '.join(problems)}")
    except Exception as e:
        check("3. audit_recall_and_precision", 4, False, f"exception: {e}")


def check_4_flags_not_duplicated() -> None:
    """Each unmatched product carries the audit flag exactly once."""
    try:
        products = _load_manifest_products()
        _, unmatched = _manifest_truth()
        bad = [
            name for name in unmatched
            if not products.get(name)
            or products[name]["description"].casefold().count(AUDIT_FLAG.casefold()) != 1
        ]
        check("4. flags_not_duplicated", 2, not bad,
              f"all {len(unmatched)} unmatched products contain one flag"
              if not bad else f"missing or repeated flags: {'; '.join(bad)}")
    except Exception as e:
        check("4. flags_not_duplicated", 2, False, f"exception: {e}")


def check_5_flag_appended_not_replaced() -> None:
    """The flag is appended after the existing description text, not used as
    a replacement for it."""
    try:
        products = _load_manifest_products()
        flagged = [
            (name, products[name]["description"])
            for name in _manifest_truth()[1]
            if products.get(name) and AUDIT_FLAG.casefold() in products[name]["description"].casefold()
        ]
        if not flagged:
            check("5. flag_appended_not_replaced", 2, False,
                  "no flagged products found to verify append position")
            return
        bad = [name for name, desc in flagged if not _visible_text_before_flag(desc)]
        check("5. flag_appended_not_replaced", 2, not bad,
              f"all {len(flagged)} flagged products keep their original text"
              if not bad
              else f"flag replaces original description in: {'; '.join(bad)}")
    except Exception as e:
        check("5. flag_appended_not_replaced", 2, False, f"exception: {e}")


def check_6_flag_targeting_exact() -> None:
    """Store-wide, exactly the unmatched manifest products carry the flag —
    no more, no fewer."""
    try:
        rows = grocy_sql_json(
            "SELECT name FROM products WHERE description LIKE "
            "'%" + AUDIT_FLAG + "%'"
        )
        flagged_names = {r["name"] for r in rows}
        if not flagged_names:
            check("6. flag_targeting_exact", 2, False,
                  "no products flagged anywhere in grocy")
            return
        expected = _manifest_truth()[1]
        extra = sorted(flagged_names - expected)
        missing = sorted(expected - flagged_names)
        problems = []
        if extra:
            problems.append(f"unexpected flags: {'; '.join(extra)}")
        if missing:
            problems.append(f"missing flags: {'; '.join(missing)}")
        check("6. flag_targeting_exact", 2, not problems,
              f"exactly the {len(expected)} unmatched products are flagged"
              if not problems else " — ".join(problems))
    except Exception as e:
        check("6. flag_targeting_exact", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_manifest_products_exist()
    check_2_farmos_logs_match_manifest()
    check_3_audit_recall_and_precision()
    check_4_flags_not_duplicated()
    check_5_flag_appended_not_replaced()
    check_6_flag_targeting_exact()

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
