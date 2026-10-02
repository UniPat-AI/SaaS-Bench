"""Client for the Target Playground service (the per-run preview-env API on Kubernetes).

``PlaygroundTarget`` (a ``TargetEnvironment``) and ``ServiceGradeRunner`` (a ``GradeRunner``) are
thin HTTP clients over the service's ``prepare``/``grade``/``release`` endpoints. The service
stands up a per-run preview env (a Helm release of the seeded app in the cluster), grades
service-side, and tears it down — so these clients carry no docker/k8s logic, only the wire
contract. Grading runs service-side; the **client** writes ``{task_id}{suffix}_verify.json`` so
stock ``reporting.py`` loads it unchanged.

Wire contract (v1):
  POST {base}/runs     {task_id, expected_manifest_digest} + X-Request-Id
       -> 202 {run_id, status=preparing, ...}; poll GET /runs/{run_id}
  POST {base}/prepare  (legacy sync; do not use for a 106-task burst)
  POST {base}/grade    {task_id, run_id}
       -> {task_id, status, score, checks, ...}   # the normalized verify payload reporting.py reads
  POST {base}/release  {run_id} -> {released: bool}
"""

from __future__ import annotations

import json
import socket
import time
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from saas_bench.grading import GradeRunner
from saas_bench.targets import PreparedEnv, TargetEnvironment

# Transport seam: (method, url, json_body_or_None, headers) -> decoded JSON dict.
Transport = Callable[[str, str, Optional[dict], dict], dict]

# Must cover the slowest app's helm --wait (apps.yaml startup_wait + 60s slack). OpenEMR is 1200s;
# OnlyOffice first-pull + seed is several minutes. The old 300s client timeout returned while the
# service kept provisioning, so the client never got a run_id to /release.
_DEFAULT_TIMEOUT = 1500


def _urllib_transport(method: str, url: str, body: Optional[dict], headers: dict) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json", **headers},
    )
    with urllib.request.urlopen(req, timeout=_DEFAULT_TIMEOUT) as resp:
        raw = resp.read().decode()
    return json.loads(raw) if raw.strip() else {}


# An ALLOWLIST of "the app is actually answering" — anything else means the request didn't reach
# the app and we should keep waiting. A denylist was previously wrong: it missed Cloudflare's 52x
# range (520-527: TLS handshake failures, origin unreachable, edge timeouts), which would falsely
# read as ready while the agent would still get those same errors at the edge. Matches the
# 2xx/3xx/401/403 contract documented in docs/playground-api-integration.md.
_READY_CODES = frozenset({200, 201, 202, 204, 301, 302, 303, 307, 308, 401, 403})


def _probe_status(url: str, timeout: float = 10.0) -> int:
    """GET ``url`` and return its HTTP status (4xx/5xx included); 0 on connect/DNS failure."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


# Transient errors that idempotent calls (grade/release) can safely retry. Includes Cloudflare's
# 52x range (origin unreachable / 524 timeouts), upstream 5xx, and the usual 408/429.
_TRANSIENT_CODES = frozenset({408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527})


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in _TRANSIENT_CODES
    return isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError))


class PlaygroundClient:
    """Thin JSON-over-HTTP client for the playground service (transport is injectable for tests)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        transport: Optional[Transport] = None,
        retry_attempts: int = 3,
        retry_base_delay: float = 1.0,
    ):
        if not base_url:
            raise ValueError("playground base_url is required (set --playground-url)")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._transport = transport or _urllib_transport
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay

    def _call(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        extra_headers: Optional[dict] = None,
    ) -> dict:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        if extra_headers:
            headers.update(extra_headers)
        return self._transport(method, f"{self.base_url}{path}", body, headers)

    def _call_idempotent(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        """For server-side-idempotent calls (grade/release): retry on Cloudflare/gateway transients
        with exponential backoff. prepare uses ``_call`` directly — retrying it would leak a second
        per-run env if the request succeeded but the response was lost."""
        for attempt in range(self._retry_attempts):
            try:
                return self._call(method, path, body)
            except Exception as exc:
                if not _is_transient(exc) or attempt == self._retry_attempts - 1:
                    raise
                if self._retry_base_delay > 0:
                    time.sleep(self._retry_base_delay * (2 ** attempt))
        return {}  # unreachable; loop either returns or raises

    def prepare(self, task_id: str, expected_manifest_digest: str = "") -> dict:
        # Legacy sync endpoint. A 106-task burst holds the HTTP request open until helm
        # finishes and starves /healthz. Prefer start_run + wait_prepared.
        return self._call(
            "POST", "/prepare",
            {"task_id": task_id, "expected_manifest_digest": expected_manifest_digest},
        )

    def start_run(
        self,
        task_id: str,
        expected_manifest_digest: str = "",
        request_id: str = "",
    ) -> dict:
        headers = {}
        if request_id:
            headers["X-Request-Id"] = request_id
        return self._call(
            "POST",
            "/runs",
            {"task_id": task_id, "expected_manifest_digest": expected_manifest_digest},
            extra_headers=headers,
        )

    def run_status(self, run_id: str) -> dict:
        return self._call("GET", f"/runs/{run_id}")

    def wait_prepared(
        self,
        run_id: str,
        timeout: float = 3600.0,
        interval: float = 5.0,
    ) -> dict:
        """Poll GET /runs/{id} until prepared and public_ready, or raise."""
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            last = self.run_status(run_id)
            status = last.get("status") or last.get("state")
            if status == "failed":
                raise RuntimeError(last.get("error") or f"prepare failed for run_id={run_id}")
            if status == "prepared" and last.get("public_ready") is True:
                return last
            time.sleep(interval)
        raise TimeoutError(
            f"run {run_id} not public_ready after {timeout:.0f}s "
            f"(last status={last.get('status')!r})"
        )

    def grade(self, task_id: str, run_id: str) -> dict:
        return self._call_idempotent("POST", "/grade", {"task_id": task_id, "run_id": run_id})

    def release(self, run_id: str) -> dict:
        return self._call_idempotent("POST", "/release", {"run_id": run_id})


class PlaygroundTarget(TargetEnvironment):
    """Target backend that provisions a per-run preview env via the playground service."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        expected_manifest_digest: str = "",
        transport: Optional[Transport] = None,
        reachable_timeout: float = 1500.0,
        reachable_interval: float = 10.0,
        reachability_probe: Optional[Callable[[str], int]] = None,
        prepare_timeout: float = 3600.0,
    ):
        self._client = PlaygroundClient(base_url, api_key, transport)
        self._expected_manifest_digest = expected_manifest_digest
        self._run_id: Optional[str] = None
        self._reachable_timeout = reachable_timeout
        self._reachable_interval = reachable_interval
        self._probe = reachability_probe or _probe_status
        self._prepare_timeout = prepare_timeout

    def prepare(self, task: dict, run_id: int) -> PreparedEnv:
        request_id = f"eval-{task.get('task_id', 'task')}-r{run_id}-{uuid.uuid4().hex[:12]}"
        started = self._client.start_run(
            task["task_id"], self._expected_manifest_digest, request_id=request_id,
        )
        self._run_id = started["run_id"]
        resp = self._client.wait_prepared(self._run_id, timeout=self._prepare_timeout)
        verify_context = dict(resp.get("verify_context", {}))
        verify_context["service_run_id"] = resp["run_id"]
        url_map = resp.get("access_urls", {})
        self._wait_reachable(url_map)
        return PreparedEnv(
            url_map=url_map,
            verify_context=verify_context,
            gradeable=True,
            skip_reason="",
        )

    def _wait_reachable(self, url_map: dict) -> None:
        """Block until each access URL answers. ``prepare``'s server-side ``helm --wait`` only
        covers the pod; the per-run GKE LB route takes ~minutes more to program, so without this
        the agent could navigate to a not-yet-routed 404/503. Polls the agent's exact path.

        Raises on timeout — the integration contract is "URLs are live when prepare returns", and
        handing dead URLs to an agent burns a run for nothing. ``self._run_id`` is already set at
        this point, so the caller's ``finally: target.release()`` cleans up the orphan env.
        """
        if self._reachable_timeout <= 0:
            return
        deadline = time.monotonic() + self._reachable_timeout
        pending = {app: url for app, url in url_map.items() if url}
        while pending and time.monotonic() < deadline:
            for app, url in list(pending.items()):
                if self._probe(url) in _READY_CODES:
                    del pending[app]
            if pending:
                time.sleep(self._reachable_interval)
        if pending:
            raise RuntimeError(
                f"per-run env unreachable after {self._reachable_timeout:.0f}s: "
                f"{sorted(pending)} (run_id={self._run_id}; caller should release + retry)"
            )

    def release(self) -> None:
        # Only clear self._run_id when the server-side release actually succeeded — if /release
        # raises (e.g. the service returned 503 because helm uninstall failed), the run is still
        # tracked server-side and the env may linger. Keeping _run_id signals "not cleanly
        # released" so a future call can retry; the TTL janitor is the ultimate backstop.
        if self._run_id is not None:
            self._client.release(self._run_id)
            self._run_id = None


class ServiceGradeRunner(GradeRunner):
    """Grade backend: grade service-side, then write the verify file client-side for reporting.py."""

    def __init__(self, base_url: str, api_key: str = "", transport: Optional[Transport] = None):
        self._client = PlaygroundClient(base_url, api_key, transport)

    def grade(self, task: dict, prepared: PreparedEnv, result_dir: str, run_suffix: str) -> dict:
        if not prepared.gradeable:
            return {
                "task_id": task["task_id"],
                "status": "SKIP",
                "score": 0.0,
                "checks": [],
                "error": prepared.skip_reason or "verification skipped",
            }
        run_id = prepared.verify_context.get("service_run_id")
        payload = self._client.grade(task["task_id"], run_id)
        # The service can't write the client's local result_dir, so the client persists the
        # verify JSON here for stock reporting.py.
        out = Path(result_dir) / f"{task['task_id']}{run_suffix}_verify.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
