"""SaaS-Bench eval harness — main entry point.

Concurrency model: ProcessPoolExecutor(max_workers) + asyncio.run() per process.
Each worker process has its own memory space, fully isolating bubus/browser-use
module-level state.
"""

import argparse
import glob
import importlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import yaml

from saas_bench.grading import make_grader
from saas_bench.loader import build_prompt, load_tasks
from saas_bench.reporting import generate_outputs
from saas_bench.targets import make_target


_SLOT_PREFIX = os.environ.get("SAAS_SLOT_PREFIX", "rollout")
_TMP_BASE = os.environ.get(
    "SAAS_BENCH_TMP",
    os.path.join(tempfile.gettempdir(), f"saas_bench_{_SLOT_PREFIX}"),
)


def _load_apps_config(apps_yaml: str) -> dict:
    with open(apps_yaml) as f:
        return yaml.safe_load(f)["apps"]


def _global_cleanup() -> None:
    """Kill stale chrome processes belonging to this instance and purge tmp dirs.

    Uses --user-data-dir path to identify only our own Chrome processes,
    so parallel run.py instances with different SAAS_SLOT_PREFIX don't
    kill each other's Chrome processes.
    """
    # 1. Kill only chrome processes whose --user-data-dir is under our _TMP_BASE
    r = subprocess.run(
        f"pkill -9 -f 'remote-debugging-port.*--user-data-dir={_TMP_BASE}'",
        shell=True, capture_output=True, text=True,
    )
    # Also kill by user-data-dir alone (order of flags may differ)
    subprocess.run(
        f"pkill -9 -f -- '--user-data-dir={_TMP_BASE}'",
        shell=True, capture_output=True, text=True,
    )
    killed = "ok" if r.returncode in (0, 1) else f"rc={r.returncode}"

    # 2. Remove leftover user-data dirs
    pattern = f"{_TMP_BASE}/chrome_*"
    leftovers = glob.glob(pattern)
    for d in leftovers:
        shutil.rmtree(d, ignore_errors=True)

    # 3. Remove any orphaned per-task file-system workdirs
    for d in glob.glob(f"{_TMP_BASE}/fs_*"):
        shutil.rmtree(d, ignore_errors=True)

    print(
        f"[startup-cleanup] killed stale chrome ({killed}) | "
        f"removed {len(leftovers)} chrome dirs (prefix={_SLOT_PREFIX}, tmp={_TMP_BASE})",
        flush=True,
    )


def _log_error(result_dir: str, slot_id: int, task_id: str, phase: str, exc: Exception) -> None:
    """Append an error entry to {result_dir}/errors.log."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    entry = (
        f"[{ts}] [slot {slot_id}] [{task_id}] [{phase}]\n"
        f"  {type(exc).__name__}: {exc}\n"
        f"  {''.join(tb[-3:]).rstrip()}\n\n"
    )
    log_path = Path(result_dir) / "errors.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a") as f:
        f.write(entry)


def _run_one(
    task: dict,
    slot_id: int,
    apps_config: dict,
    model: str,
    result_dir: str,
    max_steps: int,
    hostname: str,
    use_isolation: bool,
    run_idx: int = 0,
    tasks_dir: str = "",
    agent_module: str = "saas_bench.agent",
    target_backend: str = "slotmanager",
    grade_backend: str = "local",
    playground_url: str = "",
) -> dict:
    """Full execution of a single task (runs in a thread pool, owns its own asyncio event loop)."""
    import asyncio

    run_task = importlib.import_module(agent_module).run_task
    run_suffix = f"_r{run_idx}"

    target = make_target(
        target_backend, apps_config, slot_id, hostname, use_isolation, playground_url=playground_url,
    )
    grader = make_grader(grade_backend, playground_url=playground_url)

    agent_result: dict = {"task_id": task["task_id"], "status": "error", "trajectory": []}
    verify_result: dict = {
        "task_id": task["task_id"], "status": "SKIP", "score": 0.0,
        "checks": [], "error": "not executed",
    }

    # release() lives in the OUTER finally so it runs even when prepare fails after partially
    # provisioning (e.g. PlaygroundTarget's reachability poll times out AFTER /prepare succeeded
    # and self._run_id is set, or SlotManagerTarget partially starts containers). target.release()
    # is a no-op when the target holds no per-run state, so it's safe to call unconditionally.
    prepared = None
    task_id = task["task_id"]
    try:
        # Phase 1: prepare. A prepare failure short-circuits — agent/grade can't run without an env.
        try:
            prepared = target.prepare(task, run_idx)
        except Exception as exc:
            _log_error(result_dir, slot_id, task_id, "prepare", exc)
            agent_result["error"] = f"prepare: {type(exc).__name__}: {exc}"
            print(
                f"  [slot {slot_id}][{task_id}] ERROR in prepare: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
                flush=True,
            )
            return {
                **agent_result, "run_idx": run_idx,
                "verify_score": 0.0, "verify_status": "SKIP",
            }

        # Phase 2: agent (its own try so a grade-time error isn't logged as an agent failure).
        try:
            prompt, todo_md, input_files = build_prompt(
                task, url_map=prepared.url_map, tasks_root=tasks_dir,
            )
            agent_result = asyncio.run(
                run_task(
                    task, model, prompt, result_dir,
                    max_steps=max_steps, slot_id=slot_id, todo_md=todo_md,
                    run_idx=run_idx, input_files=input_files,
                )
            )
        except Exception as exc:
            _log_error(result_dir, slot_id, task_id, "agent", exc)
            agent_result["status"] = "error"
            agent_result["error"] = f"agent: {type(exc).__name__}: {exc}"
            print(
                f"  [slot {slot_id}][{task_id}] ERROR in agent: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
                flush=True,
            )

        # Phase 3: grade (still runs even if the agent errored — captures a partial-progress
        # score or confirms the untouched baseline). Errors here are graded errors, not agent.
        try:
            verify_result = grader.grade(task, prepared, result_dir, run_suffix)
        except Exception as exc:
            _log_error(result_dir, slot_id, task_id, "grade", exc)
            verify_result = {
                "task_id": task_id, "status": "ERROR", "score": 0.0, "checks": [],
                "error": f"grade: {type(exc).__name__}: {exc}",
            }
            print(
                f"  [slot {slot_id}][{task_id}] ERROR in grade: "
                f"{type(exc).__name__}: {str(exc)[:200]}",
                flush=True,
            )
    finally:
        target.release()

    return {
        **agent_result,
        "run_idx": run_idx,
        "verify_score": verify_result.get("score", 0.0),
        "verify_status": verify_result.get("status", "SKIP"),
    }


def _run_task_all_runs(
    task: dict,
    slot_id: int,
    apps_config: dict,
    model: str,
    result_dir: str,
    max_steps: int,
    hostname: str,
    use_isolation: bool,
    run_start: int = 0,
    runs: int = 1,
    tasks_dir: str = "",
    agent_module: str = "saas_bench.agent",
    target_backend: str = "slotmanager",
    grade_backend: str = "local",
    playground_url: str = "",
) -> list[dict]:
    """All runs of a single task execute serially on the same slot, avoiding slot contention between runs."""
    results = []
    for run_idx in range(run_start, run_start + runs):
        result = _run_one(
            task, slot_id, apps_config, model, result_dir,
            max_steps, hostname, use_isolation, run_idx, tasks_dir, agent_module,
            target_backend, grade_backend, playground_url,
        )
        results.append(result)
    return results


_JUDGE_ENV = ("JUDGE_MODEL", "JUDGE_BASE_URL", "JUDGE_API_KEY")


def _warn_if_judge_unconfigured(tasks: list[dict], grade_backend: str) -> None:
    """Say up front that judge-backed checks cannot be scored, rather than after the whole run.

    Some tasks are scored by an LLM judge, which is configured independently of the agent's model
    (see saas_bench.verify_runner.normalize_judge_env for why). Unconfigured, those checks fail and
    the run still produces a summary — a plausible-looking score that is simply too low, with
    nothing in the totals to say the judge never ran. Hence a warning at startup.

    Skipped for service grading: there the verifier runs in the platform pod, so the judge is
    configured on that deployment (values.yaml `verifierJudge`), not in this process's environment.
    """
    if grade_backend != "local":
        return
    missing = [name for name in _JUDGE_ENV if not os.environ.get(name, "").strip()]
    if not missing:
        return
    judged = [
        t["task_id"] for t in tasks
        if t.get("verify_py_path")
        and "JUDGE_MODEL" in _read_text_or_empty(t["verify_py_path"])
    ]
    if not judged:
        return
    print(
        f"[WARN] {len(judged)} of {len(tasks)} selected tasks are scored by an LLM judge, but "
        f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not set — those checks will "
        f"fail as 'judge not configured' and the reported score will be understated.\n"
        f"       The judge is deliberately separate from LLM_MODEL so that every run is graded by "
        f"the same model (see .env.example). Affected: {', '.join(judged[:5])}"
        f"{' …' if len(judged) > 5 else ''}",
        flush=True,
    )


def _read_text_or_empty(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def main(
    tasks_dir: str,
    model: str,
    workers: int,
    result_dir: str,
    max_steps: int,
    hostname: str,
    task_ids: list[str] | None,
    apps_yaml: str,
    use_isolation: bool,
    runs: int = 1,
    run_start: int = 0,
    write_report: bool = True,
    agent_module: str = "saas_bench.agent",
    target_backend: str = "slotmanager",
    grade_backend: str = "local",
    playground_url: str = "",
) -> None:
    _global_cleanup()
    started_at = datetime.now()
    t0 = time.perf_counter()

    # Nest results under a model-named subdirectory so different model runs
    # never clobber each other: results/<model_name>/...
    model_slug = model.replace("/", "_").replace(":", "_")
    result_dir = str(Path(result_dir) / model_slug)

    tasks = load_tasks(tasks_dir)
    if task_ids:
        tasks = [t for t in tasks if str(t.get("task_id")) in task_ids]

    apps_config = _load_apps_config(apps_yaml)

    total_jobs = len(tasks) * runs
    print(
        f"Loaded {len(tasks)} tasks × runs={runs} (r{run_start}..r{run_start+runs-1}) = {total_jobs} jobs | "
        f"workers={workers} | model={model} | isolation={use_isolation}",
        flush=True,
    )
    _warn_if_judge_unconfigured(tasks, grade_backend)

    # Each task's r0→r1→...→rN runs serially on the same slot.
    # Tasks themselves run in parallel across workers (one slot per task).
    # This guarantees no two concurrent jobs ever share a slot.
    task_results: list[tuple[dict, int, dict | Exception]] = []

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _run_task_all_runs, task,
                i % workers,
                apps_config, model,
                result_dir, max_steps, hostname, use_isolation,
                run_start, runs, tasks_dir, agent_module,
                target_backend, grade_backend, playground_url,
            ): task
            for i, task in enumerate(tasks)
        }
        for fut in as_completed(futures):
            task = futures[fut]
            try:
                for run_idx, result in enumerate(fut.result(), start=run_start):
                    task_results.append((task, run_idx, result))
            except Exception as e:
                _log_error(result_dir, -1, task.get("task_id", "?"), "worker", e)
                print(
                    f"  [{task.get('task_id','?')}] WORKER EXCEPTION: "
                    f"{type(e).__name__}: {str(e)[:200]}",
                    flush=True,
                )
                for run_idx in range(run_start, run_start + runs):
                    task_results.append((task, run_idx, e))

    ended_at = datetime.now()
    duration_s = round(time.perf_counter() - t0, 1)

    # Group statistics by domain (per task×run)
    domain_stats: dict[str, dict] = defaultdict(
        lambda: {"total": 0, "completed": 0, "error": 0, "exception": 0,
                 "verify_pass": 0, "verify_scores": []}
    )
    for task, run_idx, result in task_results:
        cat = task.get("category_id", "UNKNOWN")
        ds = domain_stats[cat]
        ds["total"] += 1
        if isinstance(result, Exception):
            ds["exception"] += 1
        elif isinstance(result, dict):
            if result.get("status") == "completed":
                ds["completed"] += 1
            else:
                ds["error"] += 1
            vs = result.get("verify_status", "SKIP")
            if vs == "PASS":
                ds["verify_pass"] += 1
            ds["verify_scores"].append(result.get("verify_score", 0.0))

    print("\n=== Eval done ===", flush=True)
    total_completed = sum(v["completed"] for v in domain_stats.values())
    total_jobs_done = sum(v["total"] for v in domain_stats.values())
    for cat, ds in sorted(domain_stats.items()):
        avg_score = (
            sum(ds["verify_scores"]) / len(ds["verify_scores"])
            if ds["verify_scores"] else 0.0
        )
        print(
            f"  {cat:6s}: {ds['completed']}/{ds['total']} completed | "
            f"verify_pass={ds['verify_pass']} avg_score={avg_score:.3f} | "
            f"error={ds['error']} exception={ds['exception']}",
            flush=True,
        )
    print(f"  Total : {total_completed}/{total_jobs_done} (tasks={len(tasks)}, runs={runs})", flush=True)

    Path(result_dir).mkdir(parents=True, exist_ok=True)
    run_meta = {
        "tasks_dir":  tasks_dir,
        "model":      model,
        "workers":    workers,
        "hostname":   hostname,
        "isolation":  use_isolation,
        "runs":       runs,
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at":   ended_at.isoformat(timespec="seconds"),
        "duration_s": duration_s,
    }
    summary_path, report_path = generate_outputs(
        Path(result_dir), tasks, run_meta, max_steps,
    ) if write_report else (None, None)
    if write_report:
        print(f"summary → {summary_path}", flush=True)
        print(f"report → {report_path}", flush=True)
    else:
        print("[--no-report] skipping summary.json/report.md generation", flush=True)

    # Print error log summary if any errors occurred
    error_log = Path(result_dir) / "errors.log"
    if error_log.exists():
        error_count = sum(1 for line in open(error_log) if line.startswith("["))
        print(f"\n⚠️  {error_count} errors recorded → {error_log}", flush=True)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="SaaS-Bench eval harness")
    p.add_argument("--tasks-dir", required=True,
                   help="Task directory root (containing Software/ Business/ Healthcare/ Teamwork/ subdirectories)")
    # No baked-in model name: which models exist depends entirely on the endpoint LLM_BASE_URL
    # points at, so any default silently 404s for most users. Falls back to $LLM_MODEL so
    # `python -m saas_bench.run` works directly — this module never reads .env, only
    # scripts/run*.sh source it (with allexport).
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "").strip(),
                   help="Model name handed to the agent. Defaults to $LLM_MODEL.")
    p.add_argument("--workers", type=int, default=3)
    p.add_argument("--result-dir", default="results")
    p.add_argument("--max-steps", type=int, default=400)
    p.add_argument("--hostname", default="localhost",
                   help="Hostname the agent uses to access apps")
    p.add_argument("--task-ids", nargs="*", help="Run only the specified subset of task ids")
    p.add_argument("--apps-yaml", default="saas_bench/apps.yaml")
    p.add_argument("--agent-module", default="saas_bench.agent",
                   help="Python module exposing async run_task(...); the bring-your-own-agent seam")
    p.add_argument("--target-backend", default="slotmanager", choices=["slotmanager", "playground"],
                   help="Where target apps run: 'slotmanager' (local docker) or 'playground' (hosted GKE service)")
    p.add_argument("--grade-backend", default="local", choices=["local", "service"],
                   help="Where verify.py runs: 'local' (in-process) or 'service' (hosted playground service)")
    p.add_argument("--playground-url", default="",
                   help="Base URL of the playground service (required for --target-backend playground / --grade-backend service)")
    p.add_argument("--no-isolation", action="store_true",
                   help="Do not start Docker container isolation (share already-running apps)")
    p.add_argument("--runs", type=int, default=1,
                   help="Number of independent repetitions per task (used for pass@k, k=runs)")
    p.add_argument("--run-start", type=int, default=0,
                   help="Starting run_idx; output file suffix is _r{run_start}.._r{run_start+runs-1} (used when re-running specific runs)")
    p.add_argument("--no-report", action="store_true",
                   help="Skip re-generating summary.json/report.md (avoids overwriting existing aggregates when re-running partial runs)")
    return p.parse_args()


def _validate_backend_combo(target: str, grade: str, playground_url: str = "") -> None:
    """target/grade verify_context shapes differ; cross-pairing crashes deep in the run loop.

    - ``slotmanager`` target sets ``slot_id`` / ``port_map`` / ``hostname`` (LocalGradeRunner reads these).
    - ``playground`` target sets ``service_run_id`` (ServiceGradeRunner reads this).
    Either ``playground``-side backend needs a non-empty ``--playground-url``; otherwise the worker
    crashes deep inside ``PlaygroundClient.__init__``. Fail fast with a clear message at the CLI.
    """
    if target == "playground" and grade != "service":
        raise SystemExit(
            "--target-backend playground requires --grade-backend service "
            "(the playground target doesn't set slot_id/port_map/hostname that local grading needs)"
        )
    if target == "slotmanager" and grade != "local":
        raise SystemExit(
            "--target-backend slotmanager requires --grade-backend local "
            "(slotmanager doesn't set the service_run_id that service grading needs)"
        )
    if (target == "playground" or grade == "service") and not playground_url:
        raise SystemExit(
            "--target-backend playground / --grade-backend service requires --playground-url "
            "(e.g. https://api.saas-playground.example.com)"
        )


if __name__ == "__main__":
    args = parse_args()
    if not args.model:
        raise SystemExit(
            "no model selected: pass --model, or set LLM_MODEL (see .env.example). "
            "There is no default because valid model names depend on your LLM_BASE_URL endpoint."
        )
    _validate_backend_combo(args.target_backend, args.grade_backend, args.playground_url)
    main(
        tasks_dir    = args.tasks_dir,
        model        = args.model,
        workers      = args.workers,
        result_dir   = args.result_dir,
        max_steps    = args.max_steps,
        hostname     = args.hostname,
        task_ids     = args.task_ids,
        apps_yaml    = args.apps_yaml,
        use_isolation= not args.no_isolation,
        runs         = args.runs,
        run_start    = args.run_start,
        write_report = not args.no_report,
        agent_module = args.agent_module,
        target_backend = args.target_backend,
        grade_backend = args.grade_backend,
        playground_url = args.playground_url,
    )
