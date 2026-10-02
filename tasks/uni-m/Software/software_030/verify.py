"""
Verifier for Software-030-I1: Code Complexity Audit Across Three Workspace Projects

Checks: 17 weighted checks (plus a 0pt ground-truth precheck) across
code-server, baserow, openproject.
Strategy: the verifier RECOMPUTES ground truth itself by re-running the three
awk measurement commands from the task in a throwaway container from the
code-server container's own pristine image (docker inspect → docker run), so
agent edits to the live source tree cannot move the goalposts; every
truth-dependent check is gated on that recomputed data. Agent-filled values
are never trusted as a source of expectations.

Dual-convention note: todo-api and data-analyzer each contain one empty
`tests/__init__.py`, for which the task's awk command prints ",0,0,0.0"
(empty File Path). A legal solution therefore has either 38 rows (named files
only) or 40 rows (including the 2 empty-path rows). Both consistent conventions are
accepted; the markdown report must agree with the table's convention.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER.
"""

import os
import sys
import subprocess
import json
import re
import requests

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_PORT = os.environ.get("BASEROW_PORT")
BASEROW_CONTAINER = os.environ.get("BASEROW_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

_required = {
    "CODE_SERVER_CONTAINER": CODE_SERVER_CONTAINER,
    "BASEROW_PORT": BASEROW_PORT,
    "BASEROW_CONTAINER": BASEROW_CONTAINER,
    "BASEROW_DB_CONTAINER": BASEROW_DB_CONTAINER,
    "OPENPROJECT_CONTAINER": OPENPROJECT_CONTAINER,
}
for var, val in _required.items():
    if not val:
        print(f"FATAL: {var} not set", file=sys.stderr)
        sys.exit(1)

BASEROW_URL = f"http://{HOST}:{BASEROW_PORT}"

AUDIT_DATE = "2025-05-15"
EXPECTED_PROJECTS = ("blog-engine", "data-analyzer", "todo-api")

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    detail = " ".join(str(detail).split())  # no newlines in detail
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    tail = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{tail}", file=sys.stderr)


# ── Helpers ───────────────────────────────────────────────────────────────────
def docker_exec(container: str, *args: str, timeout: int = 15):
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
    """Get Baserow JWT access token."""
    resp = requests.post(
        f"{BASEROW_URL}/api/user/token-auth/",
        json={"email": "admin@example.com", "password": "Admin1234"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    # Baserow returns access_token (JWT) or token depending on version
    return data.get("access_token") or data.get("token", "")


def baserow_get(path: str, token: str, params: dict = None):
    resp = requests.get(
        f"{BASEROW_URL}/api{path}",
        headers={"Authorization": f"JWT {token}"},
        params=params or {},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def op_db_query(sql: str) -> str:
    """Query OpenProject embedded postgres (single-value / simple queries)."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
        "-t", "-A", "-c", sql,
        timeout=15,
    )
    return out.strip("\r\n")  # NOT .strip(): \x1f is Python whitespace and would eat the last row's trailing field sep


def op_db_query_rows(sql: str):
    """Query OpenProject postgres with unit/record separators so multi-row
    results (and descriptions containing newlines) split per-row safely.

    Returns list of field lists, or None on psql error."""
    rc, out, err = docker_exec(
        OPENPROJECT_CONTAINER,
        "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject",
        "-t", "-A", "-F", "\x1f", "-R", "\x1e", "-c", sql,
        timeout=15,
    )
    if rc != 0:
        return None
    rows = []
    for rec in out.split("\x1e"):
        if rec.strip():
            rows.append(rec.split("\x1f"))
    return rows


# ── Ground truth: re-run the three task awk commands (pristine image) ─────────
# Each command is stored as a single bash -c string (raw string: the \n inside
# the awk printf and the trailing \; must reach bash literally).
AWK_COMMANDS = {
    "todo-api": r"""cd /home/coder/workspace/todo-api && find app tests -type f -name "*.py" -exec awk 'BEGIN{OFS=","} FNR==1{f=FILENAME; loc=0; fn=0; tl=0} {loc++} /^[[:space:]]*def[[:space:]]/{fn++} END{printf "%s,%d,%d,%.1f\n", f, loc, fn, (fn>0?loc/fn:0)}' {} \;""",
    "data-analyzer": r"""cd /home/coder/workspace/data-analyzer && find src tests scripts -type f -name "*.py" -exec awk 'BEGIN{OFS=","} FNR==1{f=FILENAME; loc=0; fn=0} {loc++} /^[[:space:]]*def[[:space:]]/{fn++} END{printf "%s,%d,%d,%.1f\n", f, loc, fn, (fn>0?loc/fn:0)}' {} \;""",
    "blog-engine": r"""cd /home/coder/workspace/blog-engine && find src -type f -name "*.js" -exec awk 'BEGIN{OFS=","} FNR==1{f=FILENAME; loc=0; fn=0} {loc++} /function[[:space:]]|=>|^[[:space:]]*[a-zA-Z_]+[[:space:]]*\(/{fn++} END{printf "%s,%d,%d,%.1f\n", f, loc, fn, (fn>0?loc/fn:0)}' {} \;""",
}


def _band_for(loc: int, avg: float) -> str:
    if loc >= 1000 or avg >= 40:
        return "Critical"
    if loc >= 500 or avg >= 25:
        return "High"
    if loc >= 200:
        return "Medium"
    return "Low"


def compute_truth():
    """Re-run the three awk commands and parse per-file metrics.

    Returns dict with:
      named:   [entry] for rows with a non-empty File Path, sorted by
               (project, path)
      empties: [entry] for empty-file rows (File Path == "")
      by_key:  {(project, path): entry} incl. one entry per empty-path project
    or None if any command fails / parses empty (truth-dependent checks must
    then FAIL — no fallback to agent data)."""
    named, empties = [], []
    for project in EXPECTED_PROJECTS:
        cmd = AWK_COMMANDS[project]
        try:
            rc, out, err = image_exec("bash", "-c", cmd, timeout=120)
        except Exception:
            return None
        if rc != 0 or not out.strip():
            return None
        for line in out.splitlines():
            line = line.rstrip("\r")
            if line.strip() == "":
                # skip fully blank lines only; the empty-file row ",0,0,0.0"
                # still contains digits and is parsed below
                continue
            parts = line.split(",")
            if len(parts) < 4:
                return None
            path = ",".join(parts[:-3])
            try:
                loc = int(parts[-3])
                fn = int(parts[-2])
                avg_str = parts[-1].strip()
                avg = float(avg_str)
            except ValueError:
                return None
            entry = {
                "project": project, "path": path, "loc": loc, "fn": fn,
                "avg_str": avg_str, "avg": avg, "band": _band_for(loc, avg),
            }
            (empties if path == "" else named).append(entry)
    if not named:
        return None
    named.sort(key=lambda e: (e["project"], e["path"]))
    by_key = {}
    for e in named + empties:
        by_key[(e["project"], e["path"])] = e
    # named (project, path) must be unique for 1-1 accounting
    if len({(e["project"], e["path"]) for e in named}) != len(named):
        return None
    return {"named": named, "empties": empties, "by_key": by_key}


def truth_top10(truth):
    """Expected OpenProject WP source rows: High/Critical, ordered by
    Lines Of Code desc then File Path asc, first 10."""
    hc = [e for e in truth["named"] if e["band"] in ("High", "Critical")]
    hc.sort(key=lambda e: (-e["loc"], e["path"], e["project"]))
    return hc[:10]


def wp_subject_for(e) -> str:
    return f"Refactor: {e['path']} ({e['loc']} LOC, {e['avg_str']} avg fn length)"


def truth_band_counts(truth):
    counts = {"Critical": 0, "High": 0, "Medium": 0, "Low": 0}
    for e in truth["named"]:
        counts[e["band"]] += 1
    return counts


# ── Shared state for cross-check consistency ──────────────────────────────────
_TRUTH = None        # set in main()
_baserow_rows = []   # populated by check_3 (all rows)
_rows_clean = []     # populated by check_3 (placeholder rows removed)
_caliber = None      # "named" (38) or "all" (40), set by check_3
_baserow_table_id = None


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_baserow_database_exists():
    """Baserow database 'Code Complexity Audit Q2 2025' exists."""
    try:
        token = baserow_auth()
        apps = baserow_get("/applications/", token)
        target_db = None
        for app in apps:
            if app.get("name") == "Code Complexity Audit Q2 2025" and app.get("type") == "database":
                target_db = app
                break
        check("1. Baserow DB 'Code Complexity Audit Q2 2025' exists", 1,
              target_db is not None,
              f"found DB id={target_db['id']}" if target_db else "database not found")
        return {"token": token, "db": target_db} if target_db else {"token": token, "db": None}
    except Exception as e:
        check("1. Baserow DB 'Code Complexity Audit Q2 2025' exists", 1, False, f"exception: {e}")
        return None


def check_2_table_and_fields(ctx):
    """Table 'Complexity Metrics' exists with exactly the required field
    schema (names, primary, types, select options, decimal places)."""
    global _baserow_table_id
    label = "2. Table 'Complexity Metrics' field schema exact"
    if not ctx or not ctx.get("db"):
        check(label, 2, False, "no DB context")
        return ctx
    try:
        token = ctx["token"]
        db_id = ctx["db"]["id"]
        tables = baserow_get(f"/database/tables/database/{db_id}/", token)
        target_table = None
        for t in tables:
            if t.get("name") == "Complexity Metrics":
                target_table = t
                break
        if not target_table:
            check(label, 2, False, "table not found")
            return ctx
        table_id = target_table["id"]
        _baserow_table_id = table_id
        fields = baserow_get(f"/database/fields/table/{table_id}/", token)
        fmap = {f["name"]: f for f in fields}
        ctx["table_id"] = table_id
        ctx["fields"] = fmap

        required_fields = {"Metric ID", "Project", "File Path", "Lines Of Code",
                           "Function Count", "Avg Function Length", "Complexity Band", "Captured At"}
        problems = []
        names = set(fmap)
        if names != required_fields:
            missing = required_fields - names
            extra = names - required_fields
            problems.append(f"field name set mismatch: missing={sorted(missing)}, extra={sorted(extra)}")

        def opt_values(f):
            return {o.get("value") for o in (f.get("select_options") or [])}

        f = fmap.get("Metric ID")
        if f and not (f.get("primary") is True and f.get("type") == "text"):
            problems.append(f"Metric ID: expected primary text, got primary={f.get('primary')} type={f.get('type')}")
        f = fmap.get("Project")
        if f:
            if f.get("type") != "single_select":
                problems.append(f"Project: expected single_select, got {f.get('type')}")
            elif opt_values(f) != set(EXPECTED_PROJECTS):
                problems.append(f"Project options != {sorted(EXPECTED_PROJECTS)}: got {sorted(opt_values(f))}")
        f = fmap.get("File Path")
        if f and f.get("type") != "text":
            problems.append(f"File Path: expected text, got {f.get('type')}")
        for name in ("Lines Of Code", "Function Count"):
            f = fmap.get(name)
            if f and f.get("type") != "number":
                problems.append(f"{name}: expected number, got {f.get('type')}")
        f = fmap.get("Avg Function Length")
        if f:
            if f.get("type") != "number":
                problems.append(f"Avg Function Length: expected number, got {f.get('type')}")
            elif f.get("number_decimal_places") != 1:
                problems.append(f"Avg Function Length: expected 1 decimal place, got {f.get('number_decimal_places')}")
        f = fmap.get("Complexity Band")
        if f:
            if f.get("type") != "single_select":
                problems.append(f"Complexity Band: expected single_select, got {f.get('type')}")
            elif opt_values(f) != {"Low", "Medium", "High", "Critical"}:
                problems.append(f"Complexity Band options != Low/Medium/High/Critical: got {sorted(opt_values(f))}")
        f = fmap.get("Captured At")
        if f and f.get("type") != "date":
            problems.append(f"Captured At: expected date, got {f.get('type')}")

        passed = len(problems) == 0
        check(label, 2, passed,
              f"table id={table_id}, schema OK" if passed else "; ".join(problems)[:400])
        return ctx
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return ctx


def _get_field_value(row: dict, field_name: str, fields: dict):
    """Get a field value from a row, trying both name-based and field_id-based keys."""
    val = row.get(field_name)
    if val is not None:
        return val
    field_info = fields.get(field_name, {})
    field_key = f"field_{field_info.get('id', '')}"
    return row.get(field_key)


def _scalar(row: dict, field_name: str, fields: dict):
    """Field value reduced to a scalar (single-select dicts -> their value)."""
    val = _get_field_value(row, field_name, fields)
    if isinstance(val, dict):
        val = val.get("value", "")
    return val


def _is_blank_placeholder_row(row: dict, fields: dict) -> bool:
    """Skip Baserow's auto-created blank placeholder rows: every checked
    field is empty/default (text '', None, or boolean false)."""
    for name in ("Metric ID", "Project", "File Path", "Lines Of Code",
                 "Function Count", "Avg Function Length", "Complexity Band",
                 "Captured At"):
        val = _scalar(row, name, fields)
        if val not in (None, "", False, "false", "False"):
            return False
    return True


def check_3_rows_match_truth(ctx):
    """Table rows match the recomputed awk ground truth 1-1 (rows, per-row
    LOC / Function Count / Avg Function Length), tolerating both consistent
    conventions: 38 named rows, or 38+2 empty-path rows = 40."""
    global _baserow_rows, _rows_clean, _caliber
    label = "3. Table rows match recomputed awk ground truth"
    if not ctx or "table_id" not in ctx:
        check(label, 2, False, "no table context")
        return
    try:
        token = ctx["token"]
        table_id = ctx["table_id"]
        fields = ctx.get("fields", {})
        page = 1
        all_rows = []
        while True:
            data = baserow_get(f"/database/rows/table/{table_id}/",
                               token, params={"size": 200, "page": page})
            all_rows.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        _baserow_rows = all_rows
        _rows_clean = [r for r in all_rows if not _is_blank_placeholder_row(r, fields)]

        if _TRUTH is None:
            check(label, 2, False, "ground truth recompute failed; cannot verify table")
            return

        named = _TRUTH["named"]
        empties = _TRUTH["empties"]
        n_named, n_all = len(named), len(named) + len(empties)
        rows = _rows_clean

        if len(rows) == n_named:
            _caliber = "named"
            expected = list(named)
        elif empties and len(rows) == n_all:
            _caliber = "all"
            expected = list(named) + list(empties)
        else:
            check(label, 2, False,
                  f"row count {len(rows)} != {n_named} (named files) and != {n_all} (incl. empty-path rows)")
            return

        pool = {}
        for e in expected:
            pool.setdefault((e["project"], e["path"]), []).append(e)

        problems = []
        for idx, row in enumerate(rows, 1):
            proj = str(_scalar(row, "Project", fields) or "")
            path = str(_scalar(row, "File Path", fields) or "")
            key = (proj, path)
            bucket = pool.get(key)
            if not bucket:
                problems.append(f"row {idx} ({proj},{path or '<empty>'}) not in truth (or duplicated)")
                continue
            e = bucket.pop()
            if not bucket:
                del pool[key]
            try:
                loc = float(_scalar(row, "Lines Of Code", fields))
                fn = float(_scalar(row, "Function Count", fields))
                avg = float(_scalar(row, "Avg Function Length", fields))
            except (TypeError, ValueError):
                problems.append(f"row {idx} ({proj},{path or '<empty>'}): non-numeric LOC/FN/avg")
                continue
            if abs(loc - e["loc"]) > 1e-6:
                problems.append(f"({proj},{path or '<empty>'}): LOC {loc} != {e['loc']}")
            if abs(fn - e["fn"]) > 1e-6:
                problems.append(f"({proj},{path or '<empty>'}): FN {fn} != {e['fn']}")
            if abs(avg - e["avg"]) > 0.05:
                problems.append(f"({proj},{path or '<empty>'}): avg {avg} != {e['avg_str']}")
            if path == "":
                band = str(_scalar(row, "Complexity Band", fields) or "")
                if band != "Low":
                    problems.append(f"empty-path row ({proj}): Band '{band}' != 'Low'")

        leftover = sum(len(v) for v in pool.values())
        if leftover:
            missing_keys = [f"({p},{fp or '<empty>'})" for (p, fp) in pool][:3]
            problems.append(f"{leftover} truth rows missing from table, e.g. {missing_keys}")

        passed = len(problems) == 0
        check(label, 2, passed,
              f"{len(rows)} rows match truth ({_caliber} convention)" if passed
              else f"{len(problems)} problems: " + "; ".join(problems[:4])[:350])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_metric_ids_sequential(ctx):
    """Metric IDs are exactly CM-001..CM-0NN, contiguous over ALL rows in
    physical row order (N = 38 or 40 per the truth convention); blank IDs fail."""
    label = "4. Metric IDs exactly CM-001..CM-0NN in row order"
    if not ctx:
        check(label, 1, False, "no table context")
        return
    if _TRUTH is None:
        check(label, 1, False, "ground truth recompute failed")
        return
    if not _rows_clean:
        check(label, 1, False, "no rows")
        return
    try:
        fields = ctx.get("fields", {})
        n_named = len(_TRUTH["named"])
        n_all = n_named + len(_TRUTH["empties"])
        ids = [str(_scalar(row, "Metric ID", fields) or "") for row in _rows_clean]
        expected_ids = [f"CM-{i:03d}" for i in range(1, len(ids) + 1)]
        count_ok = len(ids) in {n_named, n_all}
        seq_ok = ids == expected_ids
        passed = count_ok and seq_ok
        if passed:
            detail = f"{len(ids)} IDs, CM-001..{expected_ids[-1]}"
        else:
            first_bad = next((f"row {i + 1}: got '{a}' expected '{b}'"
                              for i, (a, b) in enumerate(zip(ids, expected_ids)) if a != b), "")
            detail = f"count_ok={count_ok} ({len(ids)} rows), sequential={seq_ok} {first_bad}"
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


def check_5_complexity_band_correct(ctx):
    """Complexity Band per row equals the band recomputed from the TRUTH
    LOC/avg values (not from the row's own numbers)."""
    label = "5. Complexity Band matches truth-derived thresholds"
    if not ctx:
        check(label, 2, False, "no table context")
        return
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return
    if not _rows_clean:
        check(label, 2, False, "no rows")
        return
    try:
        fields = ctx.get("fields", {})
        by_key = _TRUTH["by_key"]
        mismatches = []
        for row in _rows_clean:
            proj = str(_scalar(row, "Project", fields) or "")
            path = str(_scalar(row, "File Path", fields) or "")
            band = str(_scalar(row, "Complexity Band", fields) or "")
            e = by_key.get((proj, path))
            if e is None:
                mismatches.append(f"({proj},{path or '<empty>'}): not in truth")
                continue
            if band != e["band"]:
                mismatches.append(
                    f"({proj},{path or '<empty>'}): got '{band}' expected '{e['band']}' "
                    f"(truth LOC={e['loc']}, avg={e['avg_str']})")
        passed = len(mismatches) == 0
        check(label, 2, passed,
              f"all {len(_rows_clean)} rows correct" if passed
              else f"{len(mismatches)} mismatches: " + "; ".join(mismatches[:3])[:300])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_6_captured_at_date(ctx):
    """All rows have Captured At = 2025-05-15."""
    if not _rows_clean or not ctx:
        check("6. All rows have Captured At = 2025-05-15", 1, False, "no rows")
        return
    try:
        fields = ctx.get("fields", {})
        wrong = 0
        for row in _rows_clean:
            cap_val = _scalar(row, "Captured At", fields)
            date_str = str(cap_val) if cap_val else ""
            if AUDIT_DATE not in date_str:
                wrong += 1
        passed = wrong == 0 and len(_rows_clean) > 0
        check("6. All rows have Captured At = 2025-05-15", 1, passed,
              f"all {len(_rows_clean)} rows OK" if passed else f"{wrong} rows with wrong date")
    except Exception as e:
        check("6. All rows have Captured At = 2025-05-15", 1, False, f"exception: {e}")


def check_7_row_ordering(ctx):
    """Rows ordered by Project ascending, then File Path ascending."""
    if not _rows_clean or not ctx:
        check("7. Rows ordered by Project asc, File Path asc", 2, False, "no rows")
        return
    try:
        fields = ctx.get("fields", {})
        pairs = []
        for row in _rows_clean:
            proj = str(_scalar(row, "Project", fields) or "")
            fp = str(_scalar(row, "File Path", fields) or "")
            pairs.append((proj, fp))
        sorted_pairs = sorted(pairs, key=lambda x: (x[0], x[1]))
        passed = pairs == sorted_pairs and len(pairs) > 0
        check("7. Rows ordered by Project asc, File Path asc", 2, passed,
              f"{len(pairs)} rows in correct order" if passed
              else "order mismatch at first diff")
    except Exception as e:
        check("7. Rows ordered by Project asc, File Path asc", 2, False, f"exception: {e}")


def check_8_top_offenders_view(ctx):
    """'Top Offenders' Grid view: filter semantics = Complexity Band IN
    (High, Critical) (both legal encodings accepted) and exactly one sort:
    Lines Of Code DESC."""
    label = "8. 'Top Offenders' grid view filter/sort exact"
    if not ctx or "table_id" not in ctx:
        check(label, 2, False, "no table context")
        return
    try:
        token = ctx["token"]
        table_id = ctx["table_id"]
        fields = ctx.get("fields", {})
        views = baserow_get(f"/database/views/table/{table_id}/", token)
        target = None
        for v in views:
            if v.get("name") == "Top Offenders":
                target = v
                break
        if not target:
            check(label, 2, False, "view not found")
            return
        is_grid = target.get("type") == "grid"
        view_id = target["id"]
        filters_data = baserow_get(f"/database/views/{view_id}/filters/", token)
        sorts_data = baserow_get(f"/database/views/{view_id}/sortings/", token)
        if not isinstance(filters_data, list):
            filters_data = []
        if not isinstance(sorts_data, list):
            sorts_data = []

        band_field = fields.get("Complexity Band") or {}
        loc_field = fields.get("Lines Of Code") or {}
        band_id = band_field.get("id")
        loc_id = loc_field.get("id")
        opt_ids = {o.get("value"): o.get("id") for o in (band_field.get("select_options") or [])}
        want_ids = {opt_ids.get("High"), opt_ids.get("Critical")}
        want_known = None not in want_ids

        def _val_ids(raw):
            out = set()
            for part in str(raw).split(","):
                part = part.strip()
                if part.isdigit():
                    out.add(int(part))
            return out

        filter_ok = False
        filter_why = f"{len(filters_data)} filters"
        if not want_known or band_id is None:
            filter_why = "High/Critical option ids unresolved"
        elif len(filters_data) == 1:
            f = filters_data[0]
            filter_ok = (f.get("field") == band_id
                         and f.get("type") == "single_select_is_any_of"
                         and _val_ids(f.get("value")) == want_ids)
            filter_why = f"single filter field={f.get('field')} type={f.get('type')} value={f.get('value')}"
        elif len(filters_data) == 2:
            vals = set()
            shape_ok = True
            for f in filters_data:
                if f.get("field") != band_id or f.get("type") != "single_select_equal":
                    shape_ok = False
                vals |= _val_ids(f.get("value"))
            filter_ok = (shape_ok and vals == want_ids
                         and target.get("filter_type") == "OR")
            filter_why = (f"2 filters shape_ok={shape_ok} values={sorted(vals)} "
                          f"filter_type={target.get('filter_type')}")

        sort_ok = (len(sorts_data) == 1
                   and sorts_data[0].get("field") == loc_id
                   and sorts_data[0].get("order") == "DESC")
        sort_why = (f"{len(sorts_data)} sorts" if len(sorts_data) != 1 else
                    f"sort field={sorts_data[0].get('field')} order={sorts_data[0].get('order')}")

        passed = is_grid and filter_ok and sort_ok
        check(label, 2, passed,
              f"grid={is_grid}; filter: {filter_why}; sort: {sort_why}"[:350])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_by_band_gallery_view(ctx):
    """'By Band' Gallery view exists."""
    if not ctx or "table_id" not in ctx:
        check("9. 'By Band' Gallery view exists", 1, False, "no table context")
        return
    try:
        token = ctx["token"]
        table_id = ctx["table_id"]
        views = baserow_get(f"/database/views/table/{table_id}/", token)
        target = None
        for v in views:
            if v.get("name") == "By Band":
                target = v
                break
        if not target:
            check("9. 'By Band' Gallery view exists", 1, False, "view not found")
            return
        is_gallery = target.get("type") == "gallery"
        check("9. 'By Band' Gallery view exists", 1, is_gallery,
              f"type={target.get('type')}")
    except Exception as e:
        check("9. 'By Band' Gallery view exists", 1, False, f"exception: {e}")


def check_10_audit_file_exists():
    """File devops-configs/docs/complexity-audit-2025-05-15.md exists with
    exactly five lines."""
    label = "10. Audit markdown file exists with exactly 5 lines"
    try:
        rc, out, err = docker_exec(
            CODE_SERVER_CONTAINER, "cat",
            "/home/coder/workspace/devops-configs/docs/complexity-audit-2025-05-15.md",
            timeout=10,
        )
        if rc != 0:
            check(label, 1, False, f"file not found (rc={rc})")
            return []
        lines = [l.rstrip("\r") for l in out.rstrip("\n").split("\n")] if out.strip() else []
        passed = len(lines) == 5
        check(label, 1, passed, f"{len(lines)} lines (need exactly 5)")
        return lines
    except Exception as e:
        check(label, 1, False, f"exception: {e}")
        return []


def check_11_audit_file_header(lines):
    """Lines 1-2 exactly: heading with em-dash and sorted project list."""
    label = "11. Audit file lines 1-2 exact"
    if len(lines) < 2:
        check(label, 2, False, "fewer than 2 lines")
        return
    try:
        exp1 = "# Complexity Audit — 2025-05-15"
        exp2 = "Projects scanned: blog-engine, data-analyzer, todo-api"
        line1_ok = lines[0].strip() == exp1
        line2_ok = lines[1].strip() == exp2
        passed = line1_ok and line2_ok
        check(label, 2, passed,
              "lines 1-2 exact" if passed else
              f"line1_ok={line1_ok} ('{lines[0][:50]}'), line2_ok={line2_ok} ('{lines[1][:60]}')")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_audit_file_body(lines):
    """Lines 3-5 exactly equal the truth-recomputed totals, band counts and
    top file (consistent with the table's convention)."""
    label = "12. Audit file lines 3-5 match recomputed truth"
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return
    if len(lines) < 5:
        check(label, 2, False, f"only {len(lines)} lines, need 5")
        return
    try:
        named = _TRUTH["named"]
        empties = _TRUTH["empties"]
        counts = truth_band_counts(_TRUTH)
        n_named = len(named)
        n_all = n_named + len(empties)

        def expected_34(n, low_extra):
            line3 = f"Total files measured: {n}"
            line4 = (f"Critical: {counts['Critical']}; High: {counts['High']}; "
                     f"Medium: {counts['Medium']}; Low: {counts['Low'] + low_extra}")
            return line3, line4

        top = sorted(named, key=lambda e: (-e["loc"], e["path"], e["project"]))[0]
        exp5 = f"Top file: {top['path']} ({top['project']}, {top['loc']} LOC)"

        got3, got4, got5 = lines[2].strip(), lines[3].strip(), lines[4].strip()

        if _caliber == "named":
            candidates = [expected_34(n_named, 0)]
        elif _caliber == "all":
            candidates = [expected_34(n_all, len(empties))]
        else:
            # table convention undetermined (ck3 failed): accept either consistent pair
            candidates = [expected_34(n_named, 0)]
            if empties:
                candidates.append(expected_34(n_all, len(empties)))

        pair_ok = any(got3 == c3 and got4 == c4 for c3, c4 in candidates)
        line5_ok = got5 == exp5
        passed = pair_ok and line5_ok
        check(label, 2, passed,
              f"lines 3-5 match truth ({_caliber or 'either'} convention)" if passed else
              f"lines3-4_ok={pair_ok} (expected e.g. '{candidates[0][0]}' / '{candidates[0][1]}', "
              f"got '{got3[:40]}' / '{got4[:60]}'), line5_ok={line5_ok} (expected '{exp5}', got '{got5[:60]}')")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def _fetch_refactor_wps():
    """All Task-type WPs in 'security-audit' with subject LIKE 'Refactor:%',
    per-row split via unit/record separators. Returns list of dicts or None."""
    proj_row = op_db_query(
        "SELECT id FROM projects WHERE identifier = 'security-audit' LIMIT 1;"
    )
    if not proj_row:
        return None
    project_id = int(proj_row.strip())
    type_row = op_db_query("SELECT id FROM types WHERE name = 'Task' LIMIT 1;")
    task_type_id = int(type_row.strip()) if type_row.strip() else None
    type_filter = f" AND type_id = {task_type_id}" if task_type_id else ""
    rows = op_db_query_rows(
        f"SELECT wp.id, wp.subject, wp.description, "
        f"u.login AS assignee_login, "
        f"s.name AS status_name, "
        f"e.name AS priority_name "
        f"FROM work_packages wp "
        f"LEFT JOIN users u ON wp.assigned_to_id = u.id "
        f"LEFT JOIN statuses s ON wp.status_id = s.id "
        f"LEFT JOIN enumerations e ON wp.priority_id = e.id "
        f"WHERE wp.project_id = {project_id}{type_filter} "
        f"AND wp.subject LIKE 'Refactor:%' "
        f"ORDER BY wp.id;"
    )
    if rows is None:
        return None
    wps = []
    for parts in rows:
        if len(parts) >= 6:
            wps.append({
                "id": parts[0].strip(),
                "subject": parts[1].strip(),
                "description": parts[2].strip(),
                "assignee": parts[3].strip(),
                "status": parts[4].strip(),
                "priority": parts[5].strip(),
            })
    return wps


def check_13_op_work_packages_exact_set():
    """OpenProject: the set of 'Refactor:' Task WPs in 'security-audit' is
    EXACTLY the truth-derived top-10 (High/Critical rows by LOC desc then
    File Path asc); one more or one fewer fails."""
    label = "13. 'Refactor:' WP set exactly matches truth top-10"
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return []
    try:
        wps = _fetch_refactor_wps()
        if wps is None:
            check(label, 2, False, "OpenProject query failed (project/psql)")
            return []
        expected = [wp_subject_for(e) for e in truth_top10(_TRUTH)]
        subjects = [wp["subject"] for wp in wps]
        missing = sorted(set(expected) - set(subjects))
        extra = sorted(set(subjects) - set(expected))
        dupes = len(subjects) != len(set(subjects))
        passed = (len(wps) == len(expected)
                  and not missing and not extra and not dupes)
        check(label, 2, passed,
              f"{len(wps)} WPs, exact match with truth top-10" if passed else
              f"{len(wps)} WPs (expected {len(expected)}); missing={missing[:2]}; "
              f"extra={extra[:2]}; duplicates={dupes}")
        return wps
    except Exception as e:
        check(label, 2, False, f"exception: {e}")
        return []


def check_14_wp_subject_format(wps):
    """Each WP subject exactly equals a truth-derived
    'Refactor: <path> (<LOC> LOC, <avg %.1f> avg fn length)' string, each
    expected subject appearing exactly once."""
    label = "14. WP subjects exactly equal truth-derived strings"
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return
    if not wps:
        check(label, 2, False, "no work packages")
        return
    try:
        expected = [wp_subject_for(e) for e in truth_top10(_TRUTH)]
        actual = [wp["subject"] for wp in wps]
        exp_counts = {}
        for s in expected:
            exp_counts[s] = exp_counts.get(s, 0) + 1
        act_counts = {}
        for s in actual:
            act_counts[s] = act_counts.get(s, 0) + 1
        passed = exp_counts == act_counts
        bad = [s[:70] for s in actual if s not in exp_counts][:2]
        check(label, 2, passed,
              f"all {len(actual)} subjects exact" if passed else
              f"subject multiset mismatch; unexpected sample={bad}")
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_15_wp_assignee_admin(wps):
    """All work packages assigned to admin."""
    if not wps:
        check("15. Work packages assigned to admin", 1, False, "no work packages")
        return
    try:
        admin_count = sum(1 for wp in wps if wp.get("assignee") == "admin")
        passed = admin_count == len(wps)
        check("15. Work packages assigned to admin", 1, passed,
              f"{admin_count}/{len(wps)} assigned to admin")
    except Exception as e:
        check("15. Work packages assigned to admin", 1, False, f"exception: {e}")


def check_16_wp_priority_mapping(wps):
    """Priority derived from the TRUTH band of each WP's file (Critical file
    -> High priority, High file -> Normal) — not from the WP's own
    description."""
    label = "16. WP priority matches truth-band mapping"
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return
    if not wps:
        check(label, 2, False, "no work packages")
        return
    try:
        band_by_subject = {wp_subject_for(e): e["band"] for e in truth_top10(_TRUTH)}
        mismatches = []
        for wp in wps:
            band = band_by_subject.get(wp["subject"])
            if band is None:
                mismatches.append(f"'{wp['subject'][:50]}': subject not in truth top-10")
                continue
            expected_priority = "High" if band == "Critical" else "Normal"
            if wp.get("priority", "") != expected_priority:
                mismatches.append(
                    f"'{wp['subject'][:50]}': truth band={band}, "
                    f"priority={wp.get('priority')}, expected={expected_priority}")
        passed = len(mismatches) == 0 and len(wps) > 0
        check(label, 2, passed,
              f"all {len(wps)} correct" if passed
              else f"{len(mismatches)} issues: " + "; ".join(mismatches[:2])[:300])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_17_wp_description_exact(wps):
    """Description exactly equals the truth-derived
    'Project: <P>; Function Count: <FC>; Band: <B>; Audit: 2025-05-15'
    (compared after stripping CKEditor backslash escapes)."""
    label = "17. WP descriptions exactly match truth"
    if _TRUTH is None:
        check(label, 2, False, "ground truth recompute failed")
        return
    if not wps:
        check(label, 2, False, "no work packages")
        return
    try:
        desc_by_subject = {
            wp_subject_for(e):
                f"Project: {e['project']}; Function Count: {e['fn']}; "
                f"Band: {e['band']}; Audit: {AUDIT_DATE}"
            for e in truth_top10(_TRUTH)
        }
        mismatches = []
        for wp in wps:
            expected = desc_by_subject.get(wp["subject"])
            if expected is None:
                mismatches.append(f"'{wp['subject'][:50]}': subject not in truth top-10")
                continue
            actual = wp.get("description", "").replace("\\", "").strip()
            if actual != expected:
                mismatches.append(
                    f"'{wp['subject'][:40]}': desc '{actual[:60]}' != '{expected[:60]}'")
        passed = len(mismatches) == 0 and len(wps) > 0
        check(label, 2, passed,
              f"all {len(wps)} descriptions exact" if passed
              else f"{len(mismatches)} issues: " + "; ".join(mismatches[:2])[:300])
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _TRUTH
    # Ground truth precheck (0pt): re-run the three task awk commands.
    try:
        _TRUTH = compute_truth()
    except Exception:
        _TRUTH = None
    if _TRUTH is not None:
        n_named = len(_TRUTH["named"])
        n_all = n_named + len(_TRUTH["empties"])
        detail = (f"{n_named} named files (+{len(_TRUTH['empties'])} empty-path rows = {n_all}); "
                  f"bands={truth_band_counts(_TRUTH)}")
    else:
        detail = "awk recompute in pristine-image container failed; all truth-dependent checks will FAIL"
    check("0. Ground truth recompute (3 awk commands)", 0, _TRUTH is not None, detail)

    # Baserow checks
    ctx = check_1_baserow_database_exists()
    ctx = check_2_table_and_fields(ctx)
    check_3_rows_match_truth(ctx)
    check_4_metric_ids_sequential(ctx)
    check_5_complexity_band_correct(ctx)
    check_6_captured_at_date(ctx)
    check_7_row_ordering(ctx)
    check_8_top_offenders_view(ctx)
    check_9_by_band_gallery_view(ctx)

    # code-server checks
    lines = check_10_audit_file_exists()
    check_11_audit_file_header(lines)
    check_12_audit_file_body(lines)

    # OpenProject checks
    wps = check_13_op_work_packages_exact_set()
    check_14_wp_subject_format(wps)
    check_15_wp_assignee_admin(wps)
    check_16_wp_priority_mapping(wps)
    check_17_wp_description_exact(wps)

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
