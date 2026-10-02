# Target Playground service image: the FastAPI service + helm/kubectl + the docker->kubectl exec shim
# + the bundled tasks/ (verifiers) and charts/ (per-run chart). Deliberately does NOT install
# browser-use/playwright — the service never runs the agent; it only provisions + grades.
FROM python:3.11-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl bash ca-certificates nginx-light \
 && rm -rf /var/lib/apt/lists/*

# kubectl + helm (the provisioner shells these; the shim maps verifiers' `docker` -> `kubectl`)
RUN curl -fsSL -o /usr/local/bin/kubectl "https://dl.k8s.io/release/v1.30.0/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl
RUN curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash

RUN pip install --no-cache-dir fastapi "uvicorn[standard]" pyyaml requests

WORKDIR /app
ENV PYTHONPATH=/app
COPY saas_bench /app/saas_bench
COPY tasks /app/tasks
COPY charts /app/charts
COPY docker /app/docker
COPY deploy /app/deploy

# the docker->kubectl exec shim first on PATH so upstream verify.py (which calls `docker exec`) works
RUN chmod +x /app/deploy/shim/docker
ENV PATH="/app/deploy/shim:${PATH}"

EXPOSE 8080
CMD ["uvicorn", "saas_bench.deploy_entry:make_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]
