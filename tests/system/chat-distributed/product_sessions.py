"""Bounded synthetic revoke/expiry checks; requires approved existing lab forwards."""

import asyncio
import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime

import httpx2
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

API = "http://127.0.0.1:18082"
WS = "ws://127.0.0.1:18085/v1/ws"
ORIGIN = "http://127.0.0.1:18083"
ROOM = "00000000-0000-4000-8000-000000000010"


def sql(query):
    return subprocess.check_output(
        [
            "docker",
            "exec",
            "laughtale-postgres-lab-primary-1",
            "psql",
            "-U",
            "postgres",
            "-d",
            "laughtale_chat",
            "-XAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            query,
        ],
        text=True,
        timeout=5,
    ).strip()


async def denied(client, token):
    response = await client.get(
        API + "/v1/session", headers={"Cookie": "chat_session=" + token}
    )
    ws_status = None
    try:
        async with connect(
            WS, origin=ORIGIN, additional_headers={"Cookie": "chat_session=" + token}
        ):
            raise AssertionError("expired/revoked WS admitted")
    except InvalidStatus as error:
        ws_status = error.response.status_code
    assert response.status_code == 401 and ws_status == 403
    return {"old_cookie_http": response.status_code, "old_cookie_ws": ws_status}


async def scenario(natural):
    async with httpx2.AsyncClient(
        timeout=3, trust_env=False, headers={"Origin": ORIGIN}
    ) as client:
        issued = await client.post(API + "/v1/dev/session", json={"user": "user_a"})
        assert issued.status_code == 200
        token = client.cookies.get("chat_session")
        assert token
        digest = hashlib.sha256(token.encode()).hexdigest()
        expiry = None
        if natural:
            # Own freshly-issued synthetic session only; no deletion or global TTL change.
            expiry = sql(
                "UPDATE chat.shared_sessions SET expires_at=clock_timestamp()+interval '8 seconds' WHERE token_hash='"
                + digest
                + "' RETURNING expires_at;"
            ).splitlines()[0]
        async with connect(
            WS, origin=ORIGIN, additional_headers={"Cookie": "chat_session=" + token}
        ) as socket:
            await socket.send(
                json.dumps({"type": "subscribe", "conversation_id": ROOM})
            )
            async with asyncio.timeout(3):
                while json.loads(await socket.recv())["type"] != "subscribed":
                    pass
            if natural:
                before = time.monotonic()
                remaining = float(
                    sql(
                        "SELECT extract(epoch FROM expires_at-clock_timestamp()) FROM chat.shared_sessions WHERE token_hash='"
                        + digest
                        + "';"
                    )
                )
                after = time.monotonic()
                start_lower, start_upper = before + remaining, after + remaining
            else:
                start_lower = start_upper = time.monotonic()
                response = await client.post(
                    API + "/v1/dev/session", json={"user": "user_a"}
                )
                assert response.status_code == 200
            async with asyncio.timeout(16 if natural else 7):
                try:
                    while True:
                        await socket.recv()
                except ConnectionClosed as error:
                    end = time.monotonic()
                    code = error.rcvd.code if error.rcvd else None
            lower_ms, upper_ms = (end - start_upper) * 1000, (end - start_lower) * 1000
            assert code == 1008
        rejection = await denied(client, token)
        return {
            "scenario": "natural-expiry" if natural else "relogin-revocation",
            "start_semantics": "DB expires_at (local monotonic interval from SQL round trip)"
            if natural
            else "relogin_request_start",
            "end_semantics": "client observed WebSocket 1008",
            "expires_at": expiry,
            "elapsed_lower_ms": lower_ms,
            "elapsed_upper_ms": upper_ms,
            "close_code": code,
            "strict_5s": upper_ms <= 5000,
            "observation_7s": upper_ms <= 7000,
            **rejection,
        }


async def main():
    assert sql("SELECT current_database();") == "laughtale_chat"
    results = []
    for natural in (False, True):
        results.append(await scenario(natural))
    print(
        json.dumps({"at": datetime.now(UTC).isoformat(), "results": results}, indent=2)
    )


if __name__ == "__main__":
    asyncio.run(main())
