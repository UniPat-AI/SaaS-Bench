version: "3.8"

networks:
  $prefix-net:
    driver: bridge

services:
  $prefix-mysql:
    image: oo-bundle-mysql:latest
    container_name: $prefix-mysql
    networks:
      - $prefix-net
    restart: always
    environment:
      MYSQL_ROOT_PASSWORD: my-secret-pw
      MYSQL_DATABASE: onlyoffice
      MYSQL_USER: onlyoffice_user
      MYSQL_PASSWORD: onlyoffice_pass
    volumes:
      - ${prefix}_mysql_data:/var/lib/mysql
    command:
      - --sql_mode=
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_general_ci
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost", "-uroot", "-pmy-secret-pw"]
      interval: 10s
      retries: 15
      start_period: 60s
      timeout: 5s

  $prefix-elasticsearch:
    image: oo-bundle-es:latest
    container_name: $prefix-elasticsearch
    networks:
      - $prefix-net
    restart: always
    environment:
      - discovery.type=single-node
      # memlock/mlockall needs an unbounded memlock ulimit, which k8s/Autopilot won't grant
      # (compose `ulimits` aren't translated either) -> ES aborts at boot. Disable it; ES just
      # may page, which is fine for an ephemeral per-run env.
      - bootstrap.memory_lock=false
      - "ES_JAVA_OPTS=-Xms512m -Xmx512m"
      - xpack.security.enabled=false
    ulimits:
      memlock:
        soft: -1
        hard: -1
    volumes:
      - ${prefix}_es_data:/usr/share/elasticsearch/data
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:9200/_cluster/health | grep -qE '\"status\":\"(green|yellow)\"'"]
      interval: 15s
      retries: 10
      start_period: 60s
      timeout: 10s

  $prefix-documentserver:
    image: oo-bundle-ds:latest
    container_name: $prefix-documentserver
    networks:
      - $prefix-net
    restart: always
    environment:
      - JWT_ENABLED=false
      - ALLOW_PRIVATE_IP_ADDRESS=true
    # Compose gives every service its own network namespace, but the hosted compose chart runs
    # them in one Kubernetes Pod. Community Server already owns :80 there, so move Document
    # Server's nginx proxy to a pod-unique port before its stock entrypoint renders the config.
    entrypoint:
      - /bin/bash
      - -c
      - |
        sed -i 's/0\.0\.0\.0:80/0.0.0.0:8081/g; s/\[::\]:80/[::]:8081/g' \
          /etc/onlyoffice/documentserver/nginx/ds.conf.tmpl \
          /etc/onlyoffice/documentserver/nginx/ds-ssl.conf.tmpl
        exec /app/ds/run-document-server.sh
    volumes:
      - ${prefix}_ds_data:/var/www/onlyoffice/Data
      - ${prefix}_ds_logs:/var/log/onlyoffice
      - ${prefix}_ds_cache:/var/lib/onlyoffice/documentserver/App_Data/cache/files
      - ${prefix}_ds_files:/var/www/onlyoffice/documentserver-example/public/files
      - ${prefix}_ds_fonts:/usr/share/fonts
    healthcheck:
      test: ["CMD-SHELL", "curl -sf http://localhost:8000/info/info.json"]
      interval: 30s
      retries: 5
      start_period: 60s
      timeout: 10s

  $prefix-community:
    # Rootless rebuild (image-build/onlyoffice-rootless/): runs the ~22 ONLYOFFICE services under
    # supervisord instead of systemd, so it needs neither `privileged` nor the /sys/fs/cgroup
    # hostPath mount and schedules on GKE Autopilot. See that dir's Dockerfile for the how/why.
    image: oo-bundle-community2:latest
    container_name: $prefix-community
    # Community Server builds browser-facing absolute URLs (DocEditor links, redirects) from the
    # machine hostname. Without this it uses the random container id (http://<id>:<port>/...),
    # which no browser can resolve — editor links and saves die. Must be the host the AGENT uses.
    hostname: $hostname
    networks:
      - $prefix-net
    restart: always
    volumes:
      - ${prefix}_community_data:/var/www/onlyoffice/Data
      - ${prefix}_community_logs:/var/log/onlyoffice
      - ${prefix}_community_letsencrypt:/etc/letsencrypt
    entrypoint:
      - /bin/bash
      - -c
      - |
        chown -R onlyoffice:onlyoffice /var/www/onlyoffice/Data /var/log/onlyoffice 2>/dev/null || true
        until curl -fsS http://$prefix-documentserver:8081/healthcheck >/dev/null; do sleep 2; done
        exec /app/run-community-server.sh
    environment:
      - MYSQL_SERVER_HOST=$prefix-mysql
      - MYSQL_SERVER_PORT=3306
      - MYSQL_SERVER_DB_NAME=onlyoffice
      - MYSQL_SERVER_USER=onlyoffice_user
      - MYSQL_SERVER_PASS=onlyoffice_pass
      - ELASTICSEARCH_SERVER_HOST=$prefix-elasticsearch
      - ELASTICSEARCH_SERVER_HTTPPORT=9200
      - DOCUMENT_SERVER_ENABLED=true
      - DOCUMENT_SERVER_HOST=$prefix-documentserver:8081
      - DOCUMENT_SERVER_PROTOCOL=http
      - DOCUMENT_SERVER_API_URL=/ds-vpath
      - DOCUMENT_SERVER_JWT_ENABLED=false
    ports:
      - "$port:80"
    depends_on:
      $prefix-mysql:
        condition: service_healthy
      $prefix-elasticsearch:
        condition: service_healthy
      $prefix-documentserver:
        condition: service_healthy

volumes:
  ${prefix}_mysql_data:
  ${prefix}_es_data:
  ${prefix}_community_data:
  ${prefix}_community_logs:
  ${prefix}_community_letsencrypt:
  ${prefix}_ds_data:
  ${prefix}_ds_logs:
  ${prefix}_ds_cache:
  ${prefix}_ds_files:
  ${prefix}_ds_fonts:
