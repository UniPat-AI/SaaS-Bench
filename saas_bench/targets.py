"""Target-environment seam — provision the app(s) a run executes against, then release them.

The run loop is target-agnostic: per run it calls ``prepare(task, run_id)`` to get a
``PreparedEnv`` (the per-app URLs the prompt should use + the opaque context the grader needs),
runs the agent, grades, then calls ``release()``. ``SlotManagerTarget`` is the upstream
local-docker implementation (fresh containers per run on a slot, stopped after). A hosted
``PlaygroundTarget`` (a per-run preview env on GKE) implements the same two methods behind this
seam — the core loop does not change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from saas_bench.slot import SlotManager


@dataclass
class PreparedEnv:
    """What a TargetEnvironment hands back for one run."""

    url_map: dict[str, str] = field(default_factory=dict)   # app key -> base URL, fed to build_prompt(url_map=)
    verify_context: dict = field(default_factory=dict)      # opaque; consumed by the GradeRunner
    gradeable: bool = True                                   # False => skip grading (e.g. --no-isolation)
    skip_reason: str = ""                                    # SKIP message when not gradeable


class TargetEnvironment:
    """Seam: provision/release the environment a single run executes against."""

    def prepare(self, task: dict, run_id: int) -> PreparedEnv:
        raise NotImplementedError

    def release(self) -> None:
        raise NotImplementedError


class SlotManagerTarget(TargetEnvironment):
    """Upstream local-docker target: per-run fresh containers on a slot (no reset needed).

    Isolation mode starts the task's apps on this slot and grades via the per-slot container
    names; ``--no-isolation`` connects to already-running apps on their ``fixed_port`` and skips
    grading (the per-slot container names verify.py needs only exist under isolation).
    """

    def __init__(self, apps_config: dict, slot_id: int, hostname: str, use_isolation: bool):
        self.apps_config = apps_config
        self.slot_id = slot_id
        self.hostname = hostname
        self.use_isolation = use_isolation
        self._slot = SlotManager(apps_config, slot_id) if use_isolation else None
        self._known: list[str] = []

    def prepare(self, task: dict, run_id: int) -> PreparedEnv:
        sites: list[str] = task.get("meta", {}).get("meta_data", {}).get("sites", [])
        port_map: dict[str, int] = {}

        if self.use_isolation and self._slot and sites:
            known = [a for a in sites if a in self.apps_config]
            unknown = [a for a in sites if a not in self.apps_config]
            if unknown:
                print(
                    f"  [slot {self.slot_id}][{task['task_id']}] unknown apps {unknown}, skipping isolation",
                    flush=True,
                )
            # Record before starting so release() cleans up even if start_apps fails partway.
            self._known = known
            if known:
                self._slot.start_apps(known, hostname=self.hostname)
                port_map = self._slot.get_port_map(known)
        elif not self.use_isolation and self.apps_config:
            port_map = {
                app: self.apps_config[app]["fixed_port"]
                for app in sites
                if app in self.apps_config and "fixed_port" in self.apps_config[app]
            }

        gradeable = bool(self.use_isolation and task.get("verify_py_path"))
        return PreparedEnv(
            url_map={app: f"http://{self.hostname}:{port}" for app, port in port_map.items()},
            verify_context={"slot_id": self.slot_id, "port_map": port_map, "hostname": self.hostname},
            gradeable=gradeable,
            skip_reason="" if gradeable else "verification skipped in no-isolation mode",
        )

    def release(self) -> None:
        if self.use_isolation and self._slot and self._known:
            self._slot.stop_apps(self._known)


def make_target(
    backend: str,
    apps_config: dict,
    slot_id: int,
    hostname: str,
    use_isolation: bool,
    playground_url: str = "",
) -> TargetEnvironment:
    """Construct the target backend selected by ``--target-backend``.

    ``slotmanager`` is the upstream local-docker path. ``playground`` is an HTTP client to the
    hosted GKE per-run preview-env service (``--playground-url`` required).
    """
    if backend == "slotmanager":
        return SlotManagerTarget(apps_config, slot_id, hostname, use_isolation)
    if backend == "playground":
        from saas_bench.playground import PlaygroundTarget
        return PlaygroundTarget(playground_url, api_key=os.environ.get("PLAYGROUND_API_KEY", ""))
    raise ValueError(f"unknown target backend: {backend!r}")
