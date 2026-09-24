#!/usr/bin/env python3
"""HTTP verifier for code-server deployed behind the Kubernetes ingress."""

import os
import subprocess
import sys
from urllib.request import Request, urlopen


HOST = os.environ["SERVER_HOSTNAME"]
PORT = int(os.environ["CODE_SERVER_PORT"])
CONTAINER = os.environ.get("CODE_SERVER_CONTAINER", "")
INGRESS_HOST = os.getenv("K8S_INGRESS_HOST", "")
URL = f"http://{HOST}:{PORT}/login"

_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{suffix}", file=sys.stderr)


def main() -> None:
    try:
        headers = {"Host": INGRESS_HOST} if INGRESS_HOST else {}
        request = Request(URL, headers=headers)
        with urlopen(request, timeout=10) as response:
            final_url = response.geturl()
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
        check("code-server reachable", 1, status == 200, f"status={status}, final_url={final_url}")
        check("code-server UI served", 2, "code-server" in body.lower(), "body contains code-server")
    except Exception as exc:
        check("code-server reachable", 1, False, f"exception: {exc}")
        check("code-server UI served", 2, False, "request failed")

    try:
        exec_result = subprocess.run(
            ["docker", "exec", CONTAINER, "sh", "-c", "echo k8s-exec-ok"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        check(
            "container exec via shim",
            1,
            exec_result.returncode == 0 and "k8s-exec-ok" in exec_result.stdout,
            f"rc={exec_result.returncode}, stdout={exec_result.stdout.strip()!r}, stderr={exec_result.stderr.strip()!r}",
        )
    except Exception as exc:
        check("container exec via shim", 1, False, f"exception: {exc}")

    earned = sum(weight for _, weight, passed, _ in _checks if passed)
    total = sum(weight for _, weight, _, _ in _checks)
    all_pass = bool(_checks) and all(passed for _, _, passed, _ in _checks)
    score = earned / total if total else 0.0
    print(f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})", file=sys.stderr)


if __name__ == "__main__":
    main()
