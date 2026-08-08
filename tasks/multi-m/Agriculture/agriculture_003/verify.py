"""
Verifier for agriculture_003: Create Pinot Noir 2024 digital wine label in e-label

Checks: 10 weighted checks across e-label.
Strategy: docker exec MSSQL (sqlcmd) for all checks.

Required env vars:
  SERVER_HOSTNAME, E_LABEL_PORT, E_LABEL_CONTAINER
"""

import os
import sys
import subprocess
import re

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

E_LABEL_PORT = os.getenv("E_LABEL_PORT")
E_LABEL_CONTAINER = os.getenv("E_LABEL_CONTAINER")
BASE_URL = f"http://{HOST}:{E_LABEL_PORT}"

_missing = []
for var in ["E_LABEL_PORT", "E_LABEL_CONTAINER"]:
    if not os.getenv(var):
        _missing.append(var)
if _missing:
    print(f"FATAL: {', '.join(_missing)} not set", file=sys.stderr)
    sys.exit(1)


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


def _find_db_container(app_container: str, env_var: str, fallback_names: list[str]) -> str:
    candidates = [
        os.getenv(env_var, ""),
        app_container + "-db",
    ]
    if "-" in app_container:
        candidates.append(app_container.rsplit("-", 1)[0] + "-db")
    candidates.extend(fallback_names)
    for name in [c for c in candidates if c]:
        try:
            rc, _, _ = docker_exec(name, "echo", "ok", timeout=5)
            if rc == 0:
                return name
        except Exception:
            continue
    return app_container + "-db"


E_LABEL_DB = _find_db_container(E_LABEL_CONTAINER, "E_LABEL_DB_CONTAINER", ["elabel-db", "elabel_db"])


def sqlcmd(query: str, timeout: int = 15) -> tuple[int, str, str]:
    for path in ["/opt/mssql-tools18/bin/sqlcmd", "/opt/mssql-tools/bin/sqlcmd"]:
        rc, stdout, stderr = docker_exec(
            E_LABEL_DB, path,
            "-S", "localhost", "-U", "sa", "-P", "Elabel2024!Strong",
            "-d", "elabel", "-C", "-h", "-1", "-s", "|", "-W",
            "-Q", query,
            timeout=timeout,
        )
        if rc == 0 or "not found" not in stderr.lower():
            return rc, stdout.strip(), stderr
    return rc, stdout.strip(), stderr


def _parse_sqlcmd_rows(stdout: str) -> list[list[str]]:
    rows = []
    for line in stdout.split("\n"):
        line = line.strip()
        if not line or "rows affected" in line.lower():
            continue
        rows.append([c.strip() for c in line.split("|")])
    return rows


# ── Shared state loaded once ────────────────────────────────────────────────
_product: dict = {}


def _load_product() -> bool:
    global _product
    # Target only the exact product this task requires (name 'Estate Pinot
    # Noir'); the seed DB ships near-miss decoys such as 'Pinot Noir Provence
    # 2024', 'Fairview Estate Pinot Meunier 2022' and 'Heatherwood Estate
    # Pinot Noir 2023', so a loose match would lock onto a pre-existing record.
    query = (
        "SELECT TOP 1 "
        "  Name, FBOName, WineVintage, WineAppellation, WineAlcohol, Volume, "
        "  CAST(Id AS NVARCHAR(36)), Brand, WineType, ISNULL(Sku, '') "
        "FROM Product "
        "WHERE LOWER(LTRIM(RTRIM(Name))) = 'estate pinot noir' "
        "ORDER BY CreatedOn DESC"
    )
    rc, stdout, _ = sqlcmd(query)
    rows = _parse_sqlcmd_rows(stdout) if rc == 0 else []
    if rows and len(rows[0]) >= 7:
        r = rows[0]
        _product.update({
            "name": r[0], "fbo_name": r[1], "vintage": r[2],
            "appellation": r[3], "alcohol": r[4], "volume": r[5],
            "id": r[6], "brand": r[7] if len(r) > 7 else "",
            "wine_type": r[8] if len(r) > 8 else "",
            "sku": r[9] if len(r) > 9 else "",
        })
        return True
    return False


# ── Checks ────────────────────────────────────────────────────────────────────

def check_1_product_exists() -> None:
    found = _load_product()
    check("1. wine product exists", 1, found,
          f"name='{_product.get('name', '')}'" if found else "no matching product found")


def check_2_producer() -> None:
    fbo = _product.get("fbo_name", "")
    ok = fbo.lower().strip() == "boutique organic farm" if fbo and fbo.lower() not in ("null", "none", "") else False
    check("2. producer = 'Boutique Organic Farm'", 2, ok, f"FBOName='{fbo}'")


def check_3_vintage() -> None:
    v = _product.get("vintage", "")
    try:
        year = int(v)
        ok = year == 2024
    except (ValueError, TypeError):
        ok = False
        year = v
    check("3. vintage = 2024", 2, ok, f"vintage={year}")


def check_4_appellation() -> None:
    app = (_product.get("appellation", "") or "").lower()
    ok = any(t in app for t in ["burgundy", "bourgogne"]) if app else False
    check("4. appellation contains Burgundy/Bourgogne", 2, ok,
          f"appellation='{_product.get('appellation', '')}'")


def check_5_alcohol() -> None:
    try:
        alc = float(_product.get("alcohol", "0"))
        ok = abs(alc - 13.5) < 0.1
    except (ValueError, TypeError):
        alc = _product.get("alcohol", "")
        ok = False
    check("5. alcohol = 13.5%", 2, ok, f"alcohol={alc}")


def check_6_volume() -> None:
    try:
        vol = float(_product.get("volume", "0"))
        ok = abs(vol - 750.0) < 1.0 or abs(vol - 0.75) < 0.01
    except (ValueError, TypeError):
        vol = _product.get("volume", "")
        ok = False
    check("6. volume = 750 mL", 1, ok, f"volume={vol}")


def check_7_wine_type_red() -> None:
    wt = (_product.get("wine_type", "") or "").strip()
    try:
        ok = int(wt) == 2
    except (ValueError, TypeError):
        ok = False
    check("7. wine type = Red", 1, ok, f"WineType={wt} (expected 2=Red)")


def check_8_product_image() -> None:
    product_id = _product.get("id", "")
    if not product_id:
        check("8. product image uploaded", 1, False, "no product found")
        return
    rc, stdout, _ = sqlcmd(
        f"SELECT COUNT(*) FROM Image WHERE ProductId = '{product_id}'"
    )
    rows = _parse_sqlcmd_rows(stdout) if rc == 0 else []
    try:
        count = int(rows[0][0]) if rows and rows[0] else 0
    except ValueError:
        count = 0
    check("8. product image uploaded", 1, count >= 1, f"image_count={count}")


def check_9_sulphites_allergen() -> None:
    product_id = _product.get("id", "")
    if not product_id:
        check("9. sulphites allergen declared", 2, False, "no product found")
        return
    query = (
        "SELECT i.Name, i.Allergen "
        "FROM ProductIngredient pi "
        "JOIN Ingredient i ON pi.IngredientId = i.Id "
        f"WHERE pi.ProductId = '{product_id}' AND i.Allergen = 1"
    )
    rc, stdout, _ = sqlcmd(query)
    rows = _parse_sqlcmd_rows(stdout) if rc == 0 else []
    sulphite_found = any(
        any(t in r[0].lower() for t in ["sulph", "sulfite", "bisulph", "metabisulph", "so2", "sulfit"])
        for r in rows if r
    )
    ok = sulphite_found
    detail = "sulphite allergen found" if ok else (
        f"{len(rows)} non-sulphite allergen(s)" if rows else "no allergen ingredients linked"
    )
    check("9. sulphites allergen declared", 2, ok, detail)


def check_10_public_label_page() -> None:
    """The public page targeted by the QR code is reachable and shows this wine."""
    product_id = _product.get("id", "")
    sku = (_product.get("sku", "") or "").strip()
    if not product_id:
        check("10. public label page shows required wine", 2, False, "no product found")
        return
    try:
        import urllib.error
        import urllib.request

        candidates = [sku, product_id] if sku and sku.upper() != "NULL" else [product_id]
        for code in dict.fromkeys(candidates):
            try:
                with urllib.request.urlopen(f"{BASE_URL}/l/{code}", timeout=15) as response:
                    body = response.read().decode("utf-8", errors="replace")
                text = re.sub(r"<[^>]+>", " ", body).lower()
                has_identity = "estate pinot noir" in text
                has_vintage = "2024" in text
                has_alcohol = re.search(r"13[.,]5\s*%?\s*vol", text) is not None
                if response.status == 200 and has_identity and has_vintage and has_alcohol:
                    check("10. public label page shows required wine", 2, True,
                          f"reachable at /l/{code}")
                    return
            except (urllib.error.URLError, TimeoutError):
                continue
        check("10. public label page shows required wine", 2, False,
              "no public label page contained the required name, vintage, and alcohol")
    except Exception as e:
        check("10. public label page shows required wine", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_product_exists()
    check_2_producer()
    check_3_vintage()
    check_4_appellation()
    check_5_alcohol()
    check_6_volume()
    check_7_wine_type_red()
    check_8_product_image()
    check_9_sulphites_allergen()
    check_10_public_label_page()

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
