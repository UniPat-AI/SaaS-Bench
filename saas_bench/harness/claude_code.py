"""Claude Code harness: `--agent-module saas_bench.harness.claude_code`.

Drives `claude -p` headlessly with the Playwright MCP browser (vision caps on). Tools are
whitelisted via --allowedTools (browser + local file tools, **no Bash**) so the agent
cannot curl the app's API and bypass the UI — no --dangerously-skip-permissions needed:
in -p mode, anything outside the whitelist is simply denied.

Model: `--model` from run.py is passed through (override with $SAAS_CLAUDE_CODE_MODEL if
the benchmark-wide model name isn't a Claude model). Auth comes from the CLI's own login.
Kimi Code reuses this module's driver with a different binary (claude-compatible CLI).
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import time
from pathlib import Path

from saas_bench.harness import base

# All playwright MCP tools + the file tools the todo.md workflow and multimodal inputs
# need. Bash deliberately absent — but absence is not enough: the user's global
# ~/.claude permission config merges in, so anything it allows (Bash!) leaks through.
# --disallowedTools wins over every allow rule, so the ban goes there. WebFetch/WebSearch
# are banned too: fetching the app's URLs directly would bypass the UI.
_ALLOWED_TOOLS = ["mcp__playwright", "Read", "Write", "Edit", "TodoWrite"]

# Everything past the first three was observed leaking through the global config in the
# 2026-09-01 agriculture run, where they cost real wall clock and muddied trajectories:
#   Monitor      — an agent whose app had died armed a 1_900_000 ms poll waiting for it to
#                  come back, sitting idle for over half an hour of the task's budget.
#   Agent        — spawns subagents, so the recorded trajectory no longer describes the
#                  work that was actually done.
#   Task*/Cron*/ScheduleWakeup/Workflow/Skill/SendMessage/PushNotification
#                — scheduling and out-of-band messaging that has no meaning for a benchmark
#                  rollout, and in Monitor's case can outlast the task itself.
# A rollout is one browser session against one app; nothing here belongs in it. Names are
# matched as given, so a CLI that renames a tool will need this list revisited.
_DISALLOWED_TOOLS = [
    "Bash", "WebFetch", "WebSearch",
    "Monitor", "Agent", "Workflow", "Skill", "SendMessage", "PushNotification",
    "TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop",
    "ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
]


def _mcp_config(workdir) -> str:
    return json.dumps({
        "mcpServers": {
            "playwright": {
                "command": base.playwright_mcp_bin(),
                "args": base.playwright_mcp_args(workdir),
            }
        }
    })


def _parse_stream_json(stdout: str) -> tuple[list[dict], str, bool, int]:
    """-> (trajectory, final_output, success, num_turns)."""
    steps_by_id: dict[str, dict] = {}
    trajectory: list[dict] = []
    final_output, success, num_turns = "", False, 0

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "assistant":
            for item in (ev.get("message", {}).get("content") or []):
                if item.get("type") == "tool_use":
                    s = base.step(base.normalize_action_name(item.get("name", "?")),
                                  item.get("input", {}))
                    trajectory.append(s)
                    if item.get("id"):
                        steps_by_id[item["id"]] = s
        elif etype == "user":
            for item in (ev.get("message", {}).get("content") or []):
                if isinstance(item, dict) and item.get("type") == "tool_result" and item.get("is_error"):
                    s = steps_by_id.get(item.get("tool_use_id", ""))
                    if s:
                        content = item.get("content")
                        s["results"][0]["error"] = str(content)[:500] if content else "tool error"
        elif etype == "result":
            final_output = ev.get("result") or ""
            success = not ev.get("is_error", False)
            num_turns = ev.get("num_turns", 0)

    return trajectory, final_output, success, num_turns


async def run_claude_compatible(
    binary: str,
    task: dict,
    model_name: str,
    prompt: str,
    result_dir: str,
    max_steps: int,
    slot_id,
    todo_md,
    run_idx,
    input_files,
    model_env_var: str,
) -> dict:
    task_id = task["task_id"]
    tag = f"[slot {slot_id}][{task_id}_r{run_idx}]"
    model = os.environ.get(model_env_var) or model_name

    workdir = base.make_workdir(task_id)
    if todo_md:
        (workdir / "todo.md").write_text(todo_md, encoding="utf-8")
    full_prompt = base.compose_prompt(prompt, todo_md, input_files)

    if not shutil.which(binary):
        payload = {
            "task_id": task_id, "status": "error", "agent_output": "",
            "trajectory": [], "error": f"{binary}: binary not found on PATH",
        }
        return base.write_agent_result(result_dir, task_id, run_idx, payload)

    argv = [
        binary, "-p", full_prompt,
        "--output-format", "stream-json", "--verbose",
        "--max-turns", str(max_steps),
        "--mcp-config", _mcp_config(workdir),
        # hermetic run: no user/project settings (a permissive ~/.claude config would
        # allow-list tools past our whitelist), no user MCP servers
        "--setting-sources", "",
        "--strict-mcp-config",
        "--allowedTools", *_ALLOWED_TOOLS,
        "--disallowedTools", *_DISALLOWED_TOOLS,
    ]
    if model:  # empty -> the CLI's own default model
        argv += ["--model", model]

    # Per-task CLAUDE_CONFIG_DIR: concurrent slots must not share ~/.claude, or they race
    # on session/state files. ANTHROPIC_* are inherited from the shell (run_cli copies the
    # environment), which is how a relay endpoint gets injected.
    config_dir = workdir / "claude_home"
    config_dir.mkdir(parents=True, exist_ok=True)
    env_extra = {"CLAUDE_CONFIG_DIR": str(config_dir)}

    t0 = time.time()
    attempts = int(os.environ.get("SAAS_CLAUDE_RETRIES", "3")) + 1
    deadline = t0 + base.DEFAULT_TIMEOUT
    log_parts: list[str] = []
    code, stdout, stderr, timed_out = -1, "", "", False
    trajectory, output, success, num_turns = [], "", False, 0

    # Live per-step progress: without this the run prints nothing between "app ready" and
    # task completion, so a normal 5-15 min task is indistinguishable from a hang.
    seen = [0]

    def _progress(line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        if ev.get("type") != "assistant":
            return
        for item in (ev.get("message", {}).get("content") or []):
            if item.get("type") == "tool_use":
                seen[0] += 1
                print(base.progress_line(tag, seen[0],
                                         base.normalize_action_name(item.get("name", "?")),
                                         item.get("input")), flush=True)

    tried = 0
    budget_hit = False
    for attempt in range(1, attempts + 1):
        seen[0] = 0
        tried = attempt
        code, stdout, stderr, timed_out = await base.run_cli(
            argv, cwd=workdir, env_extra=env_extra, on_line=_progress)
        trajectory, output, success, num_turns = _parse_stream_json(stdout)
        log_parts.append(
            f"=== ATTEMPT {attempt} (exit={code} timed_out={timed_out} "
            f"turns={num_turns} steps={len(trajectory)}) ===\n--- STDERR ---\n"
            f"{stderr[-4000:]}\n--- STDOUT head 2k ---\n{stdout[:2000]}\n"
        )
        # `claude -p` exits 1 when it burns through --max-turns. That is a spent step
        # budget, not a failure: the work is in the trajectory and must NOT be retried.
        budget_hit = num_turns >= max_steps
        # Only retry a total failure that looks upstream-transient (a shared relay key at
        # high concurrency returns 429/overloaded). Match on stderr and on the stream-json
        # `result` event only — scanning all of stdout would match page text the agent
        # merely happened to read (a browser_wait_for result, the digits "429", ...) and
        # trigger bogus retries of a task that in fact ran to completion.
        result_evt = ""
        for ln in reversed(stdout.splitlines()):
            if '"type":"result"' in ln:
                result_evt = ln.lower()
                break
        blob = stderr[-4000:].lower() + result_evt
        transient = any(s in blob for s in
                        ("429", "rate limit", "rate_limit", "overloaded",
                         "503", "502", "connection error", "api_error"))
        if output or timed_out or code == 0 or budget_hit or not transient \
                or attempt == attempts:
            break
        # Jittered backoff: a fixed delay would make all workers retry in lockstep and
        # hit the same limit again.
        delay = min(30 * (2 ** (attempt - 1)) * (0.5 + random.random()),
                    max(0.0, deadline - time.time() - 60))
        if delay <= 0:
            break
        print(f"  {tag} {binary}: transient upstream failure, retry "
              f"{attempt}/{attempts - 1} in {delay:.0f}s", flush=True)
        await asyncio.sleep(delay)

    try:
        suffix = f"_r{run_idx}" if run_idx is not None else ""
        (Path(result_dir) / f"{task_id}{suffix}.claude.log").write_text(
            "\n".join(log_parts), encoding="utf-8")
    except Exception:
        pass

    error = None
    if timed_out:
        error = f"harness timeout after {base.DEFAULT_TIMEOUT}s"
    elif budget_hit:
        # NOT an error, and checked before `code` because `claude -p` signals a spent turn
        # budget with exit 1, same as a hard failure. Every task is solvable inside the budget,
        # so burning all of it is the agent's own result: the trajectory and the app state are
        # real and get graded like any other attempt. Kept symmetric with codex.py so a
        # cross-harness comparison isn't skewed by one side filing capped runs as failures.
        error = None
    elif code != 0 and not output:
        error = f"{binary} exited {code} after {tried} attempt(s): {stderr[-400:]}"
    elif not trajectory:
        # Zero tool calls: the CLI never drove the browser, so it failed before starting —
        # rejected auth, a model name the endpoint won't dispatch, a dead MCP server. Its one
        # assistant message is that failure, not a task report, and because the message is
        # non-empty none of the branches above fire. Left unclassified the run is filed as
        # `completed` with score 0.000 and error=None, which reads exactly like an agent that
        # tried everything and got it all wrong — the 2026-09-01 recheck lost all five tasks
        # that way to a 403 from the relay.
        detail = output.strip()[:300] or f"exit {code}, no output"
        error = f"agent took no action — {binary} reported: {detail}"

    # `success` is the agent's own claim of completion, which a budget-capped run never made.
    base.finalize_trajectory(
        trajectory, success and error is None and not budget_hit, output)
    payload = {
        "task_id": task_id,
        "status": "completed" if error is None else "error",
        "agent_output": output,
        "trajectory": trajectory,
        # budget_hit keeps a capped run identifiable in analysis even though it is not an error.
        "harness": {"cli": binary, "model": model, "num_turns": num_turns,
                    "duration_s": round(time.time() - t0, 1), "exit_code": code,
                    "budget_hit": budget_hit},
    }
    if error:
        payload["error"] = error
    print(f"  {tag} {binary}: {payload['status']} turns={num_turns} "
          f"steps={len(trajectory)} in {payload['harness']['duration_s']}s"
          + (f" [budget spent: {num_turns}/{max_steps}]" if budget_hit else ""), flush=True)
    return base.write_agent_result(result_dir, task_id, run_idx, payload)


async def run_task(
    task: dict,
    model_name: str,
    prompt: str,
    result_dir: str,
    max_steps: int = 80,
    slot_id=None,
    todo_md=None,
    run_idx=None,
    input_files=None,
) -> dict:
    return await run_claude_compatible(
        os.environ.get("SAAS_CLAUDE_CODE_BIN", "claude"),
        task, model_name, prompt, result_dir, max_steps, slot_id, todo_md, run_idx,
        input_files, model_env_var="SAAS_CLAUDE_CODE_MODEL",
    )
