#!/usr/bin/env python3
"""Smoke verifier for the controlled Kubernetes fake website fixture."""

import json
import os
import sys
from urllib.request import urlopen


HOST = os.environ["SERVER_HOSTNAME"]
PORT = int(os.environ["FAKE_SITE_PORT"])
BASE_URL = f"http://{HOST}:{PORT}"
EXPECTED_TASK_ID = os.getenv("FAKE_SITE_EXPECTED_TASK_ID", "smoke_fake_site")

_checks: list[tuple[str, int, bool, str]] = []


def check(label: str, weight: int, passed: bool, detail: str = "") -> None:
    _checks.append((label, weight, passed, detail))
    status = "PASS" if passed else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"[{status}] ({weight}pt) {label}{suffix}", file=sys.stderr)


def get_json(path: str) -> tuple[int, dict]:
    with urlopen(f"{BASE_URL}{path}", timeout=10) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main() -> None:
    try:
        status, payload = get_json("/healthz")
        check("health endpoint", 1, status == 200 and payload.get("status") == "ok",
              f"status={status}, payload={payload}")
    except Exception as exc:
        check("health endpoint", 1, False, f"exception: {exc}")

    try:
        status, payload = get_json("/result")
        check("result endpoint", 1, status == 200 and payload.get("status") == "ready",
              f"status={status}, state_status={payload.get('status')}")
        result = payload.get("result", {})
        check("result task id", 1, result.get("task_id") == EXPECTED_TASK_ID,
              f"expected={EXPECTED_TASK_ID}, got={result.get('task_id')}")
        check("result passed flag", 2, result.get("passed") is True,
              f"passed={result.get('passed')}")
        score = result.get("score")
        check("result score", 1,
              isinstance(score, (int, float)) and abs(float(score) - 1.0) < 1e-9,
              f"score={score}")
    except Exception as exc:
        check("result endpoint", 1, False, f"exception: {exc}")
        check("result task id", 1, False, "result unavailable")
        check("result passed flag", 2, False, "result unavailable")
        check("result score", 1, False, "result unavailable")

    earned = sum(weight for _, weight, passed, _ in _checks if passed)
    total = sum(weight for _, weight, _, _ in _checks)
    all_pass = bool(_checks) and all(passed for _, _, passed, _ in _checks)
    score = earned / total if total else 0.0
    print(f"SCORE: {score:.3f}  PASS: {all_pass}  ({earned}/{total})", file=sys.stderr)


if __name__ == "__main__":
    main()
