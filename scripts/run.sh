#!/usr/bin/env bash
# run.sh — SaaS-Bench concurrent evaluation launcher
# Usage: ./scripts/run.sh [options]
#
# Examples:
#   ./scripts/run.sh                                       # run with default settings
#   ./scripts/run.sh --workers 5                           # 5 concurrent workers
#   ./scripts/run.sh --no-isolation                        # do not start Docker isolation containers
#   ./scripts/run.sh --task-ids business_023 software_004
#   ./scripts/run.sh --tasks-dir tasks/Business                 # run a single domain only

set -euo pipefail

# -- Path setup --------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# -- Load .env (if present) so LLM_API_KEY/LLM_BASE_URL/LLM_MODEL get exported
if [[ -f "$REPO_ROOT/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source "$REPO_ROOT/.env"
    set +o allexport
fi

# -- Python interpreter ------------------------------------------------------
PYTHON="${PYTHON:-python3}"
export BROWSER_USE_LOGGING_LEVEL=warning

# -- Default arguments -------------------------------------------------------
TASKS_DIR="${REPO_ROOT}/tasks"
MODEL="${LLM_MODEL:-}"          # no default: valid names depend on your LLM_BASE_URL endpoint
WORKERS=4
MAX_STEPS=400
HOSTNAME_VAL="localhost"
RESULT_DIR="${REPO_ROOT}/results"
APPS_YAML="${REPO_ROOT}/saas_bench/apps.yaml"
NO_ISOLATION=""
TASK_IDS=""
LOG_FILE=""
AGENT_MODULE=""

# -- Argument parsing --------------------------------------------------------
usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --tasks-dir <path>      Task directory root (default: rollout/tasks)
  --model <name>          LLM model name (default: $LLM_MODEL from .env; required)
  --agent-module <mod>    Python module exposing run_task (bring-your-own-agent seam;
                          e.g. saas_bench.harness.kimi_code). Default: stock browser-use agent
  --workers <n>           Number of concurrent workers (default: 3)
  --max-steps <n>         Max steps per task (default: 400)
  --hostname <host>       Hostname the agent uses to access apps (default: localhost)
  --result-dir <path>     Output directory for results (default: rollout/results)
  --apps-yaml <path>      Path to apps.yaml (default: rollout/apps.yaml)
  --no-isolation          Disable Docker container isolation; connect directly to already-running apps via fixed_port
  --task-ids <id> [...]   Run only the specified task ids (space-separated)
  --log <file>            Also write output to a log file
  -h, --help              Show this help
EOF
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --tasks-dir)   TASKS_DIR="$2";       shift 2 ;;
        --model)       MODEL="$2";           shift 2 ;;
        --workers)     WORKERS="$2";         shift 2 ;;
        --max-steps)   MAX_STEPS="$2";       shift 2 ;;
        --hostname)    HOSTNAME_VAL="$2";    shift 2 ;;
        --result-dir)  RESULT_DIR="$2";      shift 2 ;;
        --apps-yaml)   APPS_YAML="$2";       shift 2 ;;
        --no-isolation) NO_ISOLATION="--no-isolation"; shift ;;
        --task-ids)
            shift
            TASK_IDS=""
            while [[ $# -gt 0 && "$1" != --* ]]; do
                TASK_IDS="$TASK_IDS $1"
                shift
            done
            ;;
        --log)         LOG_FILE="$2";        shift 2 ;;
        --agent-module) AGENT_MODULE="$2";   shift 2 ;;
        -h|--help)     usage ;;
        *) echo "Unknown option: $1"; usage ;;
    esac
done

# -- Dependency check --------------------------------------------------------
if ! "$PYTHON" -c "import saas_bench" 2>/dev/null; then
    export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
    if ! "$PYTHON" -c "import saas_bench" 2>/dev/null; then
        echo "[ERROR] saas_bench module not found; run from the repo root, or pip install -e . first" >&2
        exit 1
    fi
fi

if [[ -z "${LLM_API_KEY:-}" || -z "${LLM_BASE_URL:-}" ]]; then
    echo "[ERROR] LLM_API_KEY / LLM_BASE_URL not set; please cp .env.example .env, fill it in, and retry" >&2
    exit 1
fi

if [[ -z "$MODEL" ]]; then
    echo "[ERROR] no model selected: set LLM_MODEL in .env or pass --model <name>." >&2
    echo "        There is no default because valid model names depend on your LLM_BASE_URL endpoint." >&2
    exit 1
fi

if [[ ! -d "$TASKS_DIR" ]]; then
    echo "[ERROR] Task directory does not exist: $TASKS_DIR" >&2
    exit 1
fi

if [[ ! -f "$APPS_YAML" ]]; then
    echo "[ERROR] apps.yaml does not exist: $APPS_YAML" >&2
    exit 1
fi

# -- Print configuration summary ---------------------------------------------
TIMESTAMP="$(date '+%Y-%m-%d %H:%M:%S')"
TASK_COUNT="$(find "$TASKS_DIR" -name "meta.json" | wc -l | tr -d ' ')"

echo "============================================"
echo "  SaaS-Bench concurrent evaluation"
echo "============================================"
echo "  Start time   : $TIMESTAMP"
echo "  Task dir     : $TASKS_DIR"
echo "  Total tasks  : $TASK_COUNT"
echo "  Model        : $MODEL"
echo "  Workers      : $WORKERS"
echo "  Max steps    : $MAX_STEPS"
echo "  Hostname     : $HOSTNAME_VAL"
echo "  Result dir   : $RESULT_DIR"
echo "  Isolation    : ${NO_ISOLATION:-enabled (Docker per-slot)}"
[[ -n "$TASK_IDS" ]] && echo "  Task ids     :$TASK_IDS"
echo "============================================"
echo ""

# -- Build command -----------------------------------------------------------
CMD=(
    "$PYTHON" -m saas_bench.run
    --tasks-dir   "$TASKS_DIR"
    --model       "$MODEL"
    --workers     "$WORKERS"
    --max-steps   "$MAX_STEPS"
    --hostname    "$HOSTNAME_VAL"
    --result-dir  "$RESULT_DIR"
    --apps-yaml   "$APPS_YAML"
)

[[ -n "$NO_ISOLATION" ]] && CMD+=("$NO_ISOLATION")
[[ -n "$AGENT_MODULE" ]] && CMD+=(--agent-module "$AGENT_MODULE")

if [[ -n "$TASK_IDS" ]]; then
    CMD+=(--task-ids)
    # shellcheck disable=SC2206
    CMD+=($TASK_IDS)
fi

# -- Execute ----------------------------------------------------------------
cd "$REPO_ROOT"

if [[ -n "$LOG_FILE" ]]; then
    echo "Also writing log to: $LOG_FILE"
    mkdir -p "$(dirname "$LOG_FILE")"
    "${CMD[@]}" 2>&1 | tee "$LOG_FILE"
else
    "${CMD[@]}"
fi

EXIT_CODE=${PIPESTATUS[0]:-$?}

echo ""
echo "============================================"
echo "  Done, exit code: $EXIT_CODE"
echo "  Result dir: $RESULT_DIR"
[[ -f "$RESULT_DIR/summary.json" ]] && echo "  Summary:" && "$PYTHON" -c "
import json
s = json.load(open('$RESULT_DIR/summary.json'))
print(f\"    Total : {s.get('completed',0)}/{s.get('total',0)} completed\")
for cat, ds in sorted(s.get('domains', {}).items()):
    avg = ds.get('avg_verify_score', 0)
    print(f\"    {cat:6s}: {ds.get('completed',0)}/{ds.get('total',0)} completed  verify avg={avg:.3f}\")
"
echo "============================================"

exit $EXIT_CODE
