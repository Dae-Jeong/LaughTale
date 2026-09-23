import asyncio
from uuid import UUID

import httpx2
import pytest
from pydantic import ValidationError

from chat_service.bootstrap.app import create_app
from chat_service.core.settings import Settings
from chat_service.http.local_security import LocalChatSecurity, ingress_route

TOKEN = "synthetic-ingress-only-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ"
POD = UUID("00000000-0000-4000-8000-000000000099")
ROOM = "00000000-0000-4000-8000-000000000010"


def settings(**changes):
    values: dict = dict(
        network_profile="isolated-lab",
        app_environment="isolated-lab",
        server_host="0.0.0.0",
        dev_sessions_enabled=True,
        session_backend="postgres",
        db_primary_url="postgresql+asyncpg://synthetic:unused@127.0.0.1:5440/unused",
        lab_ingress_enabled=True,
        lab_ingress_token=TOKEN,
        lab_pod_uid=POD,
    )
    return Settings(_env_file=None, **(values | changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"lab_ingress_token": ""},
        {"lab_ingress_token": "a" * 64},
        {"lab_ingress_token": TOKEN + "\n"},
        {"lab_ingress_token": TOKEN + "한"},
        {"lab_ingress_token": "x" * 257},
        {"lab_pod_uid": None},
        {"session_backend": "local"},
        {"dev_sessions_enabled": False},
        {"external_control_token": TOKEN},
        {"mock_api_token": TOKEN},
        {"network_profile": "local"},
        {"app_environment": "production"},
    ],
)
def test_ingress_requires_explicit_isolated_shared_session_and_distinct_secret(changes):
    with pytest.raises(ValidationError):
        settings(**changes)
    assert not Settings(_env_file=None).lab_ingress_enabled


def scope():
    return {
        "type": "http",
        "path": f"/v1/internal-conversations/{ROOM}/messages",
        "method": "POST",
        "client": ("10.42.0.10", 1234),
        "headers": [
            (b"host", b"chat:18082"),
            (b"origin", b"http://127.0.0.1:18083"),
            (b"x-lab-ingress-token", TOKEN.encode()),
            (b"cookie", b"chat_session=synthetic-cookie"),
        ],
        "state": {},
    }


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("none", 200),
        ("no_token", 403),
        ("wrong_token", 403),
        ("duplicate_token", 403),
        ("duplicate_host", 403),
        ("duplicate_origin", 403),
        ("no_origin", 403),
        ("wrong_origin", 403),
        ("wrong_host", 403),
        ("outside_pod", 403),
        ("forwarded", 403),
        ("x_forwarded", 403),
        ("issue", 403),
        ("external", 403),
        ("disabled", 403),
        ("unicode_token", 403),
    ],
)
def test_guard_exact_headers_scope_and_no_credential_forwarding(mutation, expected):
    async def scenario():
        incoming = scope()
        headers = incoming["headers"]
        pairs = {"no_token": b"x-lab-ingress-token", "no_origin": b"origin"}
        if mutation in pairs:
            headers[:] = [
                (key, value) for key, value in headers if key != pairs[mutation]
            ]
        replacements = {
            "wrong_token": (b"x-lab-ingress-token", b"wrong"),
            "unicode_token": (b"x-lab-ingress-token", b"\xff"),
            "wrong_origin": (b"origin", b"http://evil.example"),
            "wrong_host": (b"host", b"chat.evil:18082"),
        }
        if mutation in replacements:
            key, value = replacements[mutation]
            headers[:] = [
                (name, value if name == key else content) for name, content in headers
            ]
        duplicates = {"duplicate_token": 2, "duplicate_host": 0, "duplicate_origin": 1}
        if mutation in duplicates:
            headers.append(headers[duplicates[mutation]])
        if mutation == "outside_pod":
            incoming["client"] = ("10.42.1.10", 1234)
        if mutation == "forwarded":
            headers.append((b"forwarded", b"for=127.0.0.1"))
        if mutation == "x_forwarded":
            headers.append((b"x-forwarded-for", b"127.0.0.1"))
        if mutation == "issue":
            incoming["path"] = "/v1/dev/session"
        if mutation == "external":
            incoming["path"] = "/v1/external-events"
        output = []

        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        async def send(message):
            output.append(message)

        async def app(received, receive, send):
            assert not any(
                key == b"x-lab-ingress-token" for key, _ in received["headers"]
            )
            assert (b"cookie", b"chat_session=synthetic-cookie") in received["headers"]
            assert (await receive())["body"] == b"{}"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"x-lab-pod-uid", b"untrusted")],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})

        await LocalChatSecurity(
            app, settings(lab_ingress_enabled=mutation != "disabled")
        )(incoming, receive, send)
        assert output[0]["status"] == expected
        identities = [
            value for key, value in output[0]["headers"] if key == b"x-lab-pod-uid"
        ]
        assert identities == ([str(POD).encode()] if expected == 200 else [])
        assert TOKEN.encode() not in repr(output).encode()

    asyncio.run(scenario())


def test_ingress_token_does_not_replace_cookie_and_error_identifies_api():
    async def scenario():
        app = create_app(settings())
        # lifespan/DB 없이 실제 session 라우트의 인증 실패만 검사합니다.
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app, client=("10.42.0.10", 1234)),
            base_url="http://chat:18082",
        ) as client:
            result = await client.get(
                "/v1/session",
                headers={
                    "Origin": "http://127.0.0.1:18083",
                    "X-Lab-Ingress-Token": TOKEN,
                },
            )
            assert result.status_code == 401
            assert result.headers["x-lab-pod-uid"] == str(POD)

    asyncio.run(scenario())


def test_allowlist_is_http_only_and_does_not_widen_routes():
    assert ingress_route("/v1/session", "GET")
    assert ingress_route(f"/v1/internal-conversations/{ROOM}/messages", "POST")
    for path in (
        "/v1/ws",
        "/v1/dev/session",
        "/v1/external-conversations",
        f"/v1/internal-conversations/{ROOM}/messages/",
        "/v1/internal-conversations/not-a-uuid/messages",
    ):
        assert not ingress_route(path, "POST")
    assert not ingress_route("/v1/session", "DELETE")


def test_valid_ingress_token_does_not_enable_websocket():
    async def scenario():
        incoming = scope()
        incoming.update(type="websocket", path="/v1/ws")
        output = []

        async def receive():
            return {"type": "websocket.connect"}

        async def send(message):
            output.append(message)

        async def app(scope, receive, send):
            pytest.fail("HTTP ingress credential must not authorize WS")

        await LocalChatSecurity(app, settings())(incoming, receive, send)
        assert output == [{"type": "websocket.close", "code": 1008}]

    asyncio.run(scenario())


def test_valid_ingress_preserves_body_cap_and_pod_identity_on_rejection():
    async def scenario():
        output = []

        async def receive():
            return {"type": "http.request", "body": b"x" * 16385, "more_body": False}

        async def send(message):
            output.append(message)

        async def app(scope, receive, send):
            pytest.fail("Oversize body must not enter business code")

        await LocalChatSecurity(app, settings())(scope(), receive, send)
        assert output[0]["status"] == 413
        assert (b"x-lab-pod-uid", str(POD).encode()) in output[0]["headers"]

    asyncio.run(scenario())
