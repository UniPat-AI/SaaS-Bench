"""HelmProvisioner — the real ``Provisioner``: a per-run preview env via ``helm`` + ``kubectl``.

  up    -> ``helm upgrade --install run-<id> <run-chart> --set ...``, then resolve the pod name
  grade -> run the task's ``verify.py`` service-side with ``env=verify_context`` and a
           ``docker``->``kubectl exec`` shim on PATH (upstream verifiers run unmodified), then parse
           the SCORE/checks via ``verify_runner``
  down  -> ``helm uninstall run-<id>``

Shells out via an injectable ``runner`` (default ``subprocess.run``) so command construction can be
asserted without a cluster; the behavior itself needs a real cluster to exercise.

Multi-app tasks use one release and public hostname per app, aggregated under one service run.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import yaml

from saas_bench.app_spec import ServiceSpec, parse_compose, render_compose_values, render_run_values
from saas_bench.playground_service import Provisioner
from saas_bench.verify_proxy import loopback_port_map
from saas_bench.verify_runner import (
    SITE_CONFIG,
    _parse_verify_output,
    normalize_judge_env,
    redact_verifier_secrets,
)

# Runner: (argv, **kwargs) -> object with .returncode/.stdout/.stderr (subprocess.run-compatible).
Runner = Callable[..., "subprocess.CompletedProcess"]

_PUBLIC_READY_CODES = frozenset({200, 201, 202, 204, 301, 302, 303, 307, 308, 401, 403})


def _sub_run_id(run_id: str, apps_map: dict, site: str) -> str:
    """The per-app release/host id. Shared by ``up`` and ``grade`` on purpose: grade rebuilds each
    app's Service DNS name and public host from the manifest, so if the two derived it separately a
    rename here would silently point grading at a nonexistent Service."""
    return f"{run_id}-{site}" if len(apps_map) > 1 else run_id


@dataclass
class HelmProvisioner(Provisioner):
    namespace: str = "saas-playground"
    run_chart_path: str = "charts/saas-playground-run"               # docker-run apps (1 container)
    compose_chart_path: str = "charts/saas-playground-run-compose"   # compose apps (multi-container pod)
    domain: str = "saas-playground.example.com"   # external host = <run_id>.<domain>
    shim_dir: str = ""                        # dir holding the docker->kubectl shim (prepended to PATH for grade)
    # Backward-compatible default for manifests without apps.yaml startup_wait. Real app manifests
    # use their per-app startup_wait, because async prepare can legitimately outlive old proxy
    # request windows while Helm still needs to wait for slow apps to become Ready.
    helm_timeout: str = "5m"
    cleanup_timeout: str = "120s"
    grade_timeout: int = 300
    public_port: str = "80"                   # bound to apps.yaml's {port}; per-run envs serve HTTP on the Gateway :80 listener
    # Browser-facing scheme for access_urls and origin-bearing app env (e.g. twenty's SERVER_URL).
    # The edge (Cloudflare) terminates TLS and reaches the Gateway's :80 listener; browsers
    # auto-upgrade navigations to https, so the canonical public origin is https://<host>.
    public_scheme: str = "https"
    public_probe_timeout: float = 5.0
    # Names of imagePullSecrets to attach to every per-run app pod. The mw-* images normally sit in
    # a private registry; without this the run pod cannot pull them and prepare surfaces only as an
    # opaque `helm --wait` timeout. Set from the platform chart's .Values.imagePullSecrets.
    image_pull_secrets: tuple = ()
    runner: Runner = subprocess.run

    def _pull_secret_args(self) -> list:
        """``--set-json imagePullSecrets=[{"name": ...}]`` for the run charts, or nothing."""
        names = [n for n in self.image_pull_secrets if str(n).strip()]
        if not names:
            return []
        return ["--set-json",
                "imagePullSecrets=" + json.dumps([{"name": str(n).strip()} for n in names])]

    def _run(self, argv, env=None, timeout=None, check=True):
        proc = self.runner(argv, capture_output=True, text=True, env=env, timeout=timeout)
        if check and proc.returncode != 0:
            raise RuntimeError(
                f"command failed ({argv[0]} rc={proc.returncode}): {proc.stderr.strip()[:500]}"
            )
        return proc

    def up(self, task_id: str, run_id: str, manifest: dict) -> dict:
        """Provision the per-run env. The catalog gives N>=1 apps in manifest['apps']; each is its
        own helm release on its own ``<run_id>[-<app>].<domain>`` subdomain (stock has one host per
        app via port offsets — we faithfully give each app its own URL)."""
        apps_map = manifest["apps"]
        access_urls: dict = {}
        verify_context: dict = {"namespace": self.namespace}
        runtime_identity: dict = {}
        first_host = first_pod = ""

        def provision_app(item: tuple[str, dict]) -> tuple[str, str, dict, str, str, str, dict]:
            site, sub = item
            sub_run_id = _sub_run_id(run_id, apps_map, site)
            host = f"{sub_run_id}.{self.domain}"
            sub_manifest = {**sub, "registry": manifest["registry"]}
            if sub.get("kind") == "compose":
                self._up_compose(sub_run_id, host, sub_manifest)
            else:
                self._up_run(sub_run_id, host, sub_manifest)
            pod_runtime = self._pod_runtime(sub_run_id)
            pod = pod_runtime["pod_name"]
            return (
                site,
                f"{self.public_scheme}://{host}",
                self._verify_vars(site, pod, host),
                host,
                pod,
                sub_run_id,
                pod_runtime,
            )

        try:
            items = list(apps_map.items())
            concurrency = min(len(items), max(1, int(manifest.get("prepare_concurrency") or 1)))
            if concurrency > 1:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    prepared = list(pool.map(provision_app, items))
            else:
                prepared = [provision_app(item) for item in items]
            for site, access_url, app_context, host, pod, sub_run_id, pod_runtime in prepared:
                access_urls[site] = access_url
                verify_context.update(app_context)
                runtime_identity[sub_run_id] = pod_runtime
                if not first_host:
                    first_host, first_pod = host, pod
        except Exception:
            # Roll back any per-app releases that did succeed. PlaygroundService also performs an
            # idempotent cleanup pass and retains the durable run handle if cleanup stays uncertain.
            try:
                self.down(run_id)
            except Exception as cleanup_exc:
                print(f"[provisioner] cleanup after partial up failed: {cleanup_exc}", flush=True)
            raise
        # The PUBLIC single-host view, used for access URLs and preflight probes. Note that grading
        # does NOT use this: `grade` overrides SERVER_HOSTNAME and every <APP>_PORT with the
        # loopback port map, because a first-app hostname plus a shared port 80 makes stock
        # `{HOST}:{<APP>_PORT}` URL building resolve to the wrong app (see _loopback_targets).
        verify_context["SERVER_HOSTNAME"] = first_host
        verify_context["pod"] = first_pod
        return {
            "access_urls": access_urls,
            "verify_context": verify_context,
            "runtime_identity": runtime_identity,
        }

    def recover(self, task_id: str, run_id: str, manifest: dict) -> dict:
        """Rebuild runtime context from deterministic hosts and stable pod labels.

        Every expected app must have exactly one Running/Ready pod; otherwise the caller receives a
        clean recovery error while `/release` remains available. The returned live identity is
        adopted only for legacy schema-1 records; schema-2 runs retain their prepared identity.
        """
        apps_map = manifest["apps"]
        access_urls: dict = {}
        verify_context: dict = {"namespace": self.namespace}
        runtime_identity: dict = {}
        first_host = first_pod = ""
        for site in apps_map:
            sub_run_id = _sub_run_id(run_id, apps_map, site)
            host = f"{sub_run_id}.{self.domain}"
            pod_runtime = self._pod_runtime(sub_run_id)
            pod = pod_runtime["pod_name"]
            access_urls[site] = f"{self.public_scheme}://{host}"
            verify_context.update(self._verify_vars(site, pod, host))
            runtime_identity[sub_run_id] = pod_runtime
            if not first_host:
                first_host, first_pod = host, pod
        verify_context["SERVER_HOSTNAME"] = first_host
        verify_context["pod"] = first_pod
        return {
            "access_urls": access_urls,
            "verify_context": verify_context,
            "runtime_identity": runtime_identity,
        }

    def probe_access_urls(self, access_urls: dict, manifest: dict) -> dict[str, bool]:
        """Probe every pending app through its public hostname.

        Helm readiness only proves the pod/container path. This deliberately traverses the same
        external Gateway/Cloudflare route the agent uses and targets each app's configured health
        path. Probes run concurrently so a four-app status poll costs one timeout, not four.
        """
        def probe(item: tuple[str, str]) -> tuple[str, bool]:
            site, base_url = item
            health_path = manifest.get("apps", {}).get(site, {}).get("health_path", "/")
            url = urllib.parse.urljoin(base_url.rstrip("/") + "/", str(health_path).lstrip("/"))
            try:
                req = urllib.request.Request(
                    url, method="GET", headers={"User-Agent": "saas-playground-readiness/1"},
                )
                with urllib.request.urlopen(req, timeout=self.public_probe_timeout) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception:
                status = 0
            return site, status in _PUBLIC_READY_CODES

        if not access_urls:
            return {}
        with ThreadPoolExecutor(max_workers=min(4, len(access_urls))) as pool:
            return dict(pool.map(probe, access_urls.items()))

    def _up_run(self, run_id: str, host: str, manifest: dict) -> int:
        """docker-run app: render the parsed `start:` spec, bind {hostname}/{port} to the public address."""
        spec = ServiceSpec(**manifest["spec"])
        vals = render_run_values(spec, run_host=host, registry=manifest["registry"],
                                 public_port=self.public_port, public_scheme=self.public_scheme)
        args = [
            "helm", "upgrade", "--install", f"run-{run_id}", self.run_chart_path,
            "-n", self.namespace,
            "--set", f"runId={run_id}",
            "--set", f"image={vals['image']}",
            "--set", f"containerPort={vals['containerPort']}",
            "--set", f"host={host}",
        ] + self._pull_secret_args()
        if vals["extraEnv"]:
            args += ["--set-json", f"extraEnv={json.dumps(vals['extraEnv'])}"]
        if vals["command"]:
            args += ["--set-json", f"command={json.dumps(vals['command'])}"]
        if str(vals["runAsUser"]):
            args += ["--set", f"securityContext.runAsUser={vals['runAsUser']}"]
        args += self._resource_args(manifest)
        args += ["--wait", "--timeout", self._helm_timeout(manifest)]
        self._run(args)
        return vals["containerPort"]

    def _resource_args(self, manifest: dict) -> list:
        """``--set`` overrides for per-app resource requests and limits.

        A request override without an explicit limit retains the legacy request==limit behavior.
        Heavy apps specify both; apps without overrides keep the chart defaults.
        """
        out: list = []
        for request_field, limit_field, key in (
            ("cpu", "cpu_limit", "cpu"),
            ("memory", "memory_limit", "memory"),
            ("ephemeral_storage", "ephemeral_storage_limit", "ephemeral-storage"),
        ):
            request = manifest.get(request_field)
            limit = manifest.get(limit_field)
            if request not in (None, ""):
                out += ["--set", f"resources.requests.{key}={request}"]
            if limit not in (None, ""):
                out += ["--set", f"resources.limits.{key}={limit}"]
            elif request not in (None, ""):
                out += ["--set", f"resources.limits.{key}={request}"]
        return out

    @staticmethod
    def _resource_block(cpu, memory, eph, cpu_limit=None, memory_limit=None, eph_limit=None) -> dict:
        """Build a resources block from optional requests and limits.

        A missing limit falls back to its request for backward compatibility.
        """
        requests: dict = {}
        limits: dict = {}
        for request, limit, key in (
            (cpu, cpu_limit, "cpu"),
            (memory, memory_limit, "memory"),
            (eph, eph_limit, "ephemeral-storage"),
        ):
            if request not in (None, ""):
                requests[key] = str(request)
            if limit not in (None, ""):
                limits[key] = str(limit)
            elif request not in (None, ""):
                limits[key] = str(request)
        return {"requests": requests, "limits": limits} if requests or limits else {}

    def _up_compose(self, run_id: str, host: str, manifest: dict) -> int:
        """compose app: render the stock template into a multi-container pod (values passed as a file
        since the structure — sidecars/hostAliases/volumes — is too nested for --set)."""
        app = parse_compose(manifest["compose_template"], prefix=f"run-{run_id}",
                            public_port=self.public_port, run_host=host,
                            public_scheme=self.public_scheme,
                            allow_privileged=bool(manifest.get("allow_privileged")))
        vals = render_compose_values(app, registry=manifest["registry"],
                                     seed_volumes=manifest.get("seed_volumes"))
        # Per-app sizing (passed in the values file, not --set, since resources are nested). The web
        # container uses cpu/memory/ephemeral_storage; sidecars use sidecar_*. Omitted → chart default.
        main_res = self._resource_block(
            manifest.get("cpu"), manifest.get("memory"), manifest.get("ephemeral_storage"),
            manifest.get("cpu_limit"), manifest.get("memory_limit"),
            manifest.get("ephemeral_storage_limit"),
        )
        side_res = self._resource_block(
            manifest.get("sidecar_cpu"), manifest.get("sidecar_memory"),
            manifest.get("sidecar_ephemeral_storage"), manifest.get("sidecar_cpu_limit"),
            manifest.get("sidecar_memory_limit"), manifest.get("sidecar_ephemeral_storage_limit"),
        )
        if main_res:
            vals["resources"] = main_res
        if side_res:
            vals["sidecarResources"] = side_res
        names = [str(n).strip() for n in self.image_pull_secrets if str(n).strip()]
        if names:
            vals["imagePullSecrets"] = [{"name": n} for n in names]
        fd, values_path = tempfile.mkstemp(prefix=f"run-{run_id}-", suffix=".yaml")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.safe_dump(vals, fh)
            self._run([
                "helm", "upgrade", "--install", f"run-{run_id}", self.compose_chart_path,
                "-n", self.namespace,
                "--set", f"runId={run_id}",
                "--set", f"host={host}",
                "-f", values_path,
                "--wait", "--timeout", self._helm_timeout(manifest),
            ])
        finally:
            try:
                os.unlink(values_path)
            except OSError:
                pass
        return vals["webContainerPort"]

    def _helm_timeout(self, manifest: dict) -> str:
        try:
            startup_wait = int(manifest.get("startup_wait") or 0)
        except (TypeError, ValueError):
            startup_wait = 0
        if startup_wait <= 0:
            return self.helm_timeout
        # Give Helm a small buffer beyond the app-level readiness budget to account for scheduling,
        # image streaming, and NEG readiness gates, while preserving the default fixed timeout floor.
        return f"{max(startup_wait + 60, self._helm_timeout_seconds())}s"

    def _helm_timeout_seconds(self) -> int:
        timeout = str(self.helm_timeout).strip()
        try:
            if timeout.endswith("h"):
                return int(float(timeout[:-1]) * 3600)
            if timeout.endswith("m"):
                return int(float(timeout[:-1]) * 60)
            if timeout.endswith("s"):
                return int(float(timeout[:-1]))
            return int(float(timeout))
        except (TypeError, ValueError):
            return 300

    def _verify_vars(self, site: str, pod: str, host: str) -> dict:
        """verify.py env for one app, per the stock SITE_CONFIG contract.

        ``<APP>_PORT`` here is the PUBLIC port (the Gateway's HTTP listener, same as the access URL
        the agent uses) and ``<APP>_HOSTNAME`` the per-app public hostname. This is the PROVISION-
        time view; it is what preflight probes and recovery use.

        **Grading overrides both.** Stock has one host (localhost) for all of a task's apps and
        disambiguates them by port; we have the inverse (one port, per-app subdomains), so a stock
        verifier's ``f"http://{HOST}:{X_PORT}"`` would resolve to whichever app happened to be
        first. ``grade`` therefore stands up a loopback port map for the verifier's lifetime and
        rewrites ``SERVER_HOSTNAME`` / ``<APP>_PORT`` / ``<APP>_HOSTNAME`` to it, restoring the
        shape the verifiers were written against. See ``verify_proxy`` and ``_loopback_targets``.

        The app's internal container port is irrelevant either way — verifiers that connect from
        inside the container reach the app via localhost on the app-known port.

        The docker->kubectl shim execs whatever ``<APP>_CONTAINER`` / ``<APP>_DB_CONTAINER`` name
        we set: a "" db_suffix means the DB is reachable from the app container (either embedded
        in the same container — baserow/openproject/twenty — OR a networked sidecar the web
        container talks to via hostname — pretix); a real suffix (e.g. ``-postgres`` for
        mattermost, ``-mariadb`` for owncloud) means the verifier execs DIRECTLY into the DB
        sidecar (addressed as ``<pod>::<container>``).

        **Pretix nuance:** pretix is compose with a separate ``$prefix-db`` postgres sidecar, but
        stock SITE_CONFIG.pretix uses ``db_suffix=""``. That's deliberate, not a bug: pretix's
        verifier exec's into the WEB container and reaches postgres over the network (via the
        ``$prefix-db`` hostname, which on K8s resolves to localhost through hostAliases). Compare
        mattermost (``-postgres``) where the verifier execs INTO the postgres container directly.
        """
        up = site.upper().replace("-", "_")
        cfg = SITE_CONFIG.get(site)
        if cfg is None:
            return {f"{up}_CONTAINER": pod, f"{up}_PORT": str(self.public_port),
                    f"{up}_HOSTNAME": host}
        out = {cfg["container_var"]: pod, cfg["port_var"]: str(self.public_port),
               f"{up}_HOSTNAME": host}
        if cfg.get("db_var"):
            suffix = cfg.get("db_suffix") or ""
            out[cfg["db_var"]] = pod if suffix == "" else f"{pod}::{suffix.lstrip('-_')}"
        return out

    def _loopback_targets(self, run_id: str, manifest: dict) -> dict:
        """Per-app ``(cluster Service DNS, port, public host)`` for the grade-time port map.

        Rebuilt from the manifest rather than read from verify_context: the context is persisted in
        the run's ConfigMap and may predate a chart change, whereas the Service name is derived by the
        chart's ``run.fullname`` (``run-<runId>``, identical in both run charts) from the same
        ``_sub_run_id`` that ``up`` installed with. ``Host`` must carry the PUBLIC host because the
        app was configured with it (``{hostname}`` in apps.yaml).
        """
        apps_map = manifest.get("apps") or {}
        targets = {}
        for site in apps_map:
            sub_run_id = _sub_run_id(run_id, apps_map, site)
            targets[site] = (
                f"run-{sub_run_id}.{self.namespace}.svc.cluster.local",
                80,                                    # both run charts' Service exposes port 80
                f"{sub_run_id}.{self.domain}",
            )
        return targets

    def _loopback_verify_vars(self, ports: dict) -> dict:
        """Point ``SERVER_HOSTNAME`` and every ``<APP>_PORT`` at the loopback map.

        Empty ports (a manifest with no apps) changes nothing — better to leave the public-host
        context in place than to hand a verifier a 127.0.0.1 with nothing listening on it.
        """
        if not ports:
            return {}
        out = {"SERVER_HOSTNAME": "127.0.0.1"}
        for site, port in ports.items():
            cfg = SITE_CONFIG.get(site)
            up = site.upper().replace("-", "_")
            out[cfg["port_var"] if cfg else f"{up}_PORT"] = str(port)
            out[f"{up}_HOSTNAME"] = "127.0.0.1"
        return out

    def _pod_runtime(self, run_id: str) -> dict:
        """Resolve the sole Ready pod and return its non-secret state identity.

        A Deployment replacement changes the pod UID/name. A container restart within the same pod
        increments ``restartCount`` and can reset writable-layer mutations. Both invalidate a
        benchmark environment whose agent work must remain unchanged through grading.
        """
        proc = self._run([
            "kubectl", "get", "pods", "-n", self.namespace,
            "-l", f"saas-playground.run-id={run_id}",
            "-o", "json",
        ])
        try:
            items = json.loads(proc.stdout or "{}").get("items", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError(f"cannot parse pod list for run-id={run_id!r}: {exc}") from exc

        def ready(item: dict) -> bool:
            meta = item.get("metadata") or {}
            status = item.get("status") or {}
            conditions = status.get("conditions") or []
            return (
                not meta.get("deletionTimestamp")
                and status.get("phase") == "Running"
                and any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
            )

        live = [item for item in items if ready(item)]
        if len(live) != 1:
            names = [(item.get("metadata") or {}).get("name", "") for item in items]
            raise RuntimeError(
                f"expected exactly one Running/Ready pod for run-id={run_id!r}, "
                f"found {len(live)} (all={names})"
            )
        pod = live[0]
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        restarts = {}
        for entry in (
            (status.get("initContainerStatuses") or [])
            + (status.get("containerStatuses") or [])
        ):
            name = str(entry.get("name") or "")
            if name:
                restarts[name] = int(entry.get("restartCount") or 0)
        return {
            "pod_name": str(metadata.get("name") or ""),
            "pod_uid": str(metadata.get("uid") or ""),
            "container_restarts": restarts,
        }

    def _pod_name(self, run_id: str) -> str:
        """Resolve exactly one non-terminating Running/Ready pod for a stable run label."""
        return self._pod_runtime(run_id)["pod_name"]

    @staticmethod
    def _grade_error(task_id: str, message: str) -> dict:
        return {
            "task_id": task_id, "status": "ERROR", "score": 0.0,
            "earned": 0, "total": 0, "all_pass": False, "checks": [],
            "returncode": None, "error": message, "warnings": [],
        }

    def _live_verify_context(
        self, run_id: str, manifest: dict, verify_context: dict,
        runtime_identity: dict | None = None,
    ) -> dict:
        """Resolve live verifier targets, rejecting a reset prepared environment."""
        refreshed = dict(verify_context)
        apps_map = manifest["apps"]
        for site in apps_map:
            sub_run_id = _sub_run_id(run_id, apps_map, site)
            current_runtime = self._pod_runtime(sub_run_id)
            current_pod = current_runtime["pod_name"]
            up = site.upper().replace("-", "_")
            cfg = SITE_CONFIG.get(site)
            container_var = cfg["container_var"] if cfg else f"{up}_CONTAINER"
            prepared_pod = str(refreshed.get(container_var, "")).split("::", 1)[0]
            expected_runtime = (runtime_identity or {}).get(sub_run_id)
            if expected_runtime:
                if expected_runtime != current_runtime:
                    raise RuntimeError(
                        f"prepared environment was reset for app {site!r}: "
                        f"prepared={expected_runtime!r}, current={current_runtime!r}; "
                        "release this run and prepare a new one"
                    )
            elif prepared_pod and prepared_pod != current_pod:
                # Compatibility for direct callers and migrated schema-1 runs without a baseline.
                raise RuntimeError(
                    f"prepared environment pod was replaced for app {site!r}: "
                    f"prepared={prepared_pod}, current={current_pod}; "
                    "release this run and prepare a new one"
                )
            host = refreshed.get(f"{up}_HOSTNAME", "")
            refreshed.update(self._verify_vars(site, current_pod, host))
        return refreshed

    def grade(
        self,
        task_id: str,
        run_id: str,
        manifest: dict,
        verify_context: dict,
        runtime_identity: dict | None = None,
    ) -> dict:
        verify_py = manifest.get("verify_py")
        if not verify_py:
            return {"task_id": task_id, "status": "SKIP", "score": 0.0, "checks": [],
                    "error": "no verify.py in manifest", "warnings": []}
        try:
            live_context = self._live_verify_context(
                run_id, manifest, verify_context, runtime_identity,
            )
        except Exception as exc:
            return self._grade_error(task_id, f"target preflight failed: {exc}")
        env = os.environ.copy()
        # The verifier never needs the public platform bearer. Do not expose it to task code.
        env.pop("PLAYGROUND_API_KEY", None)
        # Match the local runner's generic LLM_* compatibility input while giving verifier judges
        # their own provider-neutral configuration contract.
        normalize_judge_env(env)
        env.update({k: v for k, v in live_context.items() if isinstance(v, str)})
        env["SAAS_PLAYGROUND_NAMESPACE"] = self.namespace    # read by the docker->kubectl shim
        if self.shim_dir:
            env["PATH"] = f"{self.shim_dir}:{env.get('PATH', '')}"
        try:
            # Give the verifier the stock env SHAPE — one host, a distinct port per app — for as
            # long as it runs. See verify_proxy: on K8s the apps differ by hostname and share port
            # 80, so `f"http://{HOST}:{X_PORT}"` (140 such sites across the tasks) silently hit the
            # first app. Only live for this subprocess, so nothing is listening between grades.
            with loopback_port_map(self._loopback_targets(run_id, manifest)) as ports:
                env.update(self._loopback_verify_vars(ports))
                proc = self.runner(
                    ["python3", verify_py], capture_output=True, text=True, env=env,
                    timeout=self.grade_timeout,
                )
        except subprocess.TimeoutExpired:
            # Mirror verify_runner.run_verify's behavior — clean ERROR payload, not a 500.
            return self._grade_error(
                task_id, f"verify.py timeout after {self.grade_timeout}s",
            )
        except OSError as exc:
            return self._grade_error(task_id, f"verify loopback port map failed: {exc}")
        try:
            # Close the preflight/verify race: a pod or container can restart while a long verifier
            # is running. Never publish a score produced across two different app runtimes.
            self._live_verify_context(run_id, manifest, live_context, runtime_identity)
        except Exception as exc:
            return self._grade_error(task_id, f"target postflight failed: {exc}")
        parsed = _parse_verify_output(redact_verifier_secrets(proc.stderr, env))
        status = (
            "ERROR" if parsed["has_errors"]
            else ("PASS" if parsed["all_pass"] else ("FAIL" if parsed["checks"] else "ERROR"))
        )
        return {
            "task_id": task_id, "status": status,
            "score": parsed["score"],
            "earned": parsed["earned"], "total": parsed["total"], "all_pass": parsed["all_pass"],
            "checks": parsed["checks"], "returncode": proc.returncode,
            "error": (
                "one or more verifier checks errored" if parsed["has_errors"]
                else ("verifier produced no checks" if not parsed["checks"] else None)
            ),
            "warnings": [],
        }

    def down(self, run_id: str) -> None:
        """Tear down every per-run release for ``run_id``. Raises ``RuntimeError`` if any uninstall
        failed — callers (PlaygroundService.release, the multi-app cleanup path) MUST surface the
        failure so the registry isn't dropped while resources still leak in the cluster.

        Uses ``helm list --all -o json`` so failed/pending releases (a timed-out partial install
        from the multi-app rollback path) are caught too — the default ``helm list`` shows only
        ``deployed``. Then filters out ``uninstalled`` releases python-side to avoid spurious
        ``release: not loaded`` errors on a previously-cleaned run_id.
        """
        # helm list MUST succeed — silently swallowing a non-zero exit / unparseable JSON would
        # treat the release set as empty and return success while live cluster objects linger.
        proc = self._run(
            ["helm", "list", "--all", "-o", "json", "-n", self.namespace,
             "--filter", f"^run-{run_id}(-.*)?$"],
        )       # default check=True: rc != 0 raises RuntimeError, propagated to the caller
        try:
            listed = json.loads(proc.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"helm list returned unparseable JSON for run_id={run_id!r}: {exc}; "
                f"stdout={proc.stdout[:200]!r}"
            ) from exc
        releases = [r["name"] for r in listed if r.get("status") != "uninstalled"]
        failed: list = []
        for release in releases:
            p = self._run(
                [
                    "helm", "uninstall", release, "-n", self.namespace,
                    "--wait", "--timeout", self.cleanup_timeout,
                ],
                check=False,
            )
            if p.returncode != 0:
                failed.append((release, (p.stderr or "").strip()[:200]))
        if failed:
            raise RuntimeError(
                f"helm uninstall failed for {len(failed)} release(s): "
                + "; ".join(f"{r}: {e}" for r, e in failed)
            )
        self._delete_prefixed_resources(run_id)

    def _delete_prefixed_resources(self, run_id: str) -> None:
        """Remove objects still visible after Helm uninstall.

        Helm can time out a pending install and remove release metadata while child objects are
        still terminating. The playground must report cleanup only after those obvious prefixed
        resources are gone, otherwise failed async prepares leave pods behind until the TTL job.
        """
        prefix = f"run-{run_id}"
        proc = self._run(
            [
                "kubectl", "get",
                "deployment,pod,service", "-n", self.namespace,
                "-o", "name", "--ignore-not-found",
            ],
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                "kubectl get failed while checking leftover run resources: "
                f"{(proc.stderr or '').strip()[:300]}"
            )
        names = []
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            _, _, name = line.partition("/")
            if name.startswith(prefix):
                names.append(line)
        if not names:
            return
        p = self._run(
            [
                "kubectl", "delete", "-n", self.namespace, *names,
                "--ignore-not-found", "--wait=true", "--timeout", self.cleanup_timeout,
            ],
            check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(
                "kubectl delete failed while cleaning leftover run resources: "
                f"{(p.stderr or '').strip()[:300]}"
            )
