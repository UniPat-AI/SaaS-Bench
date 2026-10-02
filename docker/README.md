# Docker Images

SaaS-Bench evaluates agents against 23 self-hosted SaaS applications running
inside Docker. We distribute these as pre-built `.tar` archives so users do
not need to build images themselves.

## 1. Download

Download all image archives from Huggingface: https://huggingface.co/datasets/Marti844/SaaS-Bench-docker

Place all 23 `.tar` files under this directory — about **54 GB** of archives:

```
docker/images/
├── mw-onlyoffice.tar     9.7G    ├── mw-mediacms.tar       1.6G
├── mw-hrms.tar           5.5G    ├── mw-booklore.tar       1.5G
├── mw-code-server.tar    4.6G    ├── mw-mattermost.tar     1.4G
├── mw-photoprism.tar     3.6G    ├── mw-roundcubemail.tar  1.3G
├── mw-baserow.tar        2.9G    ├── mw-farmos.tar         1.1G
├── mw-siyuan.tar         2.9G    ├── mw-metabase.tar       850M
├── mw-bigcapital.tar     2.7G    ├── mw-recipya.tar        590M
├── mw-openemr.tar        2.6G    ├── mw-opnform.tar        470M
├── mw-pretix.tar         2.2G    ├── mw-grocy.tar          280M
├── mw-openproject.tar    2.1G    └── mw-watcharr.tar       240M
├── mw-owncloud.tar       2.0G
├── mw-twenty.tar         2.0G
└── mw-elabel.tar         1.9G
```

Three archives are named after the application but carry differently-tagged
images inside, so do not rename them or expect a 1:1 file→tag mapping:

| Archive | Images inside |
|---|---|
| `mw-code-server.tar` | `code-server-bundle:latest` |
| `mw-onlyoffice.tar` | `oo-bundle-community2`, `oo-bundle-ds`, `oo-bundle-es`, `oo-bundle-mysql` |
| `mw-owncloud.tar` | `mw-owncloud-server`, `mw-owncloud-mariadb`, `redis:6` |

(`mw-mattermost.tar` likewise carries its `mw-mattermost-postgres` sidecar.)

## 2. Load

From the repository root:

```bash
bash scripts/load_images.sh
```

This will `docker load` every archive in `docker/images/`. Verify with:

```bash
docker images | grep -cE '^(mw-|oo-bundle-|code-server-bundle)'
```

A complete load is **29 images**: 23 tagged `mw-*` (including the
`mw-mattermost-postgres`, `mw-owncloud-server` and `mw-owncloud-mariadb`
sidecars), `code-server-bundle`, four `oo-bundle-*`, and `redis:6`.

## 3. These are benchmark builds, not stock upstream images

Several `mw-*` images carry a small fixture on top of the upstream application — seed data a task
needs, or a value the app's own UI leaves unreachable. Everything required is inside the image, so
loading the tar is all you need: there is no separate fixture step, no bind mount, and nothing to
configure. A few are applied on each container start rather than baked into the image's data, so an
app may finish its first request a few seconds after the container reports ready.

## 4. Compose-based applications

Four applications run as multi-container `docker compose` stacks:

- `pretix`     → `pretix.yml.tpl`
- `onlyoffice` → `onlyoffice.yml.tpl`
- `mattermost` → `mattermost.yml.tpl`
- `owncloud`   → `owncloud.yml.tpl`

These templates are instantiated per slot at runtime by `saas_bench/slot.py`.
Every image they reference ships in the archives above **except two**, both
belonging to pretix:

```bash
docker pull postgres:13-alpine
docker pull redis:7-alpine
```

`docker compose` pulls them on first launch, so an online host needs nothing
extra; pre-pull them if the eval host is offline. (ownCloud's `redis:6` and
Mattermost's postgres sidecar are already inside their archives — only pretix
reaches out.)

## 5. Disk usage

The archives are about **54 GB**; loaded, the images occupy roughly **60 GB**.
Allow ~120 GB free on the partition holding `/var/lib/docker` — `docker load`
needs room for both the archive and the unpacked layers.

