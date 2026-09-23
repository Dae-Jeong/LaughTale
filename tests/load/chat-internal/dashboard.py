"""실측 상태 파일만 제공하는 loopback 전용 읽기 전용 대시보드입니다."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def resource(path: str, run_dir: Path) -> tuple[Path, str] | None:
    if path in ("/", "/dashboard.html"):
        return Path(__file__).with_name("dashboard.html"), "text/html; charset=utf-8"
    if path == "/live/status.json":
        target = run_dir / "live" / "status.json"
        if target.is_symlink() or target.parent.is_symlink():
            return None
        return target, "application/json"
    if path == "/infra.json":
        target = run_dir / "infra.json"
        return None if target.is_symlink() else (target, "application/json")
    if path in ("/resources.jsonl", "/verification.json"):
        target = run_dir / (
            "resources.jsonl" if path == "/resources.jsonl" else "verified.json"
        )
        content_type = (
            "application/x-ndjson" if path == "/resources.jsonl" else "application/json"
        )
        return None if target.is_symlink() else (target, content_type)
    return None


def public_body(path: str, raw: bytes) -> bytes:
    if path != "/verification.json":
        return raw
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TypeError("INVALID_VERIFICATION")
    return json.dumps(
        {
            key: value[key]
            for key in ("status", "counts", "issues", "scope")
            if key in value
        }
    ).encode()


def handler(run_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") != "127.0.0.1:18091":
                self.send_error(403)
                return
            selected = resource(self.path, run_dir)
            if selected is None:
                self.send_error(404)
                return
            target, content_type = selected
            try:
                read_limit = (
                    16 * 1024 * 1024 if self.path == "/verification.json" else 262144
                )
                with target.open("rb") as stream:
                    body = stream.read(read_limit + 1)
                if len(body) > read_limit:
                    raise ValueError("BODY_LIMIT")
                body = public_body(self.path, body)
                if len(body) > 262144:
                    raise ValueError("PUBLIC_BODY_LIMIT")
            except (OSError, ValueError, TypeError):
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_error(405)

        def log_message(self, format, *args):
            pass

    return Handler


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, choices=[18091], default=18091)
    args = parser.parse_args()
    print("Read-only dashboard: http://127.0.0.1:18091", flush=True)
    with ThreadingHTTPServer(
        ("127.0.0.1", args.port), handler(args.run_dir.resolve())
    ) as server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
