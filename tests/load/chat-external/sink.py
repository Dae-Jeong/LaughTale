"""Loopback-only, credential-free acknowledgement sink for generator calibration."""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    slots = threading.BoundedSemaphore(8)

    def log_message(self, format, *args):
        pass

    def respond(self, status: int, value: dict) -> None:
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def allowed(self) -> bool:
        return (
            self.client_address[0] == "127.0.0.1"
            and self.headers.get_all("Host") == [f"127.0.0.1:{self.server.server_port}"]
            and not any(
                name.lower() in {"authorization", "cookie", "origin", "forwarded"}
                or name.lower().startswith("x-forwarded-")
                for name in self.headers
            )
        )

    def do_GET(self) -> None:
        if not self.allowed():
            self.respond(403, {"code": "FORBIDDEN"})
        elif self.path == "/health/ready":
            self.respond(200, {"status": "ready"})
        else:
            self.respond(404, {"code": "NOT_FOUND"})

    def do_POST(self) -> None:
        self.connection.settimeout(5)
        if not self.allowed() or self.path != "/sink":
            self.respond(403, {"code": "FORBIDDEN"})
            return
        if (
            self.headers.get("Transfer-Encoding")
            or self.headers.get("Content-Length", "0") != "0"
        ):
            self.respond(413, {"code": "EMPTY_BODY_ONLY"})
            return
        if not self.slots.acquire(blocking=False):
            self.respond(429, {"code": "SINK_CONCURRENCY_LIMIT"})
            return
        try:
            self.respond(200, {"data": {"result": "acknowledged", "http_status": 201}})
        finally:
            self.slots.release()


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18089)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("Use an unprivileged local port")
    with ThreadingHTTPServer(("127.0.0.1", args.port), Handler) as server:
        server.daemon_threads = True
        server.serve_forever(poll_interval=0.1)


if __name__ == "__main__":
    main()
