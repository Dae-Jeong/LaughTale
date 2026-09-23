"""Bounded synthetic distributed chat check. Run only against the approved lab."""

import argparse
import asyncio
import importlib.util
import json
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from hashlib import sha256
from http.cookies import SimpleCookie
from pathlib import Path
from uuid import uuid4

from oracle import ACTOR, ROOM, EvidenceError, compare, normalize

HOST = "127.0.0.1:18082"
ORIGIN = "http://127.0.0.1:18083"
TARGETS = {"api_a": 18092, "api_b": 18093, "gateway_a": 18094, "gateway_b": 18095}
MESSAGE_PATH = f"/v1/internal-conversations/{ROOM}/messages"


class ResourceGuard:
    """Reuse the read-only sampler; never start or modify infrastructure."""

    def __init__(self, run_dir, sample):
        self.run_dir, self.sample = run_dir, sample
        self.baseline = None
        self.failed = False
        self.done = asyncio.Event()

    async def sample_once(self):
        try:
            value, identities = await asyncio.to_thread(self.sample, distributed=True)
            if self.baseline is None:
                self.baseline = identities
            elif self.baseline != identities:
                value.update(status="stop", reason="target_replaced_or_restarted")
        except (subprocess.SubprocessError, ValueError, KeyError, OSError):
            value = {
                "status": "incomplete",
                "reason": "required_resource_sample_failed",
            }
        append(self.run_dir / "resources.jsonl", value)
        if value["status"] != "ok":
            self.failed = True
            (self.run_dir / "STOP").touch(exist_ok=True)
        return not self.failed

    async def monitor(self):
        while not self.failed:
            try:
                await asyncio.wait_for(self.done.wait(), 5)
            except TimeoutError:
                pass
            # Completion also takes one final sample before returning.
            await self.sample_once()
            if self.done.is_set():
                break


def resource_sampler():
    path = Path(__file__).resolve().parents[2] / "load/chat-internal/resources.py"
    spec = importlib.util.spec_from_file_location("distributed_resource_sampler", path)
    if spec is None or spec.loader is None:
        raise EvidenceError("resource_sampler_unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.sample


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError("unexpected_redirect")


class LabHTTP:
    def __init__(self):
        self.requests = 0
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect()
        )

    def request_sync(self, target, path, method, cookie, body):
        headers = {"Host": HOST, "Origin": ORIGIN, "Content-Type": "application/json"}
        if cookie:
            headers["Cookie"] = "chat_session=" + cookie
        request = urllib.request.Request(
            f"http://127.0.0.1:{TARGETS[target]}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            headers=headers,
            method=method,
        )
        try:
            response = self.opener.open(request, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise EvidenceError("http_body_limit")
            parsed = json.loads(raw) if raw else None
            cookies = SimpleCookie()
            for value in response.headers.get_all("Set-Cookie", []):
                cookies.load(value)
            token = cookies["chat_session"].value if "chat_session" in cookies else None
            return response.status, parsed, token

    async def request(self, target, path, *, method="GET", cookie=None, body=None):
        if target not in ("api_a", "api_b") or not path.startswith("/v1/"):
            raise EvidenceError("target_not_allowed")
        self.requests += 1
        if self.requests > 200:
            raise EvidenceError("http_request_limit")
        return await asyncio.to_thread(
            self.request_sync, target, path, method, cookie, body
        )


def require_status(status, wanted):
    if status != wanted:
        raise EvidenceError("unexpected_http_status")


def event_message(frame):
    if frame.get("type") != "message.created" or frame.get("schema_version") != 1:
        raise EvidenceError("invalid_event")
    row = normalize(frame["message"])
    if frame.get("event_id") != row["message_id"]:
        raise EvidenceError("event_id_mismatch")
    return row


def append(path, row):
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(row, separators=(",", ":")) + "\n")


def write_new(path, value):
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2)


def connection(target, cookie):
    from websockets.asyncio.client import connect

    if target not in ("gateway_a", "gateway_b"):
        raise EvidenceError("gateway_not_allowed")
    return connect(
        f"ws://{HOST}/v1/ws",
        host="127.0.0.1",
        port=TARGETS[target],
        origin=ORIGIN,
        additional_headers={"Cookie": "chat_session=" + cookie} if cookie else None,
        proxy=None,
        compression=None,
        max_size=16384,
        max_queue=256,
        open_timeout=3,
        close_timeout=2,
        ping_interval=10,
        ping_timeout=3,
    )


async def receive(ws, peer, run_dir):
    from websockets.exceptions import ConnectionClosed

    try:
        async for raw in ws:
            frame = json.loads(raw)
            kind = frame.get("type")
            if kind == "message.created":
                if len(peer["received"]) >= 256:
                    raise EvidenceError("peer_evidence_limit")
                row = event_message(frame)
                row["received_monotonic"] = time.monotonic()
                peer["received"].append(row)
                append(run_dir / "receipts.jsonl", {"peer": peer["id"], **row})
            elif kind == "subscribed":
                if (
                    frame.get("conversation_id") != ROOM
                    or frame.get("protocol_version") != 1
                ):
                    raise EvidenceError("invalid_subscription")
                peer["baseline_head"] = frame["head_seq"]
                peer["ready"].set()
            elif kind == "heads":
                continue
            else:
                raise EvidenceError("unexpected_ws_frame")
    except ConnectionClosed:
        pass
    except (ValueError, KeyError, TypeError):
        peer["error"] = "invalid_ws_evidence"
    finally:
        peer["closed_code"] = ws.close_code
        peer["closed_at"] = time.monotonic()
        peer["closed"].set()


async def open_peer(stack, target, cookie, name, evidence, run_dir):
    ws = await stack.enter_async_context(connection(target, cookie))
    peer = {
        "id": name,
        "target": target,
        "received": [],
        "history_recovered": [],
        "offline_client_ids": [],
        "ready": asyncio.Event(),
        "closed": asyncio.Event(),
    }
    evidence["peers"].append(peer)
    task = asyncio.create_task(receive(ws, peer, run_dir))
    stack.callback(task.cancel)
    await ws.send(json.dumps({"type": "subscribe", "conversation_id": ROOM}))
    await asyncio.wait_for(peer["ready"].wait(), 4)
    return ws, peer


async def history(http, cookie, after):
    recovered, snapshot = [], None
    cursor = str(after)
    for _ in range(5):
        suffix = f"?after_seq={cursor}&limit=100"
        if snapshot is not None:
            suffix += f"&snapshot_head_seq={snapshot}"
        status, data, _ = await http.request(
            "api_b", MESSAGE_PATH + suffix, cookie=cookie
        )
        require_status(status, 200)
        meta = data["meta"]
        if snapshot is not None and meta["snapshot_head_seq"] != snapshot:
            raise EvidenceError("snapshot_changed")
        snapshot = meta["snapshot_head_seq"]
        rows = [normalize(row) for row in data["data"]]
        recovered.extend(rows)
        if not meta["has_more"]:
            seqs = [int(row["seq"]) for row in recovered]
            if seqs != list(range(int(after) + 1, int(snapshot) + 1)):
                raise EvidenceError("history_sequence_hole_or_duplicate")
            return recovered, snapshot
        next_cursor = meta["next_cursor"]
        if int(next_cursor) <= int(cursor):
            raise EvidenceError("cursor_not_progressing")
        cursor = next_cursor
    raise EvidenceError("history_page_limit")


async def wait_receipts(evidence, timeout=6):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        pending = False
        for peer in evidence["peers"]:
            wanted = {
                row["client_message_id"]
                for row in evidence["attempts"]
                if row["outcome"] == "acknowledged"
            } - set(peer["offline_client_ids"])
            seen = {row["client_message_id"] for row in peer["received"]}
            pending |= bool(wanted - seen)
        if not pending:
            return
        await asyncio.sleep(0.05)
    raise EvidenceError("live_receipt_timeout")


async def exercise(evidence, run_dir, scenario):
    http = LabHTTP()
    status, actor, cookie = await http.request(
        "api_a", "/v1/dev/session", method="POST", body={"user": "user_a"}
    )
    require_status(status, 200)
    if not cookie or actor["data"]["user_id"] != ACTOR:
        raise EvidenceError("session_issue_invalid")
    status, actor, _ = await http.request("api_b", "/v1/session", cookie=cookie)
    evidence["checks"]["shared_session"] = (
        status == 200 and actor["data"]["user_id"] == ACTOR
    )
    if not evidence["checks"]["shared_session"]:
        raise EvidenceError("shared_session_failed")
    status, _, _ = await http.request("api_b", "/v1/session")
    evidence["checks"]["anonymous_http_denied"] = status == 401
    from websockets.exceptions import InvalidStatus

    try:
        async with connection("gateway_b", None):
            evidence["checks"]["anonymous_ws_denied"] = False
    except InvalidStatus as error:
        evidence["checks"]["anonymous_ws_denied"] = error.response.status_code == 403
    async with AsyncExitStack() as stack:
        sockets = []
        for index in range(evidence["expected_peers"]):
            target = "gateway_a" if index % 2 == 0 else "gateway_b"
            sockets.append(
                await open_peer(
                    stack, target, cookie, f"peer-{index}", evidence, run_dir
                )
            )
        evidence["baseline_head"] = max(
            int(peer["baseline_head"]) for _, peer in sockets
        )
        start = time.monotonic()
        offline_socket = None
        for index, planned in enumerate(evidence["plan"]):
            if (run_dir / "STOP").exists():
                raise EvidenceError("operator_stop")
            if scenario == "reconnect" and index == 10:
                await wait_receipts(evidence)
                offline_socket, offline_peer = sockets[1]
                offline_peer["resume_after_seq"] = str(
                    max(
                        [
                            int(offline_peer["baseline_head"]),
                            *[int(row["seq"]) for row in offline_peer["received"]],
                        ]
                    )
                )
                offline_peer["offline_client_ids"] = [
                    row["client_message_id"] for row in evidence["plan"][10:15]
                ]
                await offline_socket.close()
                await offline_peer["closed"].wait()
            if scenario == "reconnect" and index == 15:
                # Establish that the live recipient has observed the offline
                # batch before B rejoins; late broker delivery is not recovery.
                await wait_receipts(evidence)
                old_peer = sockets[1][1]
                # Reconnect keeps one logical recipient's evidence, but records
                # the new transport separately for diagnosis.
                new_ws, new_peer = await open_peer(
                    stack, "gateway_b", cookie, "peer-1-reconnected", evidence, run_dir
                )
                evidence["peers"].remove(new_peer)
                old_peer["reconnect_baseline_head"] = new_peer["baseline_head"]
                new_peer["received"] = old_peer["received"]
                recovered, snapshot = await history(
                    http, cookie, old_peer["resume_after_seq"]
                )
                old_peer["history_recovered"].extend(recovered)
                old_peer["snapshot_head"] = snapshot
                sockets[1] = (new_ws, new_peer)
            await asyncio.sleep(
                max(0, start + index / evidence["rate"] - time.monotonic())
            )
            actual = time.monotonic()
            attempt = {
                **planned,
                "attempt_id": index,
                "outcome": "unknown",
                "ack_status": None,
                "generator_lag_seconds": max(
                    0, actual - start - index / evidence["rate"]
                ),
            }
            try:
                text = f"distributed-synthetic:{evidence['run_id']}:{index}"
                status, data, _ = await http.request(
                    "api_a" if index % 2 == 0 else "api_b",
                    MESSAGE_PATH,
                    cookie=cookie,
                    method="POST",
                    body={
                        "client_message_id": planned["client_message_id"],
                        "text": text,
                    },
                )
                attempt["http_status"] = status
                if status in (200, 201):
                    attempt.update(
                        outcome="acknowledged",
                        ack_status=status,
                        message=normalize(data["data"]),
                    )
                    attempt["response_message_id"] = attempt["message"]["message_id"]
                    attempt["response_seq"] = attempt["message"]["seq"]
                elif 400 <= status < 500:
                    attempt["outcome"] = "rejected"
            except (OSError, ValueError, KeyError, TypeError):
                attempt["reason"] = "transport_or_response_unknown"
            attempt["latency_seconds"] = time.monotonic() - actual
            evidence["attempts"].append(attempt)
            append(run_dir / "requests.jsonl", attempt)
        await wait_receipts(evidence)
        # Relogin invalidates exactly the previous cookie. No new public revoke API.
        evidence["checks"]["ws_live_before_relogin"] = all(
            not peer["closed"].is_set() for _, peer in sockets
        )
        revoke_start = time.monotonic()
        status, _, replacement = await http.request(
            "api_a",
            "/v1/dev/session",
            cookie=cookie,
            method="POST",
            body={"user": "user_a"},
        )
        require_status(status, 200)
        evidence["checks"]["relogin_replaced_cookie"] = bool(
            replacement and replacement != cookie
        )
        status, _, _ = await http.request("api_b", "/v1/session", cookie=cookie)
        evidence["checks"]["revoked_http_denied"] = status == 401
        try:
            async with asyncio.timeout(7):
                await asyncio.gather(*(peer["closed"].wait() for _, peer in sockets))
        except TimeoutError:
            pass
        evidence["checks"]["revoked_ws_closed"] = all(
            peer.get("closed_code") == 1008 for _, peer in sockets
        )
        for _, active_peer in sockets:
            if active_peer.get("error"):
                evidence["checks"]["revoked_ws_closed"] = False
        evidence["revocation_elapsed_seconds"] = time.monotonic() - revoke_start
        evidence["revocation"] = {
            "observation_timeout_ms": 7000,
            "design_target_ms": 5000,
            "timing_origin": "relogin_request_start",
            "peers": [
                {
                    "peer": peer["id"],
                    "close_code": peer.get("closed_code"),
                    "elapsed_ms": round((peer["closed_at"] - revoke_start) * 1000, 3)
                    if peer.get("closed_at") is not None
                    else None,
                }
                for _, peer in sockets
            ],
        }
        evidence["revocation"]["design_5s_met"] = all(
            row["close_code"] == 1008
            and row["elapsed_ms"] is not None
            and 0 <= row["elapsed_ms"] <= 5000
            for row in evidence["revocation"]["peers"]
        )
        try:
            async with connection("gateway_b", cookie):
                evidence["checks"]["revoked_ws_handshake_denied"] = False
        except InvalidStatus as error:
            evidence["checks"]["revoked_ws_handshake_denied"] = (
                error.response.status_code == 403
            )
    evidence["http_requests"] = http.requests
    evidence["complete"] = True


async def run(scenario, run_dir, resource_guard=False):
    count, rate, peers = (100, 10, 20) if scenario == "bounded" else (20, 5, 2)
    run_id = str(uuid4())
    evidence = {
        "run_id": run_id,
        "scenario": scenario,
        "rate": rate,
        "expected_peers": peers,
        "complete": False,
        "plan": [],
        "attempts": [],
        "peers": [],
        "checks": {},
        "resource_guard_enabled": resource_guard,
    }
    for index in range(count):
        text = f"distributed-synthetic:{run_id}:{index}"
        evidence["plan"].append(
            {
                "client_message_id": str(uuid4()),
                "conversation_id": ROOM,
                "sender_id": ACTOR,
                "text_sha256": sha256(text.encode()).hexdigest(),
            }
        )
    write_new(
        run_dir / "manifest.json",
        {key: value for key, value in evidence.items() if key != "peers"},
    )
    try:
        async with asyncio.timeout(120):
            guard = (
                ResourceGuard(run_dir, resource_sampler()) if resource_guard else None
            )
            if guard is not None and not await guard.sample_once():
                raise EvidenceError("resource_preflight_failed")
            monitor = asyncio.create_task(guard.monitor()) if guard else None
            try:
                await exercise(evidence, run_dir, scenario)
            finally:
                if guard is not None:
                    guard.done.set()
                    if asyncio.current_task().cancelling():
                        monitor.cancel()
                        await asyncio.gather(monitor, return_exceptions=True)
                        raise EvidenceError("deadline_resource_sampling_incomplete")
                    await monitor
                    evidence["resource_guard_passed"] = not guard.failed
                    if guard.failed:
                        evidence["complete"] = False
    except EvidenceError as error:
        evidence["error"] = str(error)
    except Exception:  # noqa: BLE001 — artifact boundary must not expose cookie-bearing exceptions.
        evidence["error"] = "execution_incomplete"
    for peer in evidence["peers"]:
        peer.pop("ready", None)
        peer.pop("closed", None)
    result = compare(evidence)
    result["revocation"] = evidence.get("revocation")
    write_new(run_dir / "evidence.json", evidence)
    write_new(run_dir / "result.json", result)
    write_new(
        run_dir / "final.json",
        {
            "run_id": run_id,
            "complete": evidence["complete"],
            "planned_count": len(evidence["plan"]),
            "attempts": evidence["attempts"],
            "stop_reason": evidence.get("error"),
            "revocation": evidence.get("revocation"),
        },
    )
    print(json.dumps(result))
    return 0 if result["status"] == "pass" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario", choices=("smoke", "bounded", "reconnect"), default="smoke"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument(
        "--resource-guard",
        action="store_true",
        help="Require distributed lab resource checks before, during and after traffic",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[3] / ".artifacts"
    output = args.run_dir.resolve()
    if not output.is_relative_to(root) or output == root:
        parser.error("run-dir must be a fresh directory beneath repository .artifacts")
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    return asyncio.run(run(args.scenario, output, args.resource_guard))


if __name__ == "__main__":
    raise SystemExit(main())
