"""Codex CLI runner adapter for Kubernetes mode."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from playwright.async_api import async_playwright


_REPO_ROOT = Path(__file__).resolve().parents[1]


async def _run_playwright_smoke(prompt: str, max_steps: int) -> dict:
    import re
    urls = re.findall(r"http://[^\s)\]]+", prompt)
    if not urls:
        raise RuntimeError("no application URL found in prompt")
    target = urls[0]
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1000)
        title = await page.title()
        text = " ".join((await page.locator("body").inner_text()).split())[:500]
        await browser.close()
    return {
        "status": "completed",
        "target": target,
        "title": title,
        "pageText": text,
        "steps": min(1, max_steps),
        "runner": "playwright-smoke-fallback",
    }


def _codex_prompt(task: dict, prompt: str, todo_md: str, max_steps: int) -> str:
    return f"""You are executing a SaaS-Bench task.

Task ID: {task.get('task_id', 'unknown')}
Max steps: {max_steps}

Task instructions:
{prompt}

Todo:
{todo_md}

Operate the application through the provided URLs. Use Python Playwright (playwright.sync_api) from the shell to launch Chromium and interact with the page; do not rely on any built-in browser extension. If Playwright cannot be launched, report that explicitly. Do not delete Kubernetes resources. When finished, return a concise JSON object with fields:
status, summary, actions, evidence, error.
"""


def _parse_codex_events(stdout: str) -> list[dict]:
    events = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _run_codex(task: dict, prompt: str, todo_md: str, max_steps: int, result_dir: Path, run_idx: int) -> dict:
    codex = shutil.which("codex")
    if not codex:
        raise RuntimeError("codex CLI is not available on PATH")

    result_dir.mkdir(parents=True, exist_ok=True)
    task_id = task.get("task_id", "unknown")
    last_message_path = result_dir / f"{task_id}_r{run_idx}_codex_last_message.txt"
    full_prompt = _codex_prompt(task, prompt, todo_md, max_steps)
    cmd = [
        codex,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C", str(_REPO_ROOT),
        "--json",
        "-o", str(last_message_path),
        "-",
    ]
    proc = subprocess.run(
        cmd,
        input=full_prompt,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=int(os.environ.get("SAAS_CODEX_TIMEOUT", "900")),
    )
    last_message = last_message_path.read_text(encoding="utf-8").strip() if last_message_path.exists() else ""
    events = _parse_codex_events(proc.stdout)
    codex_result = None
    try:
        codex_result = json.loads(last_message) if last_message else None
    except json.JSONDecodeError:
        codex_result = None
    result_status = codex_result.get("status") if isinstance(codex_result, dict) else None
    completed = proc.returncode == 0 or result_status in {"passed", "completed", "success"}
    trajectory = []
    for event in events[:100]:
        item = event.get("item") if isinstance(event, dict) else None
        trajectory.append({
            "type": event.get("type") if isinstance(event, dict) else "event",
            "itemType": item.get("type") if isinstance(item, dict) else None,
            "status": item.get("status") if isinstance(item, dict) else None,
            "command": item.get("command") if isinstance(item, dict) else None,
        })
    return {
        "status": "completed" if completed else "error",
        "agent_output": last_message or proc.stdout,
        "codex_result": codex_result,
        "returncode": proc.returncode,
        "trajectory": trajectory,
        "stderr": proc.stderr,
        "runner": "codex-exec-playwright",
    }


async def run_task(
    task: dict,
    model_name: str,
    prompt: str,
    result_dir: str,
    max_steps: int = 400,
    slot_id: int = 0,
    todo_md: str = "",
    run_idx: int = 0,
    input_files: list[str] | None = None,
) -> dict:
    result = {
        "task_id": task.get("task_id", "unknown"),
        "status": "error",
        "agent_output": "",
        "trajectory": [],
        "runner": "codex-exec-playwright",
    }
    out_dir = Path(result_dir)
    try:
        if os.environ.get("SAAS_CODEX_DISABLE") == "1":
            execution = await _run_playwright_smoke(prompt, max_steps)
            result.update({
                "status": execution["status"],
                "agent_output": json.dumps(execution, indent=2),
                "trajectory": [{"step": 1, **execution}],
                "runner": "playwright-smoke-fallback",
            })
        else:
            execution = await asyncio.to_thread(_run_codex, task, prompt, todo_md, max_steps, out_dir, run_idx)
            result.update(execution)
            result["trajectory"] = execution.get("trajectory", execution.get("events", []))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{result['task_id']}_r{run_idx}_codex_playwright.json"
        out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception as exc:
        result["agent_output"] = f"{type(exc).__name__}: {exc}"
        result["error"] = str(exc)
    return result
