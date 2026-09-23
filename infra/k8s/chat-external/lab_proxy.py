"""Loopback-only experiment entry; fixed Kubernetes Service upstream, no retries."""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

ORIGIN = "http://127.0.0.1:18083"
HOST = "127.0.0.1:18096"
PATH = re.compile(
    r"/v1/(session|internal-conversations(?:/[0-9a-f-]{36}/messages)?)(?:\?[^#\s]*)?"
)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 8

    def __init__(self, token):
        self.token = token
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(("0.0.0.0", 18096), Handler)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # Never print credential-bearing request exceptions.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(4)

    def reply(self, status, body, uid=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if uid:
            self.send_header("X-Lab-Pod-UID", uid)
        self.end_headers()
        self.wfile.write(body)

    def reject(self, status=403):
        self.reply(status, json.dumps({"code": "LAB_PROXY_REJECTED"}).encode())

    def do_GET(self):
        self.forward()

    def do_POST(self):
        self.forward()

    def forward(self):
        if self.client_address[0] != "127.0.0.1":
            self.reject()
            return
        if self.headers.get_all("Host") != [HOST]:
            self.reject()
            return
        if self.path == "/health/ready" and self.command == "GET":
            self.reply(200, b'{"status":"alive"}')
            return  # Proxy process health, not upstream readiness.
        if (
            self.headers.get_all("Origin") != [ORIGIN]
            or len(self.path) > 2048
            or PATH.fullmatch(self.path) is None
            or self.headers.get_all("Transfer-Encoding")
            or len(self.headers.get_all("Cookie", [])) != 1
            or any(
                key.lower() in {"forwarded", "authorization"}
                or key.lower().startswith(("x-forwarded-", "x-lab-"))
                for key in self.headers
            )
            or (
                self.command == "POST"
                and not self.path.split("?", 1)[0].endswith("/messages")
            )
        ):
            self.reject()
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not lengths[0].isascii()) or (
            lengths and not lengths[0].isdigit()
        ):
            self.reject(400)
            return
        if lengths and len(lengths[0]) > 5:
            self.reject(413)
            return
        size = int(lengths[0]) if lengths else 0
        if size > 16384 or (self.command == "GET" and size):
            self.reject(413)
            return
        body = self.rfile.read(size)
        if len(body) != size:
            self.reject(400)
            return
        request = Request(
            "http://chat:18082" + self.path,
            data=body if self.command == "POST" else None,
            method=self.command,
            headers={
                "Host": "chat:18082",
                "Origin": ORIGIN,
                "Content-Type": "application/json",
                "Cookie": self.headers["Cookie"],
                "X-Lab-Ingress-Token": self.server.token,
                "Connection": "close",
            },
        )
        try:
            try:
                response = build_opener(ProxyHandler({}), NoRedirect()).open(
                    request, timeout=3
                )
            except HTTPError as error:
                response = error
            with response:
                payload = response.read(2_000_001)
                uid = response.headers.get_all("X-Lab-Pod-UID", [])
                if len(payload) > 2_000_000 or len(uid) != 1 or str(UUID(uid[0])) != uid[0]:
                    self.reject(502)
                    return
                self.reply(response.status, payload, uid[0])
        except (OSError, ValueError, URLError):
            self.reject(502)


if __name__ == "__main__":
    secret = os.environ.get("LAB_INGRESS_TOKEN", "")
    if not 32 <= len(secret) <= 256 or len(set(secret)) < 8 or not all(
        33 <= ord(char) <= 126 for char in secret
    ):
        raise SystemExit("Strong dedicated ingress credential required")
    Server(secret).serve_forever()
