"""
Verifier for Software-044-I3: Engineering-wide Prettier compliance drive

Checks: 11 weighted checks (total weight 13) across code-server and Baserow.

Strategy: the verifier RECOMPUTES ground truth itself — it runs the pinned
`prettier@3.3.3 --list-different` scans from the task statement in a
throwaway container from the code-server container's own PRISTINE image
(docker run; resolved via docker inspect, never the live container — so the
agent session cannot pollute the truth trees or edit sources to poison the
counts) and compares Baserow state (read via REST API) against that. In the
pristine image the vue-hackernews-2.0 working tree IS the pre-fix state, so
no git-worktree reconstruction is needed. Only the post-commit cleanliness
scan of vue-hackernews-2.0 (an agent deliverable) still runs in the LIVE
container. Agent-filled data is never used as an expectation source.

Degrade path (network/registry down): if a live `npx prettier@3.3.3` run
fails, the affected checks fall back to the baked pinned-prettier@3.3.3
truth counts {weather-dashboard: 23, vue-hackernews-2.0: 22, blog-engine: 15,
json: 213} with a +/-2 per-project count tolerance, and say so in the check
detail ("degraded: baked prettier@3.3.3 truth"). The degrade path NEVER
falls back to agent data. When the live run succeeds, set comparisons are
exact (no tolerance).

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_CONTAINER, BASEROW_PORT.
"""

import os
import sys
import json
import subprocess
import re

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")

for var_name, var_val in [
    ("CODE_SERVER_CONTAINER", CODE_SERVER_CONTAINER),
    ("BASEROW_PORT", BASEROW_PORT),
]:
    if not var_val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

# ── Constants from task ───────────────────────────────────────────────────────
DRIVE_DATE = "2026-06-18"
DB_NAME = f"Prettier Compliance Drive {DRIVE_DATE}"
PROJECT_LIST = ["weather-dashboard", "vue-hackernews-2.0", "blog-engine", "json"]
FILES_CHECKED_MAP = {"weather-dashboard": 50, "vue-hackernews-2.0": 78, "blog-engine": 40, "json": 215}
REFERENCE_PROJECT = "vue-hackernews-2.0"
COMMIT_MSG = f"style: apply Prettier formatting {DRIVE_DATE}"
GREEN_THRESHOLD = 92
RED_THRESHOLD = 75

# Pinned prettier version (pinned in the task statement as well, so agent and
# verifier compute against the exact same formatter behaviour).
PRETTIER_PKG = "prettier@3.3.3"

# Per-project check globs, verbatim from the task statement.
GLOB_MAP = {
    "weather-dashboard": "src/**/*.{ts,tsx,css,json}",
    "vue-hackernews-2.0": "src/**/*.{js,vue,css,json}",
    "blog-engine": "**/*.{js,json,css,html}",
    "json": "docs/**/*.{md,json,yml}",
}

# Baked ground-truth counts measured on the pristine image with prettier@3.3.3
# (pre-fix state). Used ONLY on the degrade path (npx/network failure) with a
# +/-2 per-project count tolerance — never preferred over a live recompute.
BAKED_COUNTS = {"weather-dashboard": 23, "vue-hackernews-2.0": 22, "blog-engine": 15, "json": 213}
BAKED_TOL = 2

# File Type derivation from the task statement:
# .js→JS, .ts/.tsx→TS, .vue→Vue, .css/.scss→CSS, .html→HTML, .json→JSON, else Other
EXT_TYPE_MAP = {
    "js": "JS", "ts": "TS", "tsx": "TS", "vue": "Vue",
    "css": "CSS", "scss": "CSS", "html": "HTML", "json": "JSON",
}
FILE_TYPE_OPTIONS = {"JS", "TS", "Vue", "CSS", "HTML", "JSON", "Other"}
TIER_OPTIONS = {"Green", "Yellow", "Red"}

WORKSPACE = "/home/coder/workspace"
VUE_DIR = f"{WORKSPACE}/vue-hackernews-2.0"
# All git invocations carry safe.directory (docker exec user vs repo owner may differ).
GIT_SAFE = ("-c", "safe.directory=*")

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # protocol: no newlines in detail
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


def baserow_api_get(path: str, token: str) -> requests.Response:
    return requests.get(
        f"{BASEROW_URL}/api{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )


_baserow_token = None


def get_baserow_token() -> str:
    global _baserow_token
    if _baserow_token:
        return _baserow_token
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    _baserow_token = data.get("access_token") or data["token"]
    return _baserow_token


def fetch_all_rows(table_id: int, token: str) -> list[dict]:
    """Fetch the FULL table with pagination (json alone has ~213 rows)."""
    rows: list[dict] = []
    page = 1
    while True:
        resp = baserow_api_get(
            f"/database/rows/table/{table_id}/?user_field_names=true&size=200&page={page}",
            token,
        )
        resp.raise_for_status()
        data = resp.json()
        rows.extend(data.get("results", []))
        if not data.get("next"):
            break
        page += 1
        if page > 50:  # hard stop, never loop forever
            break
    return rows


def _get_cell_value(cell):
    """Extract value from a Baserow cell (single_select dict, bool, number, text)."""
    if isinstance(cell, dict):
        return cell.get("value", str(cell))
    return cell


def strip_jsonc(text: str) -> str:
    """Strip // and /* */ comments (string-safe) + trailing commas from JSONC."""
    out = []
    i, n = 0, len(text)
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
        elif c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    s = "".join(out)
    return re.sub(r",\s*([}\]])", r"\1", s)


def expected_file_type(path: str) -> str:
    ext = path.lower().rsplit(".", 1)[-1] if "." in path else ""
    return EXT_TYPE_MAP.get(ext, "Other")


# ── Ground truth recomputation ────────────────────────────────────────────────
# TRUTH[project] = {"mode": "live", "paths": [..]} or {"mode": "degraded", "paths": None}
TRUTH: dict[str, dict] = {}
# VUE_POST = post-commit working-tree scan of vue-hackernews-2.0 (cleanliness gate)
VUE_POST: dict = {"mode": "degraded", "paths": None}
FIX_SHAS: list[str] = []


def find_fix_commits() -> list[str]:
    """Exact-subject commit lookup (never a substring grep over the whole log)."""
    try:
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER, "git", "-C", VUE_DIR, *GIT_SAFE,
            "log", "--all", "--format=%H%x00%s",
        )
    except Exception:
        return []
    shas = []
    for line in out.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        if subject == COMMIT_MSG:
            shas.append(sha.strip())
    return shas


def _parse_sections(out: str) -> dict[str, tuple[int, list[str]]]:
    """Split delimited ===BEGIN/===END section output into {name: (rc, paths)}."""
    sections: dict[str, tuple[int, list[str]]] = {}
    cur, buf = None, []
    for line in out.splitlines():
        m = re.match(r"^===BEGIN (\S+)===$", line)
        if m:
            cur, buf = m.group(1), []
            continue
        m = re.match(r"^===END (\S+) rc=(\d+)===$", line)
        if m:
            paths = [l.strip() for l in buf if l.strip()]
            sections[m.group(1)] = (int(m.group(2)), paths)
            cur = None
            continue
        if cur is not None:
            buf.append(line)
    return sections


def compute_prettier_truth() -> None:
    """Run the pinned prettier scans: TRUTH in the PRISTINE image, VUE_POST live.

    Ground truth (the four pre-fix `--list-different` sets) runs in a
    throwaway `docker run` from the code-server container's own pristine
    image — agent edits to the live workspace cannot poison it. In the
    pristine image the vue-hackernews-2.0 working tree IS the pre-fix state,
    so no git-worktree reconstruction at the fix commit's parent is needed.
    Only the vue-post cleanliness scan (did the agent's fix leave the tree
    prettier-clean? — an agent deliverable) still runs in the LIVE container.

    TIME BUDGET: the harness enforces a 300s global verify timeout
    (verify_runner). The first `npx --yes prettier@3.3.3` invocation in each
    environment downloads the package (~60-90s); later invocations hit that
    environment's npx cache. The four truth scans therefore run in ONE
    `bash -lc` inside a single throwaway container — paying the download
    exactly once — with per-project delimiters, under a 270s subprocess
    timeout (the original 210s + docker-run/npx-download slack); the single
    live vue-post scan gets its own 120s. Either run failing (rc >= 2,
    timeout, or docker error) leaves its projects degraded to the baked
    prettier@3.3.3 counts — never a crash, never a fallback to agent data.
    """
    global TRUTH, VUE_POST
    TRUTH = {p: {"mode": "degraded", "paths": None} for p in PROJECT_LIST}
    VUE_POST = {"mode": "degraded", "paths": None}

    vue_glob = GLOB_MAP["vue-hackernews-2.0"]

    def section(lines: list[str], name: str, body: str) -> None:
        lines.append(f'echo "===BEGIN {name}==="')
        lines.append(body)
        lines.append(f'echo "===END {name} rc=$?==="')

    # ── Truth scans: one throwaway container from the pristine image ─────────
    truth_lines = ["set +e"]
    for proj in ["weather-dashboard", "blog-engine", "json"]:
        section(truth_lines, proj, "( cd %s/%s && npx --yes %s --list-different '%s' )"
                % (WORKSPACE, proj, PRETTIER_PKG, GLOB_MAP[proj]))
    # Pristine tree == pre-fix state; scan it directly.
    section(truth_lines, "vue-pre",
            f"( cd {VUE_DIR} && npx --yes {PRETTIER_PKG} --list-different '{vue_glob}' )")
    try:
        # prettier --list-different exits 0 (clean) or 1 (differences); >=2 is an error.
        _, out, _ = image_exec("bash", "-lc", "\n".join(truth_lines), timeout=270)
        sections = _parse_sections(out)
        for proj, key in [("weather-dashboard", "weather-dashboard"),
                          ("blog-engine", "blog-engine"),
                          ("json", "json"),
                          ("vue-hackernews-2.0", "vue-pre")]:
            got = sections.get(key)
            if got and got[0] in (0, 1):
                TRUTH[proj] = {"mode": "live", "paths": sorted(got[1])}
    except Exception:
        pass  # truth stays degraded (baked counts)

    # ── vue-post cleanliness: the agent's deliverable, scanned LIVE ──────────
    post_lines = ["set +e"]
    section(post_lines, "vue-post",
            f"( cd {VUE_DIR} && npx --yes {PRETTIER_PKG} --list-different '{vue_glob}' )")
    try:
        _, out, _ = docker_exec(CODE_SERVER_CONTAINER, "bash", "-lc",
                                "\n".join(post_lines), timeout=120)
        got = _parse_sections(out).get("vue-post")
        if got and got[0] in (0, 1):
            VUE_POST = {"mode": "live", "paths": got[1]}
    except Exception:
        pass  # VUE_POST stays degraded (git-status proxy in check 3)


# ── Workspace settings (ck1/ck2) ──────────────────────────────────────────────
_settings_sources: list[tuple[str, dict]] | None = None


def load_workspace_settings() -> list[tuple[str, dict]]:
    """Workspace scope ONLY: /home/coder/workspace/.vscode/settings.json or the
    `settings` object inside workspace.code-workspace (JSONC-tolerant).
    User-settings paths are deliberately NOT accepted."""
    global _settings_sources
    if _settings_sources is not None:
        return _settings_sources
    sources: list[tuple[str, dict]] = []
    try:
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER, "cat", f"{WORKSPACE}/.vscode/settings.json")
        if rc == 0 and out.strip():
            try:
                obj = json.loads(strip_jsonc(out))
                if isinstance(obj, dict):
                    sources.append((".vscode/settings.json", obj))
            except json.JSONDecodeError:
                pass
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER, "bash", "-c",
            "find /home/coder -maxdepth 3 -name '*.code-workspace' "
            "-not -path '*/node_modules/*' 2>/dev/null | head -5",
        )
        for path in out.splitlines():
            path = path.strip()
            if not path:
                continue
            rc2, out2, _ = docker_exec(CODE_SERVER_CONTAINER, "cat", path)
            if rc2 != 0 or not out2.strip():
                continue
            try:
                obj = json.loads(strip_jsonc(out2))
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("settings"), dict):
                sources.append((os.path.basename(path), obj["settings"]))
    except Exception:
        pass
    _settings_sources = sources
    return sources


# ── Shared state for cross-check data ────────────────────────────────────────
_baserow_db_id = None
_unformatted_table_id = None
_compliance_table_id = None
_uf_rows: list[dict] | None = None       # Unformatted Files rows, in row order
_compliance_rows: list[dict] | None = None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_default_formatter() -> None:
    """ck1 (1pt): editor.defaultFormatter == esbenp.prettier-vscode, workspace scope only."""
    try:
        sources = load_workspace_settings()
        hit = next((name for name, s in sources
                    if s.get("editor.defaultFormatter") == "esbenp.prettier-vscode"), None)
        check("1. Default Formatter (workspace scope)", 1, hit is not None,
              f"found in {hit}" if hit else
              "esbenp.prettier-vscode not set in workspace/.vscode/settings.json "
              "or workspace.code-workspace settings")
    except Exception as e:
        check("1. Default Formatter (workspace scope)", 1, False, f"exception: {e}")


def check_2_format_on_save() -> None:
    """ck2 (1pt): editor.formatOnSave == true, workspace scope only."""
    try:
        sources = load_workspace_settings()
        hit = next((name for name, s in sources
                    if s.get("editor.formatOnSave") is True), None)
        check("2. Format On Save enabled (workspace scope)", 0, hit is not None,
              f"found in {hit}" if hit else
              "editor.formatOnSave is not true in workspace/.vscode/settings.json "
              "or workspace.code-workspace settings")
    except Exception as e:
        check("2. Format On Save enabled (workspace scope)", 0, False, f"exception: {e}")


def check_3_git_commit() -> None:
    """ck3 (1pt, rebalanced from 2 to fund ck10): exact-subject commit exists; its
    file set == recomputed pre-fix prettier truth; working tree is prettier-clean
    after the commit."""
    label = "3. Prettier fix commit in vue-hackernews-2.0"
    try:
        if not FIX_SHAS:
            check(label, 1, False, f"no commit with exact message '{COMMIT_MSG}'")
            return
        sha = FIX_SHAS[0]
        errors: list[str] = []
        notes: list[str] = []

        # (a) commit file set == pre-fix truth set
        rc, out, _ = docker_exec(
            CODE_SERVER_CONTAINER, "git", "-C", VUE_DIR, *GIT_SAFE,
            "show", sha, "--pretty=format:", "--name-only",
        )
        commit_files = {l.strip() for l in out.splitlines() if l.strip()}
        t = TRUTH["vue-hackernews-2.0"]
        if t["mode"] == "live":
            truth_set = set(t["paths"])
            if commit_files != truth_set:
                errors.append(
                    f"commit file set != pre-fix prettier truth "
                    f"(missing {len(truth_set - commit_files)}, "
                    f"extra {len(commit_files - truth_set)}, expected {len(truth_set)})")
        else:
            notes.append("degraded: baked prettier@3.3.3 truth")
            baked = BAKED_COUNTS["vue-hackernews-2.0"]
            if abs(len(commit_files) - baked) > BAKED_TOL:
                errors.append(f"commit touches {len(commit_files)} files, "
                              f"baked truth {baked}+/-{BAKED_TOL}")

        # (b) after-commit cleanliness: prettier --list-different on the working
        # tree must output nothing (fix applied AND fully committed).
        if VUE_POST["mode"] == "live":
            if VUE_POST["paths"]:
                errors.append(f"working tree not prettier-clean after commit "
                              f"({len(VUE_POST['paths'])} files still differ)")
        else:
            # Degrade proxy: prettier unavailable — require the tree to at least
            # have no uncommitted changes under src (stage-all + commit done).
            notes.append("degraded: cleanliness via git status proxy")
            rc2, out2, _ = docker_exec(
                CODE_SERVER_CONTAINER, "git", "-C", VUE_DIR, *GIT_SAFE,
                "status", "--porcelain", "--", "src",
            )
            if out2.strip():
                errors.append("uncommitted changes under src/ after fix commit")

        passed = not errors
        detail = "; ".join(errors[:4]) if errors else "; ".join([f"commit {sha[:10]}"] + notes)
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_4_baserow_database() -> None:
    """ck4 (1pt): database 'Prettier Compliance Drive 2026-06-18' exists."""
    global _baserow_db_id
    try:
        token = get_baserow_token()
        resp = baserow_api_get("/applications/", token)
        resp.raise_for_status()
        for app in resp.json():
            if app.get("name") == DB_NAME and app.get("type") == "database":
                _baserow_db_id = app["id"]
                break
        check("4. Baserow database exists", 1, _baserow_db_id is not None,
              "" if _baserow_db_id else f"database '{DB_NAME}' not found")
    except Exception as e:
        check("4. Baserow database exists", 1, False, f"exception: {e}")


def _find_table(name: str):
    token = get_baserow_token()
    resp = baserow_api_get(f"/database/tables/database/{_baserow_db_id}/", token)
    resp.raise_for_status()
    for t in resp.json():
        if t.get("name") == name:
            return t["id"]
    return None


def check_5a_unformatted_row_set() -> None:
    """ck5a (1pt): Unformatted Files rows == recomputed prettier truth.
    Live truth: exact (Project, File Path) set equality per project, exactly one
    row per file. Degraded truth: per-project row count within +/-2 of the baked
    prettier@3.3.3 counts."""
    global _unformatted_table_id, _uf_rows
    label = "5a. Unformatted Files rows match prettier truth"
    try:
        if not _baserow_db_id:
            check(label, 2, False, "database not found in check 4")
            return
        _unformatted_table_id = _find_table("Unformatted Files")
        if not _unformatted_table_id:
            check(label, 2, False, "table 'Unformatted Files' not found")
            return
        token = get_baserow_token()
        _uf_rows = fetch_all_rows(_unformatted_table_id, token)

        by_proj: dict[str, list[str]] = {}
        for row in _uf_rows:
            proj = str(_get_cell_value(row.get("Project")) or "")
            path = str(row.get("File Path") or "").strip()
            by_proj.setdefault(proj, []).append(path)

        errors: list[str] = []
        notes: list[str] = []
        extra_projects = set(by_proj) - set(PROJECT_LIST) - {""}
        if extra_projects:
            errors.append(f"rows with unexpected Project values: {sorted(extra_projects)}")
        if "" in by_proj:
            errors.append(f"{len(by_proj[''])} rows with empty Project")

        for proj in PROJECT_LIST:
            agent_paths = by_proj.get(proj, [])
            t = TRUTH[proj]
            if t["mode"] == "live":
                truth_set = set(t["paths"])
                agent_set = set(agent_paths)
                if agent_set != truth_set:
                    errors.append(f"{proj}: missing {len(truth_set - agent_set)}, "
                                  f"unexpected {len(agent_set - truth_set)} "
                                  f"(expected {len(truth_set)} files, got {len(agent_paths)} rows)")
                elif len(agent_paths) != len(truth_set):
                    errors.append(f"{proj}: duplicate rows "
                                  f"({len(agent_paths)} rows for {len(truth_set)} files)")
            else:
                notes.append(f"{proj} degraded: baked prettier@3.3.3 truth")
                baked = BAKED_COUNTS[proj]
                if abs(len(agent_paths) - baked) > BAKED_TOL:
                    errors.append(f"{proj}: {len(agent_paths)} rows, "
                                  f"baked truth {baked}+/-{BAKED_TOL}")
                if len(agent_paths) != len(set(agent_paths)):
                    errors.append(f"{proj}: duplicate File Path rows")

        passed = not errors
        detail = "; ".join(errors[:5]) if errors else \
            "; ".join([f"{len(_uf_rows)} rows"] + notes)
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_5b_unformatted_order_ids() -> None:
    """ck5b (1pt): row order == Project alpha then File Path alpha; Entry ID is a
    verifier-generated continuous UF-0001..UF-NNNN sequence in row order; File
    Type recomputed from extension per row; Captured At all 2026-06-18."""
    label = "5b. Unformatted Files order/Entry ID/File Type/Captured At"
    try:
        if _uf_rows is None:
            check(label, 1, False, "Unformatted Files rows unavailable (see 5a)")
            return
        if not _uf_rows:
            check(label, 1, False, "table has 0 rows")
            return
        errors: list[str] = []

        pairs = [(str(_get_cell_value(r.get("Project")) or ""),
                  str(r.get("File Path") or "").strip()) for r in _uf_rows]
        if pairs != sorted(pairs) and \
                pairs != sorted(pairs, key=lambda t: (t[0].lower(), t[1].lower())):
            errors.append("rows not ordered by Project alpha then File Path alpha")

        expected_ids = [f"UF-{i:04d}" for i in range(1, len(_uf_rows) + 1)]
        actual_ids = [str(r.get("Entry ID") or "").strip() for r in _uf_rows]
        if actual_ids != expected_ids:
            bad = next((i for i, (a, e) in enumerate(zip(actual_ids, expected_ids)) if a != e),
                       None)
            errors.append(
                "Entry ID not the continuous sequence UF-0001..UF-%04d"
                % len(_uf_rows)
                + (f" (row {bad + 1}: got '{actual_ids[bad]}', expected '{expected_ids[bad]}')"
                   if bad is not None else ""))

        type_bad = 0
        date_bad = 0
        first_type_err = ""
        for r in _uf_rows:
            path = str(r.get("File Path") or "").strip()
            ftype = str(_get_cell_value(r.get("File Type")) or "")
            exp = expected_file_type(path)
            if ftype != exp:
                type_bad += 1
                if not first_type_err:
                    first_type_err = f"e.g. '{path}': got '{ftype}', expected '{exp}'"
            captured = str(r.get("Captured At") or "")
            if not captured.startswith(DRIVE_DATE):
                date_bad += 1
        if type_bad:
            errors.append(f"{type_bad} rows with wrong File Type ({first_type_err})")
        if date_bad:
            errors.append(f"{date_bad} rows with Captured At != {DRIVE_DATE}")

        check(label, 1, not errors, "; ".join(errors[:5]))
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_6_by_type_view() -> None:
    """ck6 (1pt): 'By Type' grid view on Unformatted Files, grouped by File Type.
    (The old 'if not found: found = True' fallback is deliberately removed —
    a missing/incorrect group_by now FAILs.)"""
    label = "6. By Type grid view grouped by File Type"
    try:
        if not _unformatted_table_id:
            check(label, 1, False, "Unformatted Files table not found")
            return
        token = get_baserow_token()
        resp = baserow_api_get(f"/database/views/table/{_unformatted_table_id}/", token)
        resp.raise_for_status()
        view = next((v for v in resp.json()
                     if v.get("name") == "By Type" and v.get("type") == "grid"), None)
        if not view:
            check(label, 1, False, "grid view named 'By Type' not found")
            return
        resp2 = baserow_api_get(f"/database/views/{view['id']}/group_bys/", token)
        if resp2.status_code != 200:
            check(label, 1, False, f"group_bys endpoint returned {resp2.status_code}")
            return
        group_bys = resp2.json()
        resp3 = baserow_api_get(f"/database/fields/table/{_unformatted_table_id}/", token)
        resp3.raise_for_status()
        field_names = {f["id"]: f["name"] for f in resp3.json()}
        grouped = any(field_names.get(gb.get("field")) == "File Type" for gb in group_bys)
        check(label, 1, grouped,
              "" if grouped else "view exists but has no group_by on the File Type field")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_7_compliance_table() -> None:
    """ck7 (1pt, rebalanced from 2 to fund ck10): Project Compliance table has
    exactly 4 rows and the Project primary values are exactly the 4 projects."""
    global _compliance_table_id, _compliance_rows
    label = "7. Project Compliance table (4 rows, exact project set)"
    try:
        if not _baserow_db_id:
            check(label, 1, False, "database not found")
            return
        _compliance_table_id = _find_table("Project Compliance")
        if not _compliance_table_id:
            check(label, 1, False, "table 'Project Compliance' not found")
            return
        token = get_baserow_token()
        _compliance_rows = fetch_all_rows(_compliance_table_id, token)
        errors = []
        if len(_compliance_rows) != len(PROJECT_LIST):
            errors.append(f"expected {len(PROJECT_LIST)} rows, got {len(_compliance_rows)}")
        projects = [str(_get_cell_value(r.get("Project")) or "").strip()
                    for r in _compliance_rows]
        if sorted(projects) != sorted(PROJECT_LIST):
            errors.append(f"Project values {sorted(projects)} != expected {sorted(PROJECT_LIST)}")
        check(label, 1, not errors, "; ".join(errors) if errors else f"{len(_compliance_rows)} rows")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_8_compliance_data() -> None:
    """ck8 (2pt): per project, Files Unformatted == recomputed truth count;
    Compliance Pct == round((checked-unformatted)/checked*100, 2) (tol 0.01);
    Compliance Tier derived from the TRUTH pct; Fixed only for vue; and
    cross-table consistency: Files Unformatted == Unformatted Files row count
    for that project."""
    label = "8. Compliance data vs recomputed truth"
    try:
        if not _compliance_rows:
            check(label, 2, False, "no Project Compliance rows found")
            return
        # Table 1 per-project row counts for the cross-table gate
        uf_counts: dict[str, int] | None = None
        if _uf_rows is not None:
            uf_counts = {}
            for r in _uf_rows:
                proj = str(_get_cell_value(r.get("Project")) or "")
                uf_counts[proj] = uf_counts.get(proj, 0) + 1

        errors: list[str] = []
        notes: list[str] = []
        seen = set()
        for row in _compliance_rows:
            project = str(_get_cell_value(row.get("Project")) or "").strip()
            if project not in FILES_CHECKED_MAP or project in seen:
                continue
            seen.add(project)
            checked = FILES_CHECKED_MAP[project]

            # Files Checked (spec constant)
            try:
                fc = float(str(row.get("Files Checked")))
            except (ValueError, TypeError):
                fc = None
            if fc != checked:
                errors.append(f"{project}: Files Checked expected {checked}, "
                              f"got {row.get('Files Checked')}")

            # Files Unformatted vs recomputed truth
            try:
                unf = float(str(row.get("Files Unformatted")))
            except (ValueError, TypeError):
                unf = None
            t = TRUTH[project]
            if unf is None:
                errors.append(f"{project}: Files Unformatted not a number: "
                              f"{row.get('Files Unformatted')}")
                continue
            if t["mode"] == "live":
                exp_unf = len(t["paths"])
                if unf != exp_unf:
                    errors.append(f"{project}: Files Unformatted expected {exp_unf} "
                                  f"(live prettier truth), got {unf:g}")
            else:
                notes.append(f"{project} degraded: baked prettier@3.3.3 truth")
                exp_unf = BAKED_COUNTS[project]
                if abs(unf - exp_unf) > BAKED_TOL:
                    errors.append(f"{project}: Files Unformatted {unf:g}, "
                                  f"baked truth {exp_unf}+/-{BAKED_TOL}")
                # within tolerance: gate the formula against the row's own count
                exp_unf = unf

            # Compliance Pct recomputed from truth
            exp_pct = round((checked - exp_unf) / checked * 100, 2)
            try:
                pct = float(str(row.get("Compliance Pct")))
            except (ValueError, TypeError):
                pct = None
            if pct is None or abs(pct - exp_pct) > 0.01:
                errors.append(f"{project}: Compliance Pct expected {exp_pct:.2f}, "
                              f"got {row.get('Compliance Pct')}")

            # Tier derived from the TRUTH pct (all Red at current truth, but computed)
            if exp_pct >= GREEN_THRESHOLD:
                exp_tier = "Green"
            elif exp_pct < RED_THRESHOLD:
                exp_tier = "Red"
            else:
                exp_tier = "Yellow"
            tier = str(_get_cell_value(row.get("Compliance Tier")) or "")
            if tier != exp_tier:
                errors.append(f"{project}: Tier expected {exp_tier} "
                              f"(truth pct {exp_pct:.2f}), got '{tier}'")

            # Fixed flag
            fixed = _get_cell_value(row.get("Fixed"))
            exp_fixed = (project == REFERENCE_PROJECT)
            if bool(fixed) != exp_fixed:
                errors.append(f"{project}: Fixed expected {exp_fixed}, got {fixed}")

            # Cross-table consistency with Table 1
            if uf_counts is None:
                errors.append(f"{project}: cannot cross-check Files Unformatted "
                              f"(Unformatted Files rows unavailable)")
            elif uf_counts.get(project, 0) != unf:
                errors.append(f"{project}: Files Unformatted {unf:g} != "
                              f"{uf_counts.get(project, 0)} Unformatted Files rows")

        missing = set(PROJECT_LIST) - seen
        if missing:
            errors.append(f"missing project rows: {sorted(missing)}")

        passed = not errors
        detail = "; ".join(errors[:6]) if errors else "; ".join(notes)
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_tier_board_view() -> None:
    """ck9 (1pt): 'Tier Board' Gallery view on Project Compliance."""
    try:
        if not _compliance_table_id:
            check("9. Tier Board Gallery view", 1, False, "Project Compliance table not found")
            return
        token = get_baserow_token()
        resp = baserow_api_get(f"/database/views/table/{_compliance_table_id}/", token)
        resp.raise_for_status()
        found = any(v.get("name") == "Tier Board" and v.get("type") == "gallery"
                    for v in resp.json())
        check("9. Tier Board Gallery view", 1, found,
              "" if found else "gallery view 'Tier Board' not found on Project Compliance")
    except Exception as e:
        check("9. Tier Board Gallery view", 1, False, f"exception: {e}")


def check_10_field_schema() -> None:
    """ck10 (2pt, NEW): both tables' field schema — exact field-name sets, types,
    exact single-select option sets, Compliance Pct 2 decimal places, Fixed
    boolean, Captured At date."""
    label = "10. Field schema of both tables"
    try:
        if not _unformatted_table_id or not _compliance_table_id:
            check(label, 2, False, "one or both tables not found")
            return
        token = get_baserow_token()
        errors: list[str] = []

        def get_fields(table_id: int) -> dict[str, dict]:
            resp = baserow_api_get(f"/database/fields/table/{table_id}/", token)
            resp.raise_for_status()
            return {f["name"]: f for f in resp.json()}

        def opts(field: dict) -> set:
            return {o.get("value") for o in field.get("select_options", [])}

        def expect(table: str, fields: dict, name: str, ftype: str,
                   primary: bool | None = None, options: set | None = None,
                   decimals: int | None = None) -> None:
            f = fields.get(name)
            if f is None:
                return  # missing fields already reported via name-set check
            if f.get("type") != ftype:
                errors.append(f"{table}.{name}: type '{f.get('type')}' != '{ftype}'")
                return
            if primary is not None and bool(f.get("primary")) != primary:
                errors.append(f"{table}.{name}: primary expected {primary}")
            if options is not None and opts(f) != options:
                errors.append(f"{table}.{name}: options {sorted(opts(f))} != {sorted(options)}")
            if decimals is not None and f.get("number_decimal_places") != decimals:
                errors.append(f"{table}.{name}: number_decimal_places "
                              f"{f.get('number_decimal_places')} != {decimals}")

        # Table 1: Unformatted Files
        f1 = get_fields(_unformatted_table_id)
        exp1 = {"Entry ID", "Project", "File Path", "File Type", "Captured At"}
        if set(f1) != exp1:
            errors.append(f"Unformatted Files fields {sorted(f1)} != {sorted(exp1)}")
        expect("UF", f1, "Entry ID", "text", primary=True)
        expect("UF", f1, "Project", "single_select", options=set(PROJECT_LIST))
        expect("UF", f1, "File Path", "text")
        expect("UF", f1, "File Type", "single_select", options=FILE_TYPE_OPTIONS)
        expect("UF", f1, "Captured At", "date")

        # Table 2: Project Compliance
        f2 = get_fields(_compliance_table_id)
        exp2 = {"Project", "Files Checked", "Files Unformatted", "Compliance Pct",
                "Fixed", "Compliance Tier"}
        if set(f2) != exp2:
            errors.append(f"Project Compliance fields {sorted(f2)} != {sorted(exp2)}")
        expect("PC", f2, "Project", "text", primary=True)
        expect("PC", f2, "Files Checked", "number")
        expect("PC", f2, "Files Unformatted", "number")
        expect("PC", f2, "Compliance Pct", "number", decimals=2)
        expect("PC", f2, "Fixed", "boolean")
        expect("PC", f2, "Compliance Tier", "single_select", options=TIER_OPTIONS)

        check(label, 2, not errors, "; ".join(errors[:6]))
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    global FIX_SHAS
    # Ground truth first: exact-subject commit lookup (live repo), then the
    # batched prettier@3.3.3 truth run in the PRISTINE image + the live
    # vue-post scan (see compute_prettier_truth docstring for the
    # 300s verify-budget rationale).
    FIX_SHAS = find_fix_commits()
    compute_prettier_truth()
    degraded = sorted(p for p, t in TRUTH.items() if t["mode"] != "live")
    if degraded:
        print(f"NOTE: live prettier truth unavailable for {degraded}; "
              f"using baked prettier@3.3.3 counts with +/-{BAKED_TOL} tolerance",
              file=sys.stderr)

    check_1_default_formatter()
    check_2_format_on_save()
    check_3_git_commit()
    check_4_baserow_database()
    check_5a_unformatted_row_set()
    check_5b_unformatted_order_ids()
    check_6_by_type_view()
    check_7_compliance_table()
    check_8_compliance_data()
    check_9_tier_board_view()
    check_10_field_schema()

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
