"""Per-slot Docker container manager for SaaS-Bench.

Port formula: BASE_PORT + slot_id * slot_offset + app_index
Default:      30000     + slot_id * 20           + app_index

Container naming: rollout_{slot_id}_{app_name}
Compose project:  rollout_{slot_id}_{app_name}
"""

import os
import subprocess
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from string import Template

import tempfile

_BASE_PORT = int(os.environ.get("SAAS_BASE_PORT", 30000))
_SLOT_OFFSET = 40  # web apps use indices 0-22; 40 leaves headroom for DB ports
_SLOT_PREFIX = os.environ.get("SAAS_SLOT_PREFIX", "rollout")

# Transient compose files live under SAAS_BENCH_TMP (defaults to the system
# temp dir) so parallel rollouts with different prefixes stay isolated.
_TMP_DIR = os.environ.get(
    "SAAS_BENCH_TMP", os.path.join(tempfile.gettempdir(), f"saas_bench_{_SLOT_PREFIX}")
)
os.makedirs(_TMP_DIR, exist_ok=True)

# Repo root = parent of the saas_bench/ package directory.
# Used to resolve {repo_root} in apps.yaml `start` commands and to anchor
# compose template paths declared there.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_READY_OK_STATUSES = {200, 301, 302, 303, 401, 403}
_READY_INTERVAL = 2.0
_PROBE_BODY_LIMIT = 128 * 1024


def _probe_http(
    port: int,
    path: str,
    hostname: str,
    bad_markers: list[str] | None = None,
    required_markers: list[str] | None = None,
) -> tuple[bool, str]:
    url = f"http://{hostname}:{port}{path}"
    status = None
    body = ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "rollout-probe/1.0"})
        with urllib.request.urlopen(req, timeout=3) as r:
            status = r.status
            body = r.read(_PROBE_BODY_LIMIT).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read(_PROBE_BODY_LIMIT).decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except Exception as e:
        return False, f"{url}: {type(e).__name__}: {str(e)[:100]}"

    if status not in _READY_OK_STATUSES:
        return False, f"{url}: HTTP {status}"

    for marker in bad_markers or []:
        if marker and marker in body:
            return False, f"{url}: HTTP {status}, bad marker {marker!r}"

    for marker in required_markers or []:
        if marker and marker not in body:
            return False, f"{url}: HTTP {status}, missing marker {marker!r}"

    return True, f"{url}: HTTP {status}"


def _health_paths(cfg: dict) -> list[str]:
    paths = [cfg.get("health_path", "/")]
    paths.extend(cfg.get("extra_health_paths", []) or [])
    return list(dict.fromkeys(paths))


def _wait_ready(port: int, cfg: dict, timeout: int, hostname: str = "localhost") -> float:
    """Poll configured HTTP health paths until ready or timeout.

    Accepts response statuses in _READY_OK_STATUSES as ready.
    Raises RuntimeError on timeout (caller treats as fatal task failure).
    Returns elapsed seconds when ready.
    """
    start = time.time()
    deadline = start + timeout
    last_err = "no probe attempted"
    paths = _health_paths(cfg)
    bad_markers = cfg.get("health_bad_markers", []) or []
    required_markers = cfg.get("health_required_markers", []) or []
    while time.time() < deadline:
        failures = []
        for path in paths:
            ok, detail = _probe_http(port, path, hostname, bad_markers, required_markers)
            if not ok:
                failures.append(detail)
        if not failures:
            return round(time.time() - start, 1)
        last_err = "; ".join(failures[-3:])
        time.sleep(_READY_INTERVAL)
    elapsed = round(time.time() - start, 1)
    raise RuntimeError(
        f"app on port {port} not ready in {timeout}s "
        f"(probed {paths}, last={last_err}, elapsed={elapsed}s)"
    )


def _docker_health_status(container: str) -> tuple[str, str]:
    cmd = (
        "docker inspect "
        "--format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "
        f"{container}"
    )
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        return "missing", r.stderr.strip() or r.stdout.strip()
    status = r.stdout.strip()
    return status, status


def _wait_compose_health(prefix: str, services: list[str], timeout: int) -> float:
    start = time.time()
    deadline = start + timeout
    last = "no inspect attempted"
    while time.time() < deadline:
        pending = []
        for service in services:
            container = prefix if service in ("", ".") else f"{prefix}-{service}"
            status, detail = _docker_health_status(container)
            if status not in {"healthy", "running"}:
                pending.append(f"{container}={detail}")
        if not pending:
            return round(time.time() - start, 1)
        last = "; ".join(pending)
        time.sleep(_READY_INTERVAL)
    elapsed = round(time.time() - start, 1)
    raise RuntimeError(
        f"compose services for {prefix} not healthy in {timeout}s "
        f"(last={last}, elapsed={elapsed}s)"
    )


class SlotManager:
    def __init__(self, apps_config: dict, slot_id: int):
        self.apps = apps_config
        self.slot_id = slot_id
        self._validate_port_layout()

    # -- Public interface -----------------------------------------------------

    def get_port(self, app: str) -> int:
        return _BASE_PORT + self.slot_id * _SLOT_OFFSET + self.apps[app]["app_index"]

    def get_container_name(self, app: str) -> str:
        return f"{_SLOT_PREFIX}_{self.slot_id}_{app}"

    def get_port_map(self, apps: list[str]) -> dict[str, int]:
        return {app: self.get_port(app) for app in apps}

    def get_pg_port(self, app: str) -> int | None:
        """Return the exposed Postgres port for an app, or None if not configured."""
        offset = self.apps.get(app, {}).get("pg_port_offset")
        if offset is not None:
            return self.get_port(app) + int(offset)
        return None

    def _validate_port_layout(self) -> None:
        claims: dict[int, str] = {}
        for app, cfg in self.apps.items():
            app_index = int(cfg["app_index"])
            if not 0 <= app_index < _SLOT_OFFSET:
                raise ValueError(
                    f"app_index for {app} must be within slot range 0-{_SLOT_OFFSET - 1}: {app_index}"
                )
            claim = claims.get(app_index)
            if claim is not None:
                raise ValueError(f"slot port index {app_index} is shared by {claim} and {app}")
            claims[app_index] = app

            pg_offset = cfg.get("pg_port_offset")
            if pg_offset is None:
                continue
            pg_index = app_index + int(pg_offset)
            if not 0 <= pg_index < _SLOT_OFFSET:
                raise ValueError(
                    f"Postgres port index for {app} must be within slot range "
                    f"0-{_SLOT_OFFSET - 1}: {pg_index}"
                )
            claim = claims.get(pg_index)
            if claim is not None:
                raise ValueError(
                    f"slot port index {pg_index} is shared by {claim} and {app} Postgres"
                )
            claims[pg_index] = f"{app} Postgres"

    def start_apps(self, apps: list[str], hostname: str = "localhost") -> None:
        # Stop any stale containers first (sequentially, fast)
        for app in apps:
            self._stop_one(app)
        # Start all apps in parallel — each polls its own readiness
        with ThreadPoolExecutor(max_workers=len(apps)) as pool:
            futures = {pool.submit(self._start_one, app, hostname): app for app in apps}
            for fut in as_completed(futures):
                fut.result()  # re-raise any exception

    def stop_apps(self, apps: list[str]) -> None:
        for app in apps:
            self._stop_one(app)

    # -- Internal helpers -----------------------------------------------------

    def _start_one(self, app: str, hostname: str) -> None:
        cfg = self._get(app)
        port = self.get_port(app)
        name = self.get_container_name(app)

        print(f"  [slot {self.slot_id}] starting {app} on :{port}...", flush=True)

        if cfg.get("start_type") == "compose":
            self._start_compose(app, cfg, port, name, hostname)
        else:
            # Build extra format vars (e.g. pg_port for Baserow Postgres exposure)
            fmt = dict(container=name, port=port, hostname=hostname, repo_root=_REPO_ROOT)
            pg_offset = cfg.get("pg_port_offset")
            if pg_offset is not None:
                fmt["pg_port"] = port + int(pg_offset)
            cmd = cfg["start"].format(**fmt)
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(f"[slot {self.slot_id}] failed to start {app}: {r.stderr.strip()}")

        # Readiness probe (replaces fixed sleep)
        timeout = cfg.get("startup_wait", 600)
        try:
            elapsed = _wait_ready(port, cfg, timeout, hostname)
            compose_services = cfg.get("compose_health_services", []) or []
            if compose_services:
                _wait_compose_health(name, compose_services, min(120, timeout))
            print(f"  [slot {self.slot_id}] {app} ready in {elapsed}s", flush=True)
        except RuntimeError as e:
            ps = subprocess.run(
                f"docker ps -a --filter 'name={name}' --format '{{{{.Names}}}} {{{{.Status}}}}'",
                shell=True, capture_output=True, text=True,
            )
            logs = subprocess.run(
                f"docker logs --tail 20 {name} 2>&1",
                shell=True, capture_output=True, text=True,
            )
            raise RuntimeError(
                f"[slot {self.slot_id}] {app} readiness FAILED: {e}\n"
                f"  docker_ps: {ps.stdout.strip()}\n"
                f"  logs_tail: {logs.stdout[-600:]}"
            ) from e

    def _start_compose(self, app: str, cfg: dict, port: int, prefix: str, hostname: str) -> None:
        tpl_path = cfg["compose_template_file"]
        # resolve relative path from repo root
        if not os.path.isabs(tpl_path):
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tpl_path = os.path.join(here, tpl_path)

        network_key = f"{_SLOT_PREFIX}:{self.slot_id}:{app}".encode()
        network_index = zlib.crc32(network_key) % (64 * 256)
        subnet = f"100.{64 + network_index // 256}.{network_index % 256}.0/24"
        with open(tpl_path) as f:
            content = Template(f.read()).safe_substitute(
                prefix=prefix,
                port=port,
                hostname=hostname,
                subnet=subnet,
            )

        attempts = int(os.environ.get("SAAS_COMPOSE_START_ATTEMPTS", "2"))
        errors = []
        for attempt in range(1, attempts + 1):
            tmp = f"{_TMP_DIR}/{prefix}.yml"
            with open(tmp, "w") as f:
                f.write(content)

            cmd = f"docker compose --project-name {prefix} -f {tmp} up -d"
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if r.returncode == 0:
                return
            errors.append(f"attempt {attempt}: {r.stderr.strip()}")
            self._stop_one(app)
            if attempt < attempts:
                time.sleep(3)

        raise RuntimeError(
            f"[slot {self.slot_id}] compose failed for {app} after {attempts} attempts: "
            + " | ".join(errors)
        )

    def _stop_one(self, app: str) -> None:
        cfg = self._get(app)
        prefix = self.get_container_name(app)

        if cfg.get("start_type") == "compose":
            tmp = f"{_TMP_DIR}/{prefix}.yml"
            if os.path.exists(tmp):
                subprocess.run(
                    f"docker compose --project-name {prefix} -f {tmp} down -v --remove-orphans --timeout 10",
                    shell=True, capture_output=True, text=True,
                )
            # Stop and remove all containers created by compose (without relying on docker compose down)
            containers = self._compose_containers(app, cfg, prefix)
            for c in containers:
                subprocess.run(
                    f"docker stop {c} 2>/dev/null; docker rm {c} 2>/dev/null",
                    shell=True, capture_output=True, text=True,
                )
            # Remove named volumes
            for suffix in cfg.get("compose_volumes", []):
                vol = f"{prefix}{suffix}"
                subprocess.run(
                    f"docker volume rm {vol} 2>/dev/null",
                    shell=True, capture_output=True, text=True,
                )
            # Remove compose network (naming convention: {project}_{network_in_yaml})
            # Template uses $prefix-net as the network name → actual name = {prefix}_{prefix}-net
            net = f"{prefix}_{prefix}-net"
            subprocess.run(
                f"docker network rm {net} 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )
            # Clean up temporary yml
            tmp = f"{_TMP_DIR}/{prefix}.yml"
            if os.path.exists(tmp):
                os.unlink(tmp)
        else:
            subprocess.run(
                f"docker stop {prefix} 2>/dev/null; docker rm -v {prefix} 2>/dev/null",
                shell=True, capture_output=True, text=True,
            )

    def _compose_containers(self, app: str, cfg: dict, prefix: str) -> list[str]:
        """Parse all container_name fields from the compose template, substituting $prefix."""
        tpl_path = cfg.get("compose_template_file", "")
        if not tpl_path:
            return [prefix]
        if not os.path.isabs(tpl_path):
            here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            tpl_path = os.path.join(here, tpl_path)
        try:
            with open(tpl_path) as f:
                content = f.read()
        except OSError:
            return [prefix]
        names = []
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("container_name:"):
                raw = stripped.split(":", 1)[1].strip()
                # Substitute $prefix (excluding volume names of the ${prefix}_* form)
                name = raw.replace("$prefix", prefix)
                names.append(name)
        return names if names else [prefix]

    def _get(self, app: str) -> dict:
        if app not in self.apps:
            raise KeyError(f"Unknown app '{app}'. Check rollout/apps.yaml.")
        return self.apps[app]
