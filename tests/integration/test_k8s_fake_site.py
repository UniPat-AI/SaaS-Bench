"""Kubernetes smoke evaluation against a controlled fake website."""

import json
from pathlib import Path

import pytest

from saas_bench.verify_runner import run_verify


TASK_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "tasks" / "smoke_fake_site"


def _task() -> dict:
    return {
        "task_id": "smoke_fake_site",
        "category_id": "Smoke",
        "meta": {"meta_data": {"sites": ["fake-site"]}},
        "verify_py_path": str(TASK_DIR / "verify.py"),
    }


@pytest.mark.k8s
def test_fake_site_result_and_reset_contract(fake_site_client):
    reset_state = fake_site_client.reset()
    assert reset_state["status"] == "ready"
    assert reset_state["result"]["passed"] is False
    assert reset_state["result"]["score"] == 0.0

    fake_site_client.set_result({
        "task_id": "smoke_fake_site",
        "passed": True,
        "score": 1.0,
    })

    state = fake_site_client.result()
    assert state["status"] == "ready"
    assert state["result"] == {
        "task_id": "smoke_fake_site",
        "passed": True,
        "score": 1.0,
        "checks": [],
    }


@pytest.mark.k8s
def test_run_verify_passes_against_fake_website(k8s_fake_site, fake_site_client, tmp_path):
    fake_site_client.reset()
    fake_site_client.set_result({
        "task_id": "smoke_fake_site",
        "passed": True,
        "score": 1.0,
    })

    result = run_verify(
        _task(),
        slot_id=0,
        port_map={"fake-site": k8s_fake_site["port"]},
        hostname=k8s_fake_site["hostname"],
        result_dir=str(tmp_path),
    )

    assert result["status"] == "PASS"
    assert result["score"] == 1.0
    assert result["all_pass"] is True
    assert [check["label"] for check in result["checks"]] == [
        "health endpoint",
        "result endpoint",
        "result task id",
        "result passed flag",
        "result score",
    ]
    written = json.loads((tmp_path / "smoke_fake_site_verify.json").read_text())
    assert written == result


@pytest.mark.k8s
def test_run_verify_reports_failure_for_bad_result(k8s_fake_site, fake_site_client, tmp_path):
    fake_site_client.reset()
    fake_site_client.set_result({
        "task_id": "smoke_fake_site",
        "passed": False,
        "score": 0.25,
    })

    result = run_verify(
        _task(),
        slot_id=1,
        port_map={"fake-site": k8s_fake_site["port"]},
        hostname=k8s_fake_site["hostname"],
        result_dir=str(tmp_path),
    )

    assert result["status"] == "FAIL"
    assert result["score"] == 0.5
    failed_labels = [check["label"] for check in result["checks"] if not check["passed"]]
    assert failed_labels == ["result passed flag", "result score"]
