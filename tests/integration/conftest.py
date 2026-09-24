"""Shared Kubernetes fixtures for controlled integration smoke tests."""

import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--run-k8s",
        action="store_true",
        default=False,
        help="run Kubernetes integration tests",
    )
    parser.addoption(
        "--keep-k8s",
        action="store_true",
        default=False,
        help="keep test-owned Kubernetes resources after the run",
    )


ROOT = Path(__file__).resolve().parents[2]
HELM_CHART = ROOT / "deploy" / "helm" / "fake-site"
RELEASE_NAME = "fake-site"


def _run_command(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _run_kubectl(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return _run_command(["kubectl", *args], check=check)


def _run_helm(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return _run_command(["helm", *args], check=check)


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
        return response.status, json.loads(body) if body else {}


def _wait_for_http(url: str, timeout_s: float = 60.0) -> None:
    deadline = time.time() + timeout_s
    last_error = "no request attempted"
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except URLError as exc:
            last_error = str(exc)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


class FakeSiteClient:
    """External evaluation client for the controlled fake website."""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def reset(self) -> dict:
        _, payload = _request_json("POST", f"{self.base_url}/reset", {})
        return payload

    def set_result(self, result: dict) -> dict:
        _, payload = _request_json(
            "POST",
            f"{self.base_url}/set-result",
            {"result": result},
        )
        return payload

    def result(self) -> dict:
        _, payload = _request_json("GET", f"{self.base_url}/result")
        return payload


@pytest.fixture(scope="session")
def k8s_fake_site(request) -> dict:
    """Install the fake website chart into a per-run Kubernetes namespace.

    Helm is the provisioner. The evaluation client runs outside the cluster
    and reaches the release through kubectl port-forward.
    """
    if not (request.config.getoption("--run-k8s") or os.getenv("K8S_SMOKE") == "1"):
        pytest.skip("pass --run-k8s to run Kubernetes integration tests")
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl is not available on PATH")
    if shutil.which("helm") is None:
        pytest.skip("helm is not available on PATH")

    namespace = os.getenv(
        "K8S_NAMESPACE",
        f"saasbench-smoke-{uuid.uuid4().hex[:8]}",
    )

    _run_helm([
        "upgrade", "--install", RELEASE_NAME, str(HELM_CHART),
        "--namespace", namespace,
        "--create-namespace",
        "--wait",
        "--timeout", "3m",
    ])
    _run_kubectl([
        "-n", namespace,
        "rollout", "status", f"deployment/{RELEASE_NAME}",
        "--timeout=120s",
    ])

    local_port = _free_local_port()
    port_forward = subprocess.Popen(
        [
            "kubectl", "-n", namespace,
            "port-forward", f"service/{RELEASE_NAME}",
            f"{local_port}:80",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        base_url = f"http://127.0.0.1:{local_port}"
        _wait_for_http(f"{base_url}/healthz")
        yield {
            "hostname": "127.0.0.1",
            "port": local_port,
            "base_url": base_url,
            "namespace": namespace,
            "release": RELEASE_NAME,
            "client_location": "external",
        }
    finally:
        port_forward.terminate()
        try:
            port_forward.wait(timeout=10)
        except subprocess.TimeoutExpired:
            port_forward.kill()
            port_forward.wait(timeout=10)

        keep_resources = (
            request.config.getoption("--keep-k8s")
            or os.getenv("K8S_KEEP_RESOURCES") == "1"
        )
        if not keep_resources:
            _run_helm(
                ["uninstall", RELEASE_NAME, "--namespace", namespace, "--wait", "--timeout", "2m"],
                check=False,
            )
            _run_kubectl(
                ["delete", "namespace", namespace, "--ignore-not-found=true", "--wait=true", "--timeout=120s"],
                check=False,
            )


@pytest.fixture()
def fake_site_client(k8s_fake_site) -> FakeSiteClient:
    return FakeSiteClient(k8s_fake_site["base_url"])
