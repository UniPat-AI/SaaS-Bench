"""Deploy entrypoint for the Target Playground service.

Builds the catalog (from the bundled ``tasks/`` + ``apps.yaml``) and wires
``PlaygroundService`` over ``HelmProvisioner``. Run in-cluster as:

    uvicorn saas_bench.deploy_entry:make_app --factory --host 0.0.0.0 --port 8080

A no-arg ``make_app`` factory (not a module-level ``app``) keeps ``load_catalog`` importable +
unit-testable without fastapi installed or the /app layout present.

Config via env (set by the platform chart): SAAS_PLAYGROUND_NAMESPACE, SAAS_PLAYGROUND_DOMAIN,
SAAS_PLAYGROUND_RUN_CHART, SAAS_PLAYGROUND_SHIM_DIR, SAAS_PLAYGROUND_REGISTRY,
SAAS_PLAYGROUND_TASKS (optional task-id allowlist), SAAS_PLAYGROUND_TASKS_DIR,
SAAS_PLAYGROUND_APPS_YAML, SAAS_PLAYGROUND_ALLOW_PRIVILEGED_APPS,
SAAS_PLAYGROUND_PREPARE_WORKERS, SAAS_PLAYGROUND_GRADE_WORKERS,
SAAS_PLAYGROUND_MAX_ACTIVE_RUNS, SAAS_PLAYGROUND_PUBLIC_SCHEME (browser-facing scheme for
access_urls/origin env; default https — the edge terminates TLS).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict

import yaml

from saas_bench.app_spec import UnsupportedAppError, load_app_spec, parse_compose
from saas_bench.loader import load_tasks
from saas_bench.playground_service import PlaygroundService, VERIFIER_AUDIT_TASK_ID
from saas_bench.provisioner_helm import HelmProvisioner

_DEFAULT_REGISTRY = "us-central1-docker.pkg.dev/your-gcp-project/saas-bench"


def _task_digest(task: dict) -> str:
    """Stable per-task digest (R-TASK-VERSION) over description + meta + verify.py."""
    h = hashlib.sha256()
    h.update((task.get("description_md") or "").encode())
    h.update(json.dumps(task.get("meta", {}), sort_keys=True).encode())
    vp = task.get("verify_py_path")
    if vp and os.path.exists(vp):
        with open(vp, "rb") as f:
            h.update(f.read())
    return h.hexdigest()[:12]


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    val = os.environ.get(name)
    if val is None or not val.strip():
        return default
    try:
        parsed = int(val)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {val!r}") from exc
    if parsed < minimum:
        raise RuntimeError(f"{name} must be >= {minimum}, got {parsed}")
    return parsed


def load_catalog(tasks_root=None, apps_yaml=None, registry=None, task_ids=None, allow_privileged_apps=None) -> dict:
    """Build the catalog: task_id -> {sites, apps: {site: sub-manifest}, ...}.

    Faithfully consumes the stock ``meta_data.sites: [...]`` task model — N>=1 apps per task
    (multi-app tasks supported, not just single-app). Each sub-manifest is rendered per-run by the
    provisioner (run-chart for docker-run apps, compose-chart for compose apps).

    By default, all tasks under ``tasks_root`` are discovered. ``task_ids`` or
    ``SAAS_PLAYGROUND_TASKS`` can still be used as an allowlist for a pinned catalog. A task is
    skipped if any of its sites is unknown or unsupported. Apps requiring privileged containers or
    hostPath mounts are skipped unless ``allow_privileged_apps`` or
    ``SAAS_PLAYGROUND_ALLOW_PRIVILEGED_APPS`` is enabled for a compatible cluster — no partial runs.
    Rootless OnlyOffice does not require that opt-in.
    """
    tasks_root = tasks_root or os.environ.get("SAAS_PLAYGROUND_TASKS_DIR", "/app/tasks")
    apps_yaml = apps_yaml or os.environ.get("SAAS_PLAYGROUND_APPS_YAML", "/app/saas_bench/apps.yaml")
    registry = registry or os.environ.get("SAAS_PLAYGROUND_REGISTRY", _DEFAULT_REGISTRY)
    if allow_privileged_apps is None:
        allow_privileged_apps = _env_bool("SAAS_PLAYGROUND_ALLOW_PRIVILEGED_APPS", False)

    with open(apps_yaml) as f:
        apps = yaml.safe_load(f)["apps"]
    all_tasks = {t["task_id"]: t for t in load_tasks(tasks_root)}
    if task_ids is None:
        tasks_env = os.environ.get("SAAS_PLAYGROUND_TASKS", "")
        task_ids = [t for t in re.split(r"[,\s]+", tasks_env) if t] or sorted(all_tasks)

    catalog: dict = {}
    for tid in task_ids:
        task = all_tasks.get(tid)
        if task is None:
            print(f"[catalog] requested task {tid!r} not found under {tasks_root}", flush=True)
            continue
        sites = task.get("meta", {}).get("meta_data", {}).get("sites", [])
        known = [s for s in sites if s in apps]
        if not known or len(known) != len(sites):
            print(f"[catalog] skipping {tid!r}: unknown sites in {sites}", flush=True)
            continue
        apps_manifests: dict = {}
        skip_reason = ""
        for app in known:
            try:
                apps_manifests[app] = _load_app_manifest(
                    apps[app], app, apps_yaml, allow_privileged_apps=allow_privileged_apps,
                )
            except (UnsupportedAppError, NotImplementedError, OSError, ValueError) as exc:
                skip_reason = f"{app}: {exc}"
                break
        if skip_reason:
            print(f"[catalog] skipping {tid!r}: {skip_reason}", flush=True)
            continue
        catalog[tid] = {
            "digest": _task_digest(task),
            "sites": known,
            "registry": registry,
            "verify_py": task.get("verify_py_path"),
            "apps": apps_manifests,
            # First 280 chars of description.md as a UX hint for portal /catalog responses.
            # The full description + meta are also kept on the manifest (below) so the service
            # can render the agent prompt via /tasks/{id}/prompt without re-reading from disk.
            "description_summary": (task.get("description_md") or "").strip()[:280],
            # Kept on the manifest so PlaygroundService.build_prompt can call loader.build_prompt
            # for non-Python integrators (the portal is Ruby, so reimplementing build_prompt there
            # would leak the task-file format across the seam — see docs/portal-playground-integration.md).
            "description_md": task.get("description_md") or "",
            "meta":           task.get("meta", {}),
        }
    return catalog


def _build_audit_pool_manifest(catalog: dict, prepare_concurrency: int = 4) -> dict:
    """Build the hidden union-of-apps manifest used by untouched verifier audits.

    Every app manifest comes from the same public task catalog and must be identical anywhere the
    app appears. Including task digests in the pool digest makes status expose catalog drift without
    invalidating a live pool.
    """
    public = {task_id: manifest for task_id, manifest in catalog.items()
              if not manifest.get("internal")}
    if not public:
        raise RuntimeError("cannot build verifier audit pool from an empty catalog")
    apps: dict = {}
    registries: set[str] = set()
    for task_id in sorted(public):
        manifest = public[task_id]
        registries.add(str(manifest.get("registry") or ""))
        for app, app_manifest in manifest.get("apps", {}).items():
            if app in apps and apps[app] != app_manifest:
                raise RuntimeError(f"inconsistent app manifest for {app!r} in task {task_id!r}")
            apps[app] = app_manifest
    if len(registries) != 1:
        raise RuntimeError(f"audit pool requires one registry, found {sorted(registries)!r}")
    canonical_apps = {app: apps[app] for app in sorted(apps)}
    digest_input = {
        "apps": canonical_apps,
        "tasks": {task_id: str(public[task_id].get("digest") or "")
                  for task_id in sorted(public)},
    }
    digest = hashlib.sha256(
        json.dumps(digest_input, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:12]
    return {
        "digest": digest,
        "sites": list(canonical_apps),
        "registry": registries.pop(),
        "verify_py": None,
        "apps": canonical_apps,
        "description_summary": "Internal shared untouched verifier audit pool",
        "description_md": "",
        "meta": {},
        "internal": True,
        "audit_pool": True,
        "prepare_concurrency": prepare_concurrency,
    }


def _load_app_manifest(cfg: dict, app: str, apps_yaml: str, *, allow_privileged_apps: bool = False) -> dict:
    """One app's sub-manifest: a docker-run spec or a compose template, faithfully from apps.yaml."""
    if cfg.get("start_type") == "compose":
        tpl_path = os.path.join(_repo_root(apps_yaml), cfg["compose_template_file"])
        template = open(tpl_path).read()
        probe = parse_compose(
            template,
            prefix="run-probe",
            public_port="80",
            run_host="probe.local",
            allow_privileged=allow_privileged_apps,
        )
        manifest = {
            "kind": "compose",
            "compose_template": template,
            "container_port": probe.web_container_port,
            "allow_privileged": allow_privileged_apps,
            "health_path": str(cfg.get("health_path") or "/"),
            "startup_wait": int(cfg.get("startup_wait") or 300),
            # Mountpaths whose baked image data must be copied into the emptyDir (Docker
            # named-volume-from-image seeding, which emptyDir drops). Opt-in per app.
            "seed_volumes": list(cfg.get("seed_volumes") or []),
        }
        # Optional per-app sizing (web container via cpu/memory/ephemeral_storage; sidecars via
        # sidecar_*). onlyoffice's 4-container stack starves on the compose chart defaults.
        manifest.update(_resource_overrides(cfg))
        return manifest
    spec = load_app_spec(cfg, app)
    manifest = {
        "kind": "run",
        "spec": asdict(spec.services[0]),
        "container_port": spec.web_container_port,
        "health_path": str(cfg.get("health_path") or "/"),
        "startup_wait": int(cfg.get("startup_wait") or 300),
    }
    # Optional per-app request/limit sizing for heavy DB-embedded apps. Omit for chart defaults.
    manifest.update(_resource_overrides(cfg))
    return manifest


_RESOURCE_KEYS = (
    "cpu", "memory", "ephemeral_storage",                              # web/main requests
    "cpu_limit", "memory_limit", "ephemeral_storage_limit",            # web/main limits
    "sidecar_cpu", "sidecar_memory", "sidecar_ephemeral_storage",      # sidecar requests
    "sidecar_cpu_limit", "sidecar_memory_limit",                        # sidecar limits
    "sidecar_ephemeral_storage_limit",
)


def _resource_overrides(cfg: dict) -> dict:
    """Per-app pod sizing from apps.yaml, as strings the provisioner sets on the chart
    (separate requests and limits when both are present). The ``sidecar_*`` keys size compose
    sidecars (mysql/ES/ds); the rest size the web/main container. Returns only the keys present, so
    apps without overrides keep the chart default. Values are stringified verbatim."""
    out: dict = {}
    for key in _RESOURCE_KEYS:
        val = cfg.get(key)
        if val not in (None, ""):
            out[key] = str(val)
    return out


def _repo_root(apps_yaml: str) -> str:
    """Repo root that compose_template_file paths (e.g. 'docker/x.yml.tpl') are relative to."""
    return os.path.dirname(os.path.dirname(os.path.abspath(apps_yaml)))


def build_service() -> PlaygroundService:
    from concurrent.futures import ThreadPoolExecutor
    from saas_bench.run_store import KubectlConfigMapRunStore

    namespace = os.environ.get("SAAS_PLAYGROUND_NAMESPACE", "saas-playground")
    provisioner = HelmProvisioner(
        namespace=namespace,
        run_chart_path=os.environ.get("SAAS_PLAYGROUND_RUN_CHART", "/app/charts/saas-playground-run"),
        compose_chart_path=os.environ.get("SAAS_PLAYGROUND_COMPOSE_CHART", "/app/charts/saas-playground-run-compose"),
        domain=os.environ.get("SAAS_PLAYGROUND_DOMAIN", "saas-playground.example.com"),
        shim_dir=os.environ.get("SAAS_PLAYGROUND_SHIM_DIR", "/app/deploy/shim"),
        public_scheme=os.environ.get("SAAS_PLAYGROUND_PUBLIC_SCHEME", "https"),
        # Comma-separated Secret names, attached to every per-run app pod. Set by the platform
        # chart from .Values.imagePullSecrets; required for a private mw-* registry.
        image_pull_secrets=tuple(
            n.strip() for n in os.environ.get("SAAS_PLAYGROUND_IMAGE_PULL_SECRETS", "").split(",")
            if n.strip()
        ),
    )
    prepare_workers = _env_int("SAAS_PLAYGROUND_PREPARE_WORKERS", 4)
    grade_workers = _env_int("SAAS_PLAYGROUND_GRADE_WORKERS", 4)
    max_active_runs = _env_int("SAAS_PLAYGROUND_MAX_ACTIVE_RUNS", 10)
    catalog = load_catalog()
    catalog[VERIFIER_AUDIT_TASK_ID] = _build_audit_pool_manifest(
        catalog,
        prepare_concurrency=_env_int("SAAS_PLAYGROUND_AUDIT_APP_WORKERS", 4),
    )
    return PlaygroundService(
        catalog=catalog,
        provisioner=provisioner,
        executor=ThreadPoolExecutor(max_workers=prepare_workers, thread_name_prefix="prepare"),
        run_store=KubectlConfigMapRunStore(namespace=namespace),
        max_concurrent_grades=grade_workers,
        max_active_runs=max_active_runs,
    )


def make_app():
    """uvicorn --factory entrypoint.

    Refuses to start if ``PLAYGROUND_API_KEY`` is empty — the service is publicly routed via the
    Gateway, so a misconfigured deploy (External Secrets disabled, secret missing, etc.) would
    otherwise quietly expose ``/prepare``/``/grade``/``/release`` unauthenticated. Local-dev that
    intentionally wants no auth can call ``create_app(service, api_key="")`` directly.
    """
    from saas_bench.playground_api import create_app
    api_key = os.environ.get("PLAYGROUND_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "PLAYGROUND_API_KEY is empty — refusing to start the public service unauthenticated. "
            "Check the ExternalSecret sync (`kubectl get externalsecret -n saas-playground`) or "
            "set the env explicitly for local-dev."
        )
    return create_app(build_service(), api_key=api_key)
