"""
Verifier for Software-027-I2: Cross-Team Dependency Map for Frontend & Infra Services

Checks: 15 checks (14 weighted + 1 diagnostic, total weight 25) across
code-server, baserow, openproject.
Strategy: baserow via REST API; openproject via its embedded Postgres. The
dependency-edge scan ground truth runs in a throwaway container from the
code-server container's own pristine image (docker inspect → docker run), so
live-workspace edits cannot move it; live code-server docker exec is kept only
for the 0pt project-dirs diagnostic.

Required env vars:
  SERVER_HOSTNAME, CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER.
"""

import os
import re
import shlex
import sys
import json
import subprocess
import unicodedata
from collections import Counter

import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_PORT = os.getenv("CODE_SERVER_PORT")
CODE_SERVER_CONTAINER = os.getenv("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.getenv("BASEROW_PORT")
BASEROW_CONTAINER = os.getenv("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.getenv("BASEROW_DB_CONTAINER")
OPENPROJECT_PORT = os.getenv("OPENPROJECT_PORT")
OPENPROJECT_CONTAINER = os.getenv("OPENPROJECT_CONTAINER")

_required = {
    "CODE_SERVER_PORT": CODE_SERVER_PORT,
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "OPENPROJECT_PORT": OPENPROJECT_PORT,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"
OPENPROJECT_URL = f"http://{HOST}:{OPENPROJECT_PORT}"

# ── Task constants ────────────────────────────────────────────────────────────
PROJECTS = ["tabler", "vue-hackernews-2.0", "json", "devops-configs", "todo-api"]
SCAN_ROOTS = {
    "tabler": "tabler/core",
    "vue-hackernews-2.0": "vue-hackernews-2.0/src",
    "json": "json/include",
    "devops-configs": "devops-configs",
    "todo-api": "todo-api/app",
}
OWNERSHIP = {
    "tabler": {"owning_team": "Frontend", "tech_lead": "Karen Brown"},
    "vue-hackernews-2.0": {"owning_team": "Frontend", "tech_lead": "Liam Robinson"},
    "json": {"owning_team": "Core Libraries", "tech_lead": "Frank Nguyen"},
    "devops-configs": {"owning_team": "Infrastructure", "tech_lead": "Noah Taylor"},
    "todo-api": {"owning_team": "Backend", "tech_lead": "Grace Patel"},
}
BASEROW_DB_NAME = "Service Coupling Atlas Q3"
OP_PROJECT = "Infrastructure Upgrade"
EPIC_SUBJECT = "Dependency map snapshot: 2025-07-22"

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


def baserow_auth() -> str:
    """Authenticate to Baserow, return JWT token."""
    r = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["token"]


def baserow_get(path: str, token: str) -> dict:
    r = requests.get(
        f"{BASEROW_URL}/api/{path}",
        headers={"Authorization": f"JWT {token}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


def op_db_query(sql: str) -> str:
    """Query OpenProject embedded Postgres via TCP with password auth."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject",
         OPENPROJECT_CONTAINER,
         "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
         "-t", "-A", "-c", sql],
        capture_output=True, text=True, errors="replace", timeout=15,
    )
    if r.returncode != 0:
        raise RuntimeError(f"OpenProject DB query failed: {r.stderr.strip()}")
    return r.stdout.strip()


# ── Check 1: code-server project directories exist (0pt diagnostic) ──────────
def check_1_project_dirs() -> None:
    """Diagnostic (0pt): all 5 seeded project directories exist. True on the
    pristine image, so it carries no weight — kept as a prerequisite signal
    for the scan-based checks."""
    try:
        missing = []
        for proj in PROJECTS:
            rc, out, err = docker_exec(
                CODE_SERVER_CONTAINER, "test", "-d", f"/home/coder/workspace/{proj}"
            )
            if rc != 0:
                missing.append(proj)
        check("1. Project dirs on code-server", 0, len(missing) == 0,
              "all 5 exist" if not missing else f"missing: {missing}")
    except Exception as e:
        check("1. Project dirs on code-server", 0, False, f"exception: {e}")


# ── Baserow checks ───────────────────────────────────────────────────────────
_baserow_token = None
_baserow_db_id = None
_team_ownership_table_id = None
_dep_edges_table_id = None
_team_ownership_rows = []
_team_ownership_fields = []
_dep_edges_rows = []
_dep_edges_fields = []


def _init_baserow() -> bool:
    """Authenticate and find the database + tables. Returns True if successful."""
    global _baserow_token, _baserow_db_id, _team_ownership_table_id, _dep_edges_table_id
    global _team_ownership_rows, _team_ownership_fields, _dep_edges_rows, _dep_edges_fields
    try:
        _baserow_token = baserow_auth()

        # Find the database
        apps = baserow_get("applications/", _baserow_token)
        for app in apps:
            if app.get("name") == BASEROW_DB_NAME and app.get("type") == "database":
                _baserow_db_id = app["id"]
                break

        if not _baserow_db_id:
            return False

        # Find tables
        tables = baserow_get(f"database/tables/database/{_baserow_db_id}/", _baserow_token)
        for t in tables:
            if t["name"] == "Team Ownership":
                _team_ownership_table_id = t["id"]
            elif t["name"] == "Dependency Edges":
                _dep_edges_table_id = t["id"]

        # Load rows if tables exist
        if _team_ownership_table_id:
            _team_ownership_fields = baserow_get(
                f"database/fields/table/{_team_ownership_table_id}/", _baserow_token
            )
            resp = baserow_get(
                f"database/rows/table/{_team_ownership_table_id}/?user_field_names=true&size=100",
                _baserow_token,
            )
            _team_ownership_rows = resp.get("results", [])

        if _dep_edges_table_id:
            _dep_edges_fields = baserow_get(
                f"database/fields/table/{_dep_edges_table_id}/", _baserow_token
            )
            resp = baserow_get(
                f"database/rows/table/{_dep_edges_table_id}/?user_field_names=true&size=200",
                _baserow_token,
            )
            _dep_edges_rows = resp.get("results", [])

        return True
    except Exception:
        return False


def check_2_baserow_db_exists() -> None:
    """Database 'Service Coupling Atlas Q3' exists in Baserow."""
    try:
        ok = _init_baserow()
        check("2. Baserow DB exists", 1, _baserow_db_id is not None,
              f"db_id={_baserow_db_id}" if _baserow_db_id else "database not found")
    except Exception as e:
        check("2. Baserow DB exists", 1, False, f"exception: {e}")


def check_3_team_ownership_rows() -> None:
    """Team Ownership table has exactly 5 rows with correct project names."""
    try:
        if not _team_ownership_table_id:
            check("3. Team Ownership rows", 2, False, "table not found")
            return

        # Find which field is the primary (Project) field
        row_projects = set()
        for row in _team_ownership_rows:
            # The primary field is typically named "Project" or is the first text field
            proj = row.get("Project") or row.get("Name") or ""
            if isinstance(proj, str) and proj:
                row_projects.add(proj)

        expected = set(PROJECTS)
        missing = expected - row_projects
        extra = row_projects - expected
        ok = len(_team_ownership_rows) == 5 and not missing
        detail = f"{len(_team_ownership_rows)} rows"
        if missing:
            detail += f", missing: {sorted(missing)}"
        if extra:
            detail += f", extra: {sorted(extra)}"
        check("3. Team Ownership rows", 2, ok, detail)
    except Exception as e:
        check("3. Team Ownership rows", 2, False, f"exception: {e}")


def check_4_team_ownership_fields() -> None:
    """Team Ownership rows have correct Owning Team and Tech Lead values."""
    try:
        if not _team_ownership_rows:
            check("4. Team Ownership fields", 2, False, "no rows to check")
            return

        issues = []
        for row in _team_ownership_rows:
            proj = row.get("Project") or row.get("Name") or ""
            if proj not in OWNERSHIP:
                continue
            expected = OWNERSHIP[proj]

            # Owning Team may be a dict (single-select) or string
            owning_team_raw = row.get("Owning Team")
            if isinstance(owning_team_raw, dict):
                owning_team = owning_team_raw.get("value", "")
            elif isinstance(owning_team_raw, list) and owning_team_raw:
                owning_team = owning_team_raw[0].get("value", "") if isinstance(owning_team_raw[0], dict) else str(owning_team_raw[0])
            else:
                owning_team = str(owning_team_raw or "")

            tech_lead = str(row.get("Tech Lead", ""))

            if owning_team != expected["owning_team"]:
                issues.append(f"{proj}: team={owning_team!r} expected {expected['owning_team']!r}")
            if tech_lead != expected["tech_lead"]:
                issues.append(f"{proj}: lead={tech_lead!r} expected {expected['tech_lead']!r}")

        ok = len(issues) == 0 and len(_team_ownership_rows) == 5
        check("4. Team Ownership fields", 2, ok,
              "all correct" if ok else f"issues: {issues[:5]}")
    except Exception as e:
        check("4. Team Ownership fields", 2, False, f"exception: {e}")


def _link_value(raw) -> str:
    """Resolve a Baserow link-row cell (list of dicts) to its display value."""
    if isinstance(raw, list) and raw:
        return raw[0].get("value", "") if isinstance(raw[0], dict) else str(raw[0])
    if isinstance(raw, str):
        return raw
    return ""


def _scan_dependency_edges() -> list[tuple[str, str, str, int]] | None:
    """Scan the workspace scan roots for cross-project import/require references.

    Ground truth: the scan runs in a throwaway container from the code-server
    container's own PRISTINE image (docker inspect → docker run), so agent
    edits to the live source tree cannot move the goalposts.
    Mirrors the task's extraction rules: import/require/include lines whose
    text references another project's name as a whole token ('-' and '_'
    spellings both count; extension-style '.json' suffixes do not).
    Returns (source, target, file, line) tuples, or None if no scan root
    could be read. The result may legitimately be empty.
    """
    edges: list[tuple[str, str, str, int]] = []
    read_any = False
    for src in PROJECTS:
        root = f"/home/coder/workspace/{SCAN_ROOTS[src]}"
        rc, _, _ = image_exec("test", "-d", root, timeout=60)
        if rc != 0:
            continue
        rc, out, _ = image_exec(
            "bash", "-c",
            "grep -rnIE '\\b(import|require|include|from)\\b' "
            + shlex.quote(root) + " 2>/dev/null || true",
            timeout=170,
        )
        if rc != 0:
            continue
        read_any = True
        for line in out.split("\n"):
            parts = line.split(":", 2)
            if len(parts) < 3 or not parts[1].isdigit():
                continue
            path, lineno, content = parts[0], int(parts[1]), parts[2]
            for target in PROJECTS:
                if target == src:
                    continue
                for variant in {target, target.replace("-", "_")}:
                    if re.search(
                        rf"(?<![A-Za-z0-9_.-]){re.escape(variant)}(?![A-Za-z0-9_-])",
                        content,
                    ):
                        edges.append((src, target, path, lineno))
                        break
    return edges if read_any else None


# ── Scan ground truth: grep-vs-rg dual variants ──────────────────────────────
# The task allows grep OR ripgrep for the scan.  GNU `grep -r` descends into
# hidden directories while `rg` skips them by default, so an honest agent can
# legitimately produce either edge set (the seed's only edge lives under a
# hidden `.build/` directory).  We compute the full grep ground truth once,
# derive the "rg variant" by excluding hits whose path contains a hidden
# directory segment, and accept the agent's data if it matches EITHER
# variant.  The variant whose edge multiset matches the Baserow table is
# picked once (defaulting to the grep variant when both match or neither) and
# ALL dependent checks — ck5, ck7's zero-edge branch, ck10's E/X, ck11's pair
# set, ck13's K and ck14's descriptions — are judged against that same
# variant.
_WORKSPACE_PREFIX = "/home/coder/workspace/"
_scan_gt: dict | None = None


def _compute_scan_gt() -> dict:
    """Run the scan once; cache normalized grep- and rg-variant edge lists."""
    global _scan_gt
    if _scan_gt is not None:
        return _scan_gt
    try:
        edges = _scan_dependency_edges()
        scan_err = None
    except Exception as e:  # e.g. pristine-image resolution failure
        edges = None
        scan_err = f": {e}"
    if edges is None:
        _scan_gt = {"error": "could not read pristine-image scan roots" + (scan_err or ""),
                    "grep": None, "rg": None, "chosen": None}
        return _scan_gt
    grep_edges = []
    for src, target, path, lineno in edges:
        p = path[len(_WORKSPACE_PREFIX):] if path.startswith(_WORKSPACE_PREFIX) else path
        grep_edges.append((src, target, p, lineno))

    def _under_hidden_dir(p: str) -> bool:
        return any(part.startswith(".") and part not in (".", "..")
                   for part in p.split("/"))

    rg_edges = [e for e in grep_edges if not _under_hidden_dir(e[2])]
    _scan_gt = {"error": None, "grep": grep_edges, "rg": rg_edges, "chosen": None}
    return _scan_gt


def _real_edge_rows() -> list[dict]:
    """Dependency Edges rows minus Baserow's auto-created blank placeholder
    rows (no Edge ID and no project links)."""
    rows = []
    for row in _dep_edges_rows:
        eid = str(row.get("Edge ID") or row.get("Name") or "").strip()
        has_links = bool(row.get("Source Project")) or bool(row.get("Target Project"))
        if not eid and not has_links:
            continue
        rows.append(row)
    return rows


def _canon_source_file(raw, src: str) -> str:
    """Normalize an agent-entered Source File to the GT's workspace-relative
    form: strip an absolute /home/coder/workspace/ prefix and './', then
    re-prefix the source project directory when the agent stored a
    project-relative path."""
    p = str(raw or "").strip().replace("\\", "/")
    if p.startswith(_WORKSPACE_PREFIX):
        p = p[len(_WORKSPACE_PREFIX):]
    p = p.lstrip("/")
    while p.startswith("./"):
        p = p[2:]
    if src and p and not (p == src or p.startswith(src + "/")):
        p = f"{src}/{p}"
    return p


def _table_edge_tuples() -> list[tuple]:
    """Canonical (source, target, normalized file, line) tuple per real row."""
    tuples = []
    for row in _real_edge_rows():
        src = _link_value(row.get("Source Project"))
        tgt = _link_value(row.get("Target Project"))
        fpath = _canon_source_file(row.get("Source File"), src)
        try:
            lineno = int(float(row.get("Line Number")))
        except (TypeError, ValueError):
            lineno = None
        tuples.append((src, tgt, fpath, lineno))
    return tuples


def _choose_gt_variant() -> tuple[list, str]:
    """Return (edges, variant_name) for the accepted GT variant. Raises
    RuntimeError when the scan truth is unavailable (dependent checks must
    then FAIL, never skip)."""
    gt = _compute_scan_gt()
    if gt["error"]:
        raise RuntimeError(gt["error"])
    if gt["chosen"] is None:
        table = Counter(_table_edge_tuples())
        if table == Counter(gt["grep"]):
            gt["chosen"] = "grep"
        elif table == Counter(gt["rg"]):
            gt["chosen"] = "rg"
        else:
            gt["chosen"] = "grep"
    return gt[gt["chosen"]], gt["chosen"]


def check_5_dep_edges_table() -> None:
    """Dependency Edges rows equal the verifier's own scan as a full
    (source, target, file, line) multiset — against the chosen grep/rg GT
    variant — and, sorted by (Source Project, Source File, Line Number),
    row i carries Edge ID DE-{i+1:03d} (contiguous from DE-001)."""
    try:
        if not _dep_edges_table_id:
            check("5. Dependency Edges table", 3, False, "table not found")
            return
        try:
            gt_edges, variant = _choose_gt_variant()
        except RuntimeError as e:
            check("5. Dependency Edges table", 3, False, str(e))
            return

        rows = _real_edge_rows()
        tuples = _table_edge_tuples()
        expected = Counter(gt_edges)
        got = Counter(tuples)
        tuples_ok = expected == got

        # Edge ID sequence in the task's mandated sort order.
        def _sort_key(pair):
            (src, _tgt, fpath, lineno), _row = pair
            return (src, fpath, lineno if lineno is not None else -1)

        seq_issues = []
        for i, (_tup, row) in enumerate(sorted(zip(tuples, rows), key=_sort_key)):
            eid = str(row.get("Edge ID") or row.get("Name") or "").strip()
            want = f"DE-{i + 1:03d}"
            if eid != want:
                seq_issues.append(f"sorted row {i + 1}: Edge ID={eid!r}, expected {want!r}")

        ok = tuples_ok and not seq_issues
        detail = (f"variant={variant}, scan found {sum(expected.values())} edge(s), "
                  f"table has {len(rows)} row(s)")
        if not tuples_ok:
            missing = list((expected - got).elements())
            extra = list((got - expected).elements())
            if missing:
                detail += f"; missing: {missing[:3]}"
            if extra:
                detail += f"; extra: {extra[:3]}"
        if seq_issues:
            detail += f"; {'; '.join(seq_issues[:2])}"
        check("5. Dependency Edges table", 3, ok, detail)
    except Exception as e:
        check("5. Dependency Edges table", 3, False, f"exception: {e}")


def check_6_dep_edges_links() -> None:
    """Dependency Edges rows have Source Project and Target Project link fields."""
    try:
        if not _dep_edges_fields:
            check("6. Dependency Edges link fields", 2, False, "no fields loaded")
            return

        field_names = {f["name"]: f["type"] for f in _dep_edges_fields}
        has_source = "Source Project" in field_names and "link" in field_names.get("Source Project", "")
        has_target = "Target Project" in field_names and "link" in field_names.get("Target Project", "")

        # Also check rows actually have linked values
        rows_with_links = 0
        for row in _dep_edges_rows:
            sp = row.get("Source Project")
            tp = row.get("Target Project")
            if sp and tp:
                rows_with_links += 1

        # zero edges in the chosen GT variant -> an empty (placeholder-free)
        # table is CORRECT
        try:
            gt_edges, _variant = _choose_gt_variant()
        except RuntimeError:
            gt_edges = None
        zero_ok = (gt_edges is not None and len(gt_edges) == 0
                   and len(_real_edge_rows()) == 0)
        ok = has_source and has_target and (
            zero_ok or (rows_with_links == len(_dep_edges_rows) and len(_dep_edges_rows) > 0))
        check("6. Dependency Edges link fields", 2, ok,
              f"Source={'link_row' if has_source else 'missing'}, Target={'link_row' if has_target else 'missing'}, "
              f"{rows_with_links}/{len(_dep_edges_rows)} rows linked")
    except Exception as e:
        check("6. Dependency Edges link fields", 2, False, f"exception: {e}")


def check_7_cross_team_flag() -> None:
    """Cross Team boolean matches the verifier's OWNERSHIP constant (not the
    agent's Team Ownership rows). Rows whose links do not resolve to one of
    the 5 known projects are failures, never skipped."""
    try:
        rows = _real_edge_rows()
        if not rows:
            # A vacuous zero-edge pass requires the agent to have BUILT the table —
            # a pristine env (no table at all) must not score here.
            if not _dep_edges_table_id:
                check("7. Cross Team flag", 2, False, "Dependency Edges table not found")
                return
            try:
                gt_edges, variant = _choose_gt_variant()
            except RuntimeError as e:
                check("7. Cross Team flag", 2, False, str(e))
                return
            if len(gt_edges) == 0:
                check("7. Cross Team flag", 2, True,
                      f"variant={variant}: 0 edges; nothing to flag")
            else:
                check("7. Cross Team flag", 2, False, "no edge rows to verify")
            return

        issues = []
        for row in rows:
            eid = str(row.get("Edge ID") or row.get("Name") or "").strip() or "<no id>"
            sp_name = _link_value(row.get("Source Project"))
            tp_name = _link_value(row.get("Target Project"))
            if sp_name not in OWNERSHIP or tp_name not in OWNERSHIP:
                issues.append(f"{eid}: unresolvable/unknown project link(s) "
                              f"src={sp_name!r}, target={tp_name!r}")
                continue
            expected_cross = (OWNERSHIP[sp_name]["owning_team"]
                              != OWNERSHIP[tp_name]["owning_team"])
            is_cross = bool(row.get("Cross Team"))
            if is_cross != expected_cross:
                issues.append(f"{eid}: cross={is_cross}, expected={expected_cross}")

        ok = len(issues) == 0
        check("7. Cross Team flag", 2, ok,
              f"all {len(rows)} correct" if ok else f"mismatches: {issues[:5]}")
    except Exception as e:
        check("7. Cross Team flag", 2, False, f"exception: {e}")


# Field spec per table: name -> (type, select option set or None)
_TO_FIELD_SPEC = {
    "Project": ("text", None),
    "Owning Team": ("single_select",
                    {"Frontend", "Infrastructure", "Core Libraries", "Backend"}),
    "Tech Lead": ("text", None),
}
_DE_FIELD_SPEC = {
    "Edge ID": ("text", None),
    "Source Project": ("link_row", None),
    "Target Project": ("link_row", None),
    "Source File": ("text", None),
    "Line Number": ("number", None),
    "Cross Team": ("boolean", None),
}


def check_7b_field_schema() -> None:
    """Both tables carry the field types/options the task specifies, with the
    correct primary fields and link_row targets (REST fields API)."""
    try:
        if not _team_ownership_fields or not _dep_edges_fields:
            check("7b. Field schema", 2, False, "table fields not loaded")
            return

        issues = []

        def _check_table(fields: list, spec: dict, primary_name: str, label: str) -> None:
            by_name = {f.get("name"): f for f in fields}
            for name, (ftype, options) in spec.items():
                f = by_name.get(name)
                if f is None:
                    issues.append(f"{label}.{name}: field missing")
                    continue
                if f.get("type") != ftype:
                    issues.append(f"{label}.{name}: type={f.get('type')!r}, expected {ftype!r}")
                if options is not None:
                    got = {o.get("value") for o in f.get("select_options", [])}
                    if got != options:
                        issues.append(f"{label}.{name}: options={sorted(got)}, "
                                      f"expected {sorted(options)}")
                if ftype == "link_row" and f.get("link_row_table_id") != _team_ownership_table_id:
                    issues.append(f"{label}.{name}: link_row_table_id="
                                  f"{f.get('link_row_table_id')}, expected "
                                  f"{_team_ownership_table_id} (Team Ownership)")
                if name == primary_name and not f.get("primary"):
                    issues.append(f"{label}.{name}: not the primary field")

        _check_table(_team_ownership_fields, _TO_FIELD_SPEC, "Project", "TO")
        _check_table(_dep_edges_fields, _DE_FIELD_SPEC, "Edge ID", "DE")

        check("7b. Field schema", 2, not issues,
              "both tables OK" if not issues else "; ".join(issues[:5]))
    except Exception as e:
        check("7b. Field schema", 2, False, f"exception: {e}")


def check_8_cross_team_view() -> None:
    """'Cross-Team Edges' grid view exists with a Cross Team = true filter and
    sortings Source Project ASC then Target Project ASC (order matters)."""
    try:
        if not _dep_edges_table_id:
            check("8. Cross-Team Edges view", 2, False, "table not found")
            return

        raw = baserow_get(f"database/views/table/{_dep_edges_table_id}/", _baserow_token)
        views = raw.get("results", raw) if isinstance(raw, dict) else raw
        view = next((v for v in views if v.get("name") == "Cross-Team Edges"), None)
        if view is None:
            check("8. Cross-Team Edges view", 2, False,
                  f"view not found among {[v.get('name') for v in views]}")
            return

        fid_by_name = {f.get("name"): f.get("id") for f in _dep_edges_fields}
        cross_fid = fid_by_name.get("Cross Team")
        src_fid = fid_by_name.get("Source Project")
        tgt_fid = fid_by_name.get("Target Project")
        issues = []

        raw = baserow_get(f"database/views/{view['id']}/filters/", _baserow_token)
        filters = raw.get("results", raw) if isinstance(raw, dict) else raw
        # Baserow encodes boolean-true filter type/value in several forms.
        matching = [
            f for f in filters
            if f.get("field") == cross_fid
            and f.get("type") in ("boolean", "equal")
            and f.get("value") in ("1", "true", "True", 1, True)
        ]
        if not matching:
            issues.append(
                "no Cross Team=true filter; filters="
                f"{[(f.get('field'), f.get('type'), f.get('value')) for f in filters]}"
            )

        raw = baserow_get(f"database/views/{view['id']}/sortings/", _baserow_token)
        sortings = raw.get("results", raw) if isinstance(raw, dict) else raw
        got_sort = [(s.get("field"), str(s.get("order", "")).upper()) for s in sortings]
        want_sort = [(src_fid, "ASC"), (tgt_fid, "ASC")]
        if got_sort != want_sort:
            issues.append(f"sortings={got_sort}, expected {want_sort} "
                          "(Source Project ASC, then Target Project ASC)")

        check("8. Cross-Team Edges view", 2, not issues,
              "filter + sortings OK" if not issues else "; ".join(issues))
    except Exception as e:
        check("8. Cross-Team Edges view", 2, False, f"exception: {e}")


# ── OpenProject checks ───────────────────────────────────────────────────────
_op_epic = None       # {"id": int, "type": str, "description": str}
_op_children = []     # [{"id", "subject", "assigned_to_id", "priority", "description"}]


def _init_openproject() -> bool:
    """Find the Infrastructure Upgrade project and the epic (via the OP DB).
    Child fields are fetched per id with single-value queries so multiline
    descriptions or '|' characters cannot corrupt parsing."""
    global _op_epic, _op_children
    try:
        proj = OP_PROJECT.replace("'", "''")
        subj = EPIC_SUBJECT.replace("'", "''")
        proj_id = op_db_query(f"SELECT id FROM projects WHERE name = '{proj}'")
        if not proj_id:
            return False
        proj_id = proj_id.split("\n")[0].strip()

        epic_row = op_db_query(
            f"SELECT wp.id || '|' || t.name FROM work_packages wp "
            f"JOIN types t ON wp.type_id = t.id "
            f"WHERE wp.project_id = {proj_id} AND wp.subject = '{subj}' "
            f"ORDER BY wp.id LIMIT 1"
        )
        if epic_row:
            epic_id_s, _, type_name = epic_row.split("\n")[0].partition("|")
            epic_id = int(epic_id_s)
            # Description may be multiline — fetch it on its own.
            desc = op_db_query(
                f"SELECT COALESCE(description, '') FROM work_packages "
                f"WHERE id = {epic_id}"
            )
            _op_epic = {"id": epic_id, "type": type_name.strip(), "description": desc}

            # Find children: work packages whose parent is the epic
            children_ids = op_db_query(
                f"SELECT wp.id FROM work_packages wp "
                f"WHERE wp.parent_id = {epic_id} ORDER BY wp.id"
            )
            _op_children = []
            for cid in [l.strip() for l in children_ids.split("\n") if l.strip()]:
                subject = op_db_query(
                    f"SELECT subject FROM work_packages WHERE id = {cid}"
                )
                assigned_id = op_db_query(
                    f"SELECT COALESCE(assigned_to_id::text, '') "
                    f"FROM work_packages WHERE id = {cid}"
                )
                priority = op_db_query(
                    f"SELECT COALESCE(e.name, '') FROM work_packages wp "
                    f"LEFT JOIN enumerations e ON e.id = wp.priority_id "
                    f"WHERE wp.id = {cid}"
                )
                child_desc = op_db_query(
                    f"SELECT COALESCE(description, '') FROM work_packages WHERE id = {cid}"
                )
                _op_children.append({
                    "id": int(cid),
                    "subject": subject,
                    "assigned_to_id": assigned_id,
                    "priority": priority,
                    "description": child_desc,
                })

        return True
    except Exception:
        return False


def check_9_op_epic_exists() -> None:
    """Epic 'Dependency map snapshot: 2025-07-22' exists in Infrastructure
    Upgrade with type exactly 'Epic' (hard gate — a Task-type WP fails)."""
    try:
        _init_openproject()
        if _op_epic is None:
            check("9. OpenProject Epic exists", 2, False, "epic not found")
            return
        wp_type = _op_epic.get("type", "").strip()
        ok = wp_type == "Epic"
        detail = f"id={_op_epic['id']}, type={wp_type!r}"
        if not ok:
            detail += " (expected 'Epic')"
        check("9. OpenProject Epic exists", 2, ok, detail)
    except Exception as e:
        check("9. OpenProject Epic exists", 2, False, f"exception: {e}")


def check_10_op_epic_description() -> None:
    """Epic description exactly equals 'Total edges: <E>; Cross-team edges:
    <X>' with E/X computed from the chosen scan-GT variant + OWNERSHIP."""
    try:
        if not _op_epic:
            check("10. Epic description", 2, False, "epic not found")
            return
        try:
            gt_edges, variant = _choose_gt_variant()
        except RuntimeError as e:
            check("10. Epic description", 2, False, str(e))
            return

        e_total = len(gt_edges)
        x_total = sum(
            1 for s, t, _f, _l in gt_edges
            if OWNERSHIP[s]["owning_team"] != OWNERSHIP[t]["owning_team"]
        )
        expected = f"Total edges: {e_total}; Cross-team edges: {x_total}"

        desc_clean = re.sub(r"<[^>]+>", " ", str(_op_epic.get("description") or ""))
        desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
        ok = desc_clean == expected
        check("10. Epic description", 2, ok,
              f"variant={variant}, expected={expected!r}, got={desc_clean!r:.100}")
    except Exception as e:
        check("10. Epic description", 2, False, f"exception: {e}")


def _expected_sync_pairs(gt_edges: list) -> dict[tuple[str, str], dict]:
    """{(TeamA, TeamB) alphabetical: {'k': cross-edge count, 'projects':
    involved project set}} for team pairs with >= 1 cross-team edge,
    derived from the chosen scan GT + the OWNERSHIP constant."""
    pairs: dict[tuple[str, str], dict] = {}
    for s, t, _f, _l in gt_edges:
        team_a = OWNERSHIP[s]["owning_team"]
        team_b = OWNERSHIP[t]["owning_team"]
        if team_a == team_b:
            continue
        key = tuple(sorted((team_a, team_b)))
        entry = pairs.setdefault(key, {"k": 0, "projects": set()})
        entry["k"] += 1
        entry["projects"].update((s, t))
    return pairs


_SYNC_SUBJECT_RE = re.compile(
    r"^Sync:\s+(.+?)\s+↔\s+(.+?)\s+\(\d+\s+coupling points?\)$"
)


def _child_pair_info(child: dict, pairs: dict) -> tuple[dict | None, str | None]:
    """Resolve a child WP to its expected GT pair entry via its subject.
    Returns (pair_info, error)."""
    subj = unicodedata.normalize("NFC", child.get("subject", ""))
    m = _SYNC_SUBJECT_RE.match(subj)
    if not m:
        return None, f"cannot parse teams from subject {child.get('subject', '')!r}"
    key = tuple(sorted((m.group(1), m.group(2))))
    info = pairs.get(key)
    if info is None:
        return None, f"{child.get('subject', '')!r}: team pair not in GT"
    return info, None


def check_11_op_child_tasks_subjects() -> None:
    """Child subjects under the Epic SET-equal the GT-derived expected
    'Sync: <TeamA> ↔ <TeamB> (<K> coupling points)' subjects (extras fail;
    zero cross pairs in GT -> no children expected)."""
    try:
        if not _op_epic:
            check("11. Child Task subjects", 2, False, "epic not found")
            return
        try:
            gt_edges, variant = _choose_gt_variant()
        except RuntimeError as e:
            check("11. Child Task subjects", 2, False, str(e))
            return

        pairs = _expected_sync_pairs(gt_edges)
        expected_subjects = {
            f"Sync: {a} ↔ {b} ({info['k']} coupling points)"
            for (a, b), info in pairs.items()
        }
        actual_subjects = {
            unicodedata.normalize("NFC", c.get("subject", "")) for c in _op_children
        }

        missing = expected_subjects - actual_subjects
        extra = actual_subjects - expected_subjects
        ok = not missing and not extra
        detail = f"variant={variant}, expected {len(expected_subjects)} subject(s)"
        if not expected_subjects and ok:
            detail += "; 0 cross pairs -> 0 sync children"
        if missing:
            detail += f"; missing: {sorted(missing)}"
        if extra:
            detail += f"; extra: {sorted(extra)}"
        check("11. Child Task subjects", 2, ok, detail)
    except Exception as e:
        check("11. Child Task subjects", 2, False, f"exception: {e}")


def check_12_op_child_assignee() -> None:
    """Child Tasks have assigned_to_id == the 'admin' login's user id."""
    try:
        if not _op_children:
            if not _op_epic:
                check("12. Child Task assignees", 1, False, "epic not found")
                return
            try:
                gt_edges, variant = _choose_gt_variant()
            except RuntimeError as e:
                check("12. Child Task assignees", 1, False, str(e))
                return
            if not _expected_sync_pairs(gt_edges):
                check("12. Child Task assignees", 1, True,
                      f"variant={variant}: 0 cross pairs -> 0 sync children expected")
            else:
                check("12. Child Task assignees", 1, False, "no children")
            return

        admin_id = op_db_query("SELECT id FROM users WHERE login = 'admin' LIMIT 1")
        issues = []
        for child in _op_children:
            assigned_id = child.get("assigned_to_id", "")
            if not admin_id or assigned_id != admin_id:
                issues.append(f"{child.get('subject', '')}: assigned_to_id="
                              f"{assigned_id or '<none>'} expected {admin_id}")

        ok = len(issues) == 0
        check("12. Child Task assignees", 1, ok,
              "all assigned to admin" if ok else f"issues: {issues[:3]}")
    except Exception as e:
        check("12. Child Task assignees", 1, False, f"exception: {e}")


def check_13_op_child_priorities() -> None:
    """Child priorities follow the GT-derived K (High when K >= 2 else
    Normal) — K comes from the chosen scan GT, not the agent's subject."""
    try:
        try:
            gt_edges, variant = _choose_gt_variant()
        except RuntimeError as e:
            check("13. Child Task priorities", 1, False, str(e))
            return
        pairs = _expected_sync_pairs(gt_edges)

        if not _op_children:
            if not _op_epic:
                check("13. Child Task priorities", 1, False, "epic not found")
                return
            if not pairs:
                check("13. Child Task priorities", 1, True,
                      f"variant={variant}: 0 cross pairs -> 0 sync children expected")
            else:
                check("13. Child Task priorities", 1, False, "no children")
            return

        issues = []
        for child in _op_children:
            info, err = _child_pair_info(child, pairs)
            if err:
                issues.append(err)
                continue
            expected_prio = "High" if info["k"] >= 2 else "Normal"
            priority_title = child.get("priority", "")
            if priority_title.lower() != expected_prio.lower():
                issues.append(f"K={info['k']}: prio={priority_title!r} "
                              f"expected {expected_prio!r}")

        ok = len(issues) == 0
        check("13. Child Task priorities", 1, ok,
              "all correct" if ok else f"issues: {issues[:3]}")
    except Exception as e:
        check("13. Child Task priorities", 1, False, f"exception: {e}")


def check_14_op_child_descriptions() -> None:
    """Each child description exactly equals 'Projects involved: <alphabetical
    comma-joined project pair>' derived from the chosen scan GT."""
    try:
        try:
            gt_edges, variant = _choose_gt_variant()
        except RuntimeError as e:
            check("14. Child Task descriptions", 1, False, str(e))
            return
        pairs = _expected_sync_pairs(gt_edges)

        if not _op_children:
            if not _op_epic:
                check("14. Child Task descriptions", 1, False, "epic not found")
                return
            if not pairs:
                check("14. Child Task descriptions", 1, True,
                      f"variant={variant}: 0 cross pairs -> 0 sync children expected")
            else:
                check("14. Child Task descriptions", 1, False, "no children")
            return

        issues = []
        for child in _op_children:
            info, err = _child_pair_info(child, pairs)
            if err:
                issues.append(err)
                continue
            expected_desc = "Projects involved: " + ", ".join(sorted(info["projects"]))
            desc_clean = re.sub(r"<[^>]+>", " ", child.get("description", ""))
            desc_clean = re.sub(r"\s+", " ", desc_clean).strip()
            if desc_clean != expected_desc:
                issues.append(f"{child.get('subject', '')}: desc={desc_clean!r} "
                              f"!= expected {expected_desc!r}")

        ok = len(issues) == 0
        check("14. Child Task descriptions", 1, ok,
              "all correct" if ok else f"issues: {issues[:3]}")
    except Exception as e:
        check("14. Child Task descriptions", 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_1_project_dirs()
    check_2_baserow_db_exists()
    check_3_team_ownership_rows()
    check_4_team_ownership_fields()
    check_5_dep_edges_table()
    check_6_dep_edges_links()
    check_7_cross_team_flag()
    check_7b_field_schema()
    check_8_cross_team_view()
    check_9_op_epic_exists()
    check_10_op_epic_description()
    check_11_op_child_tasks_subjects()
    check_12_op_child_assignee()
    check_13_op_child_priorities()
    check_14_op_child_descriptions()

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
