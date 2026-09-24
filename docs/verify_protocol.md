# Verify Protocol

Each task ships with a `verify.py` script that decides — after the agent
finishes — whether the agent's actions actually produced the required state
inside the SaaS application. This document specifies the contract between
`verify.py` and the SaaS-Bench eval harness.

## Invocation

The harness runs verify scripts as a subprocess **after** the agent has
finished and **while the docker containers are still alive**:

```bash
python <task_dir>/verify.py
```

A 300-second hard timeout is enforced. The script's working directory is
unspecified — use absolute paths or `os.path.dirname(__file__)`.

## Environment variables (injected by the harness)

For every site declared in `meta.json -> meta_data.sites`, the harness
exports a stable set of variables before exec:

| Variable                  | Set when           | Meaning                                  |
| ------------------------- | ------------------ | ---------------------------------------- |
| `SERVER_HOSTNAME`         | always             | Hostname the apps are reachable at       |
| `<APP>_PORT`              | port_map known     | Host port the app listens on             |
| `<APP>_CONTAINER`         | always             | Name of the app's container              |
| `<APP>_DB_CONTAINER`      | app has a DB       | Name of the app's database container     |

`<APP>` is the upper-case app key from `apps.yaml`, e.g. `MATTERMOST_PORT`,
`OWNCLOUD_DB_CONTAINER`. The production mapping is defined in
`saas_bench/verify_runner.py:SITE_CONFIG`.

Smoke fixtures and future non-production apps do not need a `SITE_CONFIG`
entry. For any unknown site name, the harness derives the prefix by replacing
non-alphanumeric characters with `_` and upper-casing it. For example,
`fake-site` exports `FAKE_SITE_PORT` and `FAKE_SITE_CONTAINER`.

For compose-based apps the `_CONTAINER` env vars include the per-template
suffix (e.g. `mattermost` → `rollout_<slot>_mattermost`, while
`MATTERMOST_DB_CONTAINER` → `rollout_<slot>_mattermost-postgres`).

## Output protocol

`verify.py` writes to **stderr** (stdout is reserved for human-readable
debugging if needed). Two line types are recognised:

### Per-check line

```
[PASS] (Npt) <label>
[PASS] (Npt) <label>  (<detail>)
[FAIL] (Npt) <label>  (<detail>)
```

- `Npt`         — non-negative integer weight for this check
- `<label>`     — short check description
- `<detail>`    — optional, parenthesised, separated from label by **two or more spaces**

### Score summary line (optional but recommended)

```
SCORE: <float>  PASS: <True|False>  (<earned>/<total>)
```

If the SCORE line is omitted the harness will compute it from per-check
weights: `score = sum(weight | passed) / sum(weight)`.

## Exit code

The exit code is captured but does **not** affect scoring. `verify.py` may
exit 0 even if checks failed.

## Result JSON

The harness writes `<task_id>_verify.json` to `--result-dir`:

```json
{
  "task_id": "...",
  "status":  "PASS | FAIL | ERROR | SKIP",
  "score":   0.0,
  "earned":  0,
  "total":   0,
  "all_pass": true,
  "checks": [
    {"label": "...", "weight": 1, "passed": true, "detail": "..."}
  ],
  "returncode": 0,
  "error": null
}
```

- `status` is `PASS` only if all checks passed.
- `status = ERROR` is reserved for cases where verify itself crashed or
  produced no parseable output.

## Minimal example

```python
# verify.py
import os, sys, requests

PORT = int(os.environ["MATTERMOST_PORT"])
HOST = os.environ["SERVER_HOSTNAME"]

def check(label, weight, ok, detail=""):
    tag = "PASS" if ok else "FAIL"
    suffix = f"  ({detail})" if detail else ""
    print(f"[{tag}] ({weight}pt) {label}{suffix}", file=sys.stderr)
    return weight if ok else 0

earned, total = 0, 0
total += 1; earned += check("server reachable", 1,
    requests.get(f"http://{HOST}:{PORT}/api/v4/system/ping").status_code == 200)

print(f"SCORE: {earned/total:.3f}  PASS: {earned == total}  ({earned}/{total})",
      file=sys.stderr)
```
