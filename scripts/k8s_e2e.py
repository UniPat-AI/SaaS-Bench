#!/usr/bin/env python3
"""Run the Kubernetes SaaS-Bench e2e smoke flow for code-server."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from saas_bench.verify_runner import run_verify  # noqa: E402

PLAYWRIGHT = ROOT / "scripts" / "code_server_playwright_smoke.py"
VERIFY = ROOT / "tests" / "fixtures" / "tasks" / "code_server_smoke" / "verify.py"


def run(args: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot-prefix", default="s0")
    parser.add_argument("--ingress-port", type=int, default=30090)
    parser.add_argument("--result-dir", default=str(ROOT / "results" / "k8s_e2e"))
    parser.add_argument("--keep", action="store_true", help="keep the app namespace")
    args = parser.parse_args()

    result_dir = Path(args.result_dir)
    result_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "framework": "k8s_codex_e2e",
        "app": "code-server",
        "slotPrefix": args.slot_prefix,
        "ingressPort": args.ingress_port,
        "readiness": None,
        "codexPlaywright": None,
        "verifier": None,
        "cleanup": None,
        "error": None,
    }

    try:
        ready = run([
            sys.executable,
            str(ROOT / "scripts" / "test_k8s_apps.py"),
            "--apps", "code-server",
            "--slot-prefix", args.slot_prefix,
            "--ingress-port", str(args.ingress_port),
        ], timeout=420)
        summary["readiness"] = {
            "status": "READY" if ready.returncode == 0 else "NOT_READY",
            "returncode": ready.returncode,
            "stdout": ready.stdout,
            "stderr": ready.stderr,
        }
        if ready.returncode != 0:
            raise RuntimeError("app readiness failed")

        playwright = run([sys.executable, str(PLAYWRIGHT)], timeout=120)
        if playwright.returncode != 0:
            raise RuntimeError(f"Playwright scenario failed: {playwright.stderr}")
        summary["codexPlaywright"] = json.loads(playwright.stdout)

        os.environ["SAAS_BACKEND"] = "k8s"
        os.environ["K8S_INGRESS_HOST"] = f"{args.slot_prefix}-code-server.saasbench.localhost"
        task = {
            "task_id": "code_server_smoke",
            "category_id": "Smoke",
            "meta": {"meta_data": {"sites": ["code-server"]}},
            "verify_py_path": str(VERIFY),
        }
        summary["verifier"] = run_verify(
            task,
            0,
            {"code-server": args.ingress_port},
            "127.0.0.1",
            str(result_dir),
        )
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if not args.keep:
            cleanup = run([
                sys.executable,
                str(ROOT / "scripts" / "test_k8s_apps.py"),
                "--apps", "code-server",
                "--slot-prefix", args.slot_prefix,
                "--ingress-port", str(args.ingress_port),
                "--cleanup",
            ], timeout=420)
            check = run(["kubectl", "get", "namespace", "saasbench-code-server"])
            summary["cleanup"] = {
                "returncode": cleanup.returncode,
                "namespaceExists": check.returncode == 0,
                "stdout": cleanup.stdout,
                "stderr": cleanup.stderr,
            }

    print(json.dumps(summary, indent=2))
    verifier = summary.get("verifier") or {}
    cleanup = summary.get("cleanup") or {}
    return 0 if verifier.get("status") == "PASS" and cleanup.get("namespaceExists") is False else 1


if __name__ == "__main__":
    raise SystemExit(main())
