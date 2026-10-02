"""Shared plumbing for CLI-agent harnesses.

The pieces every harness needs, kept in one place so a new CLI is only "build argv +
parse its event stream":

- per-task workdir under ``$SAAS_BENCH_TMP`` (todo.md seeded, browser output dir inside)
- the Playwright MCP server spec (pinned version, ``--headless --isolated --caps vision``)
- prompt composition (task prompt + todo + input files + browser-only guidance)
- subprocess driver with a hard wall-clock timeout (kills the whole process group —
  the CLI, its MCP server, and the chromium underneath)
- result-file writer + trajectory normalization matching what ``reporting.py`` reads:
  steps of ``{"actions": [{name: payload}], "results": [{"error", "is_done"}]}``, with
  ``navigate`` / ``done`` action names carrying their special meaning.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Callable, Optional

_TMP_BASE = os.environ.get(
    "SAAS_BENCH_TMP", os.path.join(tempfile.gettempdir(), "saas_bench")
)
os.makedirs(_TMP_BASE, exist_ok=True)

# Pinned so 30 concurrent `npx` spawns resolve from the warm cache instead of racing the
# registry; bump deliberately. --prefer-offline skips the registry round-trip once cached.
PLAYWRIGHT_MCP_VERSION = os.environ.get("SAAS_PLAYWRIGHT_MCP_VERSION", "0.0.79")

# Overall wall-clock budget per task for the CLI process (seconds). max_steps caps turns
# where the CLI supports it; this is the backstop for CLIs (codex) that have no turn cap.
DEFAULT_TIMEOUT = int(os.environ.get("SAAS_HARNESS_TIMEOUT", "2400"))

GUIDANCE = """
## Harness rules

- You are operating a real web application. Complete the task **through the web UI**
  using the Playwright browser tools (navigate, click, type, select, ...). Do not try to
  call the application's HTTP API directly or bypass the UI.
- You have vision: when a page's state is unclear, or after a significant action (form
  submit, save, delete), take a browser screenshot and look at it to confirm what
  actually happened before moving on.
- A `todo.md` with the task's steps may exist in your working directory. Keep it updated
  as you complete steps.
- Login credentials, URLs and all task requirements are in the task description below.
  Only visit the application URLs listed there.
- When the task asks you to report or extract information, put the answer in your final
  message.
""".strip()


def make_workdir(task_id: str) -> Path:
    workdir = Path(_TMP_BASE) / f"harness_{task_id}_{os.getpid()}_{int(time.time())}"
    (workdir / "browser").mkdir(parents=True, exist_ok=True)
    return workdir


def _playwright_launcher() -> tuple[str, list[str]]:
    """(command, leading args) for the pinned Playwright MCP — installed binary preferred.

    `npx` re-resolves the package on every spawn. That is cheap alone (~0.6s) but at 25
    workers on a loaded box enough spawns miss the CLI's MCP startup deadline that the
    server never registers — and codex does not fail loudly when that happens: it carries
    on with no browser tools, so the model emits a single planning message and ends the
    turn. Observed on 2026-09-02: 11 of the first 15 tasks "completed" in under 20s with
    steps<=2 and an empty browser/ dir, scored 0, and were filed as status=completed.

    Install once so this path is taken:
        npm i -g @playwright/mcp@0.0.79        # match PLAYWRIGHT_MCP_VERSION
    Override the resolved path with $SAAS_PLAYWRIGHT_MCP_BIN.
    """
    candidates = [
        os.environ.get("SAAS_PLAYWRIGHT_MCP_BIN", "").strip(),
        shutil.which("playwright-mcp") or "",
        str(Path(os.environ.get("NPM_CONFIG_PREFIX") or Path.home() / ".npm-global")
            / "bin" / "playwright-mcp"),
    ]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            return candidate, []
    # Correct but slow to start under concurrency; keeps a fresh checkout working.
    return "npx", ["-y", "--prefer-offline", f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"]


def playwright_mcp_bin() -> str:
    """The MCP server's executable — pair with playwright_mcp_args() for the same launcher."""
    return _playwright_launcher()[0]


def playwright_mcp_args(workdir: Path) -> list[str]:
    return [
        *_playwright_launcher()[1],
        # explicit chromium: the default channel is system Chrome, which isn't installed
        # here — agents "creatively" work around that if we let them
        "--browser", "chromium",
        "--headless", "--isolated", "--caps", "vision",
        "--output-dir", str(workdir / "browser"),
    ]


def compose_prompt(prompt: str, todo_md: Optional[str], input_files: Optional[list[str]]) -> str:
    parts = [GUIDANCE]
    if input_files:
        listing = "\n".join(f"- {p}" for p in input_files)
        parts.append(
            "## Input files\n\nThe task references these local files "
            f"(read/view them as needed):\n{listing}"
        )
    parts.append(prompt.strip())
    if todo_md:
        parts.append(f"## Pre-filled todo.md (also written to your working directory)\n\n{todo_md.strip()}")
    return "\n\n".join(parts) + "\n"


async def run_cli(
    argv: list[str],
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT,
    env_extra: Optional[dict] = None,
    stdin_text: Optional[str] = None,
    on_line: Optional[Callable[[str], object]] = None,
) -> tuple[int, str, str, bool]:
    """Run the CLI, return (returncode, stdout, stderr, timed_out).

    ``on_line`` is invoked for every complete stdout line as it arrives, which is what
    gives the run live per-step progress instead of silence until the task ends. Returning
    a truthy value from it stops the CLI immediately (same process-group kill as a timeout)
    — that is how a harness enforces a step budget on a CLI that has no turn cap of its own.
    The caller owns the counter, so it already knows why it asked to stop; nothing about the
    reason is reported back here.

    stdout/stderr are drained CONCURRENTLY in fixed-size chunks. Draining only one pipe
    would deadlock once the other fills, and chunked reads (rather than ``readline``)
    avoid asyncio's 64 KiB line-length limit — a single stream-json line carrying a
    base64 screenshot is far larger than that.

    start_new_session=True puts the CLI in its own process group so a timeout kill also
    takes down its MCP server and chromium instead of leaking them across slots.
    """
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )

    out_chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    stop_requested = False

    async def pump(reader, sink: list, cb: Optional[Callable[[str], object]]) -> None:
        nonlocal stop_requested
        buf = b""
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            sink.append(chunk)
            if cb is None:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                try:
                    if cb(raw.decode(errors="replace")):
                        stop_requested = True
                except Exception:
                    pass  # a progress printer must never break the run
            if stop_requested:
                return  # reap() in the caller's finally takes the process group down

    if stdin_text is not None and proc.stdin is not None:
        proc.stdin.write(stdin_text.encode())
        await proc.stdin.drain()
        proc.stdin.close()

    timed_out = False
    pumps = asyncio.gather(
        pump(proc.stdout, out_chunks, on_line),
        pump(proc.stderr, err_chunks, None),
    )

    def reap() -> None:
        """Kill the CLI's whole process group.

        Called on EVERY exit path, not just on timeout. The tree is
        CLI -> npx -> node (MCP server) -> chromium (+~10 helpers), and the CLI exiting
        does not take its descendants with it: without this, each finished task leaves an
        MCP server and a full browser running for the rest of the run.
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    async def _wait_or_stop() -> None:
        """Return as soon as the CLI exits OR on_line asks to stop, whichever is first."""
        waiter = asyncio.ensure_future(proc.wait())
        try:
            while True:
                done, _ = await asyncio.wait({waiter}, timeout=0.25)
                if done or stop_requested:
                    return
        finally:
            if not waiter.done():
                waiter.cancel()

    try:
        # Time out on PROCESS EXIT, never on pipe EOF: chromium inherits the CLI's stdout,
        # so the pipes stay open long after the CLI is gone and waiting for EOF would hang
        # the task until the wall clock ran out.
        await asyncio.wait_for(_wait_or_stop(), timeout=timeout)
        if not stop_requested:
            # Give the pumps a moment to drain a CLI that exited on its own. Skipped when we
            # asked to stop: the stopping condition was evaluated on output we already have,
            # and the CLI is still alive holding stderr, so this would burn the full 10s.
            try:
                await asyncio.wait_for(asyncio.shield(pumps), timeout=10)
            except asyncio.TimeoutError:
                pass  # descendants still hold the pipe; reap() below releases it
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        reap()
        pumps.cancel()
        # Await the cancelled gather so its CancelledError is retrieved; otherwise asyncio
        # prints "_GatheringFuture exception was never retrieved" for every stopped task.
        try:
            await pumps
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await proc.wait()
        except Exception:
            pass
    return (
        proc.returncode or 0,
        b"".join(out_chunks).decode(errors="replace"),
        b"".join(err_chunks).decode(errors="replace"),
        timed_out,
    )


# Fields worth showing in a progress line, most-informative first.
_HINT_KEYS = ("url", "text", "element", "selector", "ref", "filename", "path",
              "command", "query", "key", "values", "name")


def progress_line(tag: str, n: int, action: str, payload) -> str:
    """One-line live progress, in the spirit of browser-use's per-step log."""
    hint = ""
    if isinstance(payload, dict):
        for k in _HINT_KEYS:
            v = payload.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                hint = " ".join(str(v).split())[:70]
                break
    elif isinstance(payload, str):
        hint = " ".join(payload.split())[:70]
    return f"  {tag} #{n} {action}" + (f"  → {hint}" if hint else "")


_PLAYWRIGHT_TOOL_PREFIXES = ("mcp__playwright__browser_", "browser_")


def normalize_action_name(tool_name: str) -> str:
    """`mcp__playwright__browser_navigate` -> `navigate` so reporting.py's action
    distribution and navigate-discipline metrics stay meaningful; non-browser tools
    keep their own name."""
    for prefix in _PLAYWRIGHT_TOOL_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return tool_name.removeprefix("mcp__playwright__")


def step(action_name: str, payload, error: Optional[str] = None) -> dict:
    return {
        "actions": [{action_name: payload}],
        "results": [{"error": error, "is_done": False}],
    }


def finalize_trajectory(trajectory: list[dict], success: bool, output: str) -> list[dict]:
    """Append the synthetic `done` step reporting.py's confidence matrix keys on."""
    trajectory.append({
        "actions": [{"done": {"success": success, "text": (output or "")[:2000]}}],
        "results": [{"error": None, "is_done": True}],
    })
    return trajectory


def write_agent_result(result_dir: str, task_id: str, run_idx: Optional[int], payload: dict) -> dict:
    suffix = f"_r{run_idx}" if run_idx is not None else ""
    Path(result_dir).mkdir(parents=True, exist_ok=True)
    out = Path(result_dir) / f"{task_id}{suffix}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
