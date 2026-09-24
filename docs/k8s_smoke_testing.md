# Kubernetes Smoke Testing

The Kubernetes smoke test validates the controlled website evaluation path on
Docker Desktop Kubernetes without downloading the 23 real SaaS application
images.

The fake website runs in Kubernetes. The evaluation client runs outside the
cluster and reaches the website through a Kubernetes access boundary. The
default boundary is `kubectl port-forward`.

## Boundary

```
external pytest client
  -> helm install fake-site deploy/helm/fake-site --namespace saasbench-smoke-<run>
  -> Kubernetes Deployment / Service in per-run namespace
  -> kubectl port-forward service/fake-site
  -> GET /healthz, GET /result
  -> saas_bench.verify_runner.run_verify(task, ...)
  -> tests/fixtures/tasks/smoke_fake_site/verify.py
  -> normalized result JSON
```

The fake website exposes:

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/healthz` | GET | Readiness and liveness. |
| `/result` | GET | Return the current evaluation result. |
| `/reset` | POST | Reset state to the clean initial result. |
| `/set-result` | POST | Seed a result for smoke-test evaluation. |

The smoke app is generic, so `verify_runner` derives `FAKE_SITE_PORT` and
`FAKE_SITE_CONTAINER` from the site name when no explicit `SITE_CONFIG` entry
exists. Known production apps keep their existing explicit mapping.

## Docker Desktop Prerequisites

- Docker Desktop is running.
- Kubernetes is enabled in Docker Desktop.
- The current kubectl context is `docker-desktop`.
- `kubectl get nodes` succeeds.
- Helm is installed.

## Run

```bash
python -m pip install -e ".[test]"
bash scripts/smoke_k8s.sh
```

Useful overrides:

```bash
K8S_KEEP_RESOURCES=1 bash scripts/smoke_k8s.sh
K8S_CONTEXT=my-context bash scripts/smoke_k8s.sh
K8S_ALLOW_ANY_CONTEXT=1 bash scripts/smoke_k8s.sh
```

The tests are marked `k8s` and are skipped unless `--run-k8s` is passed.
The smoke script passes that flag automatically.

## What It Proves

- Helm can install the fake website as a per-run release.
- Docker Desktop Kubernetes can pull and run the fake website image.
- The fake website can return a machine-readable result.
- The fake website state can be reset deterministically.
- The evaluation client can run outside the cluster.
- The verifier can evaluate the returned result through the normal harness
  contract.
- The verifier output parser returns PASS and FAIL statuses correctly.
- Result JSON is written outside the cluster in the pytest result directory.
- Test-owned Kubernetes resources are cleaned up unless explicitly kept.

It intentionally does not test the browser-use agent or the full SaaS image set.

## Full Kubernetes Harness

The full harness can run with the Kubernetes backend instead of Docker:

```bash
python -m saas_bench.run \
  --tasks-dir tasks \
  --backend k8s \
  --task-ids <task_id> \
  --apps-yaml saas_bench/apps.yaml \
  --result-dir results/k8s
```

The Kubernetes backend:

- uses `K8sSlotManager` to provision per-task app namespaces
- waits for app readiness
- exposes apps through slot-prefixed Ingress or local port-forward
- runs Codex through `codex exec` with Python Playwright instructions
- runs `verify.py` through the kubectl-backed Docker shim
- emits verifier and agent JSON under the result directory
- deletes the app namespace during task cleanup

For a deterministic end-to-end smoke without the Codex CLI call:

```bash
SAAS_CODEX_DISABLE=1 python -m saas_bench.run \
  --tasks-dir tests/fixtures/tasks \
  --task-ids code_server_smoke \
  --backend k8s \
  --result-dir results/k8s_harness
```

The dedicated e2e wrapper is:

```bash
python scripts/k8s_e2e.py
```
