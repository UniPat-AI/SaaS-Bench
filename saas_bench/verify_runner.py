"""Run each task's verify.py, parse the results, and write them to JSON files.

verify.py output convention (all written to stderr):
  [PASS] ({n}pt) label  (details)
  [FAIL] ({n}pt) label  (details)
  SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})

run_verify must be called while the containers are still alive.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

# -- SITE_CONFIG --------------------------------------------------------------
# Per-app env-var configuration.
#
# container_suffix / db_suffix:
#   prefix = f"rollout_{slot_id}_{app_name}"
#   app_container = prefix  (when suffix is an empty string)
#   app_container = prefix + suffix  (when suffix is non-empty)
#   db_var = None means do not set the DB_CONTAINER env var

SITE_CONFIG: dict[str, dict] = {
    "code-server": {
        "port_var":          "CODE_SERVER_PORT",
        "container_var":     "CODE_SERVER_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "openproject": {
        "port_var":          "OPENPROJECT_PORT",
        "container_var":     "OPENPROJECT_CONTAINER",
        "container_suffix":  "",
        "db_var":            "OPENPROJECT_DB_CONTAINER",
        "db_suffix":         "",
    },
    "metabase": {
        "port_var":          "METABASE_PORT",
        "container_var":     "METABASE_CONTAINER",
        "container_suffix":  "",
        "db_var":            "METABASE_DB_CONTAINER",
        "db_suffix":         "",
    },
    "baserow": {
        "port_var":          "BASEROW_PORT",
        "container_var":     "BASEROW_CONTAINER",
        "container_suffix":  "",
        "db_var":            "BASEROW_DB_CONTAINER",
        "db_suffix":         "",
    },
    "twenty": {
        "port_var":          "TWENTY_PORT",
        "container_var":     "TWENTY_CONTAINER",
        "container_suffix":  "",
        "db_var":            "TWENTY_DB_CONTAINER",
        "db_suffix":         "",
    },
    "bigcapital": {
        "port_var":          "BIGCAPITAL_PORT",
        "container_var":     "BIGCAPITAL_CONTAINER",
        "container_suffix":  "",
        "db_var":            "BIGCAPITAL_DB_CONTAINER",
        "db_suffix":         "",
    },
    "hrms": {
        "port_var":          "HRMS_PORT",
        "container_var":     "HRMS_CONTAINER",
        "container_suffix":  "",
        "db_var":            "HRMS_DB_CONTAINER",
        "db_suffix":         "",
    },
    "pretix": {
        "port_var":          "PRETIX_PORT",
        "container_var":     "PRETIX_CONTAINER",
        "container_suffix":  "",
        "db_var":            "PRETIX_DB_CONTAINER",
        # postgres lives in the compose sidecar "$prefix-db" (docker/pretix.yml.tpl), NOT in the
        # app container — "" sent every verifier psql into a container with no database, and task
        # helpers that swallow exec return codes turned that into "event not found".
        "db_suffix":         "-db",
    },
    "openemr": {
        "port_var":          "OPENEMR_PORT",
        "container_var":     "OPENEMR_CONTAINER",
        "container_suffix":  "",
        "db_var":            "OPENEMR_DB_CONTAINER",
        "db_suffix":         "",
    },
    "opnform": {
        "port_var":          "OPNFORM_PORT",
        "container_var":     "OPNFORM_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    # compose-based apps — app/db container names carry suffixes
    "onlyoffice": {
        "port_var":          "ONLYOFFICE_PORT",
        "container_var":     "ONLYOFFICE_CONTAINER",
        "container_suffix":  "-community",
        "db_var":            "ONLYOFFICE_DB_CONTAINER",
        "db_suffix":         "-mysql",
    },
    "mattermost": {
        "port_var":          "MATTERMOST_PORT",
        "container_var":     "MATTERMOST_CONTAINER",
        "container_suffix":  "",
        "db_var":            "MATTERMOST_DB_CONTAINER",
        "db_suffix":         "-postgres",
    },
    "owncloud": {
        "port_var":          "OWNCLOUD_PORT",
        "container_var":     "OWNCLOUD_CONTAINER",
        "container_suffix":  "",
        "db_var":            "OWNCLOUD_DB_CONTAINER",
        "db_suffix":         "-mariadb",
    },
    "roundcubemail": {
        "port_var":          "ROUNDCUBEMAIL_PORT",
        "container_var":     "ROUNDCUBEMAIL_CONTAINER",
        "container_suffix":  "",
        "db_var":            "ROUNDCUBEMAIL_DB_CONTAINER",
        "db_suffix":         "",
    },
    # -- Agriculture / Media New Apps --------------------------------------------------
    "grocy": {
        "port_var":          "GROCY_PORT",
        "container_var":     "GROCY_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "recipya": {
        "port_var":          "RECIPYA_PORT",
        "container_var":     "RECIPYA_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "farmos": {
        "port_var":          "FARMOS_PORT",
        "container_var":     "FARMOS_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "e-label": {
        "port_var":          "E_LABEL_PORT",
        "container_var":     "E_LABEL_CONTAINER",
        "container_suffix":  "",
        "db_var":            "E_LABEL_DB_CONTAINER",
        "db_suffix":         "",
    },
    "siyuan": {
        "port_var":          "SIYUAN_PORT",
        "container_var":     "SIYUAN_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "watcharr": {
        "port_var":          "WATCHARR_PORT",
        "container_var":     "WATCHARR_CONTAINER",
        "container_suffix":  "",
        "db_var":            None,
        "db_suffix":         None,
    },
    "booklore": {
        "port_var":          "BOOKLORE_PORT",
        "container_var":     "BOOKLORE_CONTAINER",
        "container_suffix":  "",
        # BookLore's MariaDB and client are embedded in the single mw-booklore
        # container.  Its verifiers otherwise invent a non-existent ``-db``
        # container and silently see an empty catalog.
        "db_var":            "BOOKLORE_DB_CONTAINER",
        "db_suffix":         "",
    },
    "mediacms": {
        "port_var":          "MEDIACMS_PORT",
        "container_var":     "MEDIACMS_CONTAINER",
        "container_suffix":  "",
        "db_var":            "MEDIACMS_DB_CONTAINER",
        "db_suffix":         "",
    },
    "photoprism": {
        "port_var":          "PHOTOPRISM_PORT",
        "container_var":     "PHOTOPRISM_CONTAINER",
        "container_suffix":  "",
        "db_var":            "PHOTOPRISM_DB_CONTAINER",
        "db_suffix":         "",
    },
}


_SLOT_PREFIX = os.environ.get("SAAS_SLOT_PREFIX", "rollout")

_JUDGE_SECRET_ENV = ("JUDGE_API_KEY", "LLM_API_KEY")


def normalize_judge_env(env: dict[str, str]) -> dict[str, str]:
    """Normalize the LLM-judge configuration before a verifier subprocess sees it.

    Whitespace-strips the three JUDGE_* values and defaults the reasoning effort. Deliberately does
    NOT fill anything in from LLM_* or from a built-in provider — see below.
    """
    # The judge is configured INDEPENDENTLY of the agent's model — JUDGE_MODEL / JUDGE_BASE_URL /
    # JUDGE_API_KEY are a self-contained unit and never inherit from LLM_*. That is a benchmark
    # requirement, not a preference: 23 of the 106 tasks are scored by an LLM judge, so if the judge
    # followed LLM_MODEL then every column of a results table would be graded by a *different*
    # model — the scores would not be comparable, and each model under test would be partly grading
    # its own output. The judge must be one fixed grader across every run being compared.
    #
    # Consequently there is no default endpoint either: an implicit "https://api.openai.com/v1"
    # silently pairs one provider's endpoint with another provider's model name. Unset means
    # unconfigured, and the judge-backed checks say so (see each verifier's llm_judge* guard)
    # instead of failing as an opaque HTTP error.
    #
    # This function is the single place judge configuration is decided; it runs at both verifier
    # execution boundaries (verify_runner.run_verify locally, provisioner_helm.grade on Kubernetes).
    # Values are stripped because Secret Manager material commonly carries a trailing newline, which
    # HTTP libraries reject from an Authorization header — with the raw header in the exception.
    for _name in ("JUDGE_API_KEY", "JUDGE_BASE_URL", "JUDGE_MODEL"):
        if _name in env:
            env[_name] = env[_name].strip()
    env["JUDGE_REASONING_EFFORT"] = (
        env.get("JUDGE_REASONING_EFFORT", "").strip() or "high"
    )
    return env


def redact_verifier_secrets(text: str, env: dict[str, str]) -> str:
    """Remove judge credentials before verifier output is parsed, stored, or returned."""
    candidates: set[str] = set()
    for name in _JUDGE_SECRET_ENV:
        raw = env.get(name, "")
        if raw:
            candidates.add(raw)
            stripped = raw.strip()
            if stripped:
                candidates.add(stripped)
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def build_verify_env(
    task: dict,
    slot_id: int,
    port_map: dict[str, int],
    hostname: str,
) -> dict[str, str]:
    """Build the env dict required by verify.py (layered on top of the system env)."""
    env = os.environ.copy()
    env["SERVER_HOSTNAME"] = hostname

    # Keep the generic agent/runtime credentials usable as a compatibility input while exposing
    # the verifier-specific contract expected by judge-backed tasks.
    normalize_judge_env(env)

    sites: list[str] = task.get("meta", {}).get("meta_data", {}).get("sites", [])

    for site in sites:
        cfg = SITE_CONFIG.get(site)
        if cfg is None:
            continue  # unknown app, skip

        prefix = f"{_SLOT_PREFIX}_{slot_id}_{site}"

        # port
        port = port_map.get(site)
        if port is not None:
            env[cfg["port_var"]] = str(port)

        # app container
        app_container = prefix + cfg["container_suffix"]
        env[cfg["container_var"]] = app_container

        # db container
        if cfg["db_var"] is not None:
            db_container = prefix + cfg["db_suffix"]
            env[cfg["db_var"]] = db_container

    return env


# -- Output parsing -----------------------------------------------------------
_CHECK_RE = re.compile(
    r"^\[(PASS|FAIL|ERROR)\]\s+\((\d+)pt\)\s+(.+?)\s*$"
)
_SCORE_RE = re.compile(
    r"^SCORE:\s*([\d.]+)\s+PASS:\s*(True|False)\s+\((\d+)/(\d+)\)"
)
# Multi-line details are joined (see _parse_verify_output); cap them so a verifier that dumps an
# HTML error page or a large SQL banner cannot bloat every stored grade record.
_MAX_DETAIL_CHARS = 2000


def _parse_verify_output(stderr_text: str) -> dict:
    checks = []
    score = 0.0
    earned = 0
    total = 0
    all_pass = False
    has_errors = False
    score_found = False

    lines = stderr_text.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        idx += 1
        m = _CHECK_RE.match(line)
        if m:
            status, weight, remainder = m.groups()
            # Split detail ourselves instead of requiring its closing ``)`` on this physical line.
            # Exception strings can contain newlines (for example an HTTP response body); the old
            # regex then swallowed ``exception: ...`` into the label and failed to promote the
            # check to ERROR.
            parts = re.split(r"\s{2,}\(", remainder, maxsplit=1)
            label = parts[0].strip()
            detail = parts[1].strip() if len(parts) > 1 else ""
            # Keeping only the first physical line hid the actual cause whenever a tool wrote a
            # banner before its error: business_052's failing BigCapital check reported just
            # ``bigcapital mysql error: --------------`` because mysql echoes the statement between
            # dashed rules and the real ``ERROR ...`` text sat on a later line. Absorb continuation
            # lines up to the detail's closing ``)`` so the recorded detail is diagnosable.
            if len(parts) > 1 and not detail.endswith(")"):
                while idx < len(lines):
                    nxt = lines[idx].strip()
                    # A new check or the SCORE line means the detail was never closed; stop rather
                    # than swallowing the rest of the report.
                    if _CHECK_RE.match(nxt) or _SCORE_RE.match(nxt):
                        break
                    idx += 1
                    if nxt:
                        detail = f"{detail} {nxt}" if detail else nxt
                    if nxt.endswith(")"):
                        break
            if detail.endswith(")"):
                detail = detail[:-1].rstrip()
            if len(detail) > _MAX_DETAIL_CHARS:
                detail = detail[:_MAX_DETAIL_CHARS].rstrip() + " …[truncated]"
            # Backward compatibility for existing task verifiers, which historically caught
            # infrastructure/query exceptions and printed them as ordinary FAIL checks.
            if status == "FAIL" and detail.lower().startswith("exception:"):
                status = "ERROR"
            has_errors = has_errors or status == "ERROR"
            checks.append({
                "label":  label,
                "weight": int(weight),
                "passed": status == "PASS",
                "status": status,
                "detail": detail,
            })
            continue
        m = _SCORE_RE.match(line)
        if m:
            score      = float(m.group(1))
            all_pass   = m.group(2) == "True"
            earned     = int(m.group(3))
            total      = int(m.group(4))
            score_found = True

    if not score_found and checks:
        total  = sum(c["weight"] for c in checks)
        earned = sum(c["weight"] for c in checks if c["passed"])
        score  = earned / total if total else 0.0
        all_pass = all(c["passed"] for c in checks)

    return {
        "checks":   checks,
        "score":    score,
        "earned":   earned,
        "total":    total,
        "all_pass": all_pass,
        "has_errors": has_errors,
    }


# -- Main entry point ---------------------------------------------------------

def run_verify(
    task: dict,
    slot_id: int,
    port_map: dict[str, int],
    hostname: str,
    result_dir: str,
    run_suffix: str = "",
) -> dict:
    """Run verify.py, return the result dict, and write it to {task_id}{run_suffix}_verify.json.

    The path to verify.py is taken from task["verify_py_path"].
    Returns status=SKIP when verify.py is missing.
    """
    task_id      = task["task_id"]
    verify_path  = task.get("verify_py_path")
    out_path     = Path(result_dir) / f"{task_id}{run_suffix}_verify.json"

    def _save(result: dict) -> dict:
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    if not verify_path or not Path(verify_path).exists():
        return _save({"task_id": task_id, "status": "SKIP", "score": 0.0,
                      "checks": [], "error": "no verify.py"})

    env = build_verify_env(task, slot_id, port_map, hostname)

    try:
        proc = subprocess.run(
            [sys.executable, verify_path],
            env=env,
            capture_output=True,
            text=True,
            # 600s: tightened verifiers recompute ground truth (software_041 runs the
            # full ctest suite ~2m40s; software_031/034 re-run test/coverage commands).
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return _save({"task_id": task_id, "status": "ERROR", "score": 0.0,
                      "checks": [], "error": "timeout after 600s"})
    except Exception as e:
        return _save({"task_id": task_id, "status": "ERROR", "score": 0.0,
                      "checks": [], "error": str(e)})

    parsed = _parse_verify_output(redact_verifier_secrets(proc.stderr, env))
    status = (
        "ERROR" if parsed["has_errors"]
        else ("PASS" if parsed["all_pass"] else ("FAIL" if parsed["checks"] else "ERROR"))
    )
    result = {
        "task_id":  task_id,
        "status":   status,
        "score":    parsed["score"],
        "earned":   parsed["earned"],
        "total":    parsed["total"],
        "all_pass": parsed["all_pass"],
        "checks":   parsed["checks"],
        "returncode": proc.returncode,
        "error": (
            "one or more verifier checks errored" if parsed["has_errors"]
            else ("verifier produced no checks" if not parsed["checks"] else None)
        ),
    }
    if not parsed["checks"]:
        # A verifier that printed no check line crashed before reaching the protocol — most
        # often an uncaught exception, whose traceback is the only evidence of the cause. The
        # parsed result carries none of it, so keep a redacted tail; without it a crash is
        # indistinguishable from "agent did nothing" and can only be diagnosed by rerunning
        # the whole task.
        tail = redact_verifier_secrets(proc.stderr or "", env).strip()
        if tail:
            result["stderr_tail"] = tail[-4000:]
    return _save(result)
