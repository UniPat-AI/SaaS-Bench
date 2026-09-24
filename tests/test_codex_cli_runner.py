import asyncio
import subprocess
from pathlib import Path

from saas_bench import codex_cli_runner


def test_codex_runner_invokes_codex_exec(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        output_path = Path(cmd[cmd.index("-o") + 1])
        output_path.write_text('{"status":"ok","summary":"done"}', encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, '{"event":1}\n', "")

    monkeypatch.setattr(codex_cli_runner.shutil, "which", lambda name: "codex")
    monkeypatch.setattr(codex_cli_runner.subprocess, "run", fake_run)

    result = asyncio.run(codex_cli_runner.run_task(
        {"task_id": "demo"},
        "model",
        "Task URL: http://example.test/",
        str(tmp_path),
        max_steps=2,
        todo_md="1. Do it",
    ))

    assert seen["cmd"][1] == "exec"
    assert "--json" in seen["cmd"]
    assert result["status"] == "completed"
    assert result["runner"] == "codex-exec-playwright"
    assert "done" in result["agent_output"]
