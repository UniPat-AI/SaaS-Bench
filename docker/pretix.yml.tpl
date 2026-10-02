version: "3.8"

networks:
  $prefix-net:
    driver: bridge

volumes:
  $prefix-data:
    name: $prefix-data
  $prefix-db-data:
    name: $prefix-db-data
  $prefix-redis-data:
    name: $prefix-redis-data

services:
  $prefix-db:
    image: postgres:13-alpine
    container_name: $prefix-db
    networks:
      - $prefix-net
    restart: unless-stopped
    environment:
      - POSTGRES_DB=pretix
      - POSTGRES_USER=pretix
      - POSTGRES_PASSWORD=pretix_pass
    volumes:
      - $prefix-db-data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U pretix -d pretix"]
      interval: 10s
      retries: 10
      start_period: 15s
      timeout: 5s

  $prefix-redis:
    image: redis:7-alpine
    container_name: $prefix-redis
    networks:
      - $prefix-net
    restart: unless-stopped
    volumes:
      - $prefix-redis-data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      retries: 5
      timeout: 5s

  $prefix:
    image: mw-pretix:latest
    container_name: $prefix
    networks:
      - $prefix-net
    restart: unless-stopped
    environment:
      - TZ=Asia/Shanghai
      - PRETIX_URL=http://$hostname:$port
      # The upstream default is 2*nproc, which sees the Kubernetes node's CPUs instead of this
      # container's CPU limit and can spawn dozens of gunicorn workers. Keep the web tier bounded.
      - NUM_WORKERS=4
    # Patch the baked /etc/pretix/pretix.cfg (which uses "localhost") to point
    # to the networked postgres / redis services before invoking the upstream
    # entrypoint. Kubernetes emptyDir mounts start as root:root (unlike Docker's
    # named-volume initialization). Pre-create all paths opened by Django during the root migration,
    # otherwise the unprivileged web/task workers cannot reuse its secret or append to its logs.
    # The image's "all" supervisor mode also starts embedded Postgres/Redis, which collide with the
    # declared compose sidecars because Kubernetes pod containers share one network namespace.
    entrypoint:
      - /bin/bash
      - -c
      - |
        mkdir -p /data/logs /data/media /data/cache
        touch /data/logs/pretix.log /data/logs/csp.log
        if [ ! -s /data/.secret ]; then
          (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(50))' > /data/.secret)
        fi
        chown -R pretixuser:pretixuser /data
        chmod 0600 /data/.secret
        chmod 0644 /data/logs/pretix.log /data/logs/csp.log
        sed -i 's/^autostart=true/autostart=false/' /etc/supervisord/postgresql.conf /etc/supervisord/redis.conf
        sed -i 's/^host=localhost/host=$prefix-db/' /etc/pretix/pretix.cfg
        sed -i 's|^location=redis://localhost:6379|location=redis://$prefix-redis:6379|' /etc/pretix/pretix.cfg
        sed -i 's|^backend=redis://localhost:6379|backend=redis://$prefix-redis:6379|' /etc/pretix/pretix.cfg
        sed -i 's|^broker=redis://localhost:6379|broker=redis://$prefix-redis:6379|' /etc/pretix/pretix.cfg
        exec /entrypoint-mw.sh all
    volumes:
      - $prefix-data:/data
    ports:
      - "$port:80"
    depends_on:
      $prefix-db:
        condition: service_healthy
      $prefix-redis:
        condition: service_healthy
