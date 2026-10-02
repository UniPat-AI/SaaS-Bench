"""
Verifier for Software-042-I2: Branching strategy governance across repos

Checks: 12 weighted checks across code-server, baserow, openproject.
Strategy: docker exec (filesystem + git recompute + DB queries)

Ground-truth policy: the verifier RECOMPUTES each repo's pre-creation branch
("Current Branch") from git itself and never trusts agent-filled Baserow data.
If the git truth recompute fails for a repo, every truth-dependent check FAILs
for that repo — there is no fallback to agent data.

Required env vars:
  SERVER_HOSTNAME,
  CODE_SERVER_PORT, CODE_SERVER_CONTAINER,
  BASEROW_PORT, BASEROW_CONTAINER, BASEROW_DB_CONTAINER,
  OPENPROJECT_PORT, OPENPROJECT_CONTAINER
"""

import os
import re
import subprocess
import sys

# ── Config (from env) ─────────────────────────────────────────────────────────
HOST = os.getenv("SERVER_HOSTNAME", "localhost")

CODE_SERVER_CONTAINER = os.environ.get("CODE_SERVER_CONTAINER")
BASEROW_DB_CONTAINER = os.environ.get("BASEROW_DB_CONTAINER")
OPENPROJECT_CONTAINER = os.environ.get("OPENPROJECT_CONTAINER")

for var_name, val in [
    ("CODE_SERVER_CONTAINER", CODE_SERVER_CONTAINER),
    ("BASEROW_DB_CONTAINER", BASEROW_DB_CONTAINER),
    ("OPENPROJECT_CONTAINER", OPENPROJECT_CONTAINER),
]:
    if not val:
        print(f"FATAL: {var_name} not set", file=sys.stderr)
        sys.exit(1)

# ── Constants ─────────────────────────────────────────────────────────────────
POLICY_DATE = "2026-05-15"
POLICY_OWNER = "OpenProject Admin"
BRANCH_PREFIXES = ["feature", "bugfix", "release", "docs"]
PREFIX_DESCRIPTIONS = {
    "feature": "Long-lived feature work integrated via PR",
    "bugfix": "Non-urgent defect remediation branches",
    "release": "Release stabilization branches cut from main",
    "docs": "Documentation-only changes",
}
PROJECT_LIST = ["vue-hackernews-2.0", "json", "tabler"]
ALL_PROJECTS_ALPHA = sorted(PROJECT_LIST + ["devops-configs"])  # alphabetical
NEW_BRANCH = "docs/branching-policy-rollout"
COMMIT_MSG = "docs: formalize branching strategy 2026-05-15"
POLICY_FILE_RELPATH = "docs/BRANCHING_STRATEGY.md"
BASEROW_DB_NAME = "Branch Strategy Governance Hub"
TABLE_NAME = "Repo Branch Audit"
# Plan risk ③: the historical constant 'your-scrum-project' is suspect (the task
# description says OpenProject project "scrum-project"). We probe the live
# `projects.identifier` list at runtime and accept whichever of these two
# identifiers actually exists (tolerant dual lookup), instead of trusting the
# constant blindly.
OP_PROJECT_CANDIDATES = ["your-scrum-project", "scrum-project"]

EXPECTED_FILE_CONTENT = (
    "# Branching Strategy\n"
    f"Effective Date: {POLICY_DATE}\n"
    f"Owner: {POLICY_OWNER}\n"
    "\n"
    "## Allowed Branch Prefixes\n"
)
for prefix in BRANCH_PREFIXES:
    EXPECTED_FILE_CONTENT += f"- {prefix}/: {PREFIX_DESCRIPTIONS[prefix]}\n"

BASE_PATHS = ["/home/coder/workspace", "/home/coder/project", "/home/coder", "/config/workspace"]

# ── Result accumulator ────────────────────────────────────────────────────────
_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    # Detail strings must be single-line (verify protocol regex).
    detail = " ".join(str(detail).split())
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


def baserow_sql(query: str, timeout: int = 15) -> str:
    """Run a psql query against the Baserow DB and return stdout."""
    rc, out, err = docker_exec(
        BASEROW_DB_CONTAINER,
        "env", "PGPASSWORD=kdpzkuyhsgb22onku8y7rxkx3czej88nxpngaz4mlmgad67vpc", "psql", "-h", "127.0.0.1", "-U", "baserow", "-d", "baserow", "-t", "-A", "-c", query,
        timeout=timeout,
    )
    return out.strip()


def openproject_sql(query: str, timeout: int = 15) -> str:
    """Run a psql query against the OpenProject DB (embedded) and return stdout."""
    r = subprocess.run(
        ["docker", "exec", "-e", "PGPASSWORD=openproject", OPENPROJECT_CONTAINER,
         "env", "PGPASSWORD=openproject", "psql", "-h", "127.0.0.1", "-U", "openproject", "-d", "openproject", "-h", "127.0.0.1",
         "-t", "-A", "-c", query],
        capture_output=True, text=True, errors="replace", timeout=timeout,
    )
    return r.stdout.strip()


# ── Git ground-truth recompute ────────────────────────────────────────────────
_REPO_DIR_CACHE: dict = {}


def find_repo_dir(proj: str) -> str | None:
    """Locate the git repo dir for a project inside the code-server container."""
    if proj in _REPO_DIR_CACHE:
        return _REPO_DIR_CACHE[proj]
    for base in BASE_PATHS:
        d = f"{base}/{proj}"
        rc, _, _ = docker_exec(
            CODE_SERVER_CONTAINER, "git", "-C", d, "-c", f"safe.directory={d}",
            "rev-parse", "--git-dir",
        )
        if rc == 0:
            _REPO_DIR_CACHE[proj] = d
            return d
    # Fallback: find
    rc, found, _ = docker_exec(
        CODE_SERVER_CONTAINER, "find", "/home", "-maxdepth", "4",
        "-path", f"*/{proj}/.git", "-type", "d", timeout=30,
    )
    if rc == 0 and found.strip():
        d = found.strip().split("\n")[0].rsplit("/.git", 1)[0]
        _REPO_DIR_CACHE[proj] = d
        return d
    _REPO_DIR_CACHE[proj] = None
    return None


def git_cmd(repo_dir: str, *args: str, timeout: int = 20) -> tuple[int, str, str]:
    """Run git in the container with safe.directory set (exec user may differ)."""
    return docker_exec(
        CODE_SERVER_CONTAINER,
        "git", "-C", repo_dir, "-c", f"safe.directory={repo_dir}", *args,
        timeout=timeout,
    )


def _expected_prefix(branch: str) -> str | None:
    """First allowed prefix that `branch` starts with (prefix + '/'), else None."""
    for p in BRANCH_PREFIXES:
        if branch.startswith(p + "/"):
            return p
    return None


def _compute_git_truth() -> dict:
    """
    Recompute per-repo ground truth from git itself:
      orig            -- the pre-creation branch = the unique local branch other
                         than NEW_BRANCH (`git for-each-ref refs/heads`);
                         if >1 candidate, second evidence from `git reflog`
                         (the CREATION entry, i.e. the chronologically earliest
                         'checkout: moving from X to <NEW_BRANCH>') must agree
                         (X must be one of the candidates), else the repo FAILs.
      new_sha/orig_sha -- rev-parse of NEW_BRANCH / orig (base-point assertion).
      upstream_absent  -- rev-parse NEW_BRANCH@{upstream} must FAIL (not pushed).
      error            -- non-None means truth recompute failed for this repo;
                         all truth-dependent checks must FAIL for it.
    """
    truth: dict = {}
    for proj in ALL_PROJECTS_ALPHA:
        entry = {"dir": None, "orig": None, "new_sha": None, "orig_sha": None,
                 "upstream_absent": None, "error": None}
        truth[proj] = entry
        d = find_repo_dir(proj)
        if not d:
            entry["error"] = "repo dir not found in container"
            continue
        entry["dir"] = d

        rc, out, err = git_cmd(d, "for-each-ref", "refs/heads", "--format=%(refname:short)")
        if rc != 0:
            entry["error"] = f"for-each-ref failed: {err.strip()[:120]}"
            continue
        branches = [l.strip() for l in out.strip().split("\n") if l.strip()]
        candidates = [b for b in branches if b != NEW_BRANCH]

        if len(candidates) == 1:
            entry["orig"] = candidates[0]
        elif len(candidates) > 1:
            # Second evidence: HEAD reflog creation entry for the new branch.
            rc2, out2, _ = git_cmd(d, "reflog")
            evid = None
            if rc2 == 0:
                pat = re.compile(r"checkout: moving from (\S+) to " + re.escape(NEW_BRANCH) + r"$")
                matches = [m.group(1) for l in out2.splitlines() if (m := pat.search(l))]
                if matches:
                    evid = matches[-1]  # reflog is newest-first; last match = branch creation
            if evid is not None and evid in candidates:
                entry["orig"] = evid
            else:
                entry["error"] = (
                    f"ambiguous pre-creation branch: candidates={candidates}, "
                    f"reflog evidence={evid!r} (must be exactly one local branch besides "
                    f"{NEW_BRANCH}, or reflog creation entry must agree)"
                )
                continue
        else:
            entry["error"] = f"no candidate pre-creation branch (local branches: {branches})"
            continue

        rc3, out3, _ = git_cmd(d, "rev-parse", "--verify", f"refs/heads/{entry['orig']}")
        if rc3 == 0:
            entry["orig_sha"] = out3.strip()
        rc4, out4, _ = git_cmd(d, "rev-parse", "--verify", f"refs/heads/{NEW_BRANCH}")
        if rc4 == 0:
            entry["new_sha"] = out4.strip()
            # Not pushed: no upstream may be configured for the new branch.
            rc5, _, _ = git_cmd(d, "rev-parse", "--symbolic-full-name", f"{NEW_BRANCH}@{{upstream}}")
            entry["upstream_absent"] = (rc5 != 0)
    return truth


GIT_TRUTH: dict = {}


def _truth_nc() -> int | None:
    """Non-compliant count derived from git truth (None if any repo's truth failed)."""
    for proj in ALL_PROJECTS_ALPHA:
        e = GIT_TRUTH.get(proj, {})
        if e.get("error") or not e.get("orig"):
            return None
    return sum(1 for p in ALL_PROJECTS_ALPHA if _expected_prefix(GIT_TRUTH[p]["orig"]) is None)


# ── OpenProject project lookup (tolerant, runtime-probed) ─────────────────────
_OP_IDENT_CACHE: list | None = None


def _op_project_identifiers() -> list[str]:
    global _OP_IDENT_CACHE
    if _OP_IDENT_CACHE is None:
        out = openproject_sql("SELECT identifier FROM projects;")
        _OP_IDENT_CACHE = [l.strip() for l in out.split("\n") if l.strip()]
    return _OP_IDENT_CACHE


def _op_candidate_identifiers() -> list[str]:
    idents = _op_project_identifiers()
    return [i for i in OP_PROJECT_CANDIDATES if i in idents]


# ── Individual checks ─────────────────────────────────────────────────────────

def check_1_branching_strategy_file() -> None:
    """Verify devops-configs/docs/BRANCHING_STRATEGY.md exists with exact content."""
    try:
        # Try common code-server workspace paths
        for base in ["/home/coder/project", "/home/coder", "/config/workspace", "/home/coder/workspace"]:
            path = f"{base}/devops-configs/docs/BRANCHING_STRATEGY.md"
            rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", path)
            if rc == 0:
                break
        if rc != 0:
            # Try find
            rc2, found, _ = docker_exec(CODE_SERVER_CONTAINER, "find", "/", "-path",
                                         "*/devops-configs/docs/BRANCHING_STRATEGY.md",
                                         "-type", "f", timeout=30)
            if rc2 == 0 and found.strip():
                first = found.strip().split("\n")[0]
                rc, out, err = docker_exec(CODE_SERVER_CONTAINER, "cat", first)

        if rc != 0:
            check("1. BRANCHING_STRATEGY.md exists with correct content", 2, False,
                  "file not found in container")
            return

        actual = out
        # Normalize: ensure trailing newline for comparison
        expected = EXPECTED_FILE_CONTENT
        if not actual.endswith("\n"):
            actual += "\n"

        passed = actual == expected
        detail = "" if passed else f"content mismatch; got {len(actual)} chars"
        check("1. BRANCHING_STRATEGY.md exists with correct content", 2, passed, detail)
    except Exception as e:
        check("1. BRANCHING_STRATEGY.md exists with correct content", 2, False, f"exception: {e}")


def check_2_commit_message() -> None:
    """Verify commit with exact message exists in devops-configs, touches exactly
    docs/BRANCHING_STRATEGY.md, and sits on the original (pre-creation) branch."""
    label = "2. Commit: exact msg, only BRANCHING_STRATEGY.md, on original branch"
    try:
        d = find_repo_dir("devops-configs")
        if not d:
            check(label, 2, False, "could not access devops-configs git repo")
            return

        rc, out, err = git_cmd(d, "log", "--all", "--format=%H%x00%s")
        if rc != 0:
            check(label, 2, False, f"git log failed: {err.strip()[:120]}")
            return
        shas = []
        for line in out.strip().split("\n"):
            if "\x00" not in line:
                continue
            sha, subj = line.split("\x00", 1)
            if subj.strip() == COMMIT_MSG:  # exact subject match
                shas.append(sha.strip())
        if not shas:
            check(label, 2, False, "no commit with exact message found")
            return

        truth = GIT_TRUTH.get("devops-configs", {})
        orig = truth.get("orig")
        if truth.get("error") or not orig:
            # Truth recompute failed -> truth-dependent check FAILs (no fallback).
            check(label, 2, False, f"git truth recompute failed: {truth.get('error')}")
            return

        issues = []
        passed = False
        for sha in shas:
            sha_issues = []
            # (a) commit touches exactly one file: docs/BRANCHING_STRATEGY.md
            rc_a, out_a, err_a = git_cmd(d, "show", "--name-only", "--format=", sha)
            files = [l.strip() for l in out_a.strip().split("\n") if l.strip()] if rc_a == 0 else []
            if rc_a != 0:
                sha_issues.append(f"{sha[:8]}: git show failed")
            elif files != [POLICY_FILE_RELPATH]:
                sha_issues.append(f"{sha[:8]}: touched files {files}, expected exactly ['{POLICY_FILE_RELPATH}']")
            # (b) commit is on the original branch (recomputed: {orig})
            rc_b, _, _ = git_cmd(d, "merge-base", "--is-ancestor", sha, orig)
            if rc_b != 0:
                sha_issues.append(f"{sha[:8]}: not an ancestor of original branch {orig!r}")
            if not sha_issues:
                passed = True
                break
            issues.extend(sha_issues)

        detail = "" if passed else "; ".join(issues[:4])
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_3_branch_in_all_projects() -> None:
    """Verify branch docs/branching-policy-rollout exists in all 4 projects,
    points at the original branch tip (created from HEAD, no extra commits),
    and has no upstream (not pushed)."""
    label = "3. Branch in all 4 projects at original-branch tip, not pushed"
    try:
        issues = []
        for proj in ALL_PROJECTS_ALPHA:
            t = GIT_TRUTH.get(proj, {})
            if t.get("error"):
                issues.append(f"{proj}: git truth recompute failed ({t['error']})")
                continue
            if not t.get("new_sha"):
                issues.append(f"{proj}: branch {NEW_BRANCH} not found")
                continue
            if not t.get("orig_sha"):
                issues.append(f"{proj}: could not resolve original branch {t.get('orig')!r}")
                continue
            if t["new_sha"] != t["orig_sha"]:
                msg = (f"{proj}: {NEW_BRANCH} ({t['new_sha'][:8]}) != "
                       f"{t['orig']} tip ({t['orig_sha'][:8]})")
                if proj == "devops-configs":
                    # A mismatch here usually means the policy commit was made ON the
                    # new branch instead of on master before branching — a real
                    # non-compliance with the task's step order (commit, then branch).
                    msg += " -- policy commit likely made on the new branch (step-order non-compliance)"
                issues.append(msg)
            if t.get("upstream_absent") is False:
                issues.append(f"{proj}: {NEW_BRANCH} has an upstream configured (branch was pushed)")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues[:4])
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_4_baserow_database_exists() -> None:
    """Verify Baserow database 'Branch Strategy Governance Hub' exists."""
    try:
        result = baserow_sql(
            f"SELECT id FROM database_database da "
            f"JOIN core_application ca ON ca.id = da.application_ptr_id "
            f"WHERE ca.name = '{BASEROW_DB_NAME}';"
        )
        if not result:
            # Try alternate schema
            result = baserow_sql(
                f"SELECT id FROM core_application WHERE name = '{BASEROW_DB_NAME}';"
            )
        passed = bool(result and result.strip())
        detail = "" if passed else "database not found"
        check("4. Baserow database exists", 1, passed, detail)
    except Exception as e:
        check("4. Baserow database exists", 1, False, f"exception: {e}")


def _get_baserow_table_id() -> str | None:
    """Get the Baserow table ID for 'Repo Branch Audit'."""
    # Try joining with core_application
    result = baserow_sql(
        f"SELECT dt.id FROM database_table dt "
        f"JOIN core_application ca ON ca.id = dt.database_id "
        f"WHERE dt.name = '{TABLE_NAME}' AND ca.name = '{BASEROW_DB_NAME}';"
    )
    if result and result.strip():
        return result.strip().split("\n")[0]
    # Fallback: just find by table name
    result = baserow_sql(
        f"SELECT id FROM database_table WHERE name = '{TABLE_NAME}';"
    )
    if result and result.strip():
        return result.strip().split("\n")[0]
    return None


def _get_field_map(table_id: str) -> dict:
    """Return {field_name: (field_id, field_type_model, is_primary)} for the table."""
    result = baserow_sql(
        f"SELECT f.id, f.name, ct.model, f.\"primary\" "
        f"FROM database_field f "
        f"JOIN django_content_type ct ON ct.id = f.content_type_id "
        f"WHERE f.table_id = {table_id} AND f.trashed = false "
        f"ORDER BY f.order;"
    )
    fields = {}
    for line in result.strip().split("\n"):
        if not line.strip():
            continue
        parts = line.split("|")
        if len(parts) >= 4:
            fid, fname, ftype, fprim = (parts[0].strip(), parts[1].strip(),
                                        parts[2].strip(), parts[3].strip())
            fields[fname] = (fid, ftype, fprim.lower() in ("t", "true", "1"))
    return fields


def _get_select_options(field_id: str) -> list[str]:
    result = baserow_sql(
        f"SELECT value FROM database_selectoption WHERE field_id = {field_id};"
    )
    return [l.strip() for l in result.split("\n") if l.strip()]


def check_5_baserow_table_fields() -> None:
    """Verify table 'Repo Branch Audit' exists with expected fields (names)."""
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check("5. Table Repo Branch Audit with correct fields", 1, False, "table not found")
            return

        field_map = _get_field_map(table_id)
        expected_fields = ["Repo ID", "Project", "Current Branch", "New Branch Created",
                           "Prefix Used", "Compliant", "Audited At"]
        missing = [f for f in expected_fields if f not in field_map]
        passed = len(missing) == 0
        detail = "" if passed else f"missing fields: {missing}; found: {list(field_map.keys())}"
        check("5. Table Repo Branch Audit with correct fields", 1, passed, detail)
    except Exception as e:
        check("5. Table Repo Branch Audit with correct fields", 1, False, f"exception: {e}")


def check_6_baserow_rows_correct_projects() -> None:
    """Verify exactly 4 rows, Projects alphabetical, and Repo ID sequence
    RB-01..RB-04 by row order (verifier-generated expectation)."""
    label = "6. Exactly 4 rows, Projects alphabetical, Repo ID RB-01..RB-04"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return

        field_map = _get_field_map(table_id)
        missing = [f for f in ("Project", "Repo ID") if f not in field_map]
        if missing:
            check(label, 2, False, f"missing fields: {missing}")
            return

        project_fid = field_map["Project"][0]
        rid_fid = field_map["Repo ID"][0]

        row_count_str = baserow_sql(f"SELECT count(*) FROM database_table_{table_id};")
        row_count = int(row_count_str.strip()) if row_count_str.strip() else 0
        if row_count != 4:
            check(label, 2, False, f"expected 4 rows, got {row_count}")
            return

        # Project is single-select (option id stored in the row); Repo ID is text.
        result = baserow_sql(
            f"SELECT so.value, r.field_{rid_fid} FROM database_table_{table_id} r "
            f"LEFT JOIN database_selectoption so ON so.id = r.field_{project_fid} "
            f"ORDER BY r.\"order\", r.id;"
        )
        projects, repo_ids = [], []
        for line in result.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            projects.append(parts[0].strip())
            repo_ids.append(parts[1].strip() if len(parts) > 1 else "")

        issues = []
        if projects != ALL_PROJECTS_ALPHA:
            issues.append(f"Projects expected {ALL_PROJECTS_ALPHA}, got {projects}")
        # Repo ID sequence generated by the verifier: RB-01..RB-04 by row order.
        expected_ids = [f"RB-{i:02d}" for i in range(1, len(ALL_PROJECTS_ALPHA) + 1)]
        if repo_ids != expected_ids:
            issues.append(f"Repo ID expected {expected_ids}, got {repo_ids}")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues)
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_7_baserow_new_branch_and_date() -> None:
    """Verify New Branch Created / Audited At, and Current Branch reconciled
    against the git-recomputed pre-creation branch per repo."""
    label = "7. New Branch Created, Audited At & Current Branch match git truth"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return

        field_map = _get_field_map(table_id)
        needed = ["Project", "Current Branch", "New Branch Created", "Audited At"]
        missing = [f for f in needed if f not in field_map]
        if missing:
            check(label, 2, False, f"missing fields: {missing}")
            return

        proj_fid = field_map["Project"][0]
        cb_fid = field_map["Current Branch"][0]
        nbc_fid = field_map["New Branch Created"][0]
        aud_fid = field_map["Audited At"][0]

        result = baserow_sql(
            f"SELECT so.value, r.field_{cb_fid}, r.field_{nbc_fid}, r.field_{aud_fid}::text "
            f"FROM database_table_{table_id} r "
            f"LEFT JOIN database_selectoption so ON so.id = r.field_{proj_fid} "
            f"ORDER BY r.\"order\", r.id;"
        )
        issues = []
        seen = set()
        for line in result.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            proj = parts[0].strip() if len(parts) > 0 else ""
            cb_val = parts[1].strip() if len(parts) > 1 else ""
            nbc_val = parts[2].strip() if len(parts) > 2 else ""
            aud_val = parts[3].strip() if len(parts) > 3 else ""

            if nbc_val != NEW_BRANCH:
                issues.append(f"{proj or '?'}: New Branch Created={nbc_val!r}")
            if not aud_val.startswith(POLICY_DATE):
                issues.append(f"{proj or '?'}: Audited At={aud_val!r}")

            # Current Branch reconciliation against git-recomputed truth.
            if proj not in GIT_TRUTH:
                issues.append(f"unknown Project value {proj!r}")
                continue
            seen.add(proj)
            t = GIT_TRUTH[proj]
            if t.get("error") or not t.get("orig"):
                issues.append(f"{proj}: git truth recompute failed ({t.get('error')})")
            elif cb_val != t["orig"]:
                issues.append(f"{proj}: Current Branch={cb_val!r}, git truth={t['orig']!r}")

        for proj in ALL_PROJECTS_ALPHA:
            if proj not in seen:
                issues.append(f"{proj}: no row found")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues[:5])
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_8_baserow_compliant_logic() -> None:
    """Verify Prefix Used / Compliant against expectations derived from the
    GIT-RECOMPUTED pre-creation branch (never from the agent's Current Branch
    text). Truth in this image: all 4 repos non-compliant, Prefix Used empty."""
    label = "8. Prefix Used / Compliant match git-recomputed truth"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 2, False, "table not found")
            return

        field_map = _get_field_map(table_id)
        needed = ["Project", "Prefix Used", "Compliant"]
        missing = [f for f in needed if f not in field_map]
        if missing:
            check(label, 2, False, f"missing fields: {missing}")
            return

        proj_fid = field_map["Project"][0]
        pu_fid = field_map["Prefix Used"][0]
        comp_fid = field_map["Compliant"][0]

        result = baserow_sql(
            f"SELECT so.value, "
            f"(SELECT so2.value FROM database_selectoption so2 WHERE so2.id = r.field_{pu_fid}), "
            f"r.field_{comp_fid} "
            f"FROM database_table_{table_id} r "
            f"LEFT JOIN database_selectoption so ON so.id = r.field_{proj_fid} "
            f"ORDER BY r.\"order\", r.id;"
        )
        issues = []
        for line in result.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("|")
            if len(parts) < 3:
                issues.append(f"unexpected row format: {line}")
                continue
            proj = parts[0].strip()
            prefix_used = parts[1].strip() if parts[1].strip() else None
            compliant_str = parts[2].strip().lower()
            compliant = compliant_str in ("t", "true", "1")

            t = GIT_TRUTH.get(proj)
            if t is None:
                issues.append(f"unknown Project value {proj!r}")
                continue
            if t.get("error") or not t.get("orig"):
                # Truth recompute failed -> this row FAILs (no fallback to agent data).
                issues.append(f"{proj}: git truth recompute failed ({t.get('error')})")
                continue

            expected_prefix = _expected_prefix(t["orig"])
            expected_compliant = expected_prefix is not None

            if prefix_used != expected_prefix:
                issues.append(f"{proj} (branch={t['orig']}): prefix_used={prefix_used!r} expected={expected_prefix!r}")
            if compliant != expected_compliant:
                issues.append(f"{proj} (branch={t['orig']}): compliant={compliant} expected={expected_compliant}")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues[:5])
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_9_baserow_view_filter() -> None:
    """Verify Grid view 'Non-Compliant Repos' filtered to Compliant=false
    (filter type 'boolean' with a false-semantics value)."""
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check("9. Grid view Non-Compliant Repos with Compliant=false filter", 1, False, "table not found")
            return

        # Find the view
        view_result = baserow_sql(
            f"SELECT v.id, ct.model FROM database_view v "
            f"JOIN django_content_type ct ON ct.id = v.content_type_id "
            f"WHERE v.table_id = {table_id} AND v.name = 'Non-Compliant Repos';"
        )
        if not view_result.strip():
            check("9. Grid view Non-Compliant Repos with Compliant=false filter", 1, False, "view not found")
            return

        view_id = view_result.strip().split("|")[0].strip()

        # Check for a Compliant=false filter on the Compliant field
        field_map = _get_field_map(table_id)
        comp_fid = field_map.get("Compliant", (None, None, None))[0]

        filter_result = baserow_sql(
            f"SELECT field_id, type, value FROM database_viewfilter WHERE view_id = {view_id};"
        )

        # Filter must target the Compliant field with type 'boolean' and a
        # false-semantics value ('0' / 'false' / '').
        has_compliant_false_filter = False
        if filter_result.strip():
            for line in filter_result.strip().split("\n"):
                parts = line.split("|")
                if len(parts) >= 3:
                    fid = parts[0].strip()
                    ftype = parts[1].strip().lower()
                    fval = parts[2].strip().lower()
                    if (comp_fid and fid == comp_fid and ftype == "boolean"
                            and fval in ("0", "false", "")):
                        has_compliant_false_filter = True

        passed = has_compliant_false_filter
        detail = "" if passed else (f"no boolean=false filter on Compliant field "
                                    f"(field_id={comp_fid}); filters: {filter_result.strip()}")
        check("9. Grid view Non-Compliant Repos with Compliant=false filter", 1, passed, detail)
    except Exception as e:
        check("9. Grid view Non-Compliant Repos with Compliant=false filter", 1, False, f"exception: {e}")


def check_10_openproject_wiki_page() -> None:
    """Verify wiki page 'Branching Strategy' with exactly the three specified
    lines (normalized line equality, not substrings)."""
    label = "10. OpenProject wiki page Branching Strategy (exact 3 lines)"
    try:
        cands = _op_candidate_identifiers()
        if not cands:
            check(label, 2, False,
                  f"no scrum project found; live identifiers: {_op_project_identifiers()[:10]}")
            return

        result = ""
        for ident in cands:
            # Dual lookup: by title, then by slug.
            result = openproject_sql(
                f"SELECT wp.text FROM wiki_pages wp "
                f"JOIN wikis w ON w.id = wp.wiki_id "
                f"JOIN projects p ON p.id = w.project_id "
                f"WHERE p.identifier = '{ident}' AND wp.title = 'Branching Strategy';"
            )
            if not result.strip():
                result = openproject_sql(
                    f"SELECT wp.text FROM wiki_pages wp "
                    f"JOIN wikis w ON w.id = wp.wiki_id "
                    f"JOIN projects p ON p.id = w.project_id "
                    f"WHERE p.identifier = '{ident}' AND wp.slug = 'branching-strategy';"
                )
            if result.strip():
                break
        if not result.strip():
            check(label, 2, False, f"wiki page not found (projects tried: {cands})")
            return

        # OpenProject's CKEditor escapes markdown punctuation when storing text
        # (e.g. 'BRANCHING\_STRATEGY.md'); strip backslashes before matching.
        body = result.strip().replace("\\", "")
        # Normalized line equality: strip each line, drop empty lines, then the
        # body must be EXACTLY these three lines in order. "Repos audited" count
        # is derived from the verifier's repo list (4 == len(ALL_PROJECTS_ALPHA)),
        # consistent with ck6's exactly-4-rows assertion.
        expected_lines = [
            "Policy document: devops-configs/docs/BRANCHING_STRATEGY.md",
            f"Effective: {POLICY_DATE}",
            f"Repos audited: {len(ALL_PROJECTS_ALPHA)}",
        ]
        actual_lines = [l.strip() for l in body.splitlines()]
        actual_lines = [l for l in actual_lines if l]

        passed = actual_lines == expected_lines
        detail = "" if passed else f"expected {expected_lines}, got {actual_lines[:6]}"
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_11_openproject_work_package() -> None:
    """Verify exactly one Task WP in the scrum project with priority Normal,
    assignee login 'admin', and exact description (NC derived from git truth)."""
    label = "11. OpenProject work package: unique, Task, Normal, admin, exact desc"
    try:
        cands = _op_candidate_identifiers()
        if not cands:
            check(label, 2, False,
                  f"no scrum project found; live identifiers: {_op_project_identifiers()[:10]}")
            return
        in_clause = ", ".join(f"'{i}'" for i in cands)

        # Exactly one matching WP inside the scrum/demo project (per-row split).
        ids_out = openproject_sql(
            f"SELECT wp.id FROM work_packages wp "
            f"JOIN projects p ON p.id = wp.project_id "
            f"WHERE wp.subject = 'Enforce branching policy: {POLICY_DATE}' "
            f"AND p.identifier IN ({in_clause});"
        )
        ids = [l.strip() for l in ids_out.split("\n") if l.strip()]
        if len(ids) != 1:
            check(label, 2, False,
                  f"expected exactly 1 work package in {cands}, found {len(ids)}")
            return
        wp_id = ids[0]

        result = openproject_sql(
            f"SELECT t.name, e.name, u.login, "
            f"regexp_replace(wp.description, E'[\\r\\n]+', ' ', 'g') "
            f"FROM work_packages wp "
            f"JOIN types t ON t.id = wp.type_id "
            f"LEFT JOIN enumerations e ON e.id = wp.priority_id "
            f"LEFT JOIN users u ON u.id = wp.assigned_to_id "
            f"WHERE wp.id = {wp_id};"
        )
        parts = result.strip().split("|")
        if len(parts) < 4:
            check(label, 2, False, f"unexpected row format: {result[:120]!r}")
            return

        type_name = parts[0].strip()
        priority = parts[1].strip()
        login = parts[2].strip()
        desc_raw = "|".join(parts[3:])

        issues = []
        if type_name.lower() != "task":
            issues.append(f"type={type_name!r}, expected Task")
        if priority != "Normal":
            issues.append(f"priority={priority!r}, expected Normal")
        if login != "admin":
            issues.append(f"assignee login={login!r}, expected 'admin'")

        # NC derived from git truth at runtime (never from agent data).
        nc = _truth_nc()
        if nc is None:
            issues.append("git truth recompute failed; cannot derive Non-compliant count")
        else:
            # CKEditor escapes underscores ('\_') in stored descriptions; strip
            # backslashes, then compare the whole (whitespace-normalized) string.
            desc = " ".join(desc_raw.replace("\\", "").split())
            expected_desc = (
                f"Policy: devops-configs/docs/BRANCHING_STRATEGY.md; "
                f"Repos: {len(ALL_PROJECTS_ALPHA)}; Non-compliant: {nc}"
            )
            if desc != expected_desc:
                issues.append(f"description={desc[:120]!r}, expected {expected_desc!r}")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues)
        check(label, 2, passed, detail)
    except Exception as e:
        check(label, 2, False, f"exception: {e}")


def check_12_baserow_field_schema() -> None:
    """Field schema: Repo ID primary text; Project single-select with exactly
    the 4 repo options; Prefix Used single-select with exactly the 4 prefixes;
    Compliant boolean; Audited At date."""
    label = "12. Field schema (types, primary, exact select option sets)"
    try:
        table_id = _get_baserow_table_id()
        if not table_id:
            check(label, 1, False, "table not found")
            return

        field_map = _get_field_map(table_id)
        issues = []

        def _field(name):
            if name not in field_map:
                issues.append(f"missing field {name!r}")
                return None
            return field_map[name]

        # Repo ID: primary text
        f = _field("Repo ID")
        if f:
            if f[1] != "textfield":
                issues.append(f"Repo ID type={f[1]!r}, expected textfield")
            if not f[2]:
                issues.append("Repo ID is not the primary field")

        # Project: single-select, options exactly the 4 repos
        f = _field("Project")
        if f:
            if f[1] != "singleselectfield":
                issues.append(f"Project type={f[1]!r}, expected singleselectfield")
            else:
                opts = set(_get_select_options(f[0]))
                expected = set(ALL_PROJECTS_ALPHA)
                if opts != expected:
                    issues.append(f"Project options={sorted(opts)}, expected {sorted(expected)}")

        # Prefix Used: single-select, options exactly the 4 prefixes
        f = _field("Prefix Used")
        if f:
            if f[1] != "singleselectfield":
                issues.append(f"Prefix Used type={f[1]!r}, expected singleselectfield")
            else:
                opts = set(_get_select_options(f[0]))
                expected = set(BRANCH_PREFIXES)
                if opts != expected:
                    issues.append(f"Prefix Used options={sorted(opts)}, expected {sorted(expected)}")

        # Compliant: boolean
        f = _field("Compliant")
        if f and f[1] != "booleanfield":
            issues.append(f"Compliant type={f[1]!r}, expected booleanfield")

        # Audited At: date
        f = _field("Audited At")
        if f and f[1] != "datefield":
            issues.append(f"Audited At type={f[1]!r}, expected datefield")

        passed = len(issues) == 0
        detail = "" if passed else "; ".join(issues[:6])
        check(label, 1, passed, detail)
    except Exception as e:
        check(label, 1, False, f"exception: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    # Recompute git ground truth once, up front (checks 2/3/7/8/11 depend on it).
    global GIT_TRUTH
    try:
        GIT_TRUTH = _compute_git_truth()
    except Exception as e:
        GIT_TRUTH = {p: {"error": f"truth recompute exception: {e}"} for p in ALL_PROJECTS_ALPHA}
    for proj in ALL_PROJECTS_ALPHA:
        t = GIT_TRUTH.get(proj, {})
        summary = t.get("error") or f"orig={t.get('orig')!r} new_sha={str(t.get('new_sha'))[:8]}"
        print(f"[truth] {proj}: {summary}", file=sys.stderr)

    check_1_branching_strategy_file()
    check_2_commit_message()
    check_3_branch_in_all_projects()
    check_4_baserow_database_exists()
    check_5_baserow_table_fields()
    check_6_baserow_rows_correct_projects()
    check_7_baserow_new_branch_and_date()
    check_8_baserow_compliant_logic()
    check_9_baserow_view_filter()
    check_10_openproject_wiki_page()
    check_11_openproject_work_package()
    check_12_baserow_field_schema()

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
