"""Qwen Code harness: `--agent-module saas_bench.harness.qwen_code`.

Drives `qwen -p` headlessly with the Playwright MCP browser (vision caps on). Qwen Code's
stream-json events are the same shape claude-code emits — `{"type":"assistant","message":
{"content":[{"type":"tool_use",...}]}}` plus a trailing `{"type":"result",...}` — so the
trajectory parser is reused from claude_code rather than duplicated.

Differences from claude_code that shape this file:
  * auth must be selected explicitly (`--auth-type openai`), otherwise a non-interactive run
    exits with "No auth type is selected" and does nothing. Credentials come from
    OPENAI_BASE_URL / OPENAI_API_KEY, defaulting to $LLM_BASE_URL / $LLM_API_KEY.
  * no --max-turns, so the step budget is enforced here the way codex.py does it: the progress
    callback returns True at max_steps and base.run_cli kills the process group.
  * the system prompt goes through --append-system-prompt instead of being prepended to the
    user prompt.
  * `run_shell_command` ships in the default toolset, so --allowed-tools is a hard whitelist —
    without it the agent can curl the app's API straight past the UI.
  * qwen halts a turn at 100 tool calls unless $QWEN_HOME/settings.json lifts the cap, which in
    turn rules out --bare (it discards settings entirely). See _write_settings.

Config is isolated per task via $QWEN_HOME so concurrent slots never share session state.
Model: run.py's --model passed through, override with $SAAS_QWEN_MODEL.
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
from saas_bench.harness.claude_code import _parse_stream_json

# Playwright's MCP tools plus the file tools the todo.md workflow and multimodal inputs need.
_ALLOWED_TOOLS = [
    "read_file", "read_many_files", "write_file", "edit", "ls", "glob",
    "playwright",  # the MCP server name covers all of its browser_* tools
]

# --allowed-tools alone does NOT restrict anything: the CLI documents it as "will bypass
# confirmation", and under --yolo everything is auto-approved anyway. Verified on 0.23.0 — a probe
# run with only the list above still called run_shell_command twice. --exclude-tools is the real
# switch, so the shell has to be named here explicitly or the agent can curl the app's API
# straight past the UI, which is the one thing every task in this suite forbids.
# (claude_code.py has the same split: --allowedTools proposes, --disallowedTools decides.)
_EXCLUDED_TOOLS = [
    "run_shell_command", "web_fetch", "web_search",
    "notebook_edit", "task", "agent",
]


# Qwen Code halts a turn at 100 tool calls by default, which silently capped 52 of 106 tasks on
# 2026-09-05 (exit 1, empty stderr, no `result` event — indistinguishable from a crash). The rule
# lives in LoopDetectionService.shouldHaltOnTurnToolCallCap:
#
#     if (totalCalls <= cap) return false;              // cap = DEFAULT_MAX_TOOL_CALLS_PER_TURN = 100
#     const stuck = maxKeyRepeat >= GLOBAL_DUPLICATE_THRESHOLD;   // 6
#     return isExplicitCap || totalCalls > cap * 10 || stuck;
#
# Past 100 calls an *unset* cap is "adaptive": it halts as soon as any one (tool, args) key has been
# seen 6+ times in the turn. A browser agent re-snapshots the same page with identical arguments
# far more than 6 times, so the adaptive escape hatch never applies and the halt lands at exactly
# 100 — and STATEFUL_READ_TOOLS, the one exemption, contains only `task_list`.
#
# 0 disables the cap outright (Config.getMaxToolCallsPerTurn returns +Infinity), leaving this
# harness's own counter as the single authority on the budget, the same as codex.py and agent.py.
# Any positive value would instead be treated as an *explicit* hard cap — a second, redundant
# stopping rule racing our own. skipLoopDetection is already the default in this build but is
# pinned here because the other detectors are far more aggressive than a browser workload allows
# (6 duplicate calls anywhere in a turn, 5 consecutive identical ones).
#
# This file is why --bare is NOT passed. Bare mode does not merely gate individual keys, it
# replaces the whole settings object:
#     const settings = isBareMode(argv.bare) ? createMinimalSettings() : loadSettings();
# and createMinimalSettings() returns literally `{}`, so settings.json is never read and the cap
# stays at its default. Verified empirically: with --bare and this file in place, agriculture_007
# still halted at exactly 100 calls. There is no env-var override for the cap and no CLI flag.
#
# Dropping --bare is safe here because every scope loadSettings() reads is either absent or ours:
#   system / system-defaults  absent on this host
#   user                      $QWEN_HOME/settings.json — this file, fresh per task
#   workspace                 <workdir>/.qwen/settings.json — absent, workdir is a fresh temp dir
# In particular the real ~/.qwen/settings.json is bypassed because getUserSettingsPath() resolves
# through QWEN_HOME. The four defaults that bare mode was suppressing are pinned below instead;
# LSP needs --experimentalLsp on top of non-bare, which is not passed.
def _write_settings(qwen_home: Path) -> None:
    (qwen_home / "settings.json").write_text(json.dumps({
        "model": {"maxToolCallsPerTurn": 0, "skipLoopDetection": True},
        # Off by default only under --bare. Auto-memory and auto-dream spend extra model calls and
        # write durable state that would leak between tasks through a shared QWEN_HOME; hooks are
        # disabled because a benchmark run must not execute anything the task did not ask for.
        "memory": {"enableManagedAutoMemory": False, "enableManagedAutoDream": False,
                   "enableTeamMemory": False, "enableTeamMemorySync": False,
                   "enableAutoSkill": False},
        "disableAllHooks": True,
        "telemetry": {"enabled": False},
    }, indent=2), encoding="utf-8")


def _mcp_config(workdir: Path) -> str:
    return json.dumps({
        "mcpServers": {
            "playwright": {
                "command": base.playwright_mcp_bin(),
                "args": base.playwright_mcp_args(workdir),
            }
        }
    })


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
    binary = os.environ.get("SAAS_QWEN_BIN", "")
    if not binary:
        # Not on PATH in this deployment: npm's global bin dir is not exported.
        candidates = [shutil.which("qwen") or "",
                      str(Path(os.environ.get("NPM_CONFIG_PREFIX")
                               or Path.home() / ".npm-global") / "bin" / "qwen")]
        binary = next((c for c in candidates if c and os.access(c, os.X_OK)), "qwen")
    model = os.environ.get("SAAS_QWEN_MODEL") or model_name

    workdir = base.make_workdir(task_id)
    if todo_md:
        (workdir / "todo.md").write_text(todo_md, encoding="utf-8")
    full_prompt = base.compose_prompt(prompt, todo_md, input_files)

    if not os.access(binary, os.X_OK) and not shutil.which(binary):
        return base.write_agent_result(result_dir, task_id, run_idx, {
            "task_id": task_id, "status": "error", "agent_output": "",
            "trajectory": [], "error": f"{binary}: binary not found",
        })

    qwen_home = workdir / "qwen_home"
    qwen_home.mkdir(parents=True, exist_ok=True)
    _write_settings(qwen_home)
    env_extra = {
        "QWEN_HOME": str(qwen_home),
        # The CLI refuses to run headless with --yolo unless this is set; the warning it prints
        # otherwise lands in stderr and buries anything useful there.
        "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        "OPENAI_BASE_URL": os.environ.get("OPENAI_BASE_URL")
                           or os.environ.get("LLM_BASE_URL", ""),
        "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY")
                          or os.environ.get("LLM_API_KEY", ""),
    }

    argv = [
        binary,
        # No --bare: it would discard $QWEN_HOME/settings.json wholesale and with it the
        # tool-call-cap lift. See _write_settings for why that is safe and what replaces it.
        "--yolo",                    # non-interactive: nothing can answer an approval prompt
        "--auth-type", "openai",
        "-o", "stream-json",
        "--mcp-config", _mcp_config(workdir),
        "--allowed-tools", *_ALLOWED_TOOLS,
        "--exclude-tools", *_EXCLUDED_TOOLS,
        "--append-system-prompt", base.GUIDANCE,
    ]
    if model:
        argv += ["-m", model]
    argv += ["-p", full_prompt]

    # Live per-step progress, and the step budget. qwen has no turn cap of its own, so without
    # the second half a task would run until the wall clock and get several times the budget the
    # other harnesses enforce, making a cross-harness score comparison meaningless.
    seen = [0]

    def _progress(line: str) -> bool:
        line = line.strip()
        if not line.startswith("{"):
            return False
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return False
        if ev.get("type") != "assistant":
            return False
        for item in (ev.get("message", {}).get("content") or []):
            if isinstance(item, dict) and item.get("type") == "tool_use":
                seen[0] += 1
                print(base.progress_line(tag, seen[0],
                                         base.normalize_action_name(item.get("name", "?")),
                                         item.get("input")), flush=True)
        return max_steps > 0 and seen[0] >= max_steps

    # Retry a turn that never called a tool. Seen on every CLI in this suite: the upstream
    # answers a tool-use turn with plain prose and finish_reason=stop, so the run "completes" in
    # seconds having done nothing. A run that made even one tool call is a real attempt and its
    # result stands, however bad.
    attempts = max(1, int(os.environ.get("SAAS_QWEN_RETRIES", "5")) + 1)
    t0 = time.time()
    tried = 0
    code, stdout, stderr, timed_out = -1, "", "", False
    trajectory, output, success, num_turns = [], "", False, 0
    for attempt in range(1, attempts + 1):
        tried = attempt
        seen[0] = 0
        code, stdout, stderr, timed_out = await base.run_cli(
            argv, cwd=workdir, env_extra=env_extra, on_line=_progress)
        trajectory, output, success, num_turns = _parse_stream_json(stdout)
        if trajectory or timed_out or attempt == attempts:
            break
        delay = min(15 * attempt * (0.5 + random.random()), 90)
        print(f"  {tag} qwen: no tool call (attempt {attempt}/{attempts - 1}), "
              f"retrying in {delay:.0f}s", flush=True)
        await asyncio.sleep(delay)

    # qwen reports a loop-detection halt in its own stream, not on stderr, and never emits the
    # `result` event — so an internally-capped run looks exactly like a crash: exit 1, empty
    # stderr, empty agent_output. The tail is what carries the reason, and keeping only the head
    # is what hid the 100-call cap across all 105 tasks on 2026-09-05.
    loop_halt = any(s in stdout[-20000:].lower() for s in
                    ("loop detection halted", "turn_tool_call_cap", "loop_detected"))

    try:
        log_suffix = f"_r{run_idx}" if run_idx is not None else ""
        # write_agent_result creates result_dir, but it runs *after* this — so without the mkdir
        # the first task to finish in a run loses its log to a swallowed FileNotFoundError (the
        # 2026-09-05 run left 105 results and 104 logs), and a single-task probe loses it always.
        Path(result_dir).mkdir(parents=True, exist_ok=True)
        (Path(result_dir) / f"{task_id}{log_suffix}.qwen.log").write_text(
            f"=== exit={code} timed_out={timed_out} steps={len(trajectory)} "
            f"tool_calls={seen[0]} turns={num_turns} attempts={tried}/{attempts} "
            f"loop_halt={loop_halt} ===\n"
            f"--- STDERR ---\n{stderr[-8000:]}\n"
            f"--- STDOUT head 2k ---\n{stdout[:2000]}\n"
            f"--- STDOUT tail 8k ---\n{stdout[-8000:]}\n",
            encoding="utf-8")
    except Exception:
        pass

    budget_hit = max_steps > 0 and seen[0] >= max_steps

    error = None
    if timed_out:
        error = f"harness timeout after {base.DEFAULT_TIMEOUT}s"
    elif loop_halt and not budget_hit:
        # Loud on purpose: this means _write_settings did not take effect, and the run silently
        # got a fraction of the budget every other harness gets. Never grade it as a real attempt.
        error = (f"qwen halted itself by loop detection at {seen[0]} tool call(s) — "
                 f"model.maxToolCallsPerTurn=0 in $QWEN_HOME/settings.json did not apply")
    elif budget_hit:
        # NOT an error, and checked before `code` because the budget stop is a process-group kill
        # (signal), not a crash. Every task is solvable inside the budget, so spending all of it
        # is the agent's own result and is graded on whatever state it left. Same rule as
        # codex.py / claude_code.py / kimi_code.py.
        error = None
    elif code != 0 and not output:
        error = f"{binary} exited {code} after {tried} attempt(s): {stderr[-400:]}"
    elif not trajectory:
        error = (f"agent took no action after {tried} attempt(s) — {binary} reported: "
                 f"{output.strip()[:300] or stderr[-300:] or 'nothing'}")

    # `success` is the agent's own claim of completion, which a budget-capped run never made.
    base.finalize_trajectory(
        trajectory, success and error is None and not budget_hit, output)
    payload = {
        "task_id": task_id,
        "status": "completed" if error is None else "error",
        "agent_output": output,
        "trajectory": trajectory,
        "harness": {"cli": "qwen-code", "model": model, "num_turns": num_turns,
                    "duration_s": round(time.time() - t0, 1), "exit_code": code,
                    "tool_calls": seen[0], "budget_hit": budget_hit},
    }
    if error:
        payload["error"] = error
    print(f"  {tag} qwen: {payload['status']} steps={len(trajectory)} "
          f"in {payload['harness']['duration_s']}s"
          + (f" [budget spent: {seen[0]}/{max_steps}]" if budget_hit else ""), flush=True)
    return base.write_agent_result(result_dir, task_id, run_idx, payload)
