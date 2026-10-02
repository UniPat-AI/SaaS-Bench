"""Loopback port map that makes stock ``{SERVER_HOSTNAME}:{<APP>_PORT}`` URLs correct on Kubernetes.

Stock verifiers tell a task's apps apart by PORT on a shared host, because the local runner
publishes every app on ``localhost`` at ``BASE_PORT + slot*40 + app_index``. Kubernetes has the
opposite shape: one port (80) and a per-app hostname. So a verifier building
``f"http://{HOST}:{ONLYOFFICE_PORT}"`` resolved to the FIRST app's host on port 80 — the wrong
app, silently, in 79 of the 106 tasks (104 are multi-app; only the 2 that happen to reference
their own first app stayed correct). Single-app tasks were never affected: their one app IS the
first host.

Rather than rewrite 106 verifiers — which would make every one of them depend on two different env
contracts, one per backend, with any omission failing silently — this restores the shape they were
written against. For the duration of one grade each app gets its own loopback port inside the
platform pod, forwarded to that app's in-cluster Service; ``SERVER_HOSTNAME`` becomes 127.0.0.1 and
``<APP>_PORT`` becomes distinct again. Both backends then hand the verifier the same contract.

The forwarder is HTTP-aware rather than a plain TCP relay for one reason: each app is configured
with its public hostname (``{hostname}`` in apps.yaml) and several apps check it — Django's
ALLOWED_HOSTS (pretix) answers 400 on a mismatch, OpenProject and Baserow redirect to their
configured host. So ``Host`` is rewritten to the app's public hostname on the way out, and
``Location`` redirects back to that hostname are rewritten to the loopback address on the way in,
so a redirect-following verifier stays on the proxy instead of leaving the cluster.

No socket timeouts are set: the grade-level timeout and the verifier's own HTTP client govern how
long a request may take, and a second, shorter deadline here could only turn a slow app into a
wrong score.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

# Per RFC 7230 6.1 these are connection-scoped and must not be forwarded.
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})


class _Forwarder(BaseHTTPRequestHandler):
    """One app's forwarder. Subclassed per app so the target is a class attribute."""

    protocol_version = "HTTP/1.1"
    upstream_host = ""
    upstream_port = 80
    public_host = ""      # what the app thinks it is served as; sent as Host
    local_port = 0        # this forwarder's loopback port; used to rewrite Location back

    def log_message(self, fmt: str, *args: object) -> None:
        """Silence the default stderr access log — verify.py's stderr is the scored channel."""

    def _read_body(self) -> bytes | None:
        length = self.headers.get("Content-Length")
        if length:
            return self.rfile.read(int(length))
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks = []
            while True:
                size = int(self.rfile.readline().split(b";")[0] or b"0", 16)
                if size == 0:
                    self.rfile.readline()      # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return None

    def _rewrite_location(self, value: str) -> str:
        """Keep a redirect-following client on the proxy instead of the public gateway."""
        for scheme in ("https://", "http://"):
            if value.startswith(f"{scheme}{self.public_host}"):
                return "http://127.0.0.1:%d%s" % (
                    self.local_port, value[len(scheme) + len(self.public_host):],
                )
        return value

    def _forward(self) -> None:
        try:
            body = self._read_body()
        except (ValueError, OSError) as exc:
            self.send_error(400, explain=f"malformed request body: {exc}")
            return
        headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP_BY_HOP}
        headers["Host"] = self.public_host
        conn = HTTPConnection(self.upstream_host, self.upstream_port)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
            status, reason = resp.status, resp.reason
            out = [(k, v) for k, v in resp.getheaders()
                   if k.lower() not in _HOP_BY_HOP and k.lower() != "content-length"]
        except OSError as exc:
            # 502 rather than a dropped connection: the verifier reports an HTTP failure it can
            # print, instead of an opaque ConnectionError that reads like the app being down.
            self.send_error(502, explain=f"upstream {self.upstream_host}: {exc}")
            return
        finally:
            conn.close()
        self.send_response(status, reason)
        for key, value in out:
            self.send_header(key, self._rewrite_location(value) if key.lower() == "location"
                             else value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _forward


@contextmanager
def loopback_port_map(targets: dict[str, tuple[str, int, str]]) -> Iterator[dict[str, int]]:
    """Serve each app on its own loopback port for as long as the block runs.

    ``targets`` maps app key -> ``(upstream_host, upstream_port, public_host)``. Yields app key ->
    the loopback port it is reachable on. Ports are kernel-assigned (bind :0), so concurrent grades
    of tasks sharing an app never collide the way a fixed per-app port would.
    """
    servers: list[ThreadingHTTPServer] = []
    ports: dict[str, int] = {}
    try:
        for app, (upstream_host, upstream_port, public_host) in targets.items():
            handler = type(f"_Forwarder_{app}", (_Forwarder,), {
                "upstream_host": upstream_host,
                "upstream_port": int(upstream_port),
                "public_host": public_host,
            })
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            server.daemon_threads = True
            handler.local_port = ports[app] = server.server_address[1]
            servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        yield ports
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
