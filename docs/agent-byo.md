# Bring Your Own Agent

The run loop is agent-agnostic — point `--agent-module` at any Python module exposing an
`async run_task(...)` and your agent slots in as a drop-in replacement for the stock browser-use
reference. This is the load-bearing seam for testing **any** computer-use agent (browser-use, OpenAI
/ Anthropic computer-use, Pine's CUA, a manual human-in-loop driver) against the same hosted
playground + ground-truth grading.

## The contract

Your module must export an awaitable named `run_task` with this signature:

```python
async def run_task(
    task: dict,                  # the loaded task: {task_id, description_md, meta, verify_py_path, ...}
    model_name: str,             # what the run-loop was launched with (--model)
    prompt: str,                 # the formatted task prompt (description + access URLs + steps)
    result_dir: str,             # where to write the result JSON
    max_steps: int = 80,
    slot_id: int | None = None,  # only set for SlotManager local-docker runs; None for playground
    todo_md: str | None = None,  # pre-filled todo list from description.md (or None)
    run_idx: int | None = None,  # for pass@k (--runs K), index 0..K-1
    input_files: list[str] | None = None,  # multimodal-input absolute paths (or None)
) -> dict:
    ...
```

It **must** write `{result_dir}/{task_id}_r{run_idx}.json` (omit the `_r{...}` suffix when
`run_idx is None`) containing:

```json
{
  "task_id": "agriculture_016",
  "status": "completed",     // or "error"
  "agent_output": "<final agent message / summary>",
  "trajectory": [             // step-by-step record the run loop's report.md consumes
    {"step": 1, "action": "...", "thought": "...", "observation": "..."},
    ...
  ]
}
```

The return value is ignored by the run loop — the **file** is the contract. Stock `reporting.py`
loads it to compute per-step health, action distribution, and trajectory metrics.

## Minimal example

```python
# my_agent.py
import json
from pathlib import Path


async def run_task(task, model_name, prompt, result_dir, max_steps=80,
                   slot_id=None, todo_md=None, run_idx=None, input_files=None):
    task_id = task["task_id"]
    suffix = f"_r{run_idx}" if run_idx is not None else ""
    out = Path(result_dir) / f"{task_id}{suffix}.json"

    # 1) Drive YOUR agent here. The `prompt` already contains the per-app access URLs.
    #    For a hosted playground run, these are https://<run>.saas-playground.example.com.
    trajectory = []
    try:
        # final_output = await your_agent.act(prompt, max_steps=max_steps)
        final_output = "..."
        status = "completed"
    except Exception as exc:
        final_output = f"error: {exc}"
        status = "error"

    # 2) Write the result file — the run loop's reporting reads this.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "task_id":      task_id,
        "status":       status,
        "agent_output": final_output,
        "trajectory":   trajectory,
    }, ensure_ascii=False, indent=2))
    return {"task_id": task_id, "status": status}
```

Run it:

```bash
python -m saas_bench.run \
  --tasks-dir tasks --task-ids agriculture_016 \
  --target-backend playground \
  --grade-backend service \
  --playground-url https://api.saas-playground.example.com \
  --agent-module my_agent          # <-- your module on PYTHONPATH
```

That's it — the run loop calls `prepare` (provisions the env on the cluster), formats the prompt with
your env's URL, calls **your** `run_task`, then calls `grade` (server-side `verify.py` →
ground-truth score), then `release`.

## What's in `prompt` and `task`

- **`prompt`** is a string assembled by `loader.build_prompt`: the task's `description.md` + a
  "Application Access URLs" block (one per app in `task.meta_data.sites`) + any multimodal file
  paths. It's the same prompt the stock browser-use agent receives — adapt it freely.
- **`task["meta"]["meta_data"]["sites"]`** lists which apps the task spans (`["farmos"]` for
  single-app, e.g. `["mattermost", "openproject"]` for multi-app).
- **`task["verify_py_path"]`** is where the grader lives (your agent shouldn't run it; the
  service does).

## Trajectory shape — guidance, not gospel

The schema above is what stock `reporting.py` understands (per-step action / thought /
observation; the report renders an action distribution + a health timeline). If your agent's loop
doesn't naturally produce that shape, write whatever shape makes sense — the trajectory is
read for analytics, not correctness. Correctness comes from `/grade`.

## Smoke-test pattern

To validate your agent wires up correctly without burning tokens, point it at one cheap task with
`--task-ids agriculture_016 --runs 1` and a tiny `max_steps`. The hosted prepare/grade are
deterministic; the only variable is what your agent does in between.
