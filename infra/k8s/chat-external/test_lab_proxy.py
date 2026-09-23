"""Fixed-upstream proxy boundary tests; no sockets, credentials or cluster access."""

import importlib.util
import io
import threading
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location(
    "lab_proxy", Path(__file__).with_name("lab_proxy.py")
)
assert SPEC and SPEC.loader
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)

ROOM = "00000000-0000-4000-8000-000000000010"
TOKEN = "synthetic-ingress-token-for-unit-test-only"


def handler(
    path="/v1/session", method="GET", body=b"", headers=None, client="127.0.0.1"
):
    subject = object.__new__(proxy.Handler)
    subject.path = path
    subject.command = method
    subject.client_address = (client, 12345)
    subject.server = SimpleNamespace(token=TOKEN)
    subject.headers = Message()
    pairs = (
        headers
        if headers is not None
        else [
            ("Host", proxy.HOST),
            ("Origin", proxy.ORIGIN),
            ("Cookie", "chat_session=synthetic"),
        ]
    )
    for name, value in pairs:
        subject.headers[name] = value
    subject.rfile = io.BytesIO(body)
    subject.reply = Mock()
    return subject


class Response:
    def __init__(self, body=b'{"data":{}}', uid=None, status=200):
        self.status = status
        self.body = body
        self.headers = Message()
        if uid is not None:
            self.headers["X-Lab-Pod-UID"] = uid

    def read(self, limit):
        return self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class ProxyTests(unittest.TestCase):
    def denied(self, subject, status=403):
        with patch.object(proxy, "build_opener") as opener:
            subject.forward()
            opener.assert_not_called()
        self.assertEqual(subject.reply.call_args.args[0], status)

    def test_exact_origin_host_cookie_and_untrusted_network(self):
        base = [
            ("Host", proxy.HOST),
            ("Origin", proxy.ORIGIN),
            ("Cookie", "chat_session=synthetic"),
        ]
        for index, replacement in [
            (0, "chat:18082"),
            (1, "http://evil.example"),
            (1, "null"),
        ]:
            with self.subTest(index=index, value=replacement):
                values = list(base)
                values[index] = (values[index][0], replacement)
                self.denied(handler(headers=values))
        for name, value in base:
            with self.subTest(duplicate=name):
                self.denied(handler(headers=base + [(name, value)]))
        self.denied(handler(headers=base[:-1]))
        self.denied(handler(client="10.42.0.10"))
        self.denied(handler(client="::1"))

    def test_caller_cannot_supply_service_credentials_or_forwarding_headers(self):
        for name in (
            "Authorization",
            "Forwarded",
            "X-Forwarded-For",
            "X-Forwarded-Host",
            "X-Lab-Ingress-Token",
            "x-lab-pod-uid",
        ):
            with self.subTest(header=name):
                subject = handler()
                subject.headers[name] = "forged"
                self.denied(subject)

    def test_paths_methods_and_session_issuance_are_not_forwarded(self):
        for path, method in [
            ("/v1/dev/session", "POST"),
            ("/v1/session", "POST"),
            ("/v1/internal-conversations", "POST"),
            ("http://evil.example/v1/session", "GET"),
            ("//evil.example/v1/session", "GET"),
            ("/v1/../dev/session", "POST"),
            ("/v1/external-events", "POST"),
            ("/v1/session#fragment", "GET"),
        ]:
            with self.subTest(path=path, method=method):
                self.denied(handler(path, method))

    def test_body_limits_and_ambiguous_framing(self):
        for values, body, status in [
            ([("Content-Length", "1"), ("Content-Length", "1")], b"x", 400),
            ([("Content-Length", "-1")], b"", 400),
            ([("Content-Length", "１２")], b"", 400),
            ([("Content-Length", "16385")], b"", 413),
            ([("Content-Length", "9" * 4301)], b"", 413),
            ([("Content-Length", "3")], b"x", 400),
            ([("Transfer-Encoding", "chunked")], b"", 403),
        ]:
            with self.subTest(values=values):
                subject = handler(
                    f"/v1/internal-conversations/{ROOM}/messages", "POST", body
                )
                for name, value in values:
                    subject.headers[name] = value
                self.denied(subject, status)
        subject = handler(body=b"x")
        subject.headers["Content-Length"] = "1"
        self.denied(subject, 413)

    def test_fixed_upstream_and_one_post_attempt_preserve_body_and_uid(self):
        body = b'{"text":"synthetic","client_message_id":"synthetic"}'
        path = f"/v1/internal-conversations/{ROOM}/messages"
        subject = handler(path, "POST", body)
        subject.headers["Content-Length"] = str(len(body))
        uid = str(uuid4())
        opener = Mock()
        opener.open.return_value = Response(b'{"data":{"stored":true}}', uid, 201)
        with patch.object(proxy, "build_opener", return_value=opener) as build:
            subject.forward()
        build.assert_called_once()
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsInstance(build.call_args.args[1], proxy.NoRedirect)
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://chat:18082" + path)
        self.assertEqual(request.data, body)
        self.assertEqual(request.get_method(), "POST")
        sent = {name.lower(): value for name, value in request.header_items()}
        self.assertEqual(sent["x-lab-ingress-token"], TOKEN)
        self.assertEqual(sent["cookie"], "chat_session=synthetic")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 3)
        subject.reply.assert_called_once_with(201, b'{"data":{"stored":true}}', uid)

    def test_post_transport_failure_is_not_retried(self):
        subject = handler(f"/v1/internal-conversations/{ROOM}/messages", "POST")
        opener = Mock()
        opener.open.side_effect = URLError("synthetic failure")
        with patch.object(proxy, "build_opener", return_value=opener):
            subject.forward()
        opener.open.assert_called_once()
        self.assertEqual(subject.reply.call_args.args[0], 502)

    def test_error_status_is_preserved_only_with_valid_upstream_uid(self):
        uid = str(uuid4())
        headers = Message()
        headers["X-Lab-Pod-UID"] = uid
        error = HTTPError(
            "http://chat:18082/v1/session",
            401,
            "Unauthorized",
            headers,
            io.BytesIO(b'{"code":"UNAUTHENTICATED"}'),
        )
        opener = Mock()
        opener.open.side_effect = error
        subject = handler()
        with patch.object(proxy, "build_opener", return_value=opener):
            subject.forward()
        subject.reply.assert_called_once_with(401, b'{"code":"UNAUTHENTICATED"}', uid)

    def test_rejects_missing_duplicate_noncanonical_uid_and_oversized_response(self):
        uid = str(uuid4())
        duplicate = Response(uid=uid)
        duplicate.headers["X-Lab-Pod-UID"] = uid
        for response in (
            Response(),
            Response(uid="not-a-uuid"),
            Response(uid=uid.upper()),
            duplicate,
            Response(b"x" * 2_000_001, uid),
        ):
            subject = handler()
            opener = Mock()
            opener.open.return_value = response
            with patch.object(proxy, "build_opener", return_value=opener):
                subject.forward()
            self.assertEqual(subject.reply.call_args.args[0], 502)

    def test_redirects_are_never_followed(self):
        self.assertIsNone(
            proxy.NoRedirect().redirect_request(
                None, None, 302, "redirect", Message(), "http://evil.example"
            )
        )

    def test_health_is_local_process_only_and_never_forwards(self):
        subject = handler("/health/ready", headers=[("Host", proxy.HOST)])
        with patch.object(proxy, "build_opener") as opener:
            subject.forward()
        opener.assert_not_called()
        subject.reply.assert_called_once_with(200, b'{"status":"alive"}')
        self.denied(handler("/health/ready", client="10.42.0.10"))

    def test_connection_slots_are_bounded_and_released_on_failure(self):
        server = object.__new__(proxy.Server)
        server.slots = threading.BoundedSemaphore(8)
        for _ in range(8):
            self.assertTrue(server.slots.acquire(blocking=False))
        connection = Mock()
        server.process_request(connection, ("127.0.0.1", 1234))
        connection.close.assert_called_once()
        for _ in range(8):
            server.slots.release()
        with (
            patch.object(
                proxy.ThreadingHTTPServer,
                "process_request",
                side_effect=RuntimeError("synthetic"),
            ),
            self.assertRaises(RuntimeError),
        ):
            server.process_request(Mock(), ("127.0.0.1", 1234))
        for _ in range(8):
            self.assertTrue(server.slots.acquire(blocking=False))
        self.assertFalse(server.slots.acquire(blocking=False))


if __name__ == "__main__":
    unittest.main()
