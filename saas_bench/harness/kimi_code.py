"""Kimi Code harness: `--agent-module saas_bench.harness.kimi_code`.

IMPORTANT: this drives **kimi-cli** (the Python CLI, `~/.local/bin/kimi`, `kimi --version`
== "kimi, version 1.x"), NOT the npm `@moonshot-ai/kimi-code` (Node). The Node build has an
unbounded-context / V8-heap OOM that SIGABRTs on real browser tasks; kimi-cli is a Python
process and does not. This mirrors the proven /nvme3/keanshi/SaaS/QC runner.

Per task it writes an isolated KIMI_SHARE_DIR (config.toml with the relay provider +
[loop_control], an agent spec that excludes every non-browser builtin tool, and an
mcp.json for Playwright), then runs:
  kimi --print --output-format stream-json --yolo --work-dir <ws> --model <m>
       --agent-file <spec> --mcp-config-file <mcp> --max-steps-per-turn N --prompt <p>

Env knobs (all optional):
  SAAS_KIMI_MODEL         (default "kimi-k3")
  SAAS_KIMI_BASE_URL      (default $LLM_BASE_URL, with "/v1" appended if absent)
  SAAS_KIMI_API_KEY       (default $LLM_API_KEY)
  SAAS_KIMI_BIN           (default "kimi", resolved via PATH then ~/.local/bin/kimi)
  SAAS_KIMI_MAX_STEPS     (default 400)
  SAAS_KIMI_TOOL_TIMEOUT  (per-MCP-call seconds, default 300)
  SAAS_CHROME_EXECUTABLE  (default: newest chromium under ~/.cache/ms-playwright)
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
import subprocess
import time
from pathlib import Path

from saas_bench.harness import base

# kimi-cli exits 75 (EX_TEMPFAIL) for RETRYABLE upstream failures — 429 rate limit, 5xx,
# connection timeouts — explicitly so an eval runner can decide to retry (its CHANGELOG
# calls this out). Exit 1 means a permanent failure (bad auth/config) and is NOT retried.
_EX_TEMPFAIL = 75

# Exit 1 is documented as permanent, but in practice it also carries transient upstream and MCP
# failures, so the retry decision reads stderr as well as the exit code. These signatures are
# matched case-insensitively against the stderr tail plus the stdout head.
_RATE_LIMIT_SIGNS = ("429", "rate limit", "rate_limit", "too many requests",
                     "overloaded", "503", "502", "connection error", "connection closed")

_SYSTEM_PROMPT = (
    "You are completing a SaaS-Bench task in isolated local applications. Use only "
    "Playwright MCP browser tools for application interaction. Do not use shell commands, "
    "filesystem inspection, Docker, databases, task metadata, verifier code, benchmark "
    "repositories, application source code, web search, or sub-agents. Only access the "
    "runtime application URLs and explicitly provided local input-file URLs. Complete the "
    "task in the applications, then briefly report what you did. Never wait inside "
    "browser_evaluate for a UI confirmation dialog or another user interaction; trigger "
    "the action, return control, then handle dialogs with a separate tool call."
)

# kimi-cli builtin tools to exclude so the agent can ONLY use Playwright (browser_*).
_EXCLUDED_BUILTIN_TOOLS = [
    "kimi_cli.tools.agent:Agent",
    "kimi_cli.tools.shell:Shell",
    "kimi_cli.tools.background:TaskList",
    "kimi_cli.tools.background:TaskOutput",
    "kimi_cli.tools.background:TaskStop",
    "kimi_cli.tools.file:ReadFile",
    "kimi_cli.tools.file:ReadMediaFile",
    "kimi_cli.tools.file:Glob",
    "kimi_cli.tools.file:Grep",
    "kimi_cli.tools.file:WriteFile",
    "kimi_cli.tools.file:StrReplaceFile",
    "kimi_cli.tools.web:SearchWeb",
    "kimi_cli.tools.web:FetchURL",
    "kimi_cli.tools.plan:ExitPlanMode",
    "kimi_cli.tools.plan.enter:EnterPlanMode",
    "kimi_cli.tools.ask_user:AskUserQuestion",
    "kimi_cli.tools.todo:SetTodoList",
]


def _resolve_endpoint() -> tuple[str, str]:
    base_url = os.environ.get("SAAS_KIMI_BASE_URL")
    if not base_url:
        root = (os.environ.get("LLM_BASE_URL") or "").rstrip("/")
        base_url = f"{root}/v1" if root else ""
    if base_url and not base_url.rstrip("/").endswith("/v1"):
        base_url = base_url.rstrip("/") + "/v1"
    api_key = os.environ.get("SAAS_KIMI_API_KEY") or os.environ.get("LLM_API_KEY", "")
    return base_url, api_key


def _kimi_bin() -> str | None:
    b = os.environ.get("SAAS_KIMI_BIN", "kimi")
    return shutil.which(b) or (str(Path.home() / ".local/bin/kimi")
                               if (Path.home() / ".local/bin/kimi").exists() else None)


def _builtin_agent_yaml(kimi_bin: str) -> str:
    """Locate kimi-cli's builtin default agent.yaml (to extend, not replace)."""
    py = Path(kimi_bin).resolve().parent / "python"
    r = subprocess.run(
        [str(py), "-c",
         "import kimi_cli, os; print(os.path.join(os.path.dirname(kimi_cli.__file__), 'agents', 'default', 'agent.yaml'))"],
        capture_output=True, text=True, timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"cannot locate builtin kimi agent spec: {r.stderr.strip()[:200]}")
    return r.stdout.strip()


def _chrome_executable() -> str:
    c = os.environ.get("SAAS_CHROME_EXECUTABLE")
    if c:
        return c
    root = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                                str(Path.home() / ".cache/ms-playwright")))
    cands = sorted(root.glob("chromium-*/chrome-linux64/chrome"), reverse=True)
    if not cands:
        raise FileNotFoundError(f"no Chromium under {root}")
    return str(cands[0].resolve())


def _write_config(home: Path, model: str, base_url: str, api_key: str,
                  max_steps: int, tool_timeout_ms: int) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(
        f'''default_model = {json.dumps(model)}
telemetry = false

[providers.saas_gateway]
type = "openai_legacy"
base_url = {json.dumps(base_url)}
api_key = {json.dumps(api_key)}

[models.{json.dumps(model)}]
provider = "saas_gateway"
model = {json.dumps(model)}
max_context_size = 262144
capabilities = ["image_in"]

[loop_control]
max_steps_per_turn = {max_steps}

[mcp.client]
tool_call_timeout_ms = {tool_timeout_ms}
''',
        encoding="utf-8",
    )
    (home / "config.toml").chmod(0o600)


def _write_agent_spec(path: Path, builtin_agent_yaml: str) -> None:
    import yaml
    spec = {
        "version": 1,
        "agent": {
            "extend": builtin_agent_yaml,
            "system_prompt_args": {"ROLE_ADDITIONAL": _SYSTEM_PROMPT},
            "exclude_tools": _EXCLUDED_BUILTIN_TOOLS,
            "subagents": None,
        },
    }
    path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _write_mcp(path: Path, out_dir: Path) -> None:
    # Share base's launcher instead of spawning `npx -y @playwright/mcp@latest`. Two problems
    # with the old form: `@latest` re-resolved the version from the registry on every spawn (no
    # pin, no --prefer-offline), and at 25 workers enough of those round-trips failed that
    # kimi-cli aborted the whole run with
    #   MCPRuntimeError: Failed to connect MCP servers: {'playwright': McpError('Connection closed')}
    # — 15 of 15 tasks died that way on 2026-09-03, each in 3-23s with zero steps. base resolves
    # a pinned, already-installed binary (npm i -g @playwright/mcp@0.0.79) and only falls back to
    # npx when that is absent, so all three harnesses now start the same server the same way.
    args = list(base.playwright_mcp_args(out_dir.parent))
    # kimi-cli needs Chromium pointed at explicitly; the other CLIs resolve it themselves.
    args += ["--executable-path", _chrome_executable()]
    path.write_text(json.dumps({
        "mcpServers": {
            "playwright": {
                "command": base.playwright_mcp_bin(),
                "args": args,
            }
        }
    }, indent=2), encoding="utf-8")


def _parse_stream_json(stdout: str) -> tuple[list[dict], str, bool]:
    trajectory: list[dict] = []
    pending: dict = {}
    final_text, success = "", False
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        role = ev.get("role") or ev.get("type")
        if role in ("assistant", "Assistant"):
            calls = ev.get("tool_calls") or []
            if calls:
                for tc in calls:
                    fn = tc.get("function") or tc
                    name = fn.get("name") or tc.get("name") or "?"
                    raw = fn.get("arguments") if isinstance(fn, dict) else None
                    if isinstance(raw, str):
                        try:
                            raw = json.loads(raw)
                        except json.JSONDecodeError:
                            raw = {"_raw": raw}
                    step = base.step(base.normalize_action_name(name), raw or {})
                    trajectory.append(step)
                    tcid = tc.get("id") or tc.get("tool_call_id")
                    if tcid:
                        pending[tcid] = step
            else:
                content = ev.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                if content:
                    final_text, success = content, True
        elif role in ("tool", "Tool"):
            tcid = ev.get("tool_call_id") or ev.get("id")
            step = pending.pop(tcid, None) if tcid else (trajectory[-1] if trajectory else None)
            if step is not None and (ev.get("is_error") or ev.get("status") == "error"):
                c = ev.get("content")
                step["results"][0]["error"] = str(c)[:500] if c else "tool error"
    return trajectory, final_text, success


async def run_task(
    task: dict,
    model_name: str,
    prompt: str,
    result_dir: str,
    max_steps: int = 400,
    slot_id=None,
    todo_md=None,
    run_idx=None,
    input_files=None,
) -> dict:
    task_id = task["task_id"]
    tag = f"[slot {slot_id}][{task_id}_r{run_idx}]"
    model = os.environ.get("SAAS_KIMI_MODEL") or (model_name if model_name else "kimi-k3")
    base_url, api_key = _resolve_endpoint()
    kimi_bin = _kimi_bin()

    workdir = base.make_workdir(task_id)
    if todo_md:
        (workdir / "todo.md").write_text(todo_md, encoding="utf-8")
    full_prompt = base.compose_prompt(prompt, todo_md, input_files)

    def _err(msg):
        return base.write_agent_result(result_dir, task_id, run_idx, {
            "task_id": task_id, "status": "error", "agent_output": "",
            "trajectory": [], "error": msg})

    if not kimi_bin:
        return _err("kimi-cli not found (set SAAS_KIMI_BIN or install ~/.local/bin/kimi)")
    if not base_url or not api_key:
        return _err("kimi endpoint unresolved (set SAAS_KIMI_BASE_URL/SAAS_KIMI_API_KEY, or LLM_BASE_URL/LLM_API_KEY)")

    kimi_home = workdir / "kimi_home"
    kimi_max = int(os.environ.get("SAAS_KIMI_MAX_STEPS", str(max_steps or 400)))
    tool_ms = int(os.environ.get("SAAS_KIMI_TOOL_TIMEOUT", "300")) * 1000
    try:
        _write_config(kimi_home, model, base_url, api_key, kimi_max, tool_ms)
        agent_spec = kimi_home / "agent.yaml"
        _write_agent_spec(agent_spec, _builtin_agent_yaml(kimi_bin))
        mcp_path = kimi_home / "mcp.json"
        _write_mcp(mcp_path, workdir / "browser")
    except Exception as e:
        return _err(f"kimi setup failed: {type(e).__name__}: {e}")

    argv = [
        kimi_bin, "--print", "--output-format", "stream-json", "--yolo",
        "--work-dir", str(workdir),
        "--model", model,
        "--agent-file", str(agent_spec),
        "--mcp-config-file", str(mcp_path),
        "--max-steps-per-turn", str(kimi_max),
        "--prompt", full_prompt,
    ]

    t0 = time.time()
    # Retry loop for upstream rate limiting. Concurrent workers all share one relay key,
    # so a burst of 429s is expected; the backoff is heavily JITTERED because a fixed
    # delay would make every worker retry in lockstep and hit the same limit again.
    attempts = int(os.environ.get("SAAS_KIMI_RETRIES", "5")) + 1
    deadline = t0 + base.DEFAULT_TIMEOUT
    log_parts: list[str] = []
    code = -1
    stdout = stderr = ""
    timed_out = False
    trajectory: list[dict] = []
    output, success = "", False

    # Live per-step progress (see claude_code): silence between "app ready" and completion
    # is indistinguishable from a hang on a multi-minute task.
    seen = [0]

    def _progress(line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            return
        if (ev.get("role") or ev.get("type")) not in ("assistant", "Assistant"):
            return
        for tc in (ev.get("tool_calls") or []):
            fn = tc.get("function") or tc
            raw = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except json.JSONDecodeError:
                    raw = {"_raw": raw}
            seen[0] += 1
            print(base.progress_line(
                tag, seen[0],
                base.normalize_action_name(fn.get("name") or tc.get("name") or "?"),
                raw), flush=True)

    for attempt in range(1, attempts + 1):
        seen[0] = 0
        code, stdout, stderr, timed_out = await base.run_cli(
            argv, cwd=workdir, env_extra={"KIMI_SHARE_DIR": str(kimi_home)},
            on_line=_progress)
        trajectory, output, success = _parse_stream_json(stdout)
        log_parts.append(
            f"=== ATTEMPT {attempt} (exit={code} timed_out={timed_out} "
            f"steps={len(trajectory)}) ===\n--- STDERR ---\n{stderr[-4000:]}\n"
            f"--- STDOUT head 2k ---\n{stdout[:2000]}\n"
        )
        # Three retryable shapes, all transient and all previously fatal because the check
        # only looked at exit 75:
        #   * exit 75          kimi's own signal for an upstream 429/5xx
        #   * MCP unreachable  kimi-cli aborts the whole run with MCPRuntimeError when the
        #                      Playwright server does not come up; 15/15 tasks died this way on
        #                      2026-09-03 (exit 1, so the old condition never fired)
        #   * zero tool calls  a rate limit or transport error surfaced as exit 1, or the model
        #                      answered a tool-use turn with plain prose — nothing was attempted,
        #                      so this is not a result worth keeping
        # A run that made even one tool call is a real attempt and stands, however bad.
        blob = (stderr[-4000:] + stdout[:2000]).lower()
        if timed_out:
            reason = None
        elif code == _EX_TEMPFAIL and not output:
            reason = "exit 75 (upstream 429/5xx)"
        elif "failed to connect mcp servers" in blob:
            reason = "MCP server unreachable"
        elif not trajectory and any(s in blob for s in _RATE_LIMIT_SIGNS):
            reason = "upstream rate limit / transport error"
        elif not trajectory:
            reason = "zero tool calls"
        else:
            reason = None
        if reason is None or attempt == attempts:
            break
        # Back off 30s/60s/120s scaled by a 0.5-1.5x jitter, but never past the deadline.
        delay = min(30 * (2 ** (attempt - 1)) * (0.5 + random.random()),
                    max(0.0, deadline - time.time() - 60))
        if delay <= 0:
            break
        print(f"  {tag} kimi: {reason}, retry {attempt}/{attempts - 1} "
              f"in {delay:.0f}s", flush=True)
        await asyncio.sleep(delay)

    try:
        suffix = f"_r{run_idx}" if run_idx is not None else ""
        (Path(result_dir) / f"{task_id}{suffix}.kimi.log").write_text(
            "\n".join(log_parts), encoding="utf-8")
    except Exception:
        pass

    budget_hit = max_steps > 0 and len(trajectory) >= max_steps

    error = None
    if budget_hit:
        # NOT an error, checked first because kimi signals a spent step budget with exit 1 —
        # indistinguishable from a hard failure by exit code alone. Every task is solvable
        # inside the budget, so burning all of it is the agent's own result and is graded on
        # whatever state it left. Kept symmetric with codex.py and claude_code.py; without this
        # 33 of 106 tasks on 2026-09-03 were filed as errors despite carrying real scores.
        error = None
    elif timed_out:
        error = f"harness timeout after {base.DEFAULT_TIMEOUT}s"
    elif code == _EX_TEMPFAIL and not output:
        error = (f"kimi exited 75 (upstream rate limit / 5xx) after {attempts} attempt(s) "
                 f"— lower --workers or raise SAAS_KIMI_RETRIES: {stderr[-300:]}")
    elif "failed to connect mcp servers" in (stderr[-4000:] + stdout[:2000]).lower():
        # Reported explicitly: the old message surfaced only kimi's crash footer
        # ("To resume this session: kimi -r <uuid>") because it kept only stderr[-400:], which
        # cut the real cause in half and cost an hour of digging on 2026-09-03.
        error = (f"Playwright MCP never connected after {attempts} attempt(s) — check that "
                 f"{base.playwright_mcp_bin()} is installed and lower --workers")
    elif not trajectory:
        error = (f"agent took no action after {attempts} attempt(s) — kimi reported: "
                 f"{output.strip()[:300] or stderr[-300:] or 'nothing'}")
    elif code != 0 and not output:
        error = f"kimi exited {code}: {stderr[-400:]}"

    # `success` is the agent's own claim of completion, which a budget-capped run never made.
    base.finalize_trajectory(
        trajectory, success and error is None and not budget_hit, output)
    payload = {
        "task_id": task_id,
        "status": "completed" if error is None else "error",
        "agent_output": output,
        "trajectory": trajectory,
        # budget_hit keeps a capped run identifiable in analysis even though it is not an error.
        "harness": {"cli": "kimi-cli", "model": model,
                    "duration_s": round(time.time() - t0, 1), "exit_code": code,
                    "budget_hit": budget_hit},
    }
    if error:
        payload["error"] = error
    print(f"  {tag} kimi: {payload['status']} steps={len(trajectory)} "
          f"in {payload['harness']['duration_s']}s", flush=True)
    return base.write_agent_result(result_dir, task_id, run_idx, payload)
