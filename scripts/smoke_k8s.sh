#!/usr/bin/env bash
# Run the controlled Kubernetes smoke evaluation on Docker Desktop.
#
# The fake website runs in Kubernetes. The evaluation client runs locally,
# outside the cluster, and reaches the Service through kubectl port-forward.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON="${PYTHON:-}"
K8S_CONTEXT="${K8S_CONTEXT:-docker-desktop}"
ALLOW_ANY_CONTEXT="${K8S_ALLOW_ANY_CONTEXT:-0}"

KUBECTL="${KUBECTL:-}"
if [[ -z "$KUBECTL" ]]; then
  if command -v kubectl >/dev/null 2>&1; then
    KUBECTL="kubectl"
  elif command -v kubectl.exe >/dev/null 2>&1; then
    KUBECTL="kubectl.exe"
  else
    echo "[ERROR] kubectl is not available on PATH." >&2
    exit 1
  fi
fi

HELM="${HELM:-}"
if [[ -z "$HELM" ]]; then
  if command -v helm >/dev/null 2>&1; then
    HELM="helm"
  elif command -v helm.exe >/dev/null 2>&1; then
    HELM="helm.exe"
  else
    echo "[ERROR] helm is not available on PATH." >&2
    exit 1
  fi
fi

CURRENT_CONTEXT="$("$KUBECTL" config current-context 2>/dev/null || true)"
if [[ -z "$CURRENT_CONTEXT" ]]; then
  echo "[ERROR] No current kubectl context is configured." >&2
  exit 1
fi

if [[ "$ALLOW_ANY_CONTEXT" != "1" && "$CURRENT_CONTEXT" != "$K8S_CONTEXT" ]]; then
  echo "[ERROR] Expected kubectl context '$K8S_CONTEXT', found '$CURRENT_CONTEXT'." >&2
  echo "        Set K8S_CONTEXT or K8S_ALLOW_ANY_CONTEXT=1 to override." >&2
  exit 1
fi

if [[ -z "$PYTHON" ]]; then
  for candidate in python3 python python.exe; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -m pytest --version >/dev/null 2>&1; then
      PYTHON="$candidate"
      break
    fi
  done
fi

if [[ -z "$PYTHON" ]] || ! "$PYTHON" -m pytest --version >/dev/null 2>&1; then
  echo "[ERROR] pytest is not installed for an available Python interpreter." >&2
  echo "        Install test dependencies with: python -m pip install -e '.[test]'" >&2
  exit 1
fi

cd "$REPO_ROOT"

echo "Using kube context: $CURRENT_CONTEXT"
echo "Kept resources: ${K8S_KEEP_RESOURCES:-0}"
echo ""

PYTEST_ARGS=(-q -m k8s tests/integration --run-k8s)
if [[ "${K8S_KEEP_RESOURCES:-0}" == "1" ]]; then
  PYTEST_ARGS+=(--keep-k8s)
fi

"$PYTHON" -m pytest "${PYTEST_ARGS[@]}" "$@"
