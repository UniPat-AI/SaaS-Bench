from saas_bench.k8s_slot import K8sSlotManager
from saas_bench.run import _make_slot_manager
from saas_bench.slot import SlotManager
from saas_bench.verify_runner import _build_k8s_exec_map


def test_make_slot_manager_selects_backend():
    apps = {"code-server": {"app_index": 0}}
    assert isinstance(_make_slot_manager("docker", apps, 0), SlotManager)
    assert isinstance(_make_slot_manager("k8s", apps, 0), K8sSlotManager)


def test_k8s_exec_map_uses_namespace_prefix(monkeypatch):
    monkeypatch.setenv("SAAS_K8S_NAMESPACE_PREFIX", "saasbench-s2")
    mapping = _build_k8s_exec_map(["code-server"], 2)

    assert mapping["rollout_2_code-server"] == {
        "namespace": "saasbench-s2-code-server",
        "selector": "app=code-server",
        "container": "code-server",
    }
