"""Faithfully translate an ``apps.yaml`` app definition into a normalized run spec the playground deploys.

This consumes the **same source the stock SlotManager uses** — each app's ``start:`` docker-run string
(and, for compose apps, its ``compose_template_file``) — so any app provisions with **zero per-app code**:
no hand-maintained parallel spec, no forked definitions, no per-app branches. The only adaptation is
binding the stock ``{hostname}``/``{port}``/``{pg_port}`` placeholders — which the stock runner points at
``localhost:<published-port>`` — to the per-run **public address**, applied uniformly to every app (the
same substitution the stock runner does, just to our origin instead of a docker host).

Compose support (``parse_compose``) lands in a follow-up; ``load_app_spec`` raises for compose apps until
then so callers fail loudly rather than silently mis-provisioning.
"""

from __future__ import annotations

import shlex
import string
import re
from dataclasses import dataclass, field
from typing import List

import yaml


class UnsupportedAppError(Exception):
    """The app's stock definition uses a feature we can't run on GKE Autopilot (privileged, host
    bind mounts). Callers skip the app with this reason rather than mis-provision it."""


@dataclass
class ServiceSpec:
    """One container as written in the app definition (a docker-run app has exactly one)."""

    name: str
    image: str                       # basename exactly as written (e.g. "mw-farmos:latest", "redis:6")
    env: dict                        # KEY -> value; values may carry {hostname}/{port}/{pg_port} placeholders
    container_port: int              # the app's listen port (container side of the first published port)
    user: str = ""                   # -u value (e.g. "0"); "" if unset
    command: List[str] = field(default_factory=list)        # k8s command (= compose entrypoint / docker ENTRYPOINT)
    args: List[str] = field(default_factory=list)           # k8s args (= compose command / docker CMD)
    privileged: bool = False
    volume_mounts: List[dict] = field(default_factory=list)  # [{"name": <vol>, "mountPath": <path>}]


@dataclass
class AppSpec:
    kind: str                        # "run" | "compose"
    services: List[ServiceSpec]      # for "run", exactly one; for "compose", all services in one pod
    web_service: str                 # the service that serves HTTP through the shared run router
    web_container_port: int
    host_aliases: List[str] = field(default_factory=list)    # compose service hostnames -> 127.0.0.1 (one pod)
    volumes: List[dict] = field(default_factory=list)         # k8s volume specs


# docker-run flags apps.yaml may use: valueless ones to skip, and value-taking ones we don't need.
_VALUELESS_FLAGS = {"-d", "--detach", "--rm", "-i", "-t", "-it", "--init", "--privileged"}
_VALUED_FLAGS_IGNORED = {
    "--restart", "--network", "--net", "--platform", "--hostname", "-h",
    "--workdir", "-w", "--entrypoint", "--add-host", "--label", "-l",
}


def parse_docker_run(start: str) -> ServiceSpec:
    """Parse a ``docker run ...`` command into a :class:`ServiceSpec`.

    Handles the flags apps.yaml actually uses (``--name``, ``-e/--env``, ``-p/--publish``, ``-u/--user``),
    skips other known flags, and treats the first non-flag token as the image (the rest as its command).
    """
    toks = shlex.split(start.strip())
    if toks[:2] != ["docker", "run"]:
        raise ValueError(f"not a `docker run` command: {start!r}")

    i, n = 2, len(toks)
    name = image = user = ""
    env: dict = {}
    ports: List[str] = []
    command: List[str] = []

    while i < n:
        t = toks[i]
        if t == "--name":
            name = toks[i + 1]
            i += 2
        elif t in ("-e", "--env"):
            key, _, val = toks[i + 1].partition("=")
            env[key] = val
            i += 2
        elif t in ("-p", "--publish"):
            ports.append(toks[i + 1])
            i += 2
        elif t in ("-u", "--user"):
            user = toks[i + 1]
            i += 2
        elif t in _VALUELESS_FLAGS:
            i += 1
        elif t in _VALUED_FLAGS_IGNORED:
            i += 2
        elif t.startswith("-"):
            # Unknown flag — consume a value if the next token looks like one (conservative; apps.yaml
            # only uses the flags above, so this is a safety net, not a hot path).
            i += 2 if (i + 1 < n and not toks[i + 1].startswith("-")) else 1
        else:
            image = t
            command = toks[i + 1:]
            break

    if not image:
        raise ValueError(f"no image token found in: {start!r}")
    return ServiceSpec(
        name=name, image=image, env=env,
        container_port=_first_container_port(ports), user=user, command=command,
    )


def _first_container_port(ports: List[str]) -> int:
    """Container side of the first ``-p host:container`` publish (the app's listen port)."""
    for spec in ports:
        tail = spec.split(":")[-1].split("/")[0]   # "ip:host:container/proto" -> "container"
        if tail.isdigit():
            return int(tail)
    return 0


def load_app_spec(app_cfg: dict, app_name: str = "") -> AppSpec:
    """Build an :class:`AppSpec` from an apps.yaml entry (the stock definition), faithfully."""
    if app_cfg.get("start_type") == "compose":
        raise NotImplementedError(
            f"compose app {app_name or app_cfg!r}: use parse_compose (it needs the per-run prefix/host), "
            "not load_app_spec"
        )
    svc = parse_docker_run(app_cfg["start"])
    # apps.yaml's container_port is authoritative when present; fall back to the parsed -p.
    port = int(app_cfg.get("container_port") or svc.container_port or 0)
    svc.container_port = port
    return AppSpec(kind="run", services=[svc], web_service=svc.name, web_container_port=port)


def _canonicalize_public_origin(text: str, *, run_host: str, public_port: str, public_scheme: str) -> str:
    """Rewrite rendered public-origin references into the form the agent's browser actually uses.

    apps.yaml start strings/templates embed the public origin as ``http://{hostname}:{port}``
    (or bare ``{hostname}:{port}`` for Host-validating apps like OpenProject/ownCloud). On the
    hosted playground that renders to ``http://<host>:80`` — but the edge serves TLS and Chrome
    silently upgrades navigations to https, so an app that bakes this value into browser-facing
    config (twenty's SERVER_URL -> the shell's REACT_APP_SERVER_BASE_URL) has every API call
    blocked as mixed content on the https page. Canonical form: ``<public_scheme>://<host>`` with
    the default-port suffix dropped (browsers never send ``:80``), and bare Host values likewise.
    Internal addresses (localhost, ``<prefix>-postgres:5432`` sidecar DSNs) never contain
    ``run_host`` and are untouched. Only the default-port public origin is canonicalized — a
    non-80 ``public_port`` (no TLS edge in front) is left verbatim.
    """
    if str(public_port) != "80":
        return text
    text = text.replace(f"http://{run_host}:80", f"{public_scheme}://{run_host}")
    return text.replace(f"{run_host}:80", run_host)


def render_run_values(
    spec: ServiceSpec, *, run_host: str, registry: str,
    public_port: str = "80", public_scheme: str = "https",
) -> dict:
    """Render saas-playground-run chart values from a docker-run :class:`ServiceSpec`.

    Binds the stock placeholders to the per-run public address: ``{hostname}`` -> ``run_host``,
    ``{port}`` -> ``public_port`` (the public port; the container's own port is set separately), and
    ``{pg_port}`` -> "" (secondary ports are not published on K8s — verifiers reach the DB via exec).
    Rendered values are then canonicalized to the browser-facing origin
    (see :func:`_canonicalize_public_origin`).
    """
    subs = {"{hostname}": run_host, "{port}": str(public_port), "{pg_port}": ""}

    def render(text: str) -> str:
        for token, value in subs.items():
            text = text.replace(token, value)
        return _canonicalize_public_origin(
            text, run_host=run_host, public_port=str(public_port), public_scheme=public_scheme,
        )

    return {
        "image": _registry_image(spec.image, registry),
        "containerPort": spec.container_port,
        "extraEnv": [{"name": k, "value": render(v)} for k, v in spec.env.items()],
        "runAsUser": spec.user,                    # "" when unset
        "command": [render(c) for c in spec.command],
    }


_OUR_IMAGE_PREFIXES = ("mw-", "oo-")


def _registry_image(image: str, registry: str) -> str:
    """Map an image basename to its registry, preserving the exact name (no ``mw-<app>`` guessing).

    Our seeded images (``mw-*``, ``oo-*``, ``*-bundle``) live in Artifact Registry; public images a
    compose file references (``redis:6``, ``postgres:13-alpine``) stay on their default registry.
    """
    if "/" in image:                               # already a fully-qualified ref
        return image
    base = image.split(":")[0]
    if base.startswith(_OUR_IMAGE_PREFIXES) or base.endswith("-bundle"):
        return f"{registry.rstrip('/')}/{image}"
    return image                                   # public image (Docker Hub, etc.)


# ---- compose translation -------------------------------------------------------------------------

def parse_compose(
    template_text: str,
    *,
    prefix: str,
    public_port: str,
    run_host: str,
    public_scheme: str = "https",
    allow_privileged: bool = False,
) -> AppSpec:
    """Translate a ``compose_template_file`` into a one-pod, multi-container :class:`AppSpec`.

    Renders the stock ``$prefix``/``$port``/``$hostname`` placeholders, then maps each compose
    service to a container in a single pod. Inter-service hostnames (e.g. ``<prefix>-postgres``)
    resolve via pod ``hostAliases`` -> 127.0.0.1 (one pod shares localhost); named volumes become
    ``emptyDir`` (matching the stock fresh-per-rollout volumes). Raises :class:`UnsupportedAppError`
    for features Autopilot forbids (privileged containers, host bind mounts), unless
    ``allow_privileged`` is set for a cluster that permits them.

    **depends_on / service_healthy ordering is NOT preserved.** A K8s pod starts all of its
    containers in parallel, with no native init-ordering between sidecars. Modern app containers
    (mattermost/owncloud/pretix) retry their DB/Redis connection on startup; combined with the
    pod-default ``restartPolicy: Always``, the web container crash-loops briefly while the DB
    sidecar comes up, then converges. Apps that don't tolerate retry would need a per-app
    initContainer that waits for the sidecar's port to open — deferred to M1+.
    """
    rendered = string.Template(template_text).safe_substitute(
        prefix=prefix, port=str(public_port), hostname=run_host,
    )
    # Same browser-facing origin canonicalization as the docker-run path (mattermost SITEURL,
    # pretix PRETIX_URL, owncloud OWNCLOUD_DOMAIN all embed $hostname:$port).
    rendered = _canonicalize_public_origin(
        rendered, run_host=run_host, public_port=str(public_port), public_scheme=public_scheme,
    )
    doc = yaml.safe_load(rendered) or {}
    services = doc.get("services") or {}
    if not services:
        raise ValueError("compose template defines no services")

    specs: List[ServiceSpec] = []
    host_aliases: List[str] = []
    all_volumes: List[str] = []
    web_service, web_port = "", 0

    for sname, sdef in services.items():
        cname = sdef.get("container_name", sname)
        host_aliases.append(cname)
        privileged = bool(sdef.get("privileged"))
        if privileged and not allow_privileged:
            raise UnsupportedAppError(f"service {cname!r} needs privileged (forbidden on Autopilot)")
        mounts, volumes = _compose_volumes(
            sdef.get("volumes", []) or [], cname, prefix=prefix, allow_host_mounts=allow_privileged,
        )
        all_volumes.extend(volumes)
        ports = sdef.get("ports") or []
        cport = _first_container_port([str(p) for p in ports]) if ports else 0
        spec = ServiceSpec(
            name=_short_name(cname, prefix),
            image=sdef["image"],
            env=_compose_env(sdef.get("environment")),
            container_port=cport,
            user=str(sdef.get("user") or ""),            # compose `user:` (e.g. mattermost's root)
            command=_as_list(sdef.get("entrypoint")),    # compose entrypoint -> k8s command
            args=_as_list(sdef.get("command")),           # compose command   -> k8s args
            privileged=privileged,
            volume_mounts=mounts,
        )
        specs.append(spec)
        if ports:
            web_service, web_port = spec.name, cport

    if not web_service:
        raise ValueError("no compose service publishes a port (cannot identify the web service)")
    seen: set = set()
    volumes = [v for v in all_volumes if not (v["name"] in seen or seen.add(v["name"]))]
    return AppSpec(kind="compose", services=specs, web_service=web_service,
                   web_container_port=web_port, host_aliases=host_aliases, volumes=volumes)


def render_compose_values(app: AppSpec, *, registry: str, seed_volumes=None) -> dict:
    """Render saas-playground-run-compose chart values: a web container + sidecars + hostAliases + volumes.

    ``seed_volumes`` is the list of container mountpaths that hold baked image data to preserve (see
    :func:`_seed_init_containers`). Default ``None`` seeds nothing — seeding must be opt-in per app,
    because a blanket seed would also target lean app images (e.g. mattermost) that lack a shell and
    don't need it, breaking the pod."""
    def container(s: ServiceSpec) -> dict:
        c = {"name": s.name, "image": _registry_image(s.image, registry)}
        if s.env:
            c["env"] = [{"name": k, "value": v} for k, v in s.env.items()]
        if s.command:
            c["command"] = s.command
        if s.args:
            c["args"] = s.args
        if s.volume_mounts:
            c["volumeMounts"] = s.volume_mounts
        security_context = {}
        uid = _resolve_user(s.user)
        if uid is not None:
            security_context["runAsUser"] = uid
        if s.privileged:
            security_context["privileged"] = True
        if security_context:
            c["securityContext"] = security_context
        return c

    web = next(s for s in app.services if s.name == app.web_service)
    main = container(web)
    main["containerPort"] = web.container_port
    return {
        "mainContainer": main,
        "sidecars": [container(s) for s in app.services if s.name != app.web_service],
        "hostAliases": [{"ip": "127.0.0.1", "hostnames": app.host_aliases}],
        "volumes": app.volumes,
        "initContainers": _seed_init_containers(app, registry, seed_volumes),
        "webContainerPort": web.container_port,
    }


def _seed_init_containers(app: AppSpec, registry: str, seed_volumes=None) -> list:
    """Replicate Docker's named-volume-from-image seeding, which K8s ``emptyDir`` does NOT do.

    Our seeded ``mw-*``/``oo-*`` images bake their data INTO the volume mountpaths (e.g. the
    mattermost postgres image ships a ~112MB initialized DB at ``/var/lib/postgresql/data``; the
    ONLYOFFICE documentserver bakes fonts at ``/usr/share/fonts``). On Docker, mounting a fresh
    named volume there copies the image's content into the volume; on K8s an ``emptyDir`` mounts
    empty and HIDES the baked data — so the app boots unseeded (or crash-loops, as postgres does
    when its data dir is unexpectedly empty). For each seeded mount we add an init container (same
    image) that copies the image's baked path into the emptyDir before the app starts — but only if
    the volume is still empty, so it seeds exactly once per run. Runs as root so ``cp -a`` preserves
    the baked ownership (e.g. postgres:postgres).

    Only mountpaths in ``seed_volumes`` are seeded. This is deliberately opt-in: a lean app image
    (mattermost) has no shell for the copy command AND no baked data worth preserving (its seed is
    in postgres), so seeding it would only break the pod. apps.yaml lists the data-bearing paths."""
    wanted = set(seed_volumes or [])
    if not wanted:
        return []
    inits = []
    for s in app.services:
        image = _registry_image(s.image, registry)
        for m in s.volume_mounts:
            name, path = m["name"], m["mountPath"]
            if path not in wanted:
                continue
            # name is the short (prefix-stripped) volume name -> unique per service+volume, <63 chars
            inits.append({
                "name": _sanitize(f"seed-{s.name}-{name}"),
                "image": image,
                # Fail loudly if the copy fails: `set -e`, no `|| true`, no stderr suppression. A
                # swallowed failure would boot the app against an empty volume — the very unseeded
                # state this fixes (worse for postgres: a partial copy). Skip only when the volume is
                # already populated, so it seeds exactly once.
                "command": ["sh", "-c",
                            f'set -e; if [ -z "$(ls -A /seed-target)" ]; then '
                            f'cp -a "{path}/." /seed-target/; fi'],
                "volumeMounts": [{"name": name, "mountPath": "/seed-target"}],
                "runAsUser": 0,
            })
    return inits


def _compose_env(environment) -> dict:
    """Compose ``environment`` may be a dict or a list of ``KEY=VALUE`` strings.

    YAML ``null`` values (Python ``None``) map to empty string — Compose treats null-valued env
    vars as empty/host-inherited, so ``str(None) == "None"`` would silently break apps.
    """
    if environment is None:
        return {}
    if isinstance(environment, dict):
        return {str(k): "" if v is None else str(v) for k, v in environment.items()}
    out: dict = {}
    for item in environment:
        key, _, val = str(item).partition("=")
        out[key] = val
    return out


def _as_list(val) -> List[str]:
    """Compose ``command``/``entrypoint`` may be a list or a shell string."""
    if val is None:
        return []
    if isinstance(val, list):
        return [str(x) for x in val]
    return shlex.split(str(val))


def _compose_volumes(volumes, cname: str, *, prefix: str = "", allow_host_mounts: bool = False):
    """Map compose volume specs to k8s mounts; reject host bind mounts unless explicitly allowed.

    Volume names strip the per-run ``prefix`` (e.g. ``run-<id>-onlyoffice_community_letsencrypt`` ->
    ``community-letsencrypt``): the names are pod-local so the bare suffix is unique, and the
    prefixed form blows past the K8s 63-char volume-name limit for long run ids."""
    mounts, volume_specs = [], []
    for vol in volumes:
        parts = str(vol).split(":")
        src = parts[0]
        dst = parts[1] if len(parts) > 1 else parts[0]
        mode = parts[2] if len(parts) > 2 else ""
        if src.startswith("/") or src.startswith("."):
            if not allow_host_mounts:
                raise UnsupportedAppError(f"service {cname!r} uses host bind mount {vol!r} (not supported)")
            name = _short_name(f"{cname}-{src.strip('/') or 'root'}", prefix)
            volume_specs.append({"name": name, "hostPath": {"path": src, "type": "Directory"}})
        else:
            name = _short_name(src, prefix)
            volume_specs.append({"name": name, "emptyDir": {}})
        mount = {"name": name, "mountPath": dst}
        if mode == "ro":
            mount["readOnly"] = True
        mounts.append(mount)
    return mounts, volume_specs


def _sanitize(name: str) -> str:
    """A DNS-1123-safe name for a k8s volume (lowercase, no underscores)."""
    out = re.sub(r"[^a-z0-9-]+", "-", name.lower().replace("_", "-")).strip("-")
    return out or "volume"


def _resolve_user(user_str: str):
    """Map a docker/compose ``user:`` value to a numeric UID for k8s ``securityContext.runAsUser``.

    - ``""`` -> None (skip; let the image default apply).
    - ``"root"`` -> 0.
    - ``"1000"`` or ``"1000:1000"`` -> 1000 (group part dropped — k8s splits uid/gid).
    - named users (``"postgres"``) -> None (can't resolve without the image's /etc/passwd; skip
      rather than fail. parse_docker_run-driven apps only use numeric ``-u`` in apps.yaml).
    """
    if not user_str:
        return None
    if user_str == "root":
        return 0
    head = user_str.split(":")[0]
    try:
        return int(head)
    except ValueError:
        return None


def _short_name(container_name: str, prefix: str) -> str:
    """Short, DNS-safe container name: strip the per-run prefix; the bare web service becomes 'app'."""
    name = container_name
    for pre in (prefix + "-", prefix + "_", prefix):
        if name.startswith(pre):
            name = name[len(pre):]
            break
    name = name.strip("-_")
    return _sanitize(name) if name else "app"
