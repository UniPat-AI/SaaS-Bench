# Smoke fake site

This fixture is a synthetic task used by the Kubernetes smoke test.
It does not exercise an LLM agent; it validates that a task verifier can
evaluate a website deployed to Kubernetes through the harness's normal
`SERVER_HOSTNAME` and `<APP>_PORT` contract.
