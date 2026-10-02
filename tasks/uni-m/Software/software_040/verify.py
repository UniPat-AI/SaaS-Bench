"""
Verifier for Software-040-I1: Build tabler frontend component inventory across
code-server, Baserow, and OpenProject.

Checks: 19 checks (ck0 zero-weight truth-scan gate + 18 scored) across
code-server, baserow, openproject. Total weight: 26.
Strategy: the verifier RECOMPUTES ground truth itself by replaying the two
task regexes in a throwaway container from the live code-server container's
own PRISTINE image (immune to agent edits; os.walk over
/home/coder/workspace/tabler/core, pruning node_modules/dist). All
truth-dependent checks are gated on that recomputation; agent-filled data is
never trusted. If the truth scan fails, truth-dependent checks FAIL (no
fallback).

File-path normalization: Baserow rows / COMPONENTS.md lines / WP descriptions
may record paths as either "tabler/core/..." or "core/..." (VS Code
multi-root search ambiguity); comparison strips an optional leading "tabler/"
prefix on both sides.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import json
import os
import re
import subprocess
import sys

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

# ── Task constants (from description.md — spec constants, not agent data) ────
CAPTURE_DATE = "2026-03-20"
COMMIT_MSG = "docs: add component inventory 2026-03-20"
TABLER_REPO = "/home/coder/workspace/tabler"
COMPONENTS_MD = "/home/coder/workspace/tabler/docs/COMPONENTS.md"
CATEGORY_OPTIONS = {"Layout", "Form", "Display", "Navigation", "Chart", "Utility"}
CATEGORY_MAP = {
    "TablerTheme": "Utility", "TablerCore": "Utility",
    "NavBar": "Navigation", "SideBar": "Navigation",
    "FormInput": "Form", "FormSelect": "Form",
    "Card": "Display", "Modal": "Display",
    "PageLayout": "Layout", "GridContainer": "Layout",
    "BarChart": "Chart", "LineChart": "Chart",
}

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def _flat(s: str) -> str:
    """Detail strings must not contain newlines."""
    return re.sub(r"[\r\n]+", " | ", str(s))


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = _flat(detail)
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15,
                input_text: str | None = None) -> tuple[int, str, str]:
    cmd = ["docker", "exec"]
    if input_text is not None:
        cmd.append("-i")
    cmd.extend([container, *args])
    r = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace",
        timeout=timeout, input=input_text,
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


def baserow_sql(query: str, sep: str = "|") -> str:
    """Run a psql query against the Baserow database."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc",
        "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow",
        "-t", "-A", "-F", sep, "-c", query,
    )
    if rc != 0:
        raise RuntimeError(f"baserow psql failed: {_flat(err.strip())}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def openproject_sql(query: str) -> str:
    """Run a psql query against the OpenProject database (embedded)."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "bash", "-c",
        f"PGPASSWORD=openproject psql -h 127.0.0.1 -U openproject -d openproject -t -A -c {repr(query)}",
    )
    if rc != 0:
        raise RuntimeError(f"openproject psql failed: {_flat(err.strip())}")
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def norm_path(p: str) -> str:
    """Normalize a recorded file path: strip an optional leading 'tabler/'."""
    p = p.strip()
    if p.startswith("tabler/"):
        p = p[len("tabler/"):]
    return p


def _num_eq(raw: str, expected: float) -> bool:
    try:
        return abs(float(raw) - float(expected)) < 1e-9
    except (TypeError, ValueError):
        return False


# ── Ground-truth scan (runs in a throwaway container from the PRISTINE image,
#    never the live agent-touched container) ──────────────────────────────────
# Replays the two task regexes line-by-line over tabler/core, pruning
# node_modules/dist, skipping non-UTF-8 (binary) files, at most one hit per
# line. Output: exports [(relpath, lineno, name)] sorted by (path, lineno) and
# usage[name] = number of distinct importing files. NEVER hardcoded.
_SCANNER = r'''
import os, re, json
ROOT = "/home/coder/workspace/tabler/core"
WORKSPACE = "/home/coder/workspace"
EXPORT_RE = re.compile(r'^\s*export\s+(?:default\s+)?(?:class|function|const)\s+([A-Z][A-Za-z0-9_]*)')
IMPORT_RE = re.compile(r'^\s*import\s+\{?\s*([A-Z][A-Za-z0-9_]*)\s*\}?\s+from\s+[\'"][^\'"]+[\'"]')
exports = []
importers = {}
nfiles = 0
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in ("node_modules", "dist")]
    for fn in sorted(files):
        path = os.path.join(dirpath, fn)
        try:
            with open(path, "rb") as fh:
                text = fh.read().decode("utf-8", errors="strict")
        except Exception:
            continue
        nfiles += 1
        rel = os.path.relpath(path, WORKSPACE)
        for lineno, line in enumerate(text.splitlines(), 1):
            m = EXPORT_RE.match(line)
            if m:
                exports.append((rel, lineno, m.group(1)))
                continue
            m = IMPORT_RE.match(line)
            if m:
                importers.setdefault(m.group(1), set()).add(rel)
exports.sort(key=lambda e: (e[0], e[1]))
usage = {name: len(fs) for name, fs in sorted(importers.items())}
print(json.dumps({"exports": exports, "usage": usage, "nfiles": nfiles}))
'''

# TRUTH: None until ck0 succeeds. When set:
#   {"rows": [{"path": <tabler/core/...>, "norm": <core/...>, "line": int,
#              "name": str, "category": str, "usage": int, "dep": bool}],
#    "n": int, "d": int, "dep_names": [str], "nfiles": int}
TRUTH: dict | None = None


def check_0_truth_scan() -> None:
    """ck0 (0pt gate): recompute ground truth via regex scan in a throwaway
    container from the pristine image (agent edits to the live tree cannot
    move the goalposts)."""
    global TRUTH
    label = "0. Truth scan: component export/import regex audit recomputed"
    try:
        rc, out, err = image_exec(
            "python3", "-c", _SCANNER,
            timeout=170,
        )
        if rc != 0:
            check(label, 0, False, f"scanner rc={rc}: {_flat(err.strip()[:300])}")
            return
        data = json.loads(out.strip().split("\n")[-1])
        exports = data["exports"]
        usage = data["usage"]
        if not exports:
            check(label, 0, False, "scanner returned zero component exports")
            return
        rows = []
        for path, lineno, name in exports:
            u = int(usage.get(name, 0))
            rows.append({
                "path": path,
                "norm": norm_path(path),
                "line": int(lineno),
                "name": name,
                "category": CATEGORY_MAP.get(name, "Utility"),
                "usage": u,
                "dep": u <= 1,
            })
        rows.sort(key=lambda r: (r["norm"], r["line"]))
        dep_names = [r["name"] for r in rows if r["dep"]]
        TRUTH = {
            "rows": rows,
            "n": len(rows),
            "d": len(dep_names),
            "dep_names": dep_names,
            "nfiles": int(data.get("nfiles", 0)),
        }
        check(label, 0, True,
              f"N={TRUTH['n']} components, D={TRUTH['d']} deprecation candidates, "
              f"{TRUTH['nfiles']} files scanned")
    except Exception as e:
        check(label, 0, False, f"truth scan failed: {e}")


# ── Baserow table loading (shared by ck3-ck8, ck18) ──────────────────────────
EXPECTED_FIELDS = [
    "Component ID", "Component Name", "File Path", "Line Number",
    "Usage Count", "Category", "Deprecation Candidate", "Captured At",
]
_SEP = "\x1f"
_table_cache: dict = {}


def _get_table_id() -> str | None:
    result = baserow_sql(
        "SELECT dt.id FROM database_table dt "
        "JOIN core_application ca ON dt.database_id = ca.id "
        "WHERE ca.name = 'Tabler Component Audit' "
        "AND dt.name = 'Frontend Components' LIMIT 1;"
    )
    lines = [l.strip() for l in result.split("\n") if l.strip()]
    return lines[0] if lines else None


def _load_audit_table() -> dict:
    """Load fields (name -> {id, primary, model}), Category options and all
    non-trashed rows (physical order) once; cached."""
    if _table_cache:
        return _table_cache
    table_id = _get_table_id()
    if not table_id:
        raise RuntimeError("table 'Frontend Components' not found")
    fields: dict[str, dict] = {}
    fres = baserow_sql(
        f"SELECT df.id, df.name, df.\"primary\", ct.model "
        f"FROM database_field df "
        f"JOIN django_content_type ct ON df.content_type_id = ct.id "
        f"WHERE df.table_id = {table_id} AND df.trashed = false "
        f"ORDER BY df.id;",
        sep=_SEP,
    )
    for line in fres.split("\n"):
        if not line.strip():
            continue
        parts = line.split(_SEP)
        if len(parts) >= 4:
            fields[parts[1]] = {
                "id": parts[0].strip(),
                "primary": parts[2].strip() == "t",
                "model": parts[3].strip(),
            }
    options: dict[str, str] = {}
    if "Category" in fields:
        ores = baserow_sql(
            f"SELECT so.id, so.value FROM database_selectoption so "
            f"WHERE so.field_id = {fields['Category']['id']};",
            sep=_SEP,
        )
        for line in ores.split("\n"):
            if not line.strip():
                continue
            parts = line.split(_SEP)
            if len(parts) >= 2:
                options[parts[0].strip()] = parts[1]
    present = [f for f in EXPECTED_FIELDS if f in fields]
    rows: list[dict] = []
    if present:
        cols = ", ".join(f"field_{fields[f]['id']}" for f in present)
        rres = baserow_sql(
            f"SELECT {cols} FROM database_table_{table_id} "
            f"WHERE trashed = false ORDER BY \"order\" ASC, id ASC;",
            sep=_SEP,
        )
        for line in rres.split("\n"):
            if not line.strip():
                continue
            parts = line.split(_SEP)
            rows.append({f: (parts[i] if i < len(parts) else "")
                         for i, f in enumerate(present)})
    _table_cache.update({
        "table_id": table_id,
        "fields": fields,
        "category_options": options,
        "rows": rows,
    })
    return _table_cache


def _need(label: str, weight: int, *field_names: str):
    """Common preamble: TRUTH + table + required fields; returns (data, rows)
    or None after emitting a FAIL."""
    if TRUTH is None:
        check(label, weight, False, "truth scan failed; cannot verify against ground truth")
        return None
    try:
        data = _load_audit_table()
    except Exception as e:
        check(label, weight, False, f"exception: {e}")
        return None
    missing = [f for f in field_names if f not in data["fields"]]
    if missing:
        check(label, weight, False, f"missing fields: {missing}")
        return None
    return data


# ── Baserow checks ───────────────────────────────────────────────────────────

def check_1_baserow_database_exists() -> None:
    """Verify Baserow database 'Tabler Component Audit' exists exactly once."""
    try:
        result = baserow_sql(
            "SELECT COUNT(*) FROM database_database dd "
            "JOIN core_application ca ON dd.application_ptr_id = ca.id "
            "WHERE ca.name = 'Tabler Component Audit';"
        )
        count = int(result.split("\n")[-1])
        check("1. Baserow database 'Tabler Component Audit' exists", 1,
              count == 1, f"found {count} (expected exactly 1)")
    except Exception as e:
        check("1. Baserow database 'Tabler Component Audit' exists", 1, False, f"exception: {e}")


def check_2_baserow_table_exists() -> None:
    """Verify table 'Frontend Components' exists exactly once in the database."""
    try:
        result = baserow_sql(
            "SELECT dt.id FROM database_table dt "
            "JOIN core_application ca ON dt.database_id = ca.id "
            "WHERE ca.name = 'Tabler Component Audit' "
            "AND dt.name = 'Frontend Components';"
        )
        lines = [l.strip() for l in result.split("\n") if l.strip()]
        check("2. Table 'Frontend Components' exists", 1, len(lines) == 1,
              f"table_id={lines[0]}" if len(lines) == 1 else f"found {len(lines)} tables")
    except Exception as e:
        check("2. Table 'Frontend Components' exists", 1, False, f"exception: {e}")


def check_3_row_count_and_component_id_sequence() -> None:
    """Row count == truth N and Component ID sequence is exactly FC-001..FC-00N
    in physical row order (verifier-generated expectation)."""
    label = "3. Exactly one row per component; Component IDs are FC-001..FC-00N in row order"
    data = _need(label, 1, "Component ID")
    if data is None:
        return
    try:
        rows = data["rows"]
        n = TRUTH["n"]
        if len(rows) != n:
            check(label, 1, False, f"row count {len(rows)} != truth {n}")
            return
        actual = [r["Component ID"].strip() for r in rows]
        expected = [f"FC-{i + 1:03d}" for i in range(n)]
        check(label, 1, actual == expected,
              f"actual={actual} expected={expected}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_4_rows_match_truth_name_path_line() -> None:
    """Per-row Component Name / File Path / Line Number equal the truth
    sequence (File Path asc, Line Number asc); path prefix-normalized."""
    label = "4. Rows match truth (Component Name, File Path, Line Number) in truth order"
    data = _need(label, 1, "Component Name", "File Path", "Line Number")
    if data is None:
        return
    try:
        rows = data["rows"]
        if len(rows) != TRUTH["n"]:
            check(label, 1, False, f"row count {len(rows)} != truth {TRUTH['n']}")
            return
        bad = []
        for i, (row, t) in enumerate(zip(rows, TRUTH["rows"])):
            name_ok = row["Component Name"].strip() == t["name"]
            path_ok = norm_path(row["File Path"]) == t["norm"]
            line_ok = _num_eq(row["Line Number"], t["line"])
            if not (name_ok and path_ok and line_ok):
                bad.append(
                    f"row{i + 1}: got ({row['Component Name'].strip()}, "
                    f"{row['File Path'].strip()}, {row['Line Number'].strip()}) "
                    f"expected ({t['name']}, {t['path']}, {t['line']})"
                )
        check(label, 1, not bad, "; ".join(bad) if bad else f"{len(rows)} rows match truth")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5_category_options_and_values() -> None:
    """Category option set EXACTLY equals the 6 spec categories, and each row's
    Category equals the spec mapping (default Utility) recomputed from truth."""
    label = "5. Category option set == 6 spec categories and per-row Category matches mapping"
    data = _need(label, 2, "Category")
    if data is None:
        return
    try:
        opt_values = set(data["category_options"].values())
        options_ok = opt_values == CATEGORY_OPTIONS
        rows = data["rows"]
        problems = []
        if not options_ok:
            problems.append(f"options {sorted(opt_values)} != {sorted(CATEGORY_OPTIONS)}")
        if len(rows) != TRUTH["n"]:
            problems.append(f"row count {len(rows)} != truth {TRUTH['n']}")
        else:
            for i, (row, t) in enumerate(zip(rows, TRUTH["rows"])):
                actual = data["category_options"].get(row["Category"].strip(), "")
                if actual != t["category"]:
                    problems.append(f"row{i + 1} ({t['name']}): Category "
                                    f"{actual or 'empty'} != {t['category']}")
        check(label, 2, not problems,
              "; ".join(problems) if problems else
              f"options exact; {len(rows)} rows match spec mapping")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_usage_count_and_deprecation() -> None:
    """Per-row Usage Count == verifier-recomputed usage[name] and Deprecation
    Candidate == (usage <= 1) — no in-row self-consistency."""
    label = "6. Usage Count matches recomputed truth and Deprecation Candidate == (usage <= 1)"
    data = _need(label, 2, "Usage Count", "Deprecation Candidate")
    if data is None:
        return
    try:
        rows = data["rows"]
        if len(rows) != TRUTH["n"]:
            check(label, 2, False, f"row count {len(rows)} != truth {TRUTH['n']}")
            return
        bad = []
        for i, (row, t) in enumerate(zip(rows, TRUTH["rows"])):
            uc_ok = _num_eq(row["Usage Count"], t["usage"])
            dep_actual = row["Deprecation Candidate"].strip() == "t"
            dep_ok = dep_actual == t["dep"]
            if not (uc_ok and dep_ok):
                bad.append(f"row{i + 1} ({t['name']}): usage "
                           f"{row['Usage Count'].strip() or 'empty'} vs {t['usage']}, "
                           f"dep {dep_actual} vs {t['dep']}")
        check(label, 2, not bad, "; ".join(bad) if bad else f"{len(rows)} rows match truth usage")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_captured_at_date() -> None:
    """Verify Captured At is 2026-03-20 for all rows."""
    label = "7. Captured At = 2026-03-20"
    data = _need(label, 1, "Captured At")
    if data is None:
        return
    try:
        rows = data["rows"]
        if not rows:
            check(label, 1, False, "no rows found")
            return
        wrong = [i + 1 for i, r in enumerate(rows)
                 if not r["Captured At"].strip().startswith(CAPTURE_DATE)]
        check(label, 1, not wrong,
              f"rows with wrong date: {wrong}" if wrong else f"{len(rows)} rows OK")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_8_grid_view_filter_and_sort() -> None:
    """Grid view 'High-Impact Components' exists with filter Usage Count >= 5
    (higher_than_or_equal 5 or higher_than 4) and sort Usage Count DESC."""
    label = "8. Grid view 'High-Impact Components': filter Usage Count >= 5, sort Usage Count DESC"
    try:
        data = _load_audit_table()
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return
    try:
        table_id = data["table_id"]
        vres = baserow_sql(
            f"SELECT v.id FROM database_view v "
            f"JOIN django_content_type ct ON v.content_type_id = ct.id "
            f"WHERE v.table_id = {table_id} AND v.name = 'High-Impact Components' "
            f"AND ct.model = 'gridview';"
        )
        vids = [l.strip() for l in vres.split("\n") if l.strip()]
        if not vids:
            check(label, 1, False, "grid view not found")
            return
        view_id = vids[0]
        fres = baserow_sql(
            f"SELECT f.name, vf.type, vf.value FROM database_viewfilter vf "
            f"JOIN database_field f ON vf.field_id = f.id "
            f"WHERE vf.view_id = {view_id};",
            sep=_SEP,
        )
        filter_ok = False
        filters_seen = []
        for line in fres.split("\n"):
            if not line.strip():
                continue
            parts = line.split(_SEP)
            if len(parts) < 3:
                continue
            fname, ftype, fval = parts[0].strip(), parts[1].strip(), parts[2].strip()
            filters_seen.append(f"{fname}/{ftype}/{fval}")
            if fname == "Usage Count" and (
                (ftype == "higher_than_or_equal" and _num_eq(fval, 5)) or
                (ftype == "higher_than" and _num_eq(fval, 4))
            ):
                filter_ok = True
        sres = baserow_sql(
            f"SELECT f.name, vs.\"order\" FROM database_viewsort vs "
            f"JOIN database_field f ON vs.field_id = f.id "
            f"WHERE vs.view_id = {view_id};",
            sep=_SEP,
        )
        sort_ok = False
        sorts_seen = []
        for line in sres.split("\n"):
            if not line.strip():
                continue
            parts = line.split(_SEP)
            if len(parts) < 2:
                continue
            sorts_seen.append(f"{parts[0].strip()}/{parts[1].strip()}")
            if parts[0].strip() == "Usage Count" and parts[1].strip() == "DESC":
                sort_ok = True
        check(label, 1, filter_ok and sort_ok,
              f"filters={filters_seen or 'none'}, sorts={sorts_seen or 'none'}")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_9_gallery_view_by_category() -> None:
    """Verify Gallery view 'By Category' exists."""
    try:
        table_id = _get_table_id()
        if not table_id:
            check("9. Gallery view 'By Category' exists", 1, False, "table not found")
            return
        # database_view has no `type` column; the view type lives in the
        # polymorphic content type (database_galleryview / django_content_type).
        result = baserow_sql(
            f"SELECT COUNT(*) FROM database_view v "
            f"JOIN django_content_type ct ON v.content_type_id = ct.id "
            f"WHERE v.table_id = {table_id} AND v.name = 'By Category' "
            f"AND ct.model = 'galleryview';"
        )
        count = int(result.split("\n")[-1])
        check("9. Gallery view 'By Category' exists", 1,
              count >= 1, f"found {count}")
    except Exception as e:
        check("9. Gallery view 'By Category' exists", 1, False, f"exception: {e}")


# ── code-server checks ───────────────────────────────────────────────────────

def _read_components_md() -> list[str] | None:
    rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", COMPONENTS_MD)
    if rc != 0:
        return None
    lines = out.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def check_10_components_md_exists() -> None:
    """Verify tabler/docs/COMPONENTS.md exists in code-server."""
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "test", "-f", COMPONENTS_MD,
        )
        check("10. COMPONENTS.md exists", 1, rc == 0,
              "file found" if rc == 0 else "file not found")
    except Exception as e:
        check("10. COMPONENTS.md exists", 1, False, f"exception: {e}")


def check_11_components_md_header() -> None:
    """Verify COMPONENTS.md has correct header lines (line 1-2)."""
    try:
        lines = _read_components_md()
        if lines is None:
            check("11. COMPONENTS.md header lines correct", 2, False, "cannot read file")
            return
        line1_ok = len(lines) >= 1 and lines[0].strip() == "# tabler Component Inventory"
        line2_ok = len(lines) >= 2 and lines[1].strip() == "Captured: 2026-03-20"
        check("11. COMPONENTS.md header lines correct", 2,
              line1_ok and line2_ok,
              f"line1={'OK' if line1_ok else repr(lines[0] if lines else '')}, "
              f"line2={'OK' if line2_ok else repr(lines[1] if len(lines) > 1 else '')}")
    except Exception as e:
        check("11. COMPONENTS.md header lines correct", 2, False, f"exception: {e}")


def check_12_components_md_counts() -> None:
    """COMPONENTS.md lines 3-4 count numbers equal the recomputed truth N/D."""
    label = "12. COMPONENTS.md counts match truth (Total components / Deprecation candidates)"
    if TRUTH is None:
        check(label, 2, False, "truth scan failed; cannot verify against ground truth")
        return
    try:
        lines = _read_components_md()
        if lines is None:
            check(label, 2, False, "cannot read file")
            return
        exp3 = f"Total components: {TRUTH['n']}"
        exp4 = f"Deprecation candidates: {TRUTH['d']}"
        line3 = lines[2].strip() if len(lines) > 2 else ""
        line4 = lines[3].strip() if len(lines) > 3 else ""
        line3_ok = line3 == exp3
        line4_ok = line4 == exp4
        check(label, 2, line3_ok and line4_ok,
              f"line3={'OK' if line3_ok else repr(line3) + ' != ' + repr(exp3)}, "
              f"line4={'OK' if line4_ok else repr(line4) + ' != ' + repr(exp4)}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_13_components_md_entries_reconcile() -> None:
    """Whole-file reconciliation: lines 5+ must sequence-equal the expected
    entries built from truth, alphabetical by Component Name, with path
    prefix normalization ('tabler/core/...' or 'core/...' both accepted)."""
    label = "13. COMPONENTS.md entry lines sequence-equal truth (alpha by Component Name)"
    if TRUTH is None:
        check(label, 2, False, "truth scan failed; cannot verify against ground truth")
        return
    try:
        lines = _read_components_md()
        if lines is None:
            check(label, 2, False, "cannot read file")
            return
        entries = [l.strip() for l in lines[4:]]
        expected_rows = sorted(TRUTH["rows"], key=lambda r: r["name"])
        if len(entries) != len(expected_rows):
            check(label, 2, False,
                  f"{len(entries)} entry lines != {len(expected_rows)} truth components")
            return
        bad = []
        for i, (actual, t) in enumerate(zip(entries, expected_rows)):
            tail = f"({t['category']}, used {t['usage']}x) — "
            exp_full = f"- {t['name']} {tail}tabler/{t['norm']}:{t['line']}"
            exp_norm = f"- {t['name']} {tail}{t['norm']}:{t['line']}"
            if actual not in (exp_full, exp_norm):
                bad.append(f"line{i + 5}: {actual!r} != {exp_norm!r}")
        check(label, 2, not bad,
              "; ".join(bad) if bad else f"{len(entries)} entry lines match truth")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_14_git_commit_message_and_single_file() -> None:
    """Commit with the exact message exists AND that commit touches exactly one
    file: docs/COMPONENTS.md (stage-only-that-file requirement)."""
    label = "14. Git commit exact message and touches only docs/COMPONENTS.md"
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER,
            "git", "-C", TABLER_REPO, "-c", f"safe.directory={TABLER_REPO}",
            "log", "--all", "--format=%H%x00%s",
        )
        if rc != 0:
            check(label, 2, False, f"git log failed: {_flat(err.strip()[:200])}")
            return
        shas = [line.split("\x00", 1)[0] for line in out.strip().split("\n")
                if "\x00" in line and line.split("\x00", 1)[1] == COMMIT_MSG]
        if not shas:
            check(label, 2, False, "no commit with exact message found")
            return
        for sha in shas:
            rc2, out2, err2 = docker_exec(
                CODE_SERVER_CONTAINER,
                "git", "-C", TABLER_REPO, "-c", f"safe.directory={TABLER_REPO}",
                "show", "--name-only", "--format=", sha,
            )
            files = [l.strip() for l in out2.strip().split("\n") if l.strip()]
            if rc2 == 0 and files == ["docs/COMPONENTS.md"]:
                check(label, 2, True, f"commit {sha[:10]} touches exactly docs/COMPONENTS.md")
                return
        check(label, 2, False,
              f"commit(s) {[s[:10] for s in shas]} do not touch exactly one file docs/COMPONENTS.md")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── OpenProject checks ───────────────────────────────────────────────────────

def _op_review_tasks() -> list[str]:
    """Subjects of Task WPs in demo-project matching 'Review deprecation:%'."""
    result = openproject_sql(
        "SELECT wp.subject FROM work_packages wp "
        "JOIN projects p ON wp.project_id = p.id "
        "JOIN types t ON wp.type_id = t.id "
        "WHERE p.identifier = 'demo-project' "
        "AND t.name = 'Task' "
        "AND wp.subject LIKE 'Review deprecation:%';"
    )
    return [l.strip() for l in result.split("\n") if l.strip()]


def check_15_openproject_tasks_exact_set() -> None:
    """Exactly one 'Review deprecation: <name>' Task per truth deprecation
    candidate — subject multiset EXACTLY equals the truth-derived set."""
    label = "15. OpenProject 'Review deprecation' task subjects exactly match truth set"
    if TRUTH is None:
        check(label, 2, False, "truth scan failed; cannot verify against ground truth")
        return
    try:
        subjects = _op_review_tasks()
        expected = {f"Review deprecation: {name}" for name in TRUTH["dep_names"]}
        count_ok = len(subjects) == len(expected)
        set_ok = set(subjects) == expected
        check(label, 2, count_ok and set_ok,
              f"found {len(subjects)} tasks {sorted(set(subjects))}, "
              f"expected {len(expected)} {sorted(expected)}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_16_openproject_task_assignee() -> None:
    """Verify OpenProject tasks are assigned to OpenProject Admin with Normal priority."""
    try:
        result = openproject_sql(
            "SELECT wp.subject, u.login, e.name AS priority "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "LEFT JOIN users u ON wp.assigned_to_id = u.id "
            "LEFT JOIN enumerations e ON wp.priority_id = e.id "
            "WHERE p.identifier = 'demo-project' "
            "AND t.name = 'Task' "
            "AND wp.subject LIKE 'Review deprecation:%';"
        )
        lines = [l.strip() for l in result.split("\n") if l.strip()]
        if not lines:
            check("16. OpenProject tasks: assignee=admin, priority=Normal", 1, False, "no tasks found")
            return
        all_ok = True
        details = []
        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                assignee_ok = parts[1] == "admin"
                priority_ok = parts[2] == "Normal"
                if not (assignee_ok and priority_ok):
                    all_ok = False
                    details.append(f"assignee={parts[1]}, priority={parts[2]}")
            else:
                all_ok = False
                details.append(f"unexpected format: {line}")
        check("16. OpenProject tasks: assignee=admin, priority=Normal", 1,
              all_ok, "; ".join(details) if details else f"{len(lines)} tasks OK")
    except Exception as e:
        check("16. OpenProject tasks: assignee=admin, priority=Normal", 1, False, f"exception: {e}")


def check_17_openproject_task_descriptions() -> None:
    """Each task description exactly equals the truth-derived string
    'File: <path>:<line>; Usage: <u>; Category: <cat>; Captured: 2026-03-20'
    (backslash-stripped, path prefix-normalized, paired by subject)."""
    label = "17. OpenProject task descriptions exactly match truth-derived strings"
    if TRUTH is None:
        check(label, 2, False, "truth scan failed; cannot verify against ground truth")
        return
    try:
        result = openproject_sql(
            "SELECT wp.subject || chr(31) || "
            "regexp_replace(COALESCE(wp.description, ''), E'[\\n\\r]+', ' ', 'g') "
            "FROM work_packages wp "
            "JOIN projects p ON wp.project_id = p.id "
            "JOIN types t ON wp.type_id = t.id "
            "WHERE p.identifier = 'demo-project' "
            "AND t.name = 'Task' "
            "AND wp.subject LIKE 'Review deprecation:%';"
        )
        lines = [l for l in result.split("\n") if l.strip()]
        dep_rows = {r["name"]: r for r in TRUTH["rows"] if r["dep"]}
        if not dep_rows:
            check(label, 2, not lines,
                  "truth has no deprecation candidates; no tasks expected")
            return
        if not lines:
            check(label, 2, False, "no tasks found")
            return
        problems = []
        seen: set[str] = set()
        for line in lines:
            if "\x1f" not in line:
                problems.append(f"unexpected row format: {line[:80]}")
                continue
            subject, desc = line.split("\x1f", 1)
            subject = subject.strip()
            name = subject[len("Review deprecation: "):] if subject.startswith(
                "Review deprecation: ") else ""
            t = dep_rows.get(name)
            if t is None:
                problems.append(f"unexpected task subject: {subject}")
                continue
            if name in seen:
                problems.append(f"duplicate task for {name}")
                continue
            seen.add(name)
            # CKEditor backslash-escape stripping + whitespace collapse
            # (newlines were flattened to spaces in SQL)
            desc_clean = re.sub(r"\s+", " ", desc.replace("\\", "")).strip()
            tail = f"; Usage: {t['usage']}; Category: {t['category']}; Captured: {CAPTURE_DATE}"
            exp_full = f"File: tabler/{t['norm']}:{t['line']}{tail}"
            exp_norm = f"File: {t['norm']}:{t['line']}{tail}"
            if desc_clean not in (exp_full, exp_norm):
                problems.append(f"{name}: desc {desc_clean!r} != {exp_norm!r}")
        missing = set(dep_rows) - seen
        if missing:
            problems.append(f"missing tasks for: {sorted(missing)}")
        check(label, 2, not problems,
              "; ".join(problems) if problems else f"{len(seen)} descriptions match truth")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Field schema check ───────────────────────────────────────────────────────

def check_18_field_schema() -> None:
    """Field schema matches spec: exact field-name set; Component ID primary
    text; Component Name/File Path text; Line Number/Usage Count number;
    Category single_select; Deprecation Candidate boolean; Captured At date."""
    label = "18. Field schema matches spec (names exact, types, primary)"
    expected_models = {
        "Component ID": "textfield",
        "Component Name": "textfield",
        "File Path": "textfield",
        "Line Number": "numberfield",
        "Usage Count": "numberfield",
        "Category": "singleselectfield",
        "Deprecation Candidate": "booleanfield",
        "Captured At": "datefield",
    }
    try:
        data = _load_audit_table()
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return
    try:
        fields = data["fields"]
        problems = []
        if set(fields) != set(expected_models):
            problems.append(
                f"field names {sorted(fields)} != {sorted(expected_models)}")
        for fname, model in expected_models.items():
            meta = fields.get(fname)
            if meta is None:
                continue
            if meta["model"] != model:
                problems.append(f"{fname}: type {meta['model']} != {model}")
            want_primary = fname == "Component ID"
            if meta["primary"] != want_primary:
                problems.append(f"{fname}: primary={meta['primary']}, expected {want_primary}")
        check(label, 1, not problems,
              "; ".join(problems) if problems else "8 fields, types and primary OK")
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    check_0_truth_scan()
    check_1_baserow_database_exists()
    check_2_baserow_table_exists()
    check_3_row_count_and_component_id_sequence()
    check_4_rows_match_truth_name_path_line()
    check_5_category_options_and_values()
    check_6_usage_count_and_deprecation()
    check_7_captured_at_date()
    check_8_grid_view_filter_and_sort()
    check_9_gallery_view_by_category()
    check_10_components_md_exists()
    check_11_components_md_header()
    check_12_components_md_counts()
    check_13_components_md_entries_reconcile()
    check_14_git_commit_message_and_single_file()
    check_15_openproject_tasks_exact_set()
    check_16_openproject_task_assignee()
    check_17_openproject_task_descriptions()
    check_18_field_schema()

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
