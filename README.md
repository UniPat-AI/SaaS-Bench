<div align="center">

<img src="assets/banner.svg" alt="SaaS-Bench" width=70%/>

<h1>SaaS-Bench: Can Computer-Use Agents Leverage Real-World SaaS to Solve Professional Workflows?</h1>

[![Paper](https://img.shields.io/badge/arXiv-Paper-b91c1c?style=for-the-badge&logo=arxiv&logoColor=white)](https://arxiv.org/abs/2605.15777)
[![Blog](https://img.shields.io/badge/Blog-Read_Post-f59e0b?style=for-the-badge&logo=substack&logoColor=white)](https://unipat.ai/blog/SaaS-Bench)
[![Leaderboard](https://img.shields.io/badge/Leaderboard-Results-2563eb?style=for-the-badge&logo=googleanalytics&logoColor=white)](https://unipat.ai/benchmarks/SaaS-Bench)
[![GitHub](https://img.shields.io/badge/GitHub-Code-181717?style=for-the-badge&logo=github&logoColor=white)](https://github.com/UniPat-AI/SaaS-Bench)


</div>

---

## Overview

A benchmark for evaluating LLM agents on **real, self-hosted SaaS
applications**. Each task asks the agent to drive a browser through a
multi-step business workflow (project management, accounting, HR, document
authoring, etc.); a per-task verifier inspects the running application's
state to score the result.

The bench currently ships **106 task instances across 6 domains** (split
into a text-only **uni-m** track and a multimodal **multi-m** track) and
**23 self-hosted SaaS apps**:

| Track   | Domain      | Tasks | Representative apps                              |
| ------- | ----------- | ----- | ------------------------------------------------ |
| uni-m   | Business    | 15    | Twenty, Bigcapital, HRMS, Pretix                 |
| uni-m   | Healthcare  | 16    | OpenEMR, OnlyOffice, OpnForm                     |
| uni-m   | Software    | 31    | Baserow, OpenProject, code-server, Metabase      |
| uni-m   | Teamwork    | 12    | OnlyOffice, Mattermost, RoundcubeMail, ownCloud  |
| multi-m | Agriculture | 12    | Grocy, farmOS, Recipya, e-label                  |
| multi-m | Media       | 20    | SiYuan, Watcharr, BookLore, PhotoPrism, MediaCMS |

Multi-m tasks consume image / audio / PDF inputs from
`tasks/multi-m/inputs/`; verifiers locate them via paths relative to
`verify.py`. Use `scripts/fetch_multimodal_assets.sh` to check that the
expected input files are present before running the multi-m suite.

The reference agent is built on [browser-use](https://github.com/browser-use/browser-use)
and talks to any OpenAI-compatible chat-completions endpoint. You can swap
in your own agent — only the `verify.py` contract is load-bearing.

## Repository layout

```
saas_bench/         Eval harness (Python package)
  run.py            CLI entry: orchestrates concurrent task execution
  agent.py          browser-use reference agent
  slot.py           Per-slot Docker container manager
  loader.py         Task discovery + prompt builder
  verify_runner.py  Runs verify.py and parses results
  apps.yaml         App registry (ports, start commands, health probes)

docker/             Compose templates + image archives (download separately)
tasks/
  uni-m/            Text-only tasks (Software, Business, Healthcare, Teamwork)
  multi-m/          Multimodal tasks (Agriculture, Media) + inputs/ assets
scripts/            run.sh, stop_all.sh, load_images.sh, fetch_multimodal_assets.sh
docs/               Verify protocol and task format specifications
```

## Prerequisites

- Linux host (tested on Ubuntu 22.04 / Alibaba Cloud Linux)
- Docker 24+ with the `compose` plugin
- Python ≥ 3.10
- ~100 GB free disk for the SaaS app images
- Outbound network access (for first-time pull of compose-stack auxiliary
  images and for `pip install scipy numpy` inside the code-server container)

## Setup

```bash
# 1. Clone and install the Python package
git clone <this-repo>.git SaaS-Bench
cd SaaS-Bench
pip install -e .
playwright install chromium
pip install socksio

# 2. Download SaaS-Bench docker images from Huggingface (see docker/README.md
#    for the URL) and place the .tar files under docker/images/, then:
bash scripts/load_images.sh

# 3. Configure your LLM endpoint
cp .env.example .env
$EDITOR .env   # set LLM_API_KEY, LLM_BASE_URL, LLM_MODEL
```

## Running the eval

**We recommend running the evaluation on a machine with more than 500GB of RAM to support parallel SaaS environment deployment and long-horizon agent execution.**

Run all tasks with 4 concurrent workers:

```bash
bash scripts/run.sh
```

Useful flags:

```bash
bash scripts/run.sh --workers 8                                 # bump concurrency
bash scripts/run.sh --tasks-dir tasks/uni-m/Business                  # one domain
bash scripts/run.sh --task-ids business_023 software_004             # cherry-pick
bash scripts/run.sh --max-steps 200                             # tighter step budget
bash scripts/run.sh --result-dir results/run_2026_05_05         # custom output dir
bash scripts/run.sh --no-isolation                              # reuse already-running containers
bash scripts/run.sh --log results/run.log                       # also tee to a file
```

Per-worker the harness:
1. Picks a slot id and computes app ports `30000 + slot_id*20 + app_index`.
2. Starts the docker containers / compose stacks for that task's `sites`.
3. Launches a headless Chrome and a fresh browser-use Agent.
4. Saves the agent trajectory to `<result_dir>/<task_id>.json`.
5. Runs `verify.py` and saves the score to `<result_dir>/<task_id>_verify.json`.
6. Tears down the containers and tmp dirs.

Aggregated stats land in `<result_dir>/summary.json`. Errors are appended
to `<result_dir>/errors.log` without aborting the run.

When in doubt, you can purge stale containers from a previous (crashed) run:

```bash
bash scripts/stop_all.sh
```

## Kubernetes smoke evaluation

For a Docker Desktop Kubernetes smoke run, deploy the controlled fake website
into the cluster and evaluate it from an external pytest client:

```bash
python -m pip install -e ".[test]"
bash scripts/smoke_k8s.sh
```

The fake website is installed from a Helm chart into a per-run Kubernetes
namespace and exposes result/reset endpoints. The evaluation client runs
outside the cluster and reaches the Service through `kubectl port-forward`.
See [docs/k8s_smoke_testing.md](docs/k8s_smoke_testing.md) for details.

## Bring your own agent

The harness invokes a single async function `run_task(task, model_name,
prompt, result_dir, max_steps, slot_id, todo_md) -> dict`. Implement that
in a module of your choice and have `saas_bench.run` import it instead of
`saas_bench.agent`. The contract is intentionally tiny: return a dict with
`status` (`completed` / `error`), `agent_output` (string), and
`trajectory` (list of step dicts). The verifier runs against the live
docker state; what the agent does to get there is up to you.

## Adding a new task

See [docs/task_format.md](docs/task_format.md) and
[docs/verify_protocol.md](docs/verify_protocol.md).

## Citation

```bibtex
@misc{saasbench2026,
  title  = {SaaS-Bench: Can Computer-Use Agents Leverage Real-World SaaS to Solve Professional Workflows?},
  author = {UniPat AI},
  year   = {2026}
}
```
