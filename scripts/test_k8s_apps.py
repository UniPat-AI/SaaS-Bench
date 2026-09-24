#!/usr/bin/env python3
"""Sequentially test generated SaaS-Bench Kubernetes app manifests."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

import yaml
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "deploy" / "k8s" / "apps" / "index.json"
OK_HTTP = {200, 301, 302, 303, 401, 403}


def run(
    args: list[str],
    timeout: int | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def probe(url: str, timeout_s: int, host_header: str = "") -> tuple[bool, str]:
    deadline = time.time() + timeout_s
    last = "no attempt"
    headers = {"User-Agent": "saasbench-k8s-test/1.0"}
    if host_header:
        headers["Host"] = host_header
    request = Request(url, headers=headers)
    while time.time() < deadline:
        try:
            with urlopen(request, timeout=5) as response:
                if response.status in OK_HTTP:
                    return True, f"HTTP {response.status}"
                last = f"HTTP {response.status}"
        except HTTPError as exc:
            if exc.code in OK_HTTP:
                return True, f"HTTP {exc.code}"
            last = f"HTTP {exc.code}"
        except URLError as exc:
            last = str(exc)
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(1)
    return False, last


def delete_namespace(namespace: str) -> None:
    run(["kubectl", "delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true", "--timeout=120s"])


def ensure_ingress_controller(ingress_class: str, ingress_port: int) -> tuple[bool, str]:
    existing = run(["kubectl", "get", "ingressclass", ingress_class])
    if existing.returncode == 0:
        return True, "already installed"

    run(["helm", "repo", "add", "ingress-nginx", "https://kubernetes.github.io/ingress-nginx"])
    run(["helm", "repo", "update"])
    installed = run([
        "helm", "upgrade", "--install", "ingress-nginx", "ingress-nginx/ingress-nginx",
        "--namespace", "ingress-nginx",
        "--create-namespace",
        "--set", "controller.service.type=NodePort",
        "--set", f"controller.service.nodePorts.http={ingress_port}",
        "--wait",
        "--timeout", "10m",
    ], timeout=660)
    if installed.returncode != 0:
        return False, installed.stderr or installed.stdout
    return True, "installed"


def create_ingress(namespace: str, app: str, slot_prefix: str, ingress_class: str) -> tuple[bool, str, str]:
    name = f"{slot_prefix}-{app}"
    host = f"{slot_prefix}-{app}.saasbench.localhost"
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "ingressClassName": ingress_class,
            "rules": [{
                "host": host,
                "http": {"paths": [{
                    "path": "/",
                    "pathType": "Prefix",
                    "backend": {
                        "service": {
                            "name": app,
                            "port": {"number": 80},
                        }
                    },
                }]},
            }],
        },
    }
    applied = run(
        ["kubectl", "apply", "-f", "-"],
        input_text=yaml.safe_dump(manifest, sort_keys=False),
    )
    if applied.returncode != 0:
        return False, host, applied.stderr or applied.stdout
    return True, host, ""


def collect_logs(namespace: str, app: str) -> str:
    ps = run(["kubectl", "-n", namespace, "get", "pods", "-o", "wide"])
    desc = run(["kubectl", "-n", namespace, "describe", "deploy", app])
    logs = run(["kubectl", "-n", namespace, "logs", f"deploy/{app}", "--all-containers=true", "--tail=120"])
    return "\n".join([
        "== pods ==", ps.stdout or "", ps.stderr or "",
        "== describe ==", desc.stdout or "", desc.stderr or "",
        "== logs ==", logs.stdout or "", logs.stderr or "",
    ])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apps", nargs="*", help="app names; default all")
    parser.add_argument("--rollout-timeout", type=int, default=1200)
    parser.add_argument("--probe-timeout", type=int, default=120)
    parser.add_argument("--cleanup", action="store_true", help="delete app namespaces after testing")
    parser.add_argument("--keep-failed", action="store_true", help="with --cleanup, keep failed app namespaces")
    parser.add_argument("--no-ingress", action="store_true", help="do not create per-app Ingress resources")
    parser.add_argument("--slot-prefix", default="s0", help="slot prefix used in Ingress names and hosts")
    parser.add_argument("--ingress-class", default="nginx", help="Ingress class name")
    parser.add_argument("--ingress-port", type=int, default=30090, help="host port for Ingress controller and URLs")
    args = parser.parse_args()

    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    apps = args.apps or sorted(index)
    results = []
    failed = False

    if not args.no_ingress:
        controller_ok, controller_detail = ensure_ingress_controller(args.ingress_class, args.ingress_port)
        print(f"ingress controller: {'PASS' if controller_ok else 'FAIL'} ({controller_detail})", flush=True)
        failed = failed or not controller_ok

    for app in apps:
        meta = index[app]
        namespace = meta["namespace"]
        manifest = ROOT / meta["manifest"]
        url = f"http://127.0.0.1:{meta['nodePort']}{meta['healthPath']}"
        ingress_host = f"{args.slot_prefix}-{app}.saasbench.localhost"
        result = {"app": app, "status": "UNKNOWN", "url": url}

        print(f"\n===== {app} =====", flush=True)
        print(f"apply {manifest}", flush=True)

        try:
            applied = run(["kubectl", "apply", "-f", str(manifest)])
            if applied.returncode != 0:
                result.update({
                    "status": "APPLY_FAIL",
                    "error": applied.stderr or applied.stdout,
                })
                failed = True
                continue

            rollout = run([
                "kubectl", "-n", namespace, "rollout", "status", f"deploy/{app}",
                f"--timeout={args.rollout_timeout}s",
            ])
            if rollout.returncode != 0:
                result.update({
                    "status": "ROLLOUT_FAIL",
                    "error": rollout.stderr or rollout.stdout,
                    "logs": collect_logs(namespace, app),
                })
                failed = True
                continue

            ok, detail = probe(
                url,
                args.probe_timeout,
                host_header="" if args.no_ingress else ingress_host,
            )
            result.update({
                "status": "READY" if ok else "NOT_READY",
                "detail": detail,
            })
            if not ok:
                result["logs"] = collect_logs(namespace, app)
                failed = True
                continue

            if not args.no_ingress:
                ingress_ok, ingress_host, ingress_error = create_ingress(
                    namespace,
                    app,
                    args.slot_prefix,
                    args.ingress_class,
                )
                result["ingressHost"] = ingress_host
                result["ingressUrl"] = f"http://{ingress_host}:{args.ingress_port}/"
                if not ingress_ok:
                    result["status"] = "INGRESS_FAIL"
                    result["error"] = ingress_error
                    failed = True
                else:
                    route_ok, route_detail = probe(
                        f"http://127.0.0.1:{args.ingress_port}/",
                        args.probe_timeout,
                        host_header=ingress_host,
                    )
                    result["ingressRoute"] = route_detail
                    if not route_ok:
                        result["status"] = "INGRESS_NOT_READY"
                        result["error"] = f"Ingress route not ready: {route_detail}"
                        failed = True
            print(f"{app}: {result['status']} {detail}", flush=True)
        finally:
            results.append(result)
            if args.cleanup and not (args.keep_failed and result["status"] != "READY"):
                delete_namespace(namespace)

    print("\n===== SUMMARY =====")
    print(json.dumps(results, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
