#!/usr/bin/env python3
"""
Verifier for agriculture_012: Organic Chardonnay 2023 — EU wine label compliance

Checks: 11 weighted checks (15 pts total) on e-label (MSSQL).
Strategy: docker exec sqlcmd for DB, HTTP for label page.

Required env vars:
  SERVER_HOSTNAME, E_LABEL_PORT, E_LABEL_CONTAINER
"""

import os
import sys
import subprocess
import re

# ── Config ────────────────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")
E_LABEL_PORT = os.getenv("E_LABEL_PORT")
E_LABEL_CONTAINER = os.getenv("E_LABEL_CONTAINER")

for _vn, _vv in [("E_LABEL_PORT", E_LABEL_PORT),
                  ("E_LABEL_CONTAINER", E_LABEL_CONTAINER)]:
    if not _vv:
        print(f"FATAL: {_vn} not set", file=sys.stderr)
        sys.exit(1)

DB_CONTAINER = ""
BASE_URL = f"http://{HOST}:{E_LABEL_PORT}"

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


def _resolve_db_container() -> str:
    """e-label may run MSSQL inside the app container or in a '-db' sidecar;
    probe the candidates and use the first one that exists."""
    candidates = [
        os.getenv("E_LABEL_DB_CONTAINER", ""),
        E_LABEL_CONTAINER.replace("-app", "-db") if "-app" in E_LABEL_CONTAINER else "",
        E_LABEL_CONTAINER + "-db",
        E_LABEL_CONTAINER,
    ]
    for name in [c for c in candidates if c]:
        try:
            rc, _, _ = docker_exec(name, "echo", "ok", timeout=5)
            if rc == 0:
                return name
        except Exception:
            continue
    return E_LABEL_CONTAINER


DB_CONTAINER = _resolve_db_container()


def sqlcmd(query: str, timeout: int = 20) -> tuple[int, str, str]:
    """Run SQL against e-label MSSQL. Tries mssql-tools18, then mssql-tools."""
    for tools_dir in ["mssql-tools18", "mssql-tools"]:
        path = f"/opt/{tools_dir}/bin/sqlcmd"
        args = [path, "-S", "localhost", "-U", "sa",
                "-P", "Elabel2024!Strong", "-d", "elabel",
                "-Q", query, "-h", "-1", "-W", "-s", "|"]
        if "18" in tools_dir:
            args.append("-C")
        rc, stdout, stderr = docker_exec(DB_CONTAINER, *args, timeout=timeout)
        if rc == 0:
            return 0, stdout.strip(), stderr.strip()
        if "no such file" not in stderr.lower() and "not found" not in stderr.lower():
            return rc, stdout.strip(), stderr.strip()
    return 1, "", "sqlcmd not found"


def sql_rows(query: str) -> list[list[str]]:
    rc, out, err = sqlcmd(query)
    if rc != 0:
        raise RuntimeError(f"sqlcmd error: {err}")
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or (line.startswith("(") and line.endswith("affected)")):
            continue
        if re.match(r'^Msg \d+', line):
            raise RuntimeError(f"SQL error: {line}")
        rows.append([c.strip() for c in line.split("|")])
    return rows


def sql_scalar(query: str) -> str:
    rows = sql_rows(query)
    if rows and rows[0]:
        val = rows[0][0]
        return "" if val.upper() == "NULL" else val
    return ""


def http_get(path: str, timeout: int = 15) -> tuple[int, str]:
    import urllib.request
    import urllib.error
    try:
        req = urllib.request.Request(f"{BASE_URL}{path}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return 0, str(e)


def llm_judge(content: str, condition: str, timeout: int = 30) -> tuple[bool, str]:
    import requests
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


# ── Product lookup (cached) ──────────────────────────────────────────────────
_product = None
_product_searched = False


def _find_product():
    global _product, _product_searched
    if _product_searched:
        return _product
    _product_searched = True

    sql = (
        "SET NOCOUNT ON; SELECT TOP 1 "
        "CAST(p.Id AS NVARCHAR(36)), "
        "p.Name, "
        "CAST(p.Volume AS NVARCHAR(20)), "
        "CAST(p.WineVintage AS NVARCHAR(10)), "
        "ISNULL(p.WineAppellation, ''), "
        "CAST(p.WineAlcohol AS NVARCHAR(20)), "
        "CAST(p.WineType AS NVARCHAR(10)), "
        "ISNULL(p.FBOName, ''), "
        "CAST(ISNULL(p.Certifications_Organic, 0) AS NVARCHAR(5)), "
        "ISNULL(p.Sku, '') "
        "FROM Product p "
        "WHERE p.WineVintage = 2023 "
        "AND LOWER(LTRIM(RTRIM(p.Name))) = 'boutique organic chardonnay' "
        "ORDER BY p.CreatedOn DESC"
    )
    try:
        rows = sql_rows(sql)
    except RuntimeError:
        return None

    if not rows:
        return None

    c = rows[0]
    _product = {
        "id": c[0],
        "name": c[1] if len(c) > 1 else "",
        "volume": c[2] if len(c) > 2 else "",
        "vintage": c[3] if len(c) > 3 else "",
        "appellation": c[4] if len(c) > 4 else "",
        "alcohol": c[5] if len(c) > 5 else "",
        "wine_type": c[6] if len(c) > 6 else "",
        "fbo_name": c[7] if len(c) > 7 else "",
        "organic": c[8] if len(c) > 8 else "",
        "sku": c[9] if len(c) > 9 else "",
    }
    return _product


# Label page (cached)
_label_html = None
_label_fetched = False


def _fetch_label_page():
    global _label_html, _label_fetched
    if _label_fetched:
        return _label_html
    _label_fetched = True
    prod = _find_product()
    if not prod:
        return None
    sku = prod["sku"]
    code = sku if sku and sku.upper() != "NULL" else prod["id"]
    status, body = http_get(f"/l/{code}")
    if status == 200 and len(body) > 100:
        _label_html = body
        return _label_html
    status2, body2 = http_get(f"/l/{prod['id']}")
    if status2 == 200 and len(body2) > 100:
        _label_html = body2
    return _label_html


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_product_exists():
    """Product record with Boutique Organic Chardonnay 2023 exists."""
    try:
        prod = _find_product()
        if prod:
            check("1. product_record_found", 1, True,
                  f"name='{prod['name']}', id={prod['id']}")
        else:
            check("1. product_record_found", 1, False,
                  "no product with WineVintage=2023 and Name containing 'Boutique' + 'Chardonnay'")
    except Exception as e:
        check("1. product_record_found", 1, False, f"exception: {e}")


def check_2_producer_exact():
    """Producer (FBOName) must match the required value exactly."""
    prod = _find_product()
    if not prod:
        check("2. producer_exact_match", 1, False, "no product found")
        return
    try:
        val = prod["fbo_name"]
        if val.strip().lower() == "boutique organic farm":
            check("2. producer_exact_match", 1, True, f"producer='{val}'")
        else:
            check("2. producer_exact_match", 1, False,
                  f"producer='{val}', expected 'Boutique Organic Farm'")
    except Exception as e:
        check("2. producer_exact_match", 1, False, f"exception: {e}")


def check_3_appellation_nonempty():
    """Appellation (WineAppellation) must reference Loire."""
    prod = _find_product()
    if not prod:
        check("3. appellation_loire", 1, False, "no product found")
        return
    try:
        val = prod["appellation"]
        if val and val.upper() != "NULL" and "loire" in val.lower():
            check("3. appellation_loire", 1, True, f"appellation='{val}'")
        else:
            check("3. appellation_loire", 1, False,
                  f"WineAppellation='{val}', expected to contain 'Loire'")
    except Exception as e:
        check("3. appellation_loire", 1, False, f"exception: {e}")


def check_4_alcohol_value():
    """Alcohol must be 12.5%."""
    prod = _find_product()
    if not prod:
        check("4. alcohol_12_5_pct", 2, False, "no product found")
        return
    try:
        raw = prod["alcohol"]
        if not raw or raw.upper() == "NULL":
            check("4. alcohol_12_5_pct", 2, False, "WineAlcohol is null")
            return
        val = float(raw)
        if abs(val - 12.5) < 0.1:
            check("4. alcohol_12_5_pct", 2, True, f"alcohol={val}%")
        else:
            check("4. alcohol_12_5_pct", 2, False,
                  f"alcohol={val}%, expected 12.5")
    except ValueError:
        check("4. alcohol_12_5_pct", 2, False, f"cannot parse: '{raw}'")
    except Exception as e:
        check("4. alcohol_12_5_pct", 2, False, f"exception: {e}")


def check_5_volume_750():
    """Volume must be 750 mL (stored as 750 or 0.75 L)."""
    prod = _find_product()
    if not prod:
        check("5. volume_750ml", 1, False, "no product found")
        return
    try:
        raw = prod["volume"]
        if not raw or raw.upper() == "NULL":
            check("5. volume_750ml", 1, False, "Volume is null")
            return
        val = float(raw)
        if 749.0 <= val <= 751.0:
            check("5. volume_750ml", 1, True, f"volume={val} mL")
        elif 0.74 <= val <= 0.76:
            check("5. volume_750ml", 1, True, f"volume={val} L")
        else:
            check("5. volume_750ml", 1, False, f"volume={val}, expected ~750 mL or ~0.75 L")
    except ValueError:
        check("5. volume_750ml", 1, False, f"cannot parse: '{raw}'")
    except Exception as e:
        check("5. volume_750ml", 1, False, f"exception: {e}")


def check_6_allergens_sulphites():
    """Allergens must include sulphites (Ingredient Allergen=1 with matching name or E220-E228)."""
    prod = _find_product()
    if not prod:
        check("6. allergens_sulphites", 2, False, "no product found")
        return
    try:
        pid = prod["id"]
        rows = sql_rows(
            f"SET NOCOUNT ON; "
            f"SELECT i.Name, CAST(ISNULL(i.ENumber, 0) AS NVARCHAR(10)) "
            f"FROM ProductIngredient pi "
            f"JOIN Ingredient i ON pi.IngredientId = i.Id "
            f"WHERE pi.ProductId = '{pid}' AND i.Allergen = 1"
        )
        if not rows:
            all_rows = sql_rows(
                f"SET NOCOUNT ON; SELECT i.Name "
                f"FROM ProductIngredient pi "
                f"JOIN Ingredient i ON pi.IngredientId = i.Id "
                f"WHERE pi.ProductId = '{pid}'"
            )
            all_names = [r[0] for r in all_rows] if all_rows else []
            check("6. allergens_sulphites", 2, False,
                  f"no allergen ingredients; all ingredients: {all_names[:5]}")
            return

        has_sulphites = False
        allergen_names = []
        for row in rows:
            name = row[0] if row else ""
            e_num = int(row[1]) if len(row) > 1 and row[1].isdigit() else 0
            allergen_names.append(name)
            if (re.search(r'sulphit|sulfite|sulphur|sulfur|dioxide', name, re.IGNORECASE)
                    or '亚硫酸盐' in name
                    or 220 <= e_num <= 228):
                has_sulphites = True

        if has_sulphites:
            check("6. allergens_sulphites", 2, True, f"allergens={allergen_names}")
        else:
            check("6. allergens_sulphites", 2, False,
                  f"allergens found but none match sulphites: {allergen_names}")
    except Exception as e:
        check("6. allergens_sulphites", 2, False, f"exception: {e}")


def check_7_organic_certified():
    """Organic certification flag should be set."""
    prod = _find_product()
    if not prod:
        check("7. organic_certified", 1, False, "no product found")
        return
    try:
        val = prod["organic"]
        if val in ("1", "True", "true"):
            check("7. organic_certified", 1, True)
        else:
            check("7. organic_certified", 1, False, f"Organic={val}, expected 1")
    except Exception as e:
        check("7. organic_certified", 1, False, f"exception: {e}")


def check_8_wine_type_white():
    """Wine type must be White (enum value 1) for Chardonnay."""
    prod = _find_product()
    if not prod:
        check("8. wine_type_white", 1, False, "no product found")
        return
    try:
        val = prod["wine_type"]
        if val == "1":
            check("8. wine_type_white", 1, True)
        else:
            check("8. wine_type_white", 1, False, f"WineType={val}, expected 1 (White)")
    except Exception as e:
        check("8. wine_type_white", 1, False, f"exception: {e}")


def check_9_label_alcohol_pct_vol():
    """Label page must display alcohol with '% vol' format (EU standard)."""
    prod = _find_product()
    if not prod:
        check("9. alcohol_pct_vol_format", 1, False, "no product found")
        return
    try:
        html = _fetch_label_page()
        if html is None:
            check("9. alcohol_pct_vol_format", 1, False, "label page not accessible")
            return
        if re.search(r'%\s*vol', html, re.IGNORECASE):
            check("9. alcohol_pct_vol_format", 1, True)
        else:
            check("9. alcohol_pct_vol_format", 1, False,
                  "'% vol' not found on label page")
    except Exception as e:
        check("9. alcohol_pct_vol_format", 1, False, f"exception: {e}")


def check_10_sensory_fields():
    """Serving information is specific and appropriate for Chardonnay."""
    prod = _find_product()
    if not prod:
        check("10. sensory_fields_chardonnay", 2, False, "no product found")
        return
    try:
        html = _fetch_label_page()
        if html is None:
            check("10. sensory_fields_chardonnay", 2, False,
                  "label page not accessible")
            return
        text = re.sub(r'<[^>]+>', ' ', html)
        text = re.sub(r'\s+', ' ', text).strip()
        passed, raw = llm_judge(
            text[:4000],
            "The content includes ALL Chardonnay serving details: (1) a serving "
            "temperature range within 8-12 degrees Celsius, (2) a Chardonnay or "
            "white-wine glass, (3) at least two specific food dishes appropriate "
            "for Chardonnay, and (4) a concise tasting description with a concrete "
            "aroma, acidity, body, or finish characteristic. Generic placeholders "
            "or missing fields must result in NO."
        )
        check("10. sensory_fields_chardonnay", 2, passed, f"llm_judge: {raw}")
    except Exception as e:
        check("10. sensory_fields_chardonnay", 2, False, f"exception: {e}")


def check_11_label_page_accessible():
    """Public label page (QR code target) must be accessible."""
    prod = _find_product()
    if not prod:
        check("11. label_page_accessible", 2, False, "no product found")
        return
    try:
        html = _fetch_label_page()
        if html is not None:
            has_wine = ("chardonnay" in html.lower() or "boutique" in html.lower()
                        or prod["name"].lower().split()[0] in html.lower())
            check("11. label_page_accessible", 2, has_wine,
                  "required wine identity found" if has_wine
                  else "page reachable but required wine identity is absent")
        else:
            sku = prod["sku"]
            code = sku if sku and sku.upper() != "NULL" else prod["id"]
            check("11. label_page_accessible", 2, False,
                  f"label page /l/{code} not accessible")
    except Exception as e:
        check("11. label_page_accessible", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_product_exists()
    check_2_producer_exact()
    check_3_appellation_nonempty()
    check_4_alcohol_value()
    check_5_volume_750()
    check_6_allergens_sulphites()
    check_7_organic_certified()
    check_8_wine_type_white()
    check_9_label_alcohol_pct_vol()
    check_10_sensory_fields()
    check_11_label_page_accessible()

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
