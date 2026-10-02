"""CLI-agent harnesses for SaaS-Bench (`--agent-module saas_bench.harness.<name>`).

Each module exposes the standard agent contract — ``async run_task(...) -> dict`` that
writes ``{result_dir}/{task_id}_r{run_idx}.json`` — and drives a coding-CLI agent
(Claude Code / Codex / Kimi Code) headlessly. The browser comes from the Playwright MCP
server (``@playwright/mcp``) spawned per process with ``--caps vision`` so screenshots
flow back into the model's context. Shared subprocess/trajectory logic lives in
``base.py``; per-CLI modules only build the command line and parse the event stream.
"""
