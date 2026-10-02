"""Small durable run registry for the hosted playground.

The playground already depends on the Kubernetes API for every run. At the expected scale (single
service replica, four concurrent prepares, at most ten active runs), one ConfigMap per run keeps
restart recovery durable without introducing Redis/Postgres. Records contain control metadata only
— never credentials or application data.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable


Runner = Callable[..., "subprocess.CompletedProcess"]

_REGISTRY_LABEL = "saas-playground.run-registry"
_REGISTRY_LABEL_VALUE = "true"
_RUN_ID_LABEL = "saas-playground.root-run-id"
_NAME_PREFIX = "saas-playground-run-"
_RUN_ID_RE = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class RunStore:
    """Persistence seam used by :class:`PlaygroundService`."""

    def load_all(self) -> dict[str, dict]:
        raise NotImplementedError

    def create(self, run_id: str, record: dict) -> None:
        raise NotImplementedError

    def update(self, run_id: str, record: dict) -> None:
        raise NotImplementedError

    def delete(self, run_id: str) -> None:
        raise NotImplementedError


class InMemoryRunStore(RunStore):
    """Thread-safe test/local store. Reuse one instance to simulate a service restart."""

    def __init__(self):
        self._records: dict[str, dict] = {}
        self._lock = threading.RLock()

    def load_all(self) -> dict[str, dict]:
        with self._lock:
            return copy.deepcopy(self._records)

    def create(self, run_id: str, record: dict) -> None:
        with self._lock:
            if run_id in self._records:
                raise RuntimeError(f"run record already exists: {run_id}")
            self._records[run_id] = copy.deepcopy(record)

    def update(self, run_id: str, record: dict) -> None:
        with self._lock:
            if run_id not in self._records:
                raise RuntimeError(f"run record does not exist: {run_id}")
            self._records[run_id] = copy.deepcopy(record)

    def delete(self, run_id: str) -> None:
        with self._lock:
            self._records.pop(run_id, None)


@dataclass
class KubectlConfigMapRunStore(RunStore):
    """One small ConfigMap per run, accessed through the kubectl already baked into the image."""

    namespace: str = "saas-playground"
    runner: Runner = subprocess.run

    def _run(self, argv: list[str], *, input_text: str | None = None) -> "subprocess.CompletedProcess":
        proc = self.runner(argv, input=input_text, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(
                f"run-store command failed ({argv[0]} rc={proc.returncode}): "
                f"{(proc.stderr or '').strip()[:500]}"
            )
        return proc

    @staticmethod
    def _name(run_id: str) -> str:
        name = f"{_NAME_PREFIX}{run_id}"
        if not _RUN_ID_RE.fullmatch(run_id) or len(run_id) > 63 or len(name) > 253:
            raise RuntimeError(f"run_id is not a valid ConfigMap name component: {run_id!r}")
        return name

    @staticmethod
    def _payload(run_id: str, record: dict) -> str:
        value = copy.deepcopy(record)
        value["run_id"] = run_id
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def load_all(self) -> dict[str, dict]:
        proc = self._run([
            "kubectl", "get", "configmaps", "-n", self.namespace,
            "-l", f"{_REGISTRY_LABEL}={_REGISTRY_LABEL_VALUE}", "-o", "json",
        ])
        try:
            items = json.loads(proc.stdout or "{}").get("items", [])
        except (json.JSONDecodeError, AttributeError) as exc:
            raise RuntimeError(f"cannot parse run ConfigMap list: {exc}") from exc

        records: dict[str, dict] = {}
        for item in items:
            metadata = item.get("metadata") or {}
            name = metadata.get("name", "")
            raw = (item.get("data") or {}).get("run.json", "")
            try:
                record = json.loads(raw)
            except (json.JSONDecodeError, TypeError) as exc:
                raise RuntimeError(f"invalid run record in ConfigMap {name!r}: {exc}") from exc
            if not isinstance(record, dict):
                raise RuntimeError(f"invalid run record in ConfigMap {name!r}: expected object")
            run_id = str(record.pop("run_id", ""))
            if not run_id or name != self._name(run_id):
                raise RuntimeError(
                    f"run record identity mismatch in ConfigMap {name!r}: run_id={run_id!r}"
                )
            label_run_id = (metadata.get("labels") or {}).get(_RUN_ID_LABEL, "")
            if label_run_id != run_id:
                raise RuntimeError(
                    f"run record label mismatch in ConfigMap {name!r}: {label_run_id!r}"
                )
            records[run_id] = record
        return records

    def create(self, run_id: str, record: dict) -> None:
        # `kubectl create configmap` has no `--labels` flag. Submit the complete object in one
        # create request so the registry selectors and run identity are present atomically.
        manifest = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": self._name(run_id),
                "namespace": self.namespace,
                "labels": {
                    "app.kubernetes.io/managed-by": "saas-playground",
                    "app.kubernetes.io/part-of": "saas-playground",
                    _REGISTRY_LABEL: _REGISTRY_LABEL_VALUE,
                    _RUN_ID_LABEL: run_id,
                },
            },
            "data": {"run.json": self._payload(run_id, record)},
        }
        self._run(
            ["kubectl", "create", "-f", "-"],
            input_text=json.dumps(manifest, separators=(",", ":")),
        )

    def update(self, run_id: str, record: dict) -> None:
        patch = {"data": {"run.json": self._payload(run_id, record)}}
        self._run([
            "kubectl", "patch", "configmap", self._name(run_id), "-n", self.namespace,
            "--type=merge", "-p", json.dumps(patch, separators=(",", ":")),
        ])

    def delete(self, run_id: str) -> None:
        self._run([
            "kubectl", "delete", "configmap", self._name(run_id), "-n", self.namespace,
            "--ignore-not-found=true", "--wait=true",
        ])
