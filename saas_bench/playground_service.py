"""Server-side core of the Target Playground service (framework-agnostic, unit-testable).

``PlaygroundService`` implements the prepare/grade/release wire contract (see ``playground.py``)
over a pluggable ``Provisioner`` — the thing that actually stands up / grades / tears down a
per-run preview env (a Helm release of the seeded app on GKE). The HTTP layer (FastAPI) and the
real ``HelmProvisioner`` (helm/kubectl) wrap this core; tests inject a fake provisioner so the
catalog / digest / run-registry logic is verified without a cluster.

Per-run model (no shared-instance lease): ``prepare`` mints a ``run_id`` and provisions a fresh
env; ``grade``/``release`` key off that ``run_id``; an out-of-band TTL janitor reaps orphans.
"""

from __future__ import annotations

import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional
from urllib.parse import quote

from saas_bench.run_store import InMemoryRunStore, RunStore


DEFAULT_PREPARE_WORKERS = 4
DEFAULT_GRADE_WORKERS = 4
DEFAULT_MAX_ACTIVE_RUNS = 10
VERIFIER_AUDIT_TASK_ID = "__verifier_audit__"

_DURABLE_RUN_FIELDS = (
    "schema_version", "task_id", "state", "access_urls", "runtime_identity", "task_version",
    "request_id", "error",
)


class ServiceError(Exception):
    """A client-facing error; the HTTP layer maps ``.status`` to a response code."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class Provisioner:
    """Seam: stand up / grade / tear down one per-run preview env."""

    def up(self, task_id: str, run_id: str, manifest: dict) -> dict:
        """Provision the env; return ``{access_urls: {app: url}, verify_context: {...}}``."""
        raise NotImplementedError

    def recover(self, task_id: str, run_id: str, manifest: dict) -> dict:
        """Reconstruct live URLs/verifier context for an already-provisioned run."""
        raise NotImplementedError

    def grade(
        self,
        task_id: str,
        run_id: str,
        manifest: dict,
        verify_context: dict,
        runtime_identity: Optional[dict] = None,
    ) -> dict:
        """Run the verifier service-side; return the normalized verify payload."""
        raise NotImplementedError

    def probe_access_urls(self, access_urls: dict, manifest: dict) -> dict[str, bool]:
        """Return public reachability for each app URL, or an empty mapping if unsupported.

        Reachability is advisory and never changes the backward-compatible ``prepared`` state.
        """
        return {}

    def down(self, run_id: str) -> None:
        """Tear down the env (idempotent)."""
        raise NotImplementedError


class PlaygroundService:
    """prepare/grade/release over a task catalog + a Provisioner; owns the live run registry."""

    def __init__(
        self,
        catalog: dict,
        provisioner: Provisioner,
        run_id_factory: Optional[Callable[[], str]] = None,
        executor: Optional[ThreadPoolExecutor] = None,
        run_store: Optional[RunStore] = None,
        max_concurrent_grades: int = DEFAULT_GRADE_WORKERS,
        max_active_runs: int = DEFAULT_MAX_ACTIVE_RUNS,
    ):
        if max_concurrent_grades < 1:
            raise ValueError("max_concurrent_grades must be >= 1")
        if max_active_runs < 1:
            raise ValueError("max_active_runs must be >= 1")
        # catalog: task_id -> manifest dict; each manifest carries a "digest" (R-TASK-VERSION).
        self._catalog = catalog
        self._provisioner = provisioner
        self._new_run_id = run_id_factory or (lambda: uuid.uuid4().hex)
        self._runs: dict[str, dict] = {}  # run_id -> {task_id, verify_context}
        self._request_runs: dict[str, str] = {}  # caller X-Request-Id -> run_id
        self._lock = threading.RLock()
        # Different runs may grade concurrently, but one run's grade/release lifecycle must be
        # serialized. Each process-local lock is stored on its active run record (and excluded from
        # the durable projection), so completed runs do not leave a growing lock registry behind.
        self._grade_slots = threading.BoundedSemaphore(max_concurrent_grades)
        self._max_active_runs = max_active_runs
        self._executor = executor or ThreadPoolExecutor(
            max_workers=DEFAULT_PREPARE_WORKERS,
            thread_name_prefix="prepare",
        )
        self._run_store = run_store or InMemoryRunStore()
        self._restore_runs()

    def _run_operation_lock(self, run_id: str) -> threading.Lock:
        """Stable per-run lock for grade/release serialization within the single replica."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                # Unknown-run grade/release still needs a context-manager object, but it need not
                # be retained. A real run is rechecked after acquisition in both call paths.
                return threading.Lock()
            return run.setdefault("_operation_lock", threading.Lock())

    def _require_run_capacity_locked(self) -> None:
        """Reject before provisioning when the durable/live run budget is exhausted."""
        if len(self._runs) >= self._max_active_runs:
            raise ServiceError(
                f"active run capacity is full ({self._max_active_runs}); release a run and retry",
                status=429,
            )

    @staticmethod
    def _durable_record(run: dict) -> dict:
        """Copy only stable, non-secret state into the durable registry.

        Verifier environment variables and public reachability remain cache-only and can be
        reconstructed. ``runtime_identity`` is deliberately durable: it records only pod UID/name
        and restart counters, never credentials or application data, so grading can reject a reset
        environment even when the coordinator restarted after preparation.
        """
        record = {key: run.get(key) for key in _DURABLE_RUN_FIELDS}
        record["access_urls"] = dict(run.get("access_urls") or {})
        record["runtime_identity"] = {
            str(key): dict(value)
            for key, value in (run.get("runtime_identity") or {}).items()
            if isinstance(value, dict)
        }
        return record

    @staticmethod
    def _new_record(task_id: str, digest: str, request_id: str = "") -> dict:
        return {
            "schema_version": "2",
            "task_id": task_id,
            "state": "preparing",
            "verify_context": {},
            "runtime_identity": {},
            "access_urls": {},
            "app_reachability": {},
            "task_version": digest,
            "request_id": request_id,
            "error": None,
        }

    def _restore_runs(self) -> None:
        """Load durable records into the process cache before the API starts serving.

        A worker cannot be resumed safely after its process disappears. Interrupted prepares are
        therefore made explicitly failed/releasable; fully prepared runs are recovered lazily from
        their live pod labels on the first prompt/grade call.
        """
        records = self._run_store.load_all()
        for run_id, stored in records.items():
            task_id = str(stored.get("task_id") or "")
            state = str(stored.get("state") or "failed")
            access_urls = stored.get("access_urls")
            if not isinstance(access_urls, dict):
                access_urls = {}
            run = {
                "schema_version": str(stored.get("schema_version") or ""),
                "task_id": task_id,
                "state": state,
                "verify_context": {},
                "runtime_identity": {
                    str(key): dict(value)
                    for key, value in (stored.get("runtime_identity") or {}).items()
                    if isinstance(value, dict)
                },
                "access_urls": {str(k): str(v) for k, v in access_urls.items()},
                "app_reachability": {str(k): False for k in access_urls},
                "task_version": str(stored.get("task_version") or ""),
                "request_id": str(stored.get("request_id") or ""),
                "error": stored.get("error"),
            }
            stored_schema_version = run["schema_version"]
            manifest = self._catalog.get(task_id)
            if run["schema_version"] not in {"1", "2"}:
                run["state"] = "failed"
                run["error"] = (
                    f"unsupported durable run schema {run['schema_version']!r}; "
                    "release this run and prepare a new one"
                )
            elif manifest is None:
                run["state"] = "failed"
                run["error"] = f"task {task_id!r} is no longer in the service catalog"
            elif state == "preparing":
                run["state"] = "failed"
                run["error"] = (
                    "platform restarted while provisioning; release this run and retry"
                )
            # Schema 1 did not retain pod identity. Keep an existing prepared run usable and adopt
            # its current identity during the first lazy recovery; every newly prepared run starts
            # at schema 2 and is protected from the moment preparation commits.
            if run["schema_version"] == "1":
                run["schema_version"] = "2"
            self._runs[run_id] = run
            request_id = run["request_id"]
            if request_id:
                self._request_runs[request_id] = run_id
            if (
                run["state"] != state
                or run.get("error") != stored.get("error")
                or run["schema_version"] != stored_schema_version
            ):
                self._run_store.update(run_id, self._durable_record(run))

    def prepare(self, task_id: str, expected_manifest_digest: str = "") -> dict:
        manifest = self._catalog.get(task_id)
        if manifest is None:
            raise ServiceError(f"unknown task_id: {task_id!r}", status=404)
        digest = manifest.get("digest", "")
        if expected_manifest_digest and expected_manifest_digest != digest:
            raise ServiceError(
                f"manifest digest mismatch for {task_id!r}: "
                f"client={expected_manifest_digest!r} service={digest!r}",
                status=409,
            )
        run_id = self._new_run_id()
        run = self._new_record(task_id, digest)
        with self._lock:
            self._require_run_capacity_locked()
            try:
                self._run_store.create(run_id, self._durable_record(run))
            except Exception as exc:
                raise ServiceError(
                    f"cannot persist run before provisioning: {exc}", status=503,
                ) from exc
            self._runs[run_id] = run
        try:
            out = self._provisioner.up(task_id, run_id, manifest)
        except Exception as exc:
            cleanup_error = None
            try:
                # Provisioners normally roll back their own partial work, but this idempotent second
                # pass closes the gap when that rollback itself failed or the implementation is not
                # HelmProvisioner. Never discard the only release handle while cleanup is uncertain.
                self._provisioner.down(run_id)
            except Exception as cleanup_exc:
                cleanup_error = f"environment cleanup failed: {cleanup_exc}"

            if cleanup_error is None:
                try:
                    self._run_store.delete(run_id)
                except Exception as store_exc:
                    cleanup_error = f"run-record cleanup failed: {store_exc}"

            if cleanup_error is None:
                with self._lock:
                    self._runs.pop(run_id, None)
                raise ServiceError(f"provisioning failed: {exc}", status=503) from exc

            with self._lock:
                run = self._runs.get(run_id)
                if run is not None:
                    run["state"] = "failed"
                    run["error"] = f"provisioning failed: {exc}; {cleanup_error}"
                    durable = self._durable_record(run)
                else:
                    durable = None
            if durable is not None:
                try:
                    self._run_store.update(run_id, durable)
                except Exception:
                    # The original `preparing` record remains durable and restores conservatively
                    # as failed. Preserve the in-process handle regardless.
                    pass
            raise ServiceError(
                f"provisioning failed for run_id {run_id!r}: {exc}; {cleanup_error}; "
                "retry release with this run_id",
                status=503,
            ) from exc
        verify_context = out.get("verify_context", {})
        access_urls = out.get("access_urls", {})
        # access_urls retained on the registry so /tasks/{id}/prompt can substitute the per-run
        # public URLs into the agent prompt by run_id (build_prompt below).
        with self._lock:
            run = self._runs[run_id]
            run.update({
                "state": "prepared",
                "verify_context": verify_context,
                "runtime_identity": out.get("runtime_identity", {}),
                "access_urls": access_urls,
                "app_reachability": {app: False for app in access_urls},
                "error": None,
            })
            durable = self._durable_record(run)
        try:
            self._run_store.update(run_id, durable)
        except Exception as exc:
            cleanup_errors = []
            try:
                self._provisioner.down(run_id)
            except Exception as cleanup_exc:
                cleanup_errors.append(f"environment cleanup failed: {cleanup_exc}")
            if not cleanup_errors:
                try:
                    self._run_store.delete(run_id)
                except Exception as store_exc:
                    cleanup_errors.append(f"run-record cleanup failed: {store_exc}")
            if not cleanup_errors:
                with self._lock:
                    self._runs.pop(run_id, None)
                raise ServiceError(f"cannot persist prepared run: {exc}", status=503) from exc

            cleanup_error = "; ".join(cleanup_errors)
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["state"] = "failed"
                    current["error"] = (
                        f"prepared environment could not be persisted: {exc}; {cleanup_error}"
                    )
                    failed_durable = self._durable_record(current)
                else:
                    failed_durable = None
            if failed_durable is not None:
                try:
                    self._run_store.update(run_id, failed_durable)
                except Exception:
                    pass
            raise ServiceError(
                f"cannot persist prepared run {run_id!r}: {exc}; {cleanup_error}; "
                "retry release with this run_id",
                status=503,
            ) from exc
        return {
            "run_id":         run_id,
            "status":         "prepared",
            "access_urls":    access_urls,
            "verify_context": verify_context,
            "task_version":   digest,
        }

    def start_prepare(
        self,
        task_id: str,
        expected_manifest_digest: str = "",
        request_id: str = "",
    ) -> dict:
        """Start provisioning in the background and return immediately.

        This is the preferred HTTP/UI contract. Helm can legitimately take minutes on cold images
        or node scale-up; the portal should not hold a single request open for that whole window.
        """
        manifest = self._catalog.get(task_id)
        if manifest is None:
            raise ServiceError(f"unknown task_id: {task_id!r}", status=404)
        digest = manifest.get("digest", "")
        if expected_manifest_digest and expected_manifest_digest != digest:
            raise ServiceError(
                f"manifest digest mismatch for {task_id!r}: "
                f"client={expected_manifest_digest!r} service={digest!r}",
                status=409,
            )
        with self._lock:
            # Replaying POST /runs with the same validated X-Request-Id adopts the original run.
            # Reusing an ID for a different task is a client conflict, not a second provision.
            existing_run_id = self._request_runs.get(request_id) if request_id else None
            existing = self._runs.get(existing_run_id) if existing_run_id else None
            if existing is not None:
                if existing["task_id"] != task_id or existing.get("task_version", "") != digest:
                    raise ServiceError(
                        f"request_id {request_id!r} already belongs to "
                        f"task {existing['task_id']!r}",
                        status=409,
                    )
                run_id = existing_run_id
            else:
                if existing_run_id:
                    self._request_runs.pop(request_id, None)
                self._require_run_capacity_locked()
                run_id = self._new_run_id()
                run = self._new_record(task_id, digest, request_id)
                self._runs[run_id] = run
                if request_id:
                    self._request_runs[request_id] = run_id
                try:
                    self._run_store.create(run_id, self._durable_record(run))
                except Exception as exc:
                    self._runs.pop(run_id, None)
                    if request_id and self._request_runs.get(request_id) == run_id:
                        self._request_runs.pop(request_id, None)
                    raise ServiceError(
                        f"cannot persist run before provisioning: {exc}", status=503,
                    ) from exc
        if existing is None:
            try:
                self._executor.submit(self._prepare_worker, task_id, run_id, manifest)
            except Exception as exc:
                with self._lock:
                    run = self._runs.get(run_id)
                    if run is not None:
                        run["state"] = "failed"
                        run["error"] = f"failed to schedule provisioning: {exc}"
                        durable = self._durable_record(run)
                    else:
                        durable = None
                if durable is not None:
                    try:
                        self._run_store.update(run_id, durable)
                    except Exception:
                        pass
                raise ServiceError(f"failed to schedule provisioning: {exc}", status=503) from exc
        return self.run_status(run_id)

    def start_audit_pool(self, request_id: str = "") -> dict:
        """Start/adopt the hidden union-of-apps verifier audit pool."""
        return self.start_prepare(VERIFIER_AUDIT_TASK_ID, request_id=request_id)

    def _prepare_worker(self, task_id: str, run_id: str, manifest: dict) -> None:
        try:
            out = self._provisioner.up(task_id, run_id, manifest)
        except Exception as exc:
            with self._lock:
                run = self._runs.get(run_id)
                if run is not None:
                    run["state"] = "failed"
                    run["error"] = f"provisioning failed: {exc}"
                    durable = self._durable_record(run)
                else:
                    durable = None
            if durable is not None:
                try:
                    self._run_store.update(run_id, durable)
                except Exception as store_exc:
                    print(f"[run-store] failed to persist failed run {run_id}: {store_exc}", flush=True)
            return
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run.update({
                "state": "prepared",
                "verify_context": out.get("verify_context", {}),
                "runtime_identity": out.get("runtime_identity", {}),
                "access_urls": out.get("access_urls", {}),
                "app_reachability": {
                    app: False for app in out.get("access_urls", {})
                },
                "error": None,
            })
            durable = self._durable_record(run)
        try:
            self._run_store.update(run_id, durable)
        except Exception as exc:
            # Do not hand an agent a run whose durable commit failed. Keep it releasable; its stored
            # `preparing` record also restores conservatively as failed after a later restart.
            with self._lock:
                current = self._runs.get(run_id)
                if current is not None:
                    current["state"] = "failed"
                    current["error"] = f"prepared environment could not be persisted: {exc}"
            print(f"[run-store] failed to persist prepared run {run_id}: {exc}", flush=True)

    def run_status(self, run_id: str) -> dict:
        # Probe outside the registry lock: a slow public edge must not block grade/release or other
        # status calls. Successful observations are sticky; a transient edge failure cannot make
        # an already-reachable app flip back to false.
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ServiceError(f"unknown run_id: {run_id!r}", status=404)
            snapshot = dict(run)
            snapshot["access_urls"] = dict(run.get("access_urls") or {})
            snapshot["app_reachability"] = dict(run.get("app_reachability") or {})

        if snapshot.get("state") == "prepared" and not snapshot.get("verify_context"):
            snapshot = self._recover_runtime_context(run_id, snapshot)

        if snapshot.get("state") == "prepared" and snapshot["access_urls"]:
            pending = {
                app: url for app, url in snapshot["access_urls"].items()
                if not snapshot["app_reachability"].get(app, False)
            }
            if pending:
                try:
                    observed = self._provisioner.probe_access_urls(
                        pending, self._catalog[snapshot["task_id"]],
                    )
                except Exception:
                    observed = {}
                if observed:
                    with self._lock:
                        current = self._runs.get(run_id)
                        if current is not None:
                            reachability = current.setdefault("app_reachability", {})
                            for app, reachable in observed.items():
                                if app in current.get("access_urls", {}):
                                    reachability[app] = bool(reachability.get(app) or reachable)
                            snapshot["app_reachability"] = dict(reachability)

        public_ready = bool(snapshot["access_urls"]) and all(
            snapshot["app_reachability"].get(app, False)
            for app in snapshot["access_urls"]
        )
        prepared_task_version = str(snapshot.get("task_version") or "")
        current_task_version = str(
            self._catalog.get(snapshot["task_id"], {}).get("digest") or ""
        )
        return {
            "run_id": run_id,
            "request_id": snapshot.get("request_id", ""),
            "task_id": snapshot["task_id"],
            "status": snapshot.get("state", "prepared"),
            "access_urls": snapshot["access_urls"],
            "app_reachability": snapshot["app_reachability"],
            "public_ready": public_ready,
            "verify_context": dict(snapshot.get("verify_context") or {}),
            "task_version": prepared_task_version or current_task_version,
            "current_task_version": current_task_version,
            "task_version_changed": bool(
                prepared_task_version
                and current_task_version
                and prepared_task_version != current_task_version
            ),
            "error": snapshot.get("error"),
        }

    def lookup_run(self, request_id: str) -> dict:
        """Recover an async run by the caller-supplied X-Request-Id."""
        with self._lock:
            run_id = self._request_runs.get(request_id)
        if not run_id:
            raise ServiceError(f"unknown request_id: {request_id!r}", status=404)
        return self.run_status(run_id)

    def _prepared_run(self, run_id: str, expected_task_id: str = "") -> dict:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise ServiceError(f"unknown run_id: {run_id!r}", status=404)
            if expected_task_id and run["task_id"] != expected_task_id:
                raise ServiceError(
                    f"run_id {run_id!r} belongs to task {run['task_id']!r}, "
                    f"not {expected_task_id!r}",
                    status=409,
                )
            state = run.get("state", "prepared")
            if state == "preparing":
                raise ServiceError(f"run_id {run_id!r} is still preparing", status=409)
            if state == "failed":
                raise ServiceError(run.get("error") or f"run_id {run_id!r} failed", status=503)
            snapshot = dict(run)

        if not snapshot.get("verify_context"):
            snapshot = self._recover_runtime_context(run_id, snapshot)
        if snapshot.get("state") == "failed":
            raise ServiceError(snapshot.get("error") or f"run_id {run_id!r} failed", status=503)
        return snapshot

    def _recover_runtime_context(self, run_id: str, snapshot: dict) -> dict:
        """Resolve current pod names/hosts for a record restored from durable storage."""
        task_id = snapshot["task_id"]
        manifest = self._catalog.get(task_id)
        if manifest is None:
            raise ServiceError(f"task {task_id!r} is no longer in the catalog", status=503)
        try:
            recovered = self._provisioner.recover(task_id, run_id, manifest)
        except Exception as exc:
            raise ServiceError(f"run recovery failed for {run_id!r}: {exc}", status=503) from exc
        durable = None
        adopted_runtime = False
        with self._lock:
            current = self._runs.get(run_id)
            if current is None:
                raise ServiceError(f"unknown run_id: {run_id!r}", status=404)
            current["verify_context"] = dict(recovered.get("verify_context") or {})
            prepared_runtime = current.get("runtime_identity") or {}
            live_runtime = recovered.get("runtime_identity") or {}
            if prepared_runtime and live_runtime and prepared_runtime != live_runtime:
                current["state"] = "failed"
                current["error"] = (
                    "prepared environment was reset while the platform was unavailable; "
                    "release this run and prepare a new one"
                )
                durable = self._durable_record(current)
            elif not prepared_runtime and live_runtime:
                current["runtime_identity"] = {
                    str(key): dict(value)
                    for key, value in live_runtime.items()
                    if isinstance(value, dict)
                }
                adopted_runtime = True
                durable = self._durable_record(current)
            if recovered.get("access_urls"):
                current["access_urls"] = dict(recovered["access_urls"])
                current.setdefault("app_reachability", {}).update({
                    app: current.get("app_reachability", {}).get(app, False)
                    for app in recovered["access_urls"]
                })
            snapshot = dict(current)
        if durable is not None:
            try:
                self._run_store.update(run_id, durable)
            except Exception as exc:
                with self._lock:
                    current = self._runs.get(run_id)
                    if current is not None and adopted_runtime:
                        current["runtime_identity"] = {}
                        current["verify_context"] = {}
                raise ServiceError(
                    f"run recovery could not persist state for {run_id!r}: {exc}",
                    status=503,
                ) from exc
        return snapshot

    def build_prompt(self, task_id: str, run_id: str) -> dict:
        """Render the agent prompt for a live run, with per-run URLs substituted.

        The portal (Ruby) and other non-Python integrators consume this so they don't have to
        reimplement ``loader.build_prompt`` (and the description-md / steps / multimodal parsing
        that goes with it). The catalog manifest carries ``description_md`` + ``meta`` so this
        runs without re-reading task files from disk.

        Errors mirror grade: 404 for unknown run, 409 for task/run mismatch.
        """
        run = self._prepared_run(run_id, expected_task_id=task_id)
        # loader.build_prompt is the canonical prompt builder used by run.py — calling it here
        # keeps the hosted prompt identical to the local one (no parallel reimplementation).
        from saas_bench.loader import build_prompt as _build  # lazy: avoid cycles + cheap import

        manifest = self._catalog[task_id]
        task = {
            "task_id":        task_id,
            "description_md": manifest.get("description_md", ""),
            "meta":           manifest.get("meta", {}),
        }
        prompt, todo_md, input_files = _build(task, url_map=run.get("access_urls", {}))
        # Multimodal files: the portal can't reach the playground's container filesystem, so each
        # entry carries a `url` the caller fetches from `GET /tasks/{id}/files/{name}` (served by
        # `task_file` below). Surfacing names alone left the bytes unreachable, so every task with a
        # `multimodal_input` graded against an asset the agent never received — the photo/PDF/poster
        # checks in agriculture_011/013/031 and media_016/027 all failed on that, not on agent work.
        multimodal = [
            {
                "name": os.path.basename(p),
                "path": p,
                "url": f"/tasks/{task_id}/files/{quote(os.path.basename(p))}",
            }
            for p in input_files
        ]
        return {
            "task_id":             task_id,
            "run_id":               run_id,
            "task_version":         manifest.get("digest", ""),
            "prepared_task_version": run.get("task_version", ""),
            "prompt":               prompt,
            "todo_md":              todo_md,
            "multimodal_input_files": multimodal,
        }

    def grade(self, task_id: str, run_id: str) -> dict:
        # Acquire the per-run lock first so duplicate/retried grades do not occupy multiple global
        # slots while waiting on each other. Different run IDs remain independent.
        with self._run_operation_lock(run_id):
            run = self._prepared_run(run_id, expected_task_id=task_id)
            if not self._grade_slots.acquire(blocking=False):
                # 429 is part of the client's transient retry set. Rejecting immediately keeps a
                # bounded workload instead of allowing FastAPI threads to queue past LB timeouts.
                raise ServiceError("grade capacity is busy; retry this idempotent request", status=429)
            try:
                manifest = self._catalog[task_id]
                result = self._provisioner.grade(
                    task_id, run_id, manifest, run["verify_context"],
                    run.get("runtime_identity") or {},
                )
                current_version = str(manifest.get("digest") or "")
                prepared_version = str(run.get("task_version") or current_version)
                result["task_version"] = current_version
                result["prepared_task_version"] = prepared_version
                if prepared_version != current_version:
                    warning = (
                        f"run was prepared with task_version {prepared_version}; "
                        f"graded with current task_version {current_version}"
                    )
                    warnings = result.setdefault("warnings", [])
                    if warning not in warnings:
                        warnings.insert(0, warning)
                return result
            finally:
                self._grade_slots.release()

    def grade_from_audit_pool(self, task_id: str, run_id: str) -> dict:
        """Grade one task against the shared untouched-app audit pool.

        The pool provisions every catalog app once as ``<run_id>-<app>``. Multi-app task recovery
        already uses that naming scheme; a single-app task receives its app-specific child ID so
        the ordinary recovery/grade path resolves the same release. This endpoint is deliberately
        separate from normal grading: it proves verifier runtime health against untouched seed
        state, not task completion or per-run isolation.
        """
        with self._run_operation_lock(run_id):
            audit_run = self._prepared_run(run_id, expected_task_id=VERIFIER_AUDIT_TASK_ID)
            manifest = self._catalog.get(task_id)
            if manifest is None or manifest.get("internal"):
                raise ServiceError(f"unknown task_id: {task_id!r}", status=404)
            sites = list(manifest.get("sites") or [])
            if not sites:
                raise ServiceError(f"task {task_id!r} has no provisionable apps", status=409)
            target_run_id = run_id if len(sites) > 1 else f"{run_id}-{sites[0]}"
            if not self._grade_slots.acquire(blocking=False):
                raise ServiceError(
                    "grade capacity is busy; retry this idempotent request", status=429,
                )
            try:
                recovered = self._provisioner.recover(task_id, target_run_id, manifest)
                result = self._provisioner.grade(
                    task_id, target_run_id, manifest,
                    dict(recovered.get("verify_context") or {}),
                    audit_run.get("runtime_identity") or {},
                )
                version = str(manifest.get("digest") or "")
                result["task_version"] = version
                result["prepared_task_version"] = version
                warning = (
                    "graded against the shared untouched-app audit pool; "
                    "this validates verifier execution, not task completion isolation"
                )
                warnings = result.setdefault("warnings", [])
                if warning not in warnings:
                    warnings.insert(0, warning)
                return result
            finally:
                self._grade_slots.release()

    def task_file(self, task_id: str, name: str) -> str:
        """Resolve a task's declared multimodal attachment to a readable path on this container.

        Only basenames that the task's own ``multimodal_input`` declares are servable, so a caller
        cannot walk out of the task tree — `name` is never joined onto a path, it is matched against
        the resolved allow-list. Raises 404 for an unknown task, an undeclared name, or a declared
        file missing from the image.
        """
        manifest = self._catalog.get(task_id)
        if manifest is None or manifest.get("internal"):
            raise ServiceError(f"unknown task {task_id!r}", status=404)

        from saas_bench.loader import build_prompt as _build   # lazy: avoid cycles + cheap import

        _, _, input_files = _build({
            "task_id":        task_id,
            "description_md": manifest.get("description_md", ""),
            "meta":           manifest.get("meta", {}),
        })
        for path in input_files:
            if os.path.basename(path) == name:
                if not os.path.isfile(path):
                    # loader already skips absent files, so this means it vanished after resolution.
                    raise ServiceError(f"file {name!r} is not present in this image", status=404)
                return path
        raise ServiceError(f"task {task_id!r} declares no attachment named {name!r}", status=404)

    def list_catalog(self) -> list:
        """All tasks the service can provision. Public-shape (no internal paths/manifest)."""
        return [
            {
                "task_id":     tid,
                "sites":       m.get("sites", []),
                "task_version": m.get("digest", ""),
                "description": (m.get("description_summary") or "").strip(),
            }
            for tid, m in self._catalog.items()
            if not m.get("internal")
        ]

    def release(self, run_id: str) -> dict:
        # A release racing a grade waits for that grade to finish. Without this lock, teardown can
        # remove pods midway through verifier exec/cp and turn an otherwise valid score into ERROR.
        with self._run_operation_lock(run_id):
            return self._release_locked(run_id)

    def _release_locked(self, run_id: str) -> dict:
        # Tear down BEFORE dropping the registry entry. If helm uninstall fails (raises), keep the
        # run registered so the caller can retry release — the previous order popped first, returned
        # released:true, and silently leaked releases when the uninstall errored.
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return {"released": False}
        if run.get("state") == "preparing":
            raise ServiceError(f"run_id {run_id!r} is still preparing", status=409)
        try:
            self._provisioner.down(run_id)
        except RuntimeError as exc:
            # Translate teardown failure into a structured API error (503) so the client can
            # distinguish "retry the release" from "real server fault" — without this, FastAPI
            # bubbles RuntimeError as a generic 500 and the run is left registered but uncategorized.
            raise ServiceError(f"teardown failed: {exc}", status=503) from exc
        try:
            self._run_store.delete(run_id)
        except Exception as exc:
            # Resources are already down, but retaining the process entry makes release retryable;
            # the next call sees no Helm releases and retries only the durable-record deletion.
            raise ServiceError(f"run-record cleanup failed: {exc}", status=503) from exc
        with self._lock:
            self._runs.pop(run_id, None)
            request_id = run.get("request_id", "")
            if request_id and self._request_runs.get(request_id) == run_id:
                self._request_runs.pop(request_id, None)
        return {"released": True}
