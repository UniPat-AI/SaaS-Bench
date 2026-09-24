#!/usr/bin/env python3
"""Generate local Kubernetes manifests for all SaaS-Bench app images."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "deploy" / "k8s" / "apps"
NODE_PORT_BASE = 30100


def env(**values):
    return [{"name": k, "value": str(v)} for k, v in values.items()]


def container(name, image, port, *, env_vars=None, args=None, privileged=False, port_name="http"):
    c = {
        "name": name,
        "image": image,
        "imagePullPolicy": "Never",
        "ports": [{"name": port_name, "containerPort": port}],
    }
    if env_vars:
        c["env"] = env_vars
    if args:
        c["args"] = args
    if privileged:
        c["securityContext"] = {"privileged": True}
    return c


def app_manifest(app, index, containers, service_port, health_path, target_port_name="http"):
    main = next(c for c in containers if c["ports"][0]["name"] == target_port_name)
    main["readinessProbe"] = {
        "tcpSocket": {"port": target_port_name},
        "initialDelaySeconds": 10,
        "periodSeconds": 5,
        "failureThreshold": 60,
    }
    main["livenessProbe"] = {
        "tcpSocket": {"port": target_port_name},
        "initialDelaySeconds": 180,
        "periodSeconds": 10,
        "failureThreshold": 30,
    }

    node_port = NODE_PORT_BASE + index
    ns = f"saasbench-{app}"
    selector = {"app": app}
    deployment = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": app, "namespace": ns, "labels": selector},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": selector},
            "template": {
                "metadata": {"labels": selector},
                "spec": {"containers": containers},
            },
        },
    }
    service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": app, "namespace": ns, "labels": selector},
        "spec": {
            "type": "NodePort",
            "selector": selector,
            "ports": [{
                "name": "http",
                "port": service_port,
                "targetPort": target_port_name,
                "nodePort": node_port,
            }],
        },
    }
    return {
        "app": app,
        "namespace": ns,
        "nodePort": node_port,
        "healthPath": health_path,
        "documents": [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}},
            deployment,
            service,
        ],
    }


def single(app, index, image, port, health, env_vars=None):
    return app_manifest(
        app,
        index,
        [container(app, image, port, env_vars=env_vars)],
        80,
        health,
    )


def build_apps():
    apps = {}

    def add(spec):
        apps[spec["app"]] = spec

    add(single("code-server", 0, "code-server-bundle:latest", 8080, "/",
               env_vars=env(PASSWORD="8a128206e2177bce1e48e565")))
    apps["code-server"]["documents"][1]["spec"]["template"]["spec"]["containers"][0]["securityContext"] = {"runAsUser": 0}
    add(single("openproject", 1, "mw-openproject:latest", 80, "/health_checks/default",
               env_vars=env(OPENPROJECT_HOST__NAME="s0-openproject.saasbench.localhost",
                            OPENPROJECT_ADDITIONAL_HOSTS="localhost,127.0.0.1,s0-openproject.saasbench.localhost",
                            OPENPROJECT_HTTPS="false", SECRET_KEY_BASE="secret")))
    add(single("metabase", 2, "mw-metabase:latest", 80, "/"))
    add(single("baserow", 3, "mw-baserow:latest", 6806, "/login",
               env_vars=env(BASEROW_PUBLIC_URL="http://localhost")))
    add(single("twenty", 4, "mw-twenty:latest", 3000, "/",
               env_vars=env(SERVER_URL="http://localhost")))
    add(single("bigcapital", 5, "mw-bigcapital:latest", 80, "/"))
    add(single("hrms", 6, "mw-hrms:latest", 8000, "/"))
    add(single("openemr", 8, "mw-openemr:latest", 80, "/interface/login/login.php?site=default"))
    add(single("opnform", 9, "mw-opnform:latest", 8080, "/",
               env_vars=env(APP_URL="http://localhost")))
    add(single("roundcubemail", 13, "mw-roundcubemail:latest", 80, "/"))
    add(single("grocy", 14, "mw-grocy:latest", 80, "/"))
    add(single("recipya", 15, "mw-recipya:latest", 8078, "/",
               env_vars=env(RECIPYA_SERVER_URL="http://localhost")))
    add(single("farmos", 16, "mw-farmos:latest", 80, "/"))
    add(single("e-label", 17, "mw-elabel:latest", 8080, "/"))
    add(single("siyuan", 18, "mw-siyuan:latest", 6806, "/",
               env_vars=env(SIYUAN_ACCESS_AUTH_CODE="siyuan6037")))
    add(single("watcharr", 19, "mw-watcharr:latest", 3080, "/"))
    add(single("booklore", 20, "mw-booklore:latest", 8080, "/"))
    add(single("mediacms", 21, "mw-mediacms:latest", 80, "/"))
    add(single("photoprism", 22, "mw-photoprism:latest", 2342, "/api/v1/status",
               env_vars=env(PHOTOPRISM_SITE_URL="http://localhost/")))

    add(app_manifest("mattermost", 11, [
        container("postgres", "mw-mattermost-postgres:latest", 5432,
                  env_vars=env(POSTGRES_USER="mmuser", POSTGRES_PASSWORD="mmuser_password",
                               POSTGRES_DB="mattermost", POSTGRES_HOST_AUTH_METHOD="md5"),
                  port_name="db"),
        container("mattermost", "mw-mattermost:latest", 8065,
                  env_vars=env(MM_SQLSETTINGS_DRIVERNAME="postgres",
                               MM_SQLSETTINGS_DATASOURCE="postgres://mmuser:mmuser_password@127.0.0.1:5432/mattermost?sslmode=disable&connect_timeout=10",
                               MM_BLEVESETTINGS_INDEXDIR="/mattermost/bleve-indexes",
                               MM_SERVICESETTINGS_SITEURL="http://localhost",
                               MM_SERVICESETTINGS_LISTENADDRESS=":8065"),
                  port_name="main"),
    ], 80, "/api/v4/system/ping", target_port_name="main"))

    add(app_manifest("owncloud", 12, [
        container("mariadb", "mw-owncloud-mariadb:latest", 3306,
                  env_vars=env(MYSQL_ROOT_PASSWORD="owncloud", MYSQL_USER="owncloud",
                               MYSQL_PASSWORD="owncloud", MYSQL_DATABASE="owncloud"),
                  args=["--max-allowed-packet=128M", "--innodb-log-file-size=64M"],
                  port_name="db"),
        container("redis", "redis:6", 6379, port_name="redis"),
        container("owncloud", "mw-owncloud-server:latest", 8080,
                  env_vars=env(OWNCLOUD_DOMAIN="localhost",
                               OWNCLOUD_TRUSTED_DOMAINS="localhost,127.0.0.1,s0-owncloud.saasbench.localhost",
                               OWNCLOUD_DB_TYPE="mysql", OWNCLOUD_DB_NAME="owncloud",
                               OWNCLOUD_DB_USERNAME="owncloud", OWNCLOUD_DB_PASSWORD="owncloud",
                               OWNCLOUD_DB_HOST="127.0.0.1", OWNCLOUD_ADMIN_USERNAME="admin",
                               OWNCLOUD_ADMIN_PASSWORD="admin", OWNCLOUD_MYSQL_UTF8MB4="true",
                               OWNCLOUD_REDIS_ENABLED="true", OWNCLOUD_REDIS_HOST="127.0.0.1"),
                  port_name="main"),
    ], 80, "/status.php", target_port_name="main"))

    add(app_manifest("onlyoffice", 10, [
        container("mysql", "oo-bundle-mysql:latest", 3306,
                  env_vars=env(MYSQL_ROOT_PASSWORD="my-secret-pw", MYSQL_DATABASE="onlyoffice",
                               MYSQL_USER="onlyoffice_user", MYSQL_PASSWORD="onlyoffice_pass"),
                  args=["--sql_mode=", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_general_ci"],
                  port_name="db"),
        container("elasticsearch", "oo-bundle-es:latest", 9200,
                  env_vars=env(**{"discovery.type": "single-node", "bootstrap.memory_lock": "true",
                                  "ES_JAVA_OPTS": "-Xms512m -Xmx512m", "xpack.security.enabled": "false"}),
                  port_name="es"),
        container("documentserver", "oo-bundle-ds:latest", 8000,
                  env_vars=env(JWT_ENABLED="false", ALLOW_PRIVATE_IP_ADDRESS="true"),
                  port_name="ds"),
        container("community", "oo-bundle-community2:latest", 80,
                  env_vars=env(MYSQL_SERVER_HOST="127.0.0.1", MYSQL_SERVER_PORT="3306",
                               MYSQL_SERVER_DB_NAME="onlyoffice", MYSQL_SERVER_USER="onlyoffice_user",
                               MYSQL_SERVER_PASS="onlyoffice_pass", ELASTICSEARCH_SERVER_HOST="127.0.0.1",
                               ELASTICSEARCH_SERVER_HTTPPORT="9200", DOCUMENT_SERVER_ENABLED="true",
                               DOCUMENT_SERVER_HOST="127.0.0.1", DOCUMENT_SERVER_PROTOCOL="http",
                               DOCUMENT_SERVER_API_URL="/ds-vpath", DOCUMENT_SERVER_JWT_ENABLED="false"),
                  privileged=True, port_name="main"),
    ], 80, "/", target_port_name="main"))

    add(app_manifest("pretix", 7, [
        container("db", "pgvector/pgvector:pg18-trixie", 5432,
                  env_vars=env(POSTGRES_DB="pretix", POSTGRES_USER="pretix",
                               POSTGRES_PASSWORD="pretix_pass", POSTGRES_HOST_AUTH_METHOD="md5"),
                  port_name="db"),
        container("redis", "redis:6", 6379, port_name="redis"),
        container("pretix", "mw-pretix:latest", 80,
                  env_vars=env(TZ="Asia/Shanghai", PRETIX_URL="http://s0-pretix.saasbench.localhost:30090"),
                  port_name="main"),
    ], 80, "/control/login/", target_port_name="main"))
    apps["pretix"]["documents"][1]["spec"]["template"]["spec"]["containers"][2]["command"] = [
        "sh",
        "-c",
        "until python -c \"import socket; socket.create_connection(('127.0.0.1', 5432), timeout=1).close()\" 2>/dev/null; do sleep 2; done; exec /entrypoint-mw.sh all",
    ]

    return apps


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    apps = build_apps()
    index = {}
    for app, spec in sorted(apps.items()):
        path = OUT_DIR / f"{app}.yaml"
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump_all(spec["documents"], f, sort_keys=False)
        index[app] = {
            "namespace": spec["namespace"],
            "nodePort": spec["nodePort"],
            "healthPath": spec["healthPath"],
            "manifest": str(path.relative_to(ROOT)).replace("\\", "/"),
        }
    (OUT_DIR / "index.json").write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")

    chart_dir = ROOT / "deploy" / "helm" / "saas-bench-apps"
    files_dir = chart_dir / "files"
    templates_dir = chart_dir / "templates"
    files_dir.mkdir(parents=True, exist_ok=True)
    templates_dir.mkdir(parents=True, exist_ok=True)

    with (files_dir / "apps.yaml").open("w", encoding="utf-8") as f:
        for app, spec in sorted(apps.items()):
            yaml.safe_dump_all(spec["documents"], f, sort_keys=False)

    hosts = ["127.0.0.1 " + " ".join(f"{app}.saasbench.localhost" for app in sorted(apps))]
    (files_dir / "hosts.txt").write_text("\n".join(hosts) + "\n", encoding="utf-8")

    print(json.dumps(index, indent=2))


if __name__ == "__main__":
    main()
