from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "saas_bench" / "infra" / "nap" / "default-nodepool.yaml"


def _load_nodepool() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_default_nodepool_uses_memory_balanced_sku_and_headroom() -> None:
    nodepool = _load_nodepool()

    assert nodepool["apiVersion"] == "karpenter.sh/v1"
    assert nodepool["kind"] == "NodePool"
    assert nodepool["metadata"]["name"] == "default"

    spec = nodepool["spec"]
    assert spec["limits"] == {
        "cpu": "64",
        "memory": "256Gi",
        "nodes": "8",
    }
    assert spec["disruption"]["consolidateAfter"] == "5m"
    assert spec["disruption"]["consolidationPolicy"] == "WhenEmptyOrUnderutilized"

    requirements = {
        item["key"]: item["values"]
        for item in spec["template"]["spec"]["requirements"]
    }
    assert requirements["karpenter.azure.com/sku-name"] == [
        "Standard_D8as_v5",
        "Standard_D8s_v5",
    ]
    assert requirements["karpenter.azure.com/sku-family"] == ["D"]
    assert requirements["karpenter.sh/capacity-type"] == ["on-demand"]


def test_fakeapp_taint_is_preserved() -> None:
    nodepool = _load_nodepool()
    template_spec = nodepool["spec"]["template"]["spec"]

    assert template_spec["taints"] == [
        {
            "effect": "NoSchedule",
            "key": "saas-bench.io/fakeapp",
            "value": "true",
        }
    ]
