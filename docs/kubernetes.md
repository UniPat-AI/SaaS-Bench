# Running the Playground on Kubernetes

The bundled evaluation loop starts every task's SaaS apps with `docker run` on the machine that
also drives the agent. That is fine for a single host, but it caps you at one run at a time per
port range and puts the browser, the apps, and the verifier on the same box.

The **hosted playground** splits those responsibilities:

| Component | Runs where | Does what |
|---|---|---|
| **Platform** | your cluster | provisions each run's apps as pods, serves an HTTP API, runs `verify.py` server-side, reaps expired runs |
| **Client** (`scripts/run_k8s.sh`) | your workstation / CI | drives the agent, calls the API over HTTP |

The client never talks to Kubernetes. It needs a URL and a bearer token, nothing else — so an
agent written in any language can use the platform through plain HTTP.

---

## 1. What the platform needs from your cluster

Before you start, confirm all four. The first is the one people miss.

1. **`kubectl exec` into app pods.** Grading is not negotiable on this: the bundled `verify.py`
   scripts inspect live application state by shelling out to `docker exec` / `docker cp`, and the
   platform makes them work unmodified by putting a `docker`→`kubectl` shim
   (`deploy/shim/docker`) first on `PATH`. A cluster that blocks exec (some serverless tiers do)
   cannot grade.
2. **A way to expose HTTP.** Either an Ingress/Gateway controller or a `LoadBalancer` Service.
3. **Wildcard DNS.** Each run is addressed as `<run_id>.<your-domain>`, and the API lives at
   `api.<your-domain>`. For a proof of concept you can skip real DNS and use a
   `<LB-IP>.sslip.io`-style wildcard resolver.
4. **A container registry the cluster can pull from**, holding the platform image and the SaaS
   app images.

Resource-wise, one run is one pod per app in the task (a few tasks need four), and the shipped
values allow up to 110 concurrent runs. Size for the concurrency you actually want, not 110.

---

## 2. Push the images

Two kinds of image are involved.

**The platform image**, built from this repo:

```bash
export REG=<your-registry>/<namespace>      # e.g. ghcr.io/acme, registry.example.com/saas
docker build -f deploy/Dockerfile -t "$REG/saas-playground:latest" .
docker push "$REG/saas-playground:latest"
```

It is a `python:3.11-slim` image with FastAPI + uvicorn, `kubectl`, `helm`, and the repo's
`saas_bench/`, `tasks/`, `charts/`, `docker/`, `deploy/` directories. It listens on 8080 and is
started as a uvicorn factory (`saas_bench.deploy_entry:make_app`).

**The SaaS app images** — the same `mw-*` images the local path uses. Load them once, retag, push:

```bash
bash scripts/load_images.sh                 # see docker/README.md for where to get the archives
for img in $(docker images --format '{{.Repository}}' | grep '^mw-'); do
    docker tag "$img:latest" "$REG/$img:latest"
    docker push "$REG/$img:latest"
done
```

Some of these are several GB. Push only the apps your task subset needs if you are starting small
— `meta.json`'s `meta_data.sites` lists them per task.

---

## 3. Configure

Everything is in `charts/saas-playground-platform/values.yaml`. The values you must change:

```yaml
image:
  repository: <your-registry>/<namespace>/saas-playground
  tag: latest

config:
  domain: playground.example.com     # runs resolve at <run_id>.<domain>
  publicScheme: https                # http if you have no TLS yet
  maxActiveRuns: 110                 # the 111th POST /runs returns 429
  prepareWorkers: 4
  gradeWorkers: 4

gateway:
  apiHost: api.playground.example.com
  wildcardHost: "*.playground.example.com"
```

### Ports

There are no host-port assignments to manage. The local runner derives ports arithmetically
(`BASE_PORT + slot*40 + app_index`) and must avoid collisions; on Kubernetes each run gets its own
DNS name instead, so `{hostname}` / `{port}` / `{pg_port}` in `apps.yaml` are bound to that run's
public address and every app can keep its natural container port.

Grading is the one place that still needs per-app ports, because the bundled verifiers were written
against the local layout and tell a task's apps apart *by port* on a shared host
(`f"http://{HOST}:{ONLYOFFICE_PORT}"`). On Kubernetes that is inverted — per-app hostname, shared
port 80 — so the platform gives the verifier the local shape back: for the duration of one `POST
/grade` each app is served on its own loopback port inside the platform pod
(`saas_bench/verify_proxy.py`), `SERVER_HOSTNAME` becomes `127.0.0.1`, and each `<APP>_PORT` becomes
distinct again. Outgoing `Host` is rewritten to the app's real public hostname, since several apps
check it (pretix's `ALLOWED_HOSTS` answers 400 on a mismatch). Nothing listens between grades, and
the ports are kernel-assigned, so concurrent grades of tasks sharing an app never collide. **A
verifier you write yourself needs no special handling — the same env contract holds on both
backends.**

The ports you *can* set are the service-level ones:

| Value | Meaning | Default |
|---|---|---|
| `service.port` / `service.targetPort` | platform API Service | 80 → 8080 |
| `runRouter.servicePort` / `targetPort` | per-run reverse proxy | 80 → 8080 |
| run chart `containerPort` | an app's own listen port | from `apps.yaml`'s `container_port` |

### If you are not on GKE

The shipped chart is a working GKE reference and pulls in Google-specific objects. On any other
platform you must override or replace:

| What | Where | Replace with |
|---|---|---|
| `gke-l7-global-external-managed` Gateway class | `gateway.className` | your own Gateway class, or disable `gateway.enabled` and write an Ingress |
| `networking.gke.io` health-check and backend-policy objects | `templates/healthcheck.yaml`, `templates/networking.yaml`, `templates/run-router.yaml` | your controller's equivalents, or delete them |
| Workload Identity annotation (`iam.gke.io`, `serviceAccount.gcpServiceAccount`) | `templates/rbac.yaml` | leave `gcpServiceAccount` empty to skip it |
| External Secrets against GCP Secret Manager | `externalSecret.*` | set `externalSecret.enabled: false` and create the Secret named by `externalSecret.k8sSecretName` yourself |

`values-staging.yaml` is a smaller-footprint example to copy from.

---

## 4. Install

```bash
kubectl create namespace saas-playground

# the API bearer token clients will use
kubectl -n saas-playground create secret generic saas-playground-api-key \
    --from-literal=api-key="$(openssl rand -hex 24)"

helm upgrade --install saas-playground charts/saas-playground-platform \
    -n saas-playground -f my-values.yaml

kubectl -n saas-playground rollout status deploy/saas-playground
curl -s https://api.<your-domain>/healthz
```

The platform installs `charts/saas-playground-run` (single-container apps) and
`charts/saas-playground-run-compose` (multi-container apps such as Mattermost or OnlyOffice) per
run; you do not install those yourself.

A CronJob (`janitor.*`, every 5 min by default) releases runs older than
`janitor.ttlMinutes`, so an abandoned client cannot leak pods forever.

---

## 5. Run an evaluation

```bash
export PLAYGROUND_URL=https://api.<your-domain>
export PLAYGROUND_API_KEY=<the api-key you created>
export LLM_API_KEY=... LLM_BASE_URL=... LLM_MODEL=...   # your agent's model; never sent to the platform

bash scripts/run_k8s.sh --task-ids agriculture_020 --workers 1   # smoke test: one task, one worker
bash scripts/run_k8s.sh --workers 16            # everything
```

Concurrency is bounded by what your host and your LLM endpoint can sustain, not by the platform —
the browser agents all run on the client.

Results land in `results/` in the same layout the local runner produces, so `reporting.py` and any
downstream tooling work unchanged.

---

## 6. The API, if you are not using these scripts

Bearer auth on every endpoint. `GET /docs` serves Swagger UI.

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness |
| `GET /catalog` | tasks and the apps each needs |
| `POST /runs` | create a run |
| `GET /runs`, `GET /runs/{run_id}` | list / inspect |
| `POST /prepare` | provision the run's apps, wait for readiness |
| `GET /tasks/{task_id}/prompt` | the prompt, with this run's URLs substituted |
| `GET /tasks/{task_id}/files/{name}` | multimodal input files |
| `POST /grade` | run `verify.py` server-side, return checks + score |
| `POST /release` | tear the run down |
| `GET /audit/runs`, `POST /audit/grade` | operator views |

The contract that matters for a bring-your-own-agent integration: **prepare → prompt → (your agent
acts) → grade → release**. `docs/agent-byo.md` covers the in-process seam if you would rather plug
a Python agent into the local runner instead.

---

## 7. Troubleshooting

**`FileNotFoundError: 'kubectl'` at startup.** The platform restores in-flight runs on boot and
needs `kubectl` on `PATH` plus a usable kubeconfig. Inside the shipped image both are present; you
will hit this if you run `deploy_entry` directly on a workstation without a cluster.

**Grading fails but the app is up.** Almost always exec: check that your cluster allows
`kubectl exec` into the app pods, and that `SAAS_PLAYGROUND_NAMESPACE` matches where they run. The
shim exits non-zero and loudly on any `docker` subcommand other than `exec`/`cp`, so an unported
verifier shows up as a clear error rather than a silent pass.

**`POST /runs` returns 429.** `config.maxActiveRuns` is reached — either raise it and the cluster's
capacity, or wait for the janitor.

**An app never becomes ready.** Per-app `startup_wait` in `saas_bench/apps.yaml` is the budget.
The heavy ones already get more (HRMS 2100s, OnlyOffice and OpenEMR 900–1200s); under heavy
concurrency they may still need more.
