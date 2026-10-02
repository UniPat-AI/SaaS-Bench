"""Codex CLI harness: `--agent-module saas_bench.harness.codex`.

Drives `codex exec --json` with the Playwright MCP browser (vision caps on), configured
inline via `-c mcp_servers.playwright.*` so nothing is written to the user's
~/.codex/config.toml. Sandbox stays at workspace-write: shell commands get no network
(so the agent can't curl the app's API around the UI), while the Playwright MCP — marked
`default_tools_approval_mode="approve"` — drives the browser normally. Without that mark
every MCP call dies on "requires approval, but approval policy is never" under any sandbox
short of danger-full-access, and codex reports the run as completed anyway.

Codex has no --max-turns equivalent; the harness wall-clock timeout ($SAAS_HARNESS_TIMEOUT)
is the backstop. The final answer is read from --output-last-message (stable across the
CLI's evolving JSON event schema); events are parsed best-effort for the trajectory only.
Model: run.py's --model passed through, override with $SAAS_CODEX_MODEL.
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


def _config_args(workdir) -> list[str]:
    # startup_timeout_sec is load-bearing at high concurrency. Codex's default deadline is a
    # few seconds; when a server misses it codex does NOT fail the run — it proceeds with the
    # server's tools absent, the model emits one planning message and ends the turn, and the
    # task lands with an empty trajectory and a 0 score. 120s was still too tight: a 20-worker
    # run on 2026-09-02 lost 25 tasks that way, all of them exiting in 8-11s with nothing in
    # stderr, and its failure rate climbed from 0% in the first 4.5h to 76% in the last half
    # hour as container and chromium pressure accumulated. Cheap to over-provision — the
    # deadline only bounds a cold start, it does not slow a healthy one.
    startup = os.environ.get("SAAS_CODEX_MCP_STARTUP_SEC", "300")
    args = [
        "-c", f'mcp_servers.playwright.command="{base.playwright_mcp_bin()}"',
        "-c", "mcp_servers.playwright.args=" + json.dumps(base.playwright_mcp_args(workdir)),
        "-c", f"mcp_servers.playwright.startup_timeout_sec={startup}",
        # Pre-approve this server's tools. `codex exec` runs with approval_policy=never, and
        # every other value of this field ("auto", "prompt", "writes") still routes MCP calls
        # through an approval decision that nothing can answer, so they all fail with
        # "MCP tool call requires approval, but approval policy is never" — verified
        # 2026-09-02 against codex-cli 0.152.1. "approve" is what lets the sandbox stay at
        # workspace-write instead of being widened to danger-full-access.
        "-c", 'mcp_servers.playwright.default_tools_approval_mode="approve"',
    ]
    tool_timeout = os.environ.get("SAAS_CODEX_MCP_TOOL_SEC", "").strip()
    if tool_timeout:
        args += ["-c", f"mcp_servers.playwright.tool_timeout_sec={tool_timeout}"]
    return args


def _tool_step(server: str, tool: str, arguments, error=None) -> dict:
    name = base.normalize_action_name(tool if not server else f"mcp__{server}__{tool}")
    return base.step(name, arguments, error=error)


def _live_action(ev: dict) -> tuple[str, object] | None:
    """(action_name, payload) for an event worth printing live, else None.

    Covers both JSONL schemas, and prefers each one's *earliest* signal so a slow tool call
    shows up when it starts rather than when it returns: the older schema has explicit
    `*_begin` events, the newer one only reports `item.completed`.
    """
    if ev.get("type") == "item.completed":
        item = ev.get("item")
        if not isinstance(item, dict):
            return None
        itype = item.get("type") or item.get("item_type")
        if itype == "mcp_tool_call":
            server, tool = item.get("server", ""), item.get("tool", "?")
            return base.normalize_action_name(
                tool if not server else f"mcp__{server}__{tool}"), item.get("arguments")
        if itype == "command_execution":
            return "shell", item.get("command", "")
        return None

    msg = ev.get("msg")
    if isinstance(msg, dict):
        if msg.get("type") == "mcp_tool_call_begin":
            inv = msg.get("invocation") or {}
            server, tool = inv.get("server", ""), inv.get("tool", "?")
            return base.normalize_action_name(
                tool if not server else f"mcp__{server}__{tool}"), inv.get("arguments")
        if msg.get("type") == "exec_command_begin":
            cmd = msg.get("command")
            return "shell", " ".join(cmd) if isinstance(cmd, list) else cmd
    return None


def _parse_events(stdout: str) -> tuple[list[dict], str]:
    """Best-effort over both codex JSONL schemas -> (trajectory, last_agent_message)."""
    trajectory: list[dict] = []
    last_message = ""

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Newer schema: {"type": "item.completed", "item": {...}}
        item = ev.get("item") if ev.get("type") == "item.completed" else None
        if isinstance(item, dict):
            itype = item.get("type") or item.get("item_type")
            if itype == "mcp_tool_call":
                failed = (item.get("status") == "failed")
                trajectory.append(_tool_step(
                    item.get("server", ""), item.get("tool", "?"),
                    item.get("arguments"), error="tool failed" if failed else None,
                ))
            elif itype == "command_execution":
                trajectory.append(base.step(
                    "shell", item.get("command", ""),
                    error=None if item.get("exit_code") in (0, None) else f"exit {item.get('exit_code')}",
                ))
            elif itype == "agent_message":
                last_message = item.get("text") or last_message
            continue

        # Older schema: {"msg": {"type": ..., ...}}
        msg = ev.get("msg")
        if isinstance(msg, dict):
            mtype = msg.get("type")
            if mtype == "mcp_tool_call_begin":
                inv = msg.get("invocation") or {}
                trajectory.append(_tool_step(
                    inv.get("server", ""), inv.get("tool", "?"), inv.get("arguments"),
                ))
            elif mtype == "mcp_tool_call_end" and trajectory:
                result = msg.get("result")
                if isinstance(result, dict) and result.get("Err") is not None:
                    trajectory[-1]["results"][0]["error"] = str(result["Err"])[:500]
            elif mtype == "exec_command_begin":
                trajectory.append(base.step("shell", msg.get("command", "")))
            elif mtype == "agent_message":
                last_message = msg.get("message") or last_message

    return trajectory, last_message


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
    task_id = task["task_id"]
    tag = f"[slot {slot_id}][{task_id}_r{run_idx}]"
    binary = os.environ.get("SAAS_CODEX_BIN", "codex")
    model = os.environ.get("SAAS_CODEX_MODEL") or model_name

    workdir = base.make_workdir(task_id)
    if todo_md:
        (workdir / "todo.md").write_text(todo_md, encoding="utf-8")
    full_prompt = base.compose_prompt(prompt, todo_md, input_files)
    last_msg_file = workdir / "last_message.txt"

    if not shutil.which(binary):
        payload = {
            "task_id": task_id, "status": "error", "agent_output": "",
            "trajectory": [], "error": f"{binary}: binary not found on PATH",
        }
        return base.write_agent_result(result_dir, task_id, run_idx, payload)

    argv = [
        binary, "exec", "--json",
        "--skip-git-repo-check",
        "-C", str(workdir),
        # workspace-write keeps the agent's shell off the network, so it cannot curl the app's
        # API around the UI. Safe to keep now that the MCP server is pre-approved above;
        # override only to debug (SAAS_CODEX_SANDBOX=danger-full-access).
        "--sandbox", os.environ.get("SAAS_CODEX_SANDBOX", "workspace-write"),
        *_config_args(workdir),
        "-o", str(last_msg_file),
    ]
    if model:  # empty -> the CLI's own default model
        argv += ["-m", model]
    argv.append(full_prompt)

    # Live per-step progress: without it the run prints nothing between "app ready" and task
    # completion, so a normal multi-minute task is indistinguishable from a hang — and codex's
    # worst failure mode (MCP absent, one planning message, exit 0) looks identical to a fast
    # success until the summary lands. Mirrors claude_code.py's callback.
    seen = [0]

    def _progress(line: str) -> bool:
        """Print the step, and return True once the step budget is spent (stops the CLI).

        codex has no --max-turns of its own, so without this the only bound is the wall clock
        and tasks routinely ran past 600 tool calls — several times the budget the other
        harnesses enforce, which makes a cross-harness score comparison meaningless.
        """
        line = line.strip()
        if not line.startswith("{"):
            return False
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return False
        hit = _live_action(ev)
        if hit is None:
            return False
        seen[0] += 1
        print(base.progress_line(tag, seen[0], hit[0], hit[1]), flush=True)
        return max_steps > 0 and seen[0] >= max_steps

    # Retry a turn that ended without ever calling a tool. Upstream occasionally answers a
    # tool-use turn with a plain preamble ("I'll complete this through the three web UIs...")
    # and finish_reason=stop, so codex closes the turn after ~44 output tokens with
    # reasoning_output_tokens=0 and the task "completes" in 5-12s having done nothing. It is
    # random, not task-bound: business_144 / software_025 / software_038 each did 400-620 steps
    # in one round and 0 in the next, and it reproduces at 4 workers, so it is neither load nor
    # prompt content. Only this exact shape is retried — a run that made even one tool call is
    # a real attempt and its result stands, however bad.
    attempts = max(1, int(os.environ.get("SAAS_CODEX_RETRIES", "5")) + 1)
    t0 = time.time()
    tried = 0
    for attempt in range(1, attempts + 1):
        tried = attempt
        seen[0] = 0
        code, stdout, stderr, timed_out = await base.run_cli(
            argv, cwd=workdir, on_line=_progress)
        trajectory, stream_msg = _parse_events(stdout)
        output = ""
        if last_msg_file.exists():
            output = last_msg_file.read_text(encoding="utf-8", errors="replace").strip()
        output = output or stream_msg
        if trajectory or timed_out or attempt == attempts:
            break
        # Jittered backoff: a fixed delay would make every stuck worker retry in lockstep.
        delay = min(15 * attempt * (0.5 + random.random()), 90)
        print(f"  {tag} {binary}: no tool call (attempt {attempt}/{attempts - 1}), "
              f"retrying in {delay:.0f}s", flush=True)
        await asyncio.sleep(delay)
        try:
            last_msg_file.unlink()
        except OSError:
            pass

    # Keep the raw stderr tail before classifying. codex's exit-1 paths — stream idle timeout,
    # request retries exhausted when the upstream API drops — leave nothing else behind, and
    # last_message.txt is never written on those, so the cause is otherwise unrecoverable.
    # Mirrors claude_code.py's .claude.log.
    try:
        log_suffix = f"_r{run_idx}" if run_idx is not None else ""
        (Path(result_dir) / f"{task_id}{log_suffix}.codex.log").write_text(
            f"=== exit={code} timed_out={timed_out} steps={len(trajectory)} "
            f"attempts={tried}/{attempts} "
            f"last_message_file={'written' if output else 'empty'} ===\n"
            f"--- STDERR ---\n{stderr[-8000:]}\n"
            f"--- STDOUT head 2k ---\n{stdout[:2000]}\n",
            encoding="utf-8")
    except Exception:
        pass

    budget_hit = max_steps > 0 and seen[0] >= max_steps

    error = None
    if timed_out:
        error = f"harness timeout after {base.DEFAULT_TIMEOUT}s"
    elif budget_hit:
        # NOT an error. Every task is solvable inside the step budget, so spending all of it is
        # the agent's own result and gets graded on whatever state it left behind — exactly like
        # an agent that finished and got things wrong. Checked before `code` because the budget
        # kill is a signal (-9), not a crash.
        error = None
    elif code != 0:
        # Deliberately NOT gated on empty output. codex writes --output-last-message only on a
        # clean finish, so a mid-run death leaves `output` holding whatever narration happened
        # to be the last agent_message — an intermediate "I'm now adding..." sentence. Treating
        # that as a completed run turned 6 agriculture tasks that had done real work (8-38
        # browser artifacts, up to 19 steps) into silent 0.000s on 2026-09-02, indistinguishable
        # from an agent that tried and got everything wrong.
        error = (f"{binary} exited {code} after {len(trajectory)} step(s) "
                 f"(last_message {'written' if output else 'unwritten'}): {stderr[-400:]}")
    elif not trajectory:
        # Zero tool calls: the MCP server never registered, so the model had no browser and
        # said so in prose. That prose is non-empty, so no branch above fires.
        error = (f"agent took no action — {binary} reported: "
                 f"{output.strip()[:300] or 'no output'}")

    # `success` is the agent's own claim of completion, which a budget-capped run never made.
    base.finalize_trajectory(
        trajectory, error is None and bool(output) and not budget_hit, output)
    payload = {
        "task_id": task_id,
        "status": "completed" if error is None else "error",
        "agent_output": output,
        "trajectory": trajectory,
        # budget_hit / tool_calls are recorded so a capped run stays identifiable in analysis
        # even though it is not an error — otherwise it is indistinguishable from a clean finish.
        "harness": {"cli": binary, "model": model,
                    "duration_s": round(time.time() - t0, 1), "exit_code": code,
                    "tool_calls": seen[0], "budget_hit": budget_hit},
    }
    if error:
        payload["error"] = error
    print(f"  {tag} {binary}: {payload['status']} steps={len(trajectory)} "
          f"in {payload['harness']['duration_s']}s"
          + (f" [budget spent: {seen[0]}/{max_steps}]" if budget_hit else ""), flush=True)
    return base.write_agent_result(result_dir, task_id, run_idx, payload)
