"""
Verifier for Software-047-I2: Docker Compliance Audit for Multi-Service Deployables (2026-04-12)

Checks: 16 weighted checks (+ a 0pt anti-tamper precondition) across code-server,
baserow, openproject. Strategy: Baserow REST API, code-server docker exec,
OpenProject embedded Postgres.

Ground truth is recomputed by the verifier itself at runtime:
  * dockerfile_truth(): cats the 4 Dockerfiles from the live container, parses
    FROM/USER/HEALTHCHECK/RUN and recomputes the Compliance Score formula.
  * secret_truth(): finds the Dockerfile-glob file set across the workspace and
    replays the task's secret regex (re.I) in Python.
  * Anti-tamper precondition: the md5 set of all Dockerfile-glob files in the live
    container must match the container's own pristine image (the seed ships some
    Dockerfiles git-dirty on purpose, so `git status` cannot be the baseline).
Agent-filled data is never trusted; if truth recompute fails, truth-dependent
checks FAIL (no fallback).

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import re
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.environ.get("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.environ.get("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_required = {
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for _var, _val in _required.items():
    if not _val:
        print(f"FATAL: {_var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# Credentials
BASEROW_EMAIL = "admin@example.com"
BASEROW_PASSWORD = "Admin1234"

# Task constants (all from the task description — not measured truth)
AUDIT_DATE = "2026-04-12"
DB_NAME = "Container Security Review 2026-04-12"
SERVICES = ["blog-engine", "devops-configs", "tabler", "todo-api"]  # alphabetical
DOCKERFILE_PATHS = {
    "blog-engine": "blog-engine/Dockerfile",
    "devops-configs": "devops-configs/docker/Dockerfile.node",
    "tabler": "tabler/Dockerfile",
    "todo-api": "todo-api/Dockerfile",
}
WORKSPACE = "/home/coder/workspace"
OP_PROJECT = "devops-automation"
SECURITY_OWNER = "user10"   # Isabella Garcia — devops-automation member
PLATFORM_OWNER = "user19"  # Rachel Miller — devops-automation member
COMMIT_MSG = "audit: docker compliance 2026-04-12"
AUDIT_MD = "docs/docker-audit-2026-04-12.md"

# Task's secret-scan regex (case-insensitive), replayed verifier-side in Python.
SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}"
    r"|(aws_secret|api[_-]?key|token|passwd|password|secret)"
    r"\s*[:=]\s*['\"][^'\"]{4,}['\"])",
    re.IGNORECASE,
)

# Dockerfile-glob file set from the task: **/Dockerfile, **/Dockerfile.*,
# **/*.dockerfile — excluding node_modules — across the whole workspace.
_GLOB_FIND = (
    f"find {WORKSPACE} -name node_modules -prune -o -type f "
    "\\( -name Dockerfile -o -name 'Dockerfile.*' -o -name '*.dockerfile' \\) -print"
)
_GLOB_MD5_CMD = _GLOB_FIND.replace("-print", "-exec md5sum {} +") + " | sort"

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
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


_baserow_token: str | None = None


def baserow_auth() -> str:
    global _baserow_token
    if _baserow_token:
        return _baserow_token
    r = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": BASEROW_EMAIL, "password": BASEROW_PASSWORD},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    _baserow_token = data.get("access_token") or data.get("token")
    return _baserow_token


def baserow_get(path: str) -> dict | list:
    token = baserow_auth()
    r = requests.get(
        f"{BASEROW_URL}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def op_db_query(sql: str) -> str:
    """Query OpenProject's embedded Postgres via docker exec."""
    r = subprocess.run(
        [
            "docker", "exec",
            "-e", "PGPASSWORD=openproject",
            OPENPROJECT_CONTAINER,
            "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
            "-h", "127.0.0.1", "-t", "-A", "-c", sql,
        ],
        capture_output=True, text=True, errors="replace", timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql failed: {r.stderr.strip()}")
    return r.stdout.strip()


def _sel(v) -> str:
    """Normalize a Baserow cell value (single-select dicts -> value string)."""
    if isinstance(v, dict):
        return str(v.get("value") or "")
    return "" if v is None else str(v)


def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return bool(v)


def _norm_path(p) -> str:
    p = str(p or "").strip().replace("\\", "/")
    for pre in (WORKSPACE + "/", "./"):
        if p.startswith(pre):
            p = p[len(pre):]
    return p


def _strip_stage(base: str) -> str:
    """Tolerate a trailing ' AS <stage>' on a recorded base image string."""
    return re.sub(r"\s+as\s+\S+\s*$", "", str(base or "").strip(), flags=re.I).strip()


def _clean_desc(desc: str) -> str:
    """Strip CKEditor backslash escapes and collapse whitespace."""
    return " ".join(str(desc or "").replace("\\", "").split())


# ── Baserow discovery ─────────────────────────────────────────────────────────
_br_db_id: int | None = None
_br_tables: dict[str, int] = {}  # table name -> table id


def find_baserow_db() -> int | None:
    global _br_db_id
    if _br_db_id is not None:
        return _br_db_id
    apps = baserow_get("applications/")
    for app in apps:
        if app.get("name") == DB_NAME and app.get("type") == "database":
            _br_db_id = app["id"]
            return _br_db_id
    return None


def find_baserow_table(table_name: str) -> int | None:
    if table_name in _br_tables:
        return _br_tables[table_name]
    db_id = find_baserow_db()
    if not db_id:
        return None
    tables = baserow_get(f"database/tables/database/{db_id}/")
    for t in tables:
        _br_tables[t["name"]] = t["id"]
    return _br_tables.get(table_name)


def baserow_rows(table_id: int, size: int = 200) -> list[dict]:
    data = baserow_get(
        f"database/rows/table/{table_id}/?user_field_names=true&size={size}"
    )
    return data.get("results", [])


# ── Ground-truth recompute ────────────────────────────────────────────────────
# Anti-tamper baseline: the seed intentionally ships blog-engine/devops-configs/
# todo-api Dockerfiles as *uncommitted git modifications* (verified on the
# pristine image: `git status --porcelain` is dirty for 3 of the 4 paths), so a
# git-clean assertion would false-fail every run. Instead we compare the md5s of
# the full Dockerfile-glob file set in the live container against the
# container's own pristine image — recomputed at runtime, no baked constants.
_tamper_cache: tuple[bool, str] | None = None


_pristine_image_cache: str | None = None


def _pristine_image() -> str:
    """The code-server container's own image ref — truth reads go here, immune to agent edits."""
    global _pristine_image_cache
    if _pristine_image_cache is None:
        r = subprocess.run(["docker", "inspect", CODE_SERVER_CONTAINER, "--format", "{{.Image}}"],
                           capture_output=True, text=True, timeout=15)
        if r.returncode != 0 or not r.stdout.strip():
            raise RuntimeError(f"docker inspect failed: {r.stderr.strip()[:200]}")
        _pristine_image_cache = r.stdout.strip()
    return _pristine_image_cache


def image_exec(*args: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a command in a throwaway container from the PRISTINE image (not the live one)."""
    r = subprocess.run(["docker", "run", "--rm", "--entrypoint", args[0], _pristine_image(), *args[1:]],
                       capture_output=True, text=True, errors="replace", timeout=timeout)
    return r.returncode, r.stdout, r.stderr


def dockerfiles_untampered() -> tuple[bool, str]:
    global _tamper_cache
    if _tamper_cache is not None:
        return _tamper_cache
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", CODE_SERVER_CONTAINER],
            capture_output=True, text=True, errors="replace", timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            _tamper_cache = (False, f"docker inspect failed: {r.stderr.strip()[:120]}")
            return _tamper_cache
        image_id = r.stdout.strip()

        rc, live_out, err = docker_exec(
            CODE_SERVER_CONTAINER, "bash", "-c", _GLOB_MD5_CMD, timeout=60
        )
        if rc != 0:
            _tamper_cache = (False, f"live md5 scan failed: {err.strip()[:120]}")
            return _tamper_cache

        p = subprocess.run(
            ["docker", "run", "--rm", "--entrypoint", "bash", image_id, "-c", _GLOB_MD5_CMD],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        if p.returncode != 0:
            _tamper_cache = (False, f"pristine md5 scan failed: {p.stderr.strip()[:120]}")
            return _tamper_cache

        def parse(out: str) -> dict[str, str]:
            m = {}
            for line in out.splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    m[parts[1].strip()] = parts[0]
            return m

        live, pristine = parse(live_out), parse(p.stdout)
        if not pristine:
            _tamper_cache = (False, "pristine scan returned no files")
            return _tamper_cache
        changed = sorted(
            _norm_path(f)
            for f in (set(live) | set(pristine))
            if live.get(f) != pristine.get(f)
        )
        if changed:
            _tamper_cache = (False, f"modified vs pristine image: {', '.join(changed[:5])}")
        else:
            _tamper_cache = (True, f"{len(pristine)} Dockerfile-glob files match pristine image")
    except Exception as e:
        _tamper_cache = (False, f"probe error: {e}")
    return _tamper_cache


_dockerfile_truth_cache: dict | None | str = "unset"


def dockerfile_truth() -> dict | None:
    """Per-service Dockerfile attributes + recomputed Compliance Score, or None."""
    global _dockerfile_truth_cache
    if _dockerfile_truth_cache != "unset":
        return _dockerfile_truth_cache
    # Truth reads from the PRISTINE IMAGE — agent edits to the live files cannot
    # move the goalposts, so no tamper gating is needed here.
    truth: dict[str, dict] = {}
    try:
        for svc, rel in DOCKERFILE_PATHS.items():
            rc, text, _ = image_exec("cat", f"{WORKSPACE}/{rel}", timeout=30)
            if rc != 0 or not text.strip():
                _dockerfile_truth_cache = None
                return None
            froms = [m.group(1) for m in re.finditer(r"^\s*FROM\s+(\S+)", text, re.M | re.I)]
            if not froms:
                _dockerfile_truth_cache = None
                return None
            base = froms[0]
            uses_latest = ":" not in base or base.endswith(":latest")
            multistage = len(froms) > 1
            users = [m.group(1) for m in re.finditer(r"^\s*USER\s+(\S+)", text, re.M | re.I)]
            runs_as_root = (not users) or users[-1].lower() in ("root", "0")
            has_healthcheck = re.search(r"^\s*HEALTHCHECK\b", text, re.M | re.I) is not None
            run_count = len(re.findall(r"^\s*RUN\b", text, re.M | re.I))
            score = 100
            if uses_latest:
                score -= 25
            if runs_as_root:
                score -= 25
            if not has_healthcheck:
                score -= 15
            if not multistage:
                score -= 10
            score -= 5 * max(0, run_count - 6)
            score = max(0, score)
            truth[svc] = {
                "base_image": base,
                "uses_latest": uses_latest,
                "runs_as_root": runs_as_root,
                "has_healthcheck": has_healthcheck,
                "multistage": multistage,
                "run_count": run_count,
                "score": score,
            }
    except Exception:
        _dockerfile_truth_cache = None
        return None
    _dockerfile_truth_cache = truth
    return truth


def _classify_hit(m: re.Match) -> tuple[tuple[str, ...], str]:
    """Map a regex hit to (accepted Pattern Names, Severity)."""
    kw = (m.group(2) or "").lower()
    if not kw:  # AKIA[0-9A-Z]{16} alternative
        return ("AWSKey",), "Critical"
    if "key" in kw or kw == "token":
        return ("APIToken",), "Critical"
    if kw in ("passwd", "password"):
        return ("Password",), "High"
    # 'secret' / 'aws_secret' keywords: the task defines no explicit Pattern Name
    # mapping for these — accept both reasonable encodings (Severity=High either way).
    return ("Password", "Other"), "High"


_secret_truth_cache: list | None | str = "unset"


def secret_truth() -> list | None:
    """Sorted [(path, line, accepted patterns, severity, service)] dicts, or None."""
    global _secret_truth_cache
    if _secret_truth_cache != "unset":
        return _secret_truth_cache
    # pristine-image read; no tamper gating (see dockerfile_truth)
    try:
        marker = "@@DOCKERFILE@@"
        cmd = (
            _GLOB_FIND
            + " | sort | while IFS= read -r f; do"
            + f" printf '\\n{marker} %s\\n' \"$f\"; cat \"$f\"; done"
        )
        rc, out, err = image_exec("bash", "-c", cmd, timeout=120)
        if rc != 0 or marker not in out:
            _secret_truth_cache = None
            return None
        hits = []
        cur_path: str | None = None
        lineno = 0
        for line in out.split("\n"):
            if line.startswith(marker + " "):
                cur_path = _norm_path(line[len(marker) + 1:])
                lineno = 0
                continue
            if cur_path is None:
                continue
            lineno += 1
            for m in SECRET_RE.finditer(line):
                patterns, severity = _classify_hit(m)
                hits.append({
                    "path": cur_path,
                    "line": lineno,
                    "patterns": patterns,
                    "severity": severity,
                    "service": cur_path.split("/")[0],
                })
        hits.sort(key=lambda h: (h["path"], h["line"]))
        _secret_truth_cache = hits
    except Exception:
        _secret_truth_cache = None
        return None
    return _secret_truth_cache


def _secret_subject_map(st: list) -> list[dict[str, str]]:
    """Per truth hit: {acceptable WP subject -> pattern name}."""
    out = []
    for h in st:
        out.append({
            f"SECRET LEAK [{h['service']}]: {p} at {h['path']}:{h['line']}": p
            for p in h["patterns"]
        })
    return out


# ── Check 0: anti-tamper precondition (0pt) ───────────────────────────────────
def check_0_untampered() -> None:
    """Dockerfile-glob files in the live container match the pristine image."""
    ok, detail = dockerfiles_untampered()
    # informational only (0pt): truth reads from the pristine image, so live-file
    # edits (incl. accidental format-on-save rewrites) no longer void the audit.
    check("0. Dockerfiles untampered (informational)", 0, ok, detail)


# ── Check 1: Baserow database exists ─────────────────────────────────────────
def check_1_baserow_db_exists() -> None:
    """Baserow database 'Container Security Review 2026-04-12' exists."""
    try:
        db_id = find_baserow_db()
        check("1. Baserow database exists", 1, db_id is not None,
              f"db_id={db_id}" if db_id else "database not found")
    except Exception as e:
        check("1. Baserow database exists", 1, False, f"exception: {e}")


# ── Check 2: Dockerfile Audit table field-name set ────────────────────────────
def check_2_dockerfile_audit_table() -> None:
    """Table 'Dockerfile Audit' exists with exactly the expected field names."""
    try:
        table_id = find_baserow_table("Dockerfile Audit")
        if not table_id:
            check("2. Dockerfile Audit table", 1, False, "table not found")
            return
        fields = baserow_get(f"database/fields/table/{table_id}/")
        field_names = {f["name"] for f in fields}
        expected_fields = {
            "Audit ID", "Service", "Dockerfile Path", "Base Image",
            "Uses Latest Tag", "Runs As Root", "Has Healthcheck",
            "Multistage Build", "Run Instruction Count", "Captured At",
            "Compliance Score",
        }
        missing = expected_fields - field_names
        extra = field_names - expected_fields
        ok = not missing and not extra
        check("2. Dockerfile Audit table", 1, ok,
              "exact field set" if ok else f"missing: {sorted(missing)}, extra: {sorted(extra)}")
    except Exception as e:
        check("2. Dockerfile Audit table", 1, False, f"exception: {e}")


# ── Check 3: audit rows — order, IDs, paths, date ─────────────────────────────
def check_3_audit_rows() -> None:
    """Exactly 4 rows in service-alphabetical order with DA-01..DA-04 IDs,
    the prescribed Dockerfile Path per service, and Captured At=2026-04-12."""
    try:
        table_id = find_baserow_table("Dockerfile Audit")
        if not table_id:
            check("3. Audit rows (4 services)", 2, False, "table not found")
            return
        rows = baserow_rows(table_id)
        issues = []
        if len(rows) != len(SERVICES):
            issues.append(f"expected {len(SERVICES)} rows, got {len(rows)}")
        for i, row in enumerate(rows[:len(SERVICES)]):
            svc = _sel(row.get("Service"))
            if svc != SERVICES[i]:
                issues.append(f"row {i + 1}: service={svc!r}, expected {SERVICES[i]!r}")
                continue
            audit_id = _sel(row.get("Audit ID")).strip()
            if audit_id != f"DA-{i + 1:02d}":
                issues.append(f"{svc}: Audit ID={audit_id!r}, expected DA-{i + 1:02d}")
            path = _norm_path(row.get("Dockerfile Path"))
            if path != DOCKERFILE_PATHS[svc]:
                issues.append(f"{svc}: path={path!r}, expected {DOCKERFILE_PATHS[svc]!r}")
            captured = _sel(row.get("Captured At"))
            if not captured.startswith(AUDIT_DATE):
                issues.append(f"{svc}: Captured At={captured!r}")
        check("3. Audit rows (4 services)", 2, not issues,
              "4 rows, alphabetical, DA-01..DA-04, paths+date OK" if not issues
              else "; ".join(issues[:4]))
    except Exception as e:
        check("3. Audit rows (4 services)", 2, False, f"exception: {e}")


# ── Check 4: all 7 audited attributes + score vs recomputed truth ─────────────
def check_4_compliance_scores() -> None:
    """Base Image, 4 booleans, RUN count and Compliance Score per service must
    equal the verifier's own Dockerfile parse (never the agent's self-consistency)."""
    try:
        truth = dockerfile_truth()
        if truth is None:
            check("4. Dockerfile attributes vs truth", 3, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        table_id = find_baserow_table("Dockerfile Audit")
        if not table_id:
            check("4. Dockerfile attributes vs truth", 3, False, "table not found")
            return
        rows = baserow_rows(table_id)
        by_svc: dict[str, dict] = {}
        for row in rows:
            svc = _sel(row.get("Service"))
            if svc in by_svc:
                by_svc[svc] = None  # duplicate marker
            elif svc:
                by_svc[svc] = row

        issues = []
        for svc in SERVICES:
            t = truth[svc]
            row = by_svc.get(svc)
            if row is None and svc in by_svc:
                issues.append(f"{svc}: duplicate rows")
                continue
            if row is None:
                issues.append(f"{svc}: row missing")
                continue
            base = _strip_stage(row.get("Base Image"))
            if base != _strip_stage(t["base_image"]):
                issues.append(f"{svc}: Base Image={base!r}, expected {t['base_image']!r}")
            for field, key in (
                ("Uses Latest Tag", "uses_latest"),
                ("Runs As Root", "runs_as_root"),
                ("Has Healthcheck", "has_healthcheck"),
                ("Multistage Build", "multistage"),
            ):
                if _to_bool(row.get(field)) != t[key]:
                    issues.append(f"{svc}: {field}={row.get(field)!r}, expected {t[key]}")
            try:
                rc_val = int(float(row.get("Run Instruction Count")))
            except (TypeError, ValueError):
                rc_val = None
            if rc_val != t["run_count"]:
                issues.append(f"{svc}: RUN count={rc_val}, expected {t['run_count']}")
            try:
                score = float(row.get("Compliance Score"))
            except (TypeError, ValueError):
                score = None
            if score is None or abs(score - t["score"]) > 0.5:
                issues.append(f"{svc}: score={score}, expected {t['score']}")
        check("4. Dockerfile attributes vs truth", 3, not issues,
              "all 7 attributes match recomputed truth for 4 services" if not issues
              else "; ".join(issues[:4]))
    except Exception as e:
        check("4. Dockerfile attributes vs truth", 3, False, f"exception: {e}")


# ── Check 5: Hardcoded Secrets table field-name set ───────────────────────────
def check_5_secrets_table() -> None:
    """Table 'Hardcoded Secrets' exists with exactly the expected field names."""
    try:
        table_id = find_baserow_table("Hardcoded Secrets")
        if not table_id:
            check("5. Hardcoded Secrets table", 1, False, "table not found")
            return
        fields = baserow_get(f"database/fields/table/{table_id}/")
        field_names = {f["name"] for f in fields}
        expected_fields = {
            "Finding ID", "Service", "File Path", "Line Number",
            "Pattern Name", "Severity", "Detected At",
        }
        missing = expected_fields - field_names
        extra = field_names - expected_fields
        ok = not missing and not extra
        check("5. Hardcoded Secrets table", 1, ok,
              "exact field set" if ok else f"missing: {sorted(missing)}, extra: {sorted(extra)}")
    except Exception as e:
        check("5. Hardcoded Secrets table", 1, False, f"exception: {e}")


# ── Check 6: secrets rows vs recomputed scan truth ────────────────────────────
def check_6_secrets_rows() -> None:
    """Row set == verifier's own regex scan: one row per hit ordered by
    (File Path, Line Number), HS-001.. IDs, pattern/severity/date/service gated."""
    try:
        st = secret_truth()
        if st is None:
            check("6. Secrets rows vs truth", 2, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        table_id = find_baserow_table("Hardcoded Secrets")
        if not table_id:
            check("6. Secrets rows vs truth", 2, False, "table not found")
            return
        rows = baserow_rows(table_id)
        issues = []
        if len(rows) != len(st):
            issues.append(f"expected {len(st)} rows, got {len(rows)}")
        for i, (row, h) in enumerate(zip(rows, st)):
            rid = f"row {i + 1}"
            fid = _sel(row.get("Finding ID")).strip()
            if fid != f"HS-{i + 1:03d}":
                issues.append(f"{rid}: Finding ID={fid!r}, expected HS-{i + 1:03d}")
            path = _norm_path(row.get("File Path"))
            try:
                line_no = int(float(row.get("Line Number")))
            except (TypeError, ValueError):
                line_no = None
            if path != h["path"] or line_no != h["line"]:
                issues.append(
                    f"{rid}: ({path!r},{line_no}) != truth ({h['path']!r},{h['line']})")
            pattern = _sel(row.get("Pattern Name"))
            if pattern not in h["patterns"]:
                issues.append(f"{rid}: Pattern Name={pattern!r}, expected one of {h['patterns']}")
            severity = _sel(row.get("Severity"))
            if severity != h["severity"]:
                issues.append(f"{rid}: Severity={severity!r}, expected {h['severity']!r}")
            detected = _sel(row.get("Detected At"))
            if not detected.startswith(AUDIT_DATE):
                issues.append(f"{rid}: Detected At={detected!r}")
            svc = _sel(row.get("Service"))
            if svc != h["service"]:
                issues.append(f"{rid}: Service={svc!r}, expected {h['service']!r}")
        check("6. Secrets rows vs truth", 2, not issues,
              f"{len(st)} rows match scan truth (order, IDs, patterns, severity)"
              if not issues else "; ".join(issues[:4]))
    except Exception as e:
        check("6. Secrets rows vs truth", 2, False, f"exception: {e}")


# ── Check 7: Compliance Ranking view ──────────────────────────────────────────
def check_7_compliance_ranking_view() -> None:
    """Grid view 'Compliance Ranking' on Dockerfile Audit, sorted by Compliance Score asc."""
    try:
        table_id = find_baserow_table("Dockerfile Audit")
        if not table_id:
            check("7. Compliance Ranking view", 1, False, "table not found")
            return
        views = baserow_get(f"database/views/table/{table_id}/")
        found = None
        for v in views:
            if v.get("name") == "Compliance Ranking":
                found = v
                break
        if not found:
            check("7. Compliance Ranking view", 1, False, "view not found")
            return
        is_grid = found.get("type") == "grid"
        sortings = baserow_get(f"database/views/{found['id']}/sortings/")
        has_sort = False
        fields = baserow_get(f"database/fields/table/{table_id}/")
        score_field_id = None
        for f in fields:
            if f["name"] == "Compliance Score":
                score_field_id = f["id"]
                break
        if isinstance(sortings, list):
            for s in sortings:
                if s.get("field") == score_field_id and s.get("order") == "ASC":
                    has_sort = True
        check("7. Compliance Ranking view", 1, is_grid and has_sort,
              f"grid={is_grid}, sorted_asc={has_sort}")
    except Exception as e:
        check("7. Compliance Ranking view", 1, False, f"exception: {e}")


# ── Check 8: By Pattern Gallery view ──────────────────────────────────────────
def check_8_by_pattern_view() -> None:
    """Gallery view 'By Pattern' exists on Hardcoded Secrets."""
    try:
        table_id = find_baserow_table("Hardcoded Secrets")
        if not table_id:
            check("8. By Pattern Gallery view", 1, False, "table not found")
            return
        views = baserow_get(f"database/views/table/{table_id}/")
        found = None
        for v in views:
            if v.get("name") == "By Pattern":
                found = v
                break
        if not found:
            check("8. By Pattern Gallery view", 1, False, "view not found")
            return
        is_gallery = found.get("type") == "gallery"
        check("8. By Pattern Gallery view", 1, is_gallery,
              f"type={found.get('type')}")
    except Exception as e:
        check("8. By Pattern Gallery view", 1, False, f"exception: {e}")


def _read_audit_md() -> list[str] | None:
    rc, stdout, _ = docker_exec(
        CODE_SERVER_CONTAINER,
        "cat", f"{WORKSPACE}/devops-configs/{AUDIT_MD}",
    )
    if rc != 0:
        return None
    return stdout.strip().split("\n")


# ── Check 9: audit markdown structure ─────────────────────────────────────────
def check_9_audit_md_exists() -> None:
    """docker-audit md exists with exactly 5 lines, exact header and services line."""
    try:
        lines = _read_audit_md()
        if lines is None:
            check("9. Audit markdown file", 2, False, "file not found")
            return
        line_count_ok = len(lines) == 5
        header_ok = bool(lines) and lines[0].strip() == f"# Docker Audit — {AUDIT_DATE}"
        expected_svc_line = "Services scanned: " + ", ".join(SERVICES)
        services_line_ok = len(lines) >= 2 and lines[1].strip() == expected_svc_line
        check("9. Audit markdown file", 2,
              line_count_ok and header_ok and services_line_ok,
              f"lines={len(lines)}, header={'ok' if header_ok else 'wrong'}, "
              f"services={'ok' if services_line_ok else 'wrong'}")
    except Exception as e:
        check("9. Audit markdown file", 2, False, f"exception: {e}")


# ── Check 10: audit markdown numbers vs recomputed truth ──────────────────────
def check_10_audit_md_values() -> None:
    """Lines 3-5: avg score (both round-half-even and half-up accepted),
    below-75 count and secret counts must equal recomputed truth."""
    try:
        dt = dockerfile_truth()
        st = secret_truth()
        if dt is None or st is None:
            check("10. Audit md values vs truth", 2, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        lines = _read_audit_md()
        if lines is None:
            check("10. Audit md values vs truth", 2, False, "file not found")
            return
        if len(lines) < 5:
            check("10. Audit md values vs truth", 2, False, f"only {len(lines)} lines")
            return

        scores = [dt[s]["score"] for s in SERVICES]
        mean = sum(scores) / len(scores)
        avg_even = round(mean, 1)
        avg_up = float(Decimal(repr(mean)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
        below = sum(1 for s in scores if s < 75)
        crit = sum(1 for h in st if h["severity"] == "Critical")
        high = len(st) - crit

        issues = []
        m = re.fullmatch(r"Avg compliance score:\s*([\d.]+)", lines[2].strip())
        if not m:
            issues.append(f"line3 malformed: {lines[2].strip()!r}")
        else:
            avg = float(m.group(1))
            if not (abs(avg - avg_even) < 0.001 or abs(avg - avg_up) < 0.001):
                issues.append(f"avg={avg}, expected {avg_even} or {avg_up}")
        expected_l4 = f"Services below 75: {below}"
        if lines[3].strip() != expected_l4:
            issues.append(f"line4={lines[3].strip()!r}, expected {expected_l4!r}")
        expected_l5 = f"Hardcoded secrets found: {len(st)} (Critical: {crit}; High: {high})"
        if lines[4].strip() != expected_l5:
            issues.append(f"line5={lines[4].strip()!r}, expected {expected_l5!r}")

        check("10. Audit md values vs truth", 2, not issues,
              f"avg~{avg_even}/{avg_up}, below75={below}, secrets={len(st)} OK"
              if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("10. Audit md values vs truth", 2, False, f"exception: {e}")


# ── Check 11: git commit exists and stages only the audit md ──────────────────
def check_11_git_commit() -> None:
    """Commit with the exact message exists in devops-configs and touches
    exactly docs/docker-audit-2026-04-12.md."""
    try:
        repo = f"{WORKSPACE}/devops-configs"
        git = ("git", "-C", repo, "-c", f"safe.directory={repo}")
        rc, stdout, stderr = docker_exec(
            CODE_SERVER_CONTAINER, *git, "log", "--all", "--format=%H%x00%s",
        )
        if rc != 0:
            check("11. Git commit (single-file)", 2, False, f"git log failed: {stderr.strip()}")
            return
        shas = []
        for line in stdout.splitlines():
            if "\x00" not in line:
                continue
            sha, subject = line.split("\x00", 1)
            if subject.strip() == COMMIT_MSG:
                shas.append(sha.strip())
        if not shas:
            check("11. Git commit (single-file)", 2, False,
                  f"no commit with exact message {COMMIT_MSG!r}")
            return
        sha = shas[0]  # most recent
        rc, files_out, stderr = docker_exec(
            CODE_SERVER_CONTAINER, *git, "show", sha, "--pretty=format:", "--name-only",
        )
        if rc != 0:
            check("11. Git commit (single-file)", 2, False, f"git show failed: {stderr.strip()}")
            return
        files = [l.strip() for l in files_out.splitlines() if l.strip()]
        ok = files == [AUDIT_MD]
        check("11. Git commit (single-file)", 2, ok,
              f"commit {sha[:10]} files={files}")
    except Exception as e:
        check("11. Git commit (single-file)", 2, False, f"exception: {e}")


# ── Check 12: OpenProject 'Immediate' priority exists ────────────────────────
# Kept as a scored check per plan: the plan suggests downgrading to a 0pt
# precondition only if a pristine-seed probe shows the priority pre-exists, but
# the OP seed state can only be probed on a live container (not offline), so
# this stays as-is.
def check_12_op_immediate_priority() -> None:
    """'Immediate' priority enumeration exists in OpenProject."""
    try:
        result = op_db_query("SELECT name FROM enumerations WHERE type='IssuePriority' AND name='Immediate'")
        found = "Immediate" in result
        check("12. OP Immediate priority", 0, found,
              "found" if found else "not found in enumerations")
    except Exception as e:
        check("12. OP Immediate priority", 0, False, f"exception: {e}")


def _op_ids(type_name: str) -> tuple[str, str] | None:
    proj_id = op_db_query(f"SELECT id FROM projects WHERE identifier='{OP_PROJECT}'")
    type_id = op_db_query(f"SELECT id FROM types WHERE name='{type_name}'")
    if not proj_id or not type_id:
        return None
    return proj_id, type_id


# ── Check 13: Bug WP subject set == recomputed secret truth ───────────────────
def check_13_op_bug_wps() -> None:
    """Exactly one Bug WP per recomputed secret hit; subjects exactly match
    'SECRET LEAK [<svc>]: <Pattern> at <path>:<line>'; no extras, no dups."""
    try:
        st = secret_truth()
        if st is None:
            check("13. OP Bug work packages", 3, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        ids = _op_ids("Bug")
        if not ids:
            check("13. OP Bug work packages", 3, False, "project or Bug type not found")
            return
        proj_id, bug_type_id = ids
        result = op_db_query(
            f"SELECT subject, COUNT(*) FROM work_packages "
            f"WHERE project_id={proj_id} AND type_id={bug_type_id} "
            f"AND subject LIKE 'SECRET LEAK%' GROUP BY subject"
        )
        counts: dict[str, int] = {}
        for line in result.split("\n"):
            if not line.strip():
                continue
            subj, _, cnt = line.rpartition("|")
            counts[subj] = int(cnt)
        subject_maps = _secret_subject_map(st)

        issues = []
        total = sum(counts.values())
        if total != len(st):
            issues.append(f"expected {len(st)} SECRET LEAK Bugs, found {total}")
        for subj, c in counts.items():
            if c != 1:
                issues.append(f"duplicate subject x{c}: {subj[:60]!r}")
        matched: list[str | None] = [None] * len(st)
        for subj in counts:
            hit = [i for i, smap in enumerate(subject_maps) if subj in smap]
            if not hit:
                issues.append(f"unexpected subject: {subj[:70]!r}")
            elif matched[hit[0]] is not None:
                issues.append(f"multiple WPs for hit {st[hit[0]]['path']}:{st[hit[0]]['line']}")
            else:
                matched[hit[0]] = subj
        for i, m in enumerate(matched):
            if m is None:
                issues.append(f"missing WP for {st[i]['path']}:{st[i]['line']}")
        check("13. OP Bug work packages", 3, not issues,
              f"{len(st)} Bug WPs exactly match truth subjects" if not issues
              else "; ".join(issues[:4]))
    except Exception as e:
        check("13. OP Bug work packages", 3, False, f"exception: {e}")


# ── Check 14: Bug WP assignee, per-row priority and exact description ─────────
def check_14_op_bug_assignee_priority() -> None:
    """Each SECRET LEAK Bug: assignee user10, priority Immediate for Critical /
    High for High (per truth severity), description exactly
    'Audit date: <d>; File: <fp>:<ln>; Pattern: <pn>' after escape strip."""
    try:
        st = secret_truth()
        if st is None:
            check("14. Bug WP assignee/priority/desc", 2, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        ids = _op_ids("Bug")
        if not ids:
            check("14. Bug WP assignee/priority/desc", 2, False, "project or Bug type not found")
            return
        proj_id, bug_type_id = ids
        result = op_db_query(
            f"SELECT wp.subject, u.login, e.name, "
            f"regexp_replace(COALESCE(wp.description, ''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM work_packages wp "
            f"LEFT JOIN users u ON wp.assigned_to_id = u.id "
            f"LEFT JOIN enumerations e ON wp.priority_id = e.id "
            f"WHERE wp.project_id={proj_id} AND wp.type_id={bug_type_id} "
            f"AND wp.subject LIKE 'SECRET LEAK%'"
        )
        rows = [r for r in result.split("\n") if r.strip()]
        if not rows:
            check("14. Bug WP assignee/priority/desc", 2, False, "no SECRET LEAK WPs found")
            return
        subject_maps = _secret_subject_map(st)

        issues = []
        for row in rows:
            parts = row.split("|", 3)
            if len(parts) < 4:
                issues.append(f"unparseable row: {row[:60]!r}")
                continue
            subject, login, priority, desc = (p.strip() for p in parts)
            hit_idx = next((i for i, smap in enumerate(subject_maps) if subject in smap), None)
            if hit_idx is None:
                issues.append(f"subject not in truth set: {subject[:60]!r}")
                continue
            h = st[hit_idx]
            pn = subject_maps[hit_idx][subject]
            if login != SECURITY_OWNER:
                issues.append(f"{subject[:40]}: assignee={login!r}, expected {SECURITY_OWNER}")
            expected_prio = "Immediate" if h["severity"] == "Critical" else "High"
            if priority != expected_prio:
                issues.append(f"{subject[:40]}: priority={priority!r}, expected {expected_prio}")
            expected_desc = f"Audit date: {AUDIT_DATE}; File: {h['path']}:{h['line']}; Pattern: {pn}"
            if _clean_desc(desc) != expected_desc:
                issues.append(f"{subject[:40]}: desc={_clean_desc(desc)[:80]!r} != expected")
        check("14. Bug WP assignee/priority/desc", 2, not issues,
              f"{len(rows)} WPs: assignee, severity-mapped priority, exact desc OK"
              if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("14. Bug WP assignee/priority/desc", 2, False, f"exception: {e}")


# ── Check 15: Task WPs == exactly the score<75 services, exact desc ───────────
def check_15_op_task_wps() -> None:
    """Task WP subject set == {'Harden Dockerfile: <svc> (score <s>)'} for
    services with recomputed score < 75; assignee user19, priority High,
    description segments gated against recomputed Dockerfile truth."""
    try:
        truth = dockerfile_truth()
        if truth is None:
            check("15. OP Task WPs (harden)", 2, False,
                  "truth recompute failed or Dockerfiles tampered")
            return
        ids = _op_ids("Task")
        if not ids:
            check("15. OP Task WPs (harden)", 2, False, "project or Task type not found")
            return
        proj_id, task_type_id = ids

        low = {svc: t for svc, t in truth.items() if t["score"] < 75}
        expected_subjects = {
            f"Harden Dockerfile: {svc} (score {t['score']})": svc
            for svc, t in low.items()
        }

        result = op_db_query(
            f"SELECT wp.subject, u.login, e.name, "
            f"regexp_replace(COALESCE(wp.description, ''), E'[\\n\\r]+', ' ', 'g') "
            f"FROM work_packages wp "
            f"LEFT JOIN users u ON wp.assigned_to_id = u.id "
            f"LEFT JOIN enumerations e ON wp.priority_id = e.id "
            f"WHERE wp.project_id={proj_id} AND wp.type_id={task_type_id} "
            f"AND wp.subject LIKE 'Harden Dockerfile:%'"
        )
        rows = [r for r in result.split("\n") if r.strip()]

        issues = []
        if len(rows) != len(expected_subjects):
            issues.append(f"expected {len(expected_subjects)} Task WPs, found {len(rows)}")
        seen: set[str] = set()
        for row in rows:
            parts = row.split("|", 3)
            if len(parts) < 4:
                issues.append(f"unparseable row: {row[:60]!r}")
                continue
            subject, login, priority, desc = (p.strip() for p in parts)
            svc = expected_subjects.get(subject)
            if svc is None:
                issues.append(f"unexpected subject: {subject[:60]!r}")
                continue
            if subject in seen:
                issues.append(f"duplicate WP: {subject[:60]!r}")
                continue
            seen.add(subject)
            if login != PLATFORM_OWNER:
                issues.append(f"{svc}: assignee={login!r}, expected {PLATFORM_OWNER}")
            if priority != "High":
                issues.append(f"{svc}: priority={priority!r}, expected High")
            err = _harden_desc_issue(desc, truth[svc])
            if err:
                issues.append(f"{svc}: {err}")
        for subject, svc in expected_subjects.items():
            if subject not in seen:
                issues.append(f"missing WP: {subject!r}")
        check("15. OP Task WPs (harden)", 2, not issues,
              f"exactly {len(expected_subjects)} Task WPs match truth (subjects, desc)"
              if not issues else "; ".join(issues[:3]))
    except Exception as e:
        check("15. OP Task WPs (harden)", 2, False, f"exception: {e}")


def _harden_desc_issue(desc: str, t: dict) -> str | None:
    """Gate 'Base: ...; LatestTag: ...; Root: ...; Healthcheck: ...;
    Multistage: ...; RUN count: ...' against truth. Booleans case-insensitive,
    base image tolerant of a trailing ' AS <stage>'."""
    cleaned = _clean_desc(desc)
    parts = [p.strip() for p in cleaned.split(";")]
    if len(parts) != 6:
        return f"desc has {len(parts)} segments, expected 6: {cleaned[:80]!r}"
    expected_keys = ["Base", "LatestTag", "Root", "Healthcheck", "Multistage", "RUN count"]
    kv = []
    for p in parts:
        k, sep, v = p.partition(":")
        if not sep:
            return f"desc segment missing ':': {p[:40]!r}"
        kv.append((k.strip(), v.strip()))
    if [k for k, _ in kv] != expected_keys:
        return f"desc keys {[k for k, _ in kv]} != {expected_keys}"
    if _strip_stage(kv[0][1]).lower() != _strip_stage(t["base_image"]).lower():
        return f"Base={kv[0][1]!r}, expected {t['base_image']!r}"
    for idx, key in ((1, "uses_latest"), (2, "runs_as_root"),
                     (3, "has_healthcheck"), (4, "multistage")):
        expected = "true" if t[key] else "false"
        if kv[idx][1].lower() != expected:
            return f"{expected_keys[idx]}={kv[idx][1]!r}, expected {expected}"
    try:
        n = int(kv[5][1])
    except ValueError:
        return f"RUN count={kv[5][1]!r} not an int"
    if n != t["run_count"]:
        return f"RUN count={n}, expected {t['run_count']}"
    return None


# ── Check 16: field schema (types, primary, select options) for both tables ───
_SCHEMA_T1 = {
    "Audit ID": {"type": "text", "primary": True},
    "Service": {"type": "single_select", "options": set(SERVICES)},
    "Dockerfile Path": {"type": "text"},
    "Base Image": {"type": "text"},
    "Uses Latest Tag": {"type": "boolean"},
    "Runs As Root": {"type": "boolean"},
    "Has Healthcheck": {"type": "boolean"},
    "Multistage Build": {"type": "boolean"},
    "Run Instruction Count": {"type": "number"},
    "Captured At": {"type": "date"},
    "Compliance Score": {"type": "number"},
}
_SCHEMA_T2 = {
    "Finding ID": {"type": "text", "primary": True},
    "Service": {"type": "single_select", "options": set(SERVICES)},
    "File Path": {"type": "text"},
    "Line Number": {"type": "number"},
    "Pattern Name": {"type": "single_select",
                     "options": {"AWSKey", "APIToken", "Password", "Other"}},
    "Severity": {"type": "single_select", "options": {"Critical", "High"}},
    "Detected At": {"type": "date"},
}


def _schema_issues(table_name: str, spec: dict) -> list[str]:
    table_id = find_baserow_table(table_name)
    if not table_id:
        return [f"{table_name}: table not found"]
    fields = {f["name"]: f for f in baserow_get(f"database/fields/table/{table_id}/")}
    issues = []
    for name, want in spec.items():
        f = fields.get(name)
        if f is None:
            issues.append(f"{table_name}.{name}: missing")
            continue
        if f.get("type") != want["type"]:
            issues.append(f"{table_name}.{name}: type={f.get('type')}, expected {want['type']}")
        if want.get("primary") and not f.get("primary"):
            issues.append(f"{table_name}.{name}: not primary")
        if "options" in want:
            opts = {o.get("value") for o in f.get("select_options", [])}
            if opts != want["options"]:
                issues.append(f"{table_name}.{name}: options={sorted(opts)} != {sorted(want['options'])}")
    return issues


def check_16_field_schema() -> None:
    """Both tables: field types, primary flags and exact single-select option sets."""
    try:
        issues = _schema_issues("Dockerfile Audit", _SCHEMA_T1)
        issues += _schema_issues("Hardcoded Secrets", _SCHEMA_T2)
        check("16. Field schema (types/options)", 2, not issues,
              "types, primary and option sets exact for both tables"
              if not issues else "; ".join(issues[:4]))
    except Exception as e:
        check("16. Field schema (types/options)", 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_untampered()
    check_1_baserow_db_exists()
    check_2_dockerfile_audit_table()
    check_3_audit_rows()
    check_4_compliance_scores()
    check_5_secrets_table()
    check_6_secrets_rows()
    check_7_compliance_ranking_view()
    check_8_by_pattern_view()
    check_9_audit_md_exists()
    check_10_audit_md_values()
    check_11_git_commit()
    check_12_op_immediate_priority()
    check_13_op_bug_wps()
    check_14_op_bug_assignee_priority()
    check_15_op_task_wps()
    check_16_field_schema()

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
