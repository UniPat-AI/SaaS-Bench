"""Grade-runner seam — score a finished run against the live application state.

``LocalGradeRunner`` runs the task's ``verify.py`` in-process against the per-slot containers (the
upstream path). A service-side ``ServiceGradeRunner`` will grade beside the apps (where
``docker``/``kubectl exec`` graders work) and return the same normalized payload, which the client
writes to ``{task_id}{suffix}_verify.json`` so reporting.py stays unchanged.
"""

from __future__ import annotations

import os

from saas_bench.targets import PreparedEnv
from saas_bench.verify_runner import run_verify


class GradeRunner:
    """Seam: grade a finished run; returns the verify-result dict reporting.py consumes."""

    def grade(self, task: dict, prepared: PreparedEnv, result_dir: str, run_suffix: str) -> dict:
        raise NotImplementedError


class LocalGradeRunner(GradeRunner):
    """Run verify.py in-process against the per-slot containers (upstream behavior)."""

    def grade(self, task: dict, prepared: PreparedEnv, result_dir: str, run_suffix: str) -> dict:
        if not prepared.gradeable:
            return {
                "task_id": task["task_id"],
                "status": "SKIP",
                "score": 0.0,
                "checks": [],
                "error": prepared.skip_reason or "verification skipped",
            }
        vc = prepared.verify_context
        return run_verify(
            task, vc["slot_id"], vc["port_map"], vc["hostname"], result_dir, run_suffix=run_suffix,
        )


def make_grader(backend: str, playground_url: str = "") -> GradeRunner:
    """Construct the grade backend selected by ``--grade-backend``.

    ``local`` runs verify.py in-process (upstream). ``service`` is an HTTP client to the hosted
    playground, which grades beside the apps and returns the payload the client writes
    (``--playground-url`` required).
    """
    if backend == "local":
        return LocalGradeRunner()
    if backend == "service":
        from saas_bench.playground import ServiceGradeRunner
        return ServiceGradeRunner(playground_url, api_key=os.environ.get("PLAYGROUND_API_KEY", ""))
    raise ValueError(f"unknown grade backend: {backend!r}")
