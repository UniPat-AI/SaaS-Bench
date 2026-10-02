{{- define "pg.runRouterConfig" -}}
worker_processes 1;
pid /tmp/nginx.pid;
error_log /dev/stderr notice;

events {
    worker_connections 4096;
}

http {
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log /dev/stdout combined;
    server_tokens off;
    sendfile on;
    keepalive_timeout 65s;

    client_body_temp_path /tmp/nginx-client-body;
    proxy_temp_path /tmp/nginx-proxy;
    fastcgi_temp_path /tmp/nginx-fastcgi;
    uwsgi_temp_path /tmp/nginx-uwsgi;
    scgi_temp_path /tmp/nginx-scgi;

    # Resolve short-lived per-run ClusterIP Services at request time. A missing or released run
    # produces a normal proxy 502; the platform's public probe keeps that run not-ready.
    resolver {{ .Values.runRouter.dnsResolver }} valid={{ .Values.runRouter.dnsValid }} ipv6=off;
    resolver_timeout {{ .Values.runRouter.dnsTimeout }};

    map $http_upgrade $connection_upgrade {
        default upgrade;
        "" close;
    }

    # The router's upstream hop is always HTTP and GKE does not preserve a reliable original
    # X-Forwarded-Proto through every Cloudflare/Gateway path. Use the same explicit public-scheme
    # contract that the service uses to construct browser access URLs.
    map $host $public_scheme {
        default "{{ .Values.config.publicScheme }}";
    }

    # ONLYOFFICE's chunked-upload session endpoint still serializes its same-origin upload URL
    # with the internal http scheme, even when the public-origin override below is present. Make
    # that small JSON response eligible for a body rewrite by asking only ONLYOFFICE upstreams for
    # an uncompressed response. Other run applications retain the browser's Accept-Encoding.
    map $host $run_upstream_accept_encoding {
        default $http_accept_encoding;
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "";
    }

    # Use inert sentinels outside ONLYOFFICE so the response filter cannot alter another app's
    # response. On ONLYOFFICE hosts it upgrades only URLs for the current public origin; external
    # http links and internal service URLs are deliberately left alone.
    map $host $onlyoffice_internal_origin {
        default "__no_onlyoffice_internal_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "http://$host";
    }

    map $host $onlyoffice_public_origin {
        default "__no_onlyoffice_public_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "$public_scheme://$host";
    }

    # The upload-session response uses JSON's optional escaped-slash spelling (`http:\/\/`),
    # so its raw bytes do not contain the plain `http://` prefix above. Keep a second scoped pair
    # for that representation; the replacement preserves valid JSON escaping.
    map $host $onlyoffice_internal_json_origin {
        default "__no_onlyoffice_internal_json_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "http:\\/\\/$host";
    }

    map $host $onlyoffice_public_json_origin {
        default "__no_onlyoffice_public_json_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "$public_scheme:\\/\\/$host";
    }

    # Editor iframe query strings also carry the same origin in percent-encoded form.
    map $host $onlyoffice_internal_encoded_origin {
        default "__no_onlyoffice_internal_encoded_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "http%3A%2F%2F$host";
    }

    map $host $onlyoffice_public_encoded_origin {
        default "__no_onlyoffice_public_encoded_origin__";
        "~^[a-f0-9]{32}-onlyoffice\.{{ .Values.config.domain | replace "." "\\." }}$" "$public_scheme%3A%2F%2F$host";
    }

    # Production root run IDs are uuid4 hex. Multi-app releases append a DNS-safe site suffix;
    # cap that suffix so 59 characters plus `run-` stays inside Kubernetes' 63-character limit.
    # Prefixing with `run-` and rejecting every other host prevents access to arbitrary Services.
    map $host $run_upstream {
        default "";
        "~^(?<run_id>[a-f0-9]{32}(?:-[a-z0-9](?:[-a-z0-9]{0,24}[a-z0-9])?)?)\.{{ .Values.config.domain | replace "." "\\." }}$" run-$run_id.{{ .Release.Namespace }}.svc.cluster.local:80;
    }

    server {
        listen {{ .Values.runRouter.targetPort }} default_server;
        server_name _;

        location = /healthz {
            access_log off;
            add_header Content-Type text/plain;
            return 200 "ok\n";
        }

        location / {
            if ($run_upstream = "") { return 404; }

            proxy_pass http://$run_upstream;
            proxy_http_version 1.1;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Host $host;
            proxy_set_header X-Forwarded-Proto $public_scheme;
            # ONLYOFFICE Community Server builds its absolute browser URLs from its OWN listener
            # scheme (`set $X_REWRITER_URL $scheme://$http_host` in its bundled nginx), which is
            # plain http behind this gateway — so file viewUrl/webUrl and the editor's document.url
            # came back as http:// on an https page and the browser blocked them as mixed content.
            # X-Rewriter-Url is ONLYOFFICE's own proxy override and its shipped config already
            # honors it (`if ($http_x_rewriter_url != '')`). Apps that don't know the header ignore
            # it, and it states the same public origin as the X-Forwarded-* headers above.
            proxy_set_header X-Rewriter-Url $public_scheme://$host;
            proxy_set_header Accept-Encoding $run_upstream_accept_encoding;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection $connection_upgrade;

            # The upload-session API returns its handler URL in JSON. The public-origin header
            # fixes other ONLYOFFICE URLs but this endpoint ignores the header in the deployed
            # Community Server build, so upgrade the one same-origin prefix in the response.
            sub_filter_types application/json;
            sub_filter $onlyoffice_internal_origin $onlyoffice_public_origin;
            sub_filter $onlyoffice_internal_json_origin $onlyoffice_public_json_origin;
            sub_filter $onlyoffice_internal_encoded_origin $onlyoffice_public_encoded_origin;
            sub_filter_once off;

            proxy_connect_timeout {{ .Values.runRouter.connectTimeout }};
            proxy_read_timeout {{ .Values.runRouter.readTimeout }};
            proxy_send_timeout {{ .Values.runRouter.sendTimeout }};
            proxy_buffering off;
            proxy_request_buffering off;
            # Document Server version-pins editor assets with an absolute same-origin redirect.
            # Scope the Location-header upgrade with the same ONLYOFFICE-only sentinel maps.
            proxy_redirect $onlyoffice_internal_origin $onlyoffice_public_origin;
            client_max_body_size 0;
        }
    }
}
{{- end -}}
