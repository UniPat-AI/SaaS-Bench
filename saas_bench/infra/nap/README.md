# SaaS-Bench NAP NodePool

This directory owns the Karpenter NodePool configuration for fake-app workloads.

The current capacity target is:

- 8 vCPU / 32 GiB nodes.
- AMD `Standard_D8as_v5` preferred; Intel `Standard_D8s_v5` retained as fallback.
- On-demand capacity only.
- Up to 8 nodes, 64 vCPU, and 256 GiB.
- Five-minute consolidation delay to avoid rapid node churn.
- Existing `saas-bench.io/fakeapp=true:NoSchedule` taint preserved.

## Apply

```powershell
kubectl apply -f saas_bench/infra/nap/default-nodepool.yaml
kubectl get nodepool default -o yaml
```

## Verify

```powershell
kubectl get nodeclaims.karpenter.sh -o wide
kubectl get nodes -o custom-columns=NAME:.metadata.name,SKU:.metadata.labels.node\.kubernetes\.io/instance-type,CPU:.status.capacity.cpu,MEMORY:.status.capacity.memory
```

Existing nodes are not deleted by this declaration. Karpenter replaces them through its disruption and consolidation policy, or when new capacity is required.

## Rollback

Restore the previous NodePool requirements and limits through the same Karpenter NodePool resource. Do not manually delete fake-app pods to force replacement.
