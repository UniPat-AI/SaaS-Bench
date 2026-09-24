"""Kubernetes SlotManager implementation for SaaS-Bench.

This mirrors the Docker SlotManager public interface but provisions apps from
deploy/k8s/apps generated manifests. It uses kubectl and Helm only; no Docker
daemon or docker exec is required.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml


_BASE_PORT = int(os.environ.get("SAAS_BASE_PORT", 30000))
_SLOT_OFFSET = 40
_REPO_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_DIR = _REPO_ROOT / "deploy" / "k8s" / "apps"
_INDEX_PATH = _MANIFEST_DIR / "index.json"
_READY_OK = {200, 301, 302, 303, 401, 403}
_INGRESS_PORT = int(os.environ.get("SAAS_K8S_INGRESS_PORT", "30090"))


def _run(args: list[str], input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _check(args: list[str], input_text: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    result = _run(args, input_text=input_text, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class K8sSlotManager:
    def __init__(self, apps_config: dict, slot_id: int):
        self.apps = apps_config
        self.slot_id = slot_id
        self.slot_prefix = f"s{slot_id}"
        self.port_forwards: dict[str, subprocess.Popen] = {}
        self.namespaces: dict[str, str] = {}
        self.index = json.loads(_INDEX_PATH.read_text(encoding="utf-8"))

    def get_port(self, app: str) -> int:
        return _BASE_PORT + self.slot_id * _SLOT_OFFSET + self.apps[app]["app_index"]

    def get_port_map(self, apps: list[str]) -> dict[str, int]:
        return {app: self.get_port(app) for app in apps}

    def get_container_name(self, app: str) -> str:
        return f"rollout_{self.slot_id}_{app}"

    def start_apps(self, apps: list[str], hostname: str = "localhost") -> None:
        self._ensure_ingress_controller()
        os.environ["SAAS_K8S_NAMESPACE_PREFIX"] = f"saasbench-{self.slot_prefix}"
        for app in apps:
            self._start_one(app)

    def stop_apps(self, apps: list[str]) -> None:
        for app in apps:
            proc = self.port_forwards.pop(app, None)
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
            ns = self.namespaces.get(app)
            if ns:
                _run(["kubectl", "delete", "namespace", ns, "--ignore-not-found=true", "--wait=true", "--timeout=120s"], timeout=150)

    def _start_one(self, app: str) -> None:
        meta = self.index[app]
        ns = f"saasbench-{self.slot_prefix}-{app}"
        self.namespaces[app] = ns
        manifest = _MANIFEST_DIR / f"{app}.yaml"
        docs = list(yaml.safe_load_all(manifest.read_text(encoding="utf-8")))
        for doc in docs:
            if not doc:
                continue
            if doc.get("kind") == "Namespace":
                doc["metadata"]["name"] = ns
            elif "metadata" in doc:
                doc["metadata"]["namespace"] = ns
            if doc.get("kind") == "Service":
                doc["spec"]["type"] = "ClusterIP"
                for port in doc["spec"].get("ports", []):
                    port.pop("nodePort", None)
        _check(["kubectl", "apply", "-f", "-"], input_text=yaml.safe_dump_all(docs, sort_keys=False), timeout=120)
        _check(["kubectl", "-n", ns, "rollout", "status", f"deploy/{app}", "--timeout=1200s"], timeout=1260)
        self._create_ingress(app, ns)
        self._start_port_forward(app, ns)
        self._wait_ready(app)

    def _create_ingress(self, app: str, ns: str) -> None:
        host = f"{self.slot_prefix}-{app}.saasbench.localhost"
        ingress = {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {"name": f"{self.slot_prefix}-{app}", "namespace": ns},
            "spec": {
                "ingressClassName": "nginx",
                "rules": [{
                    "host": host,
                    "http": {"paths": [{
                        "path": "/",
                        "pathType": "Prefix",
                        "backend": {"service": {"name": app, "port": {"number": 80}}},
                    }]},
                }],
            },
        }
        _check(["kubectl", "apply", "-f", "-"], input_text=yaml.safe_dump(ingress, sort_keys=False), timeout=60)

    def _start_port_forward(self, app: str, ns: str) -> None:
        port = self.get_port(app)
        proc = subprocess.Popen(
            ["kubectl", "-n", ns, "port-forward", f"service/{app}", f"{port}:80", "--address", "127.0.0.1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.port_forwards[app] = proc

    def _wait_ready(self, app: str) -> None:
        cfg = self.apps[app]
        path = cfg.get("health_path", "/")
        timeout = int(cfg.get("startup_wait", 600))
        port = self.get_port(app)
        host = f"{self.slot_prefix}-{app}.saasbench.localhost"
        deadline = time.time() + timeout
        last = "no attempt"
        while time.time() < deadline:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{path}",
                    headers={"Host": host, "User-Agent": "saasbench-k8s/1.0"},
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception as exc:
                status = None
                last = f"{type(exc).__name__}: {exc}"
            if status in _READY_OK:
                return
            if status is not None:
                last = f"HTTP {status}"
            time.sleep(2)
        raise RuntimeError(f"k8s app {app} not ready: {last}")

    def _ensure_ingress_controller(self) -> None:
        existing = _run(["kubectl", "get", "ingressclass", "nginx"])
        if existing.returncode == 0:
            return
        _run(["helm", "repo", "add", "ingress-nginx", "https://kubernetes.github.io/ingress-nginx"], timeout=60)
        _run(["helm", "repo", "update"], timeout=120)
        _check([
            "helm", "upgrade", "--install", "ingress-nginx", "ingress-nginx/ingress-nginx",
            "--namespace", "ingress-nginx",
            "--create-namespace",
            "--set", "controller.service.type=NodePort",
            "--set", f"controller.service.nodePorts.http={_INGRESS_PORT}",
            "--wait",
            "--timeout", "10m",
        ], timeout=660)
