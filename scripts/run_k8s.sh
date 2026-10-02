#!/usr/bin/env bash
# run_k8s.sh — run the eval against the HOSTED k8s playground (prepare / grade / release API).
#
# Split of responsibilities:
#   - cluster  : provisions each task's app env (pod) and runs verify.py server-side.
#   - THIS host: runs the browser-use agent + the orchestration loop, calling the HTTP API.
#
# Required (export, or put in a .env next to this repo):
#   PLAYGROUND_URL       e.g. https://api.<your-domain>   (hosted playground base URL)
#   PLAYGROUND_API_KEY   bearer key from the platform admin
#   LLM_API_KEY / LLM_BASE_URL / LLM_MODEL   the agent's own model (never sent to the platform)
#
# The agent needs browser-use + playwright installed in $PYTHON's env. The platform allows up to
# 110 concurrent runs (the 111th /runs returns 429); your real ceiling is how many browser agents
# THIS host + the LLM endpoint can sustain.
#
# Examples:
#   bash scripts/run_k8s.sh --task-ids agriculture_020 --workers 1     # smoke one task
#   bash scripts/run_k8s.sh --workers 16                               # run all bundled tasks
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load .env (PLAYGROUND_*, LLM_*) if present.
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -o allexport; # shellcheck disable=SC1091
  source "$REPO_ROOT/.env"; set +o allexport
fi

PYTHON="${PYTHON:-python3}"          # must have browser-use + playwright + saas_bench
export BROWSER_USE_LOGGING_LEVEL="${BROWSER_USE_LOGGING_LEVEL:-warning}"

TASKS_DIR="${REPO_ROOT}/tasks"
MODEL="${LLM_MODEL:-}"   # no default: valid names depend on your LLM_BASE_URL endpoint
WORKERS=32          # default; platform allows up to 110 concurrent runs (111th /runs -> 429)
RUNS=1
RESULT_DIR="${REPO_ROOT}/results/run_$(date +%Y%m%d_%H%M%S)"   # timestamped per invocation; run.py nests <model> under it
# Accept the client-guide names API/KEY as fallbacks for PLAYGROUND_URL/PLAYGROUND_API_KEY.
PLAYGROUND_URL="${PLAYGROUND_URL:-${API:-}}"
export PLAYGROUND_API_KEY="${PLAYGROUND_API_KEY:-${KEY:-}}"
TASK_IDS=""
MAX_STEPS=400

usage() { sed -n '2,20p' "$0"; exit 0; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --playground-url) PLAYGROUND_URL="$2"; shift 2 ;;
    --tasks-dir)      TASKS_DIR="$2"; shift 2 ;;
    --model)          MODEL="$2"; shift 2 ;;
    --workers)        WORKERS="$2"; shift 2 ;;
    --runs)           RUNS="$2"; shift 2 ;;
    --max-steps)      MAX_STEPS="$2"; shift 2 ;;
    --result-dir)     RESULT_DIR="$2"; shift 2 ;;
    --task-ids)       shift; TASK_IDS=""; while [[ $# -gt 0 && "$1" != --* ]]; do TASK_IDS="$TASK_IDS $1"; shift; done ;;
    -h|--help)        usage ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# --- Preflight -------------------------------------------------------------
fail() { echo "[ERROR] $1" >&2; exit 1; }
[[ -n "$PLAYGROUND_URL" ]]        || fail "PLAYGROUND_URL not set (export it, put in .env, or pass --playground-url)"
[[ -n "${PLAYGROUND_API_KEY:-}" ]] || fail "PLAYGROUND_API_KEY not set (bearer key from the platform admin)"
[[ -n "${LLM_API_KEY:-}" && -n "${LLM_BASE_URL:-}" ]] || fail "LLM_API_KEY / LLM_BASE_URL not set (the agent's own model)"
[[ -n "$MODEL" ]] || fail "no model selected: set LLM_MODEL in .env or pass --model <name> (no default — valid names depend on your LLM_BASE_URL endpoint)"
(( WORKERS >= 1 )) || fail "--workers must be >= 1"
(( WORKERS <= 110 )) || echo "  [note] --workers=$WORKERS (>110): platform maxActiveRuns is 110; the 111th /runs returns 429." \
  "Also confirm THIS host + the LLM endpoint can sustain $WORKERS concurrent browser agents, or you'll hit rate limits."
if ! "$PYTHON" -c "import saas_bench" 2>/dev/null; then
  export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
  "$PYTHON" -c "import saas_bench" 2>/dev/null || fail "saas_bench not importable by '$PYTHON' — run from repo root / pip install -e ."
fi
"$PYTHON" -c "import browser_use" 2>/dev/null || fail "browser-use not installed in '$PYTHON' — set PYTHON=<env-with-browser-use> or pip install browser-use"

# Quick liveness ping (unauthenticated) so we fail fast if the platform is down.
if command -v curl >/dev/null; then
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 10 "${PLAYGROUND_URL%/}/healthz" 2>/dev/null || echo 000)
  echo "  platform /healthz -> HTTP $code"
fi

echo "============================================"
echo "  SaaS-Bench eval — hosted k8s backend"
echo "  URL     : $PLAYGROUND_URL"
echo "  Model   : $MODEL"
echo "  Workers : $WORKERS  (must be <= platform maxActiveRuns)"
echo "  Tasks   : ${TASK_IDS:-<all bundled>}"
echo "  Result  : $RESULT_DIR"
echo "============================================"

CMD=( "$PYTHON" -m saas_bench.run
  --tasks-dir "$TASKS_DIR" --model "$MODEL" --workers "$WORKERS" --runs "$RUNS"
  --result-dir "$RESULT_DIR" --max-steps "$MAX_STEPS"
  --target-backend playground --grade-backend service --playground-url "$PLAYGROUND_URL" )
if [[ -n "$TASK_IDS" ]]; then CMD+=(--task-ids); # shellcheck disable=SC2206
  CMD+=($TASK_IDS); fi

cd "$REPO_ROOT"
exec "${CMD[@]}"
