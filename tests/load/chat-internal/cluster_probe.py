"""Opt-in local cluster path experiment with scoped cleanup and independent DB proof."""

import asyncio
import json
import math
import sys
from collections import Counter
from uuid import uuid4

from e2e_probe import assess
from lock_probe import (
    ACTOR,
    HASH,
    KUBE,
    REPLICA,
    ROOT,
    append,
    command,
    guard,
    pods,
    sql,
)
from prometheus_client.parser import text_string_to_metric_families
from relay_cpu_ab import deployment, set_cpu

NAMES = [
    ("pod", "chat-cluster-probe"),
    ("configmap", "chat-cluster-probe-source"),
    ("networkpolicy", "cluster-probe-egress"),
    ("networkpolicy", "gateway-from-cluster-probe"),
]


def p99(values):
    values = sorted(values)
    return values[math.ceil(len(values) * 0.99) - 1] if values else None


def restoration_ok(recovery):
    return (
        recovery.get("relay_restored") is True
        and recovery.get("fanout_unchanged") is True
        and recovery.get("cleanup_absent") is True
        and recovery.get("pending") == 0
        and "guard" in recovery
        and "error_type" not in recovery
        and not any("/" in key for key in recovery)
    )


def metric_deltas(before, after):
    def samples(text):
        return {
            (s.name, tuple(sorted(s.labels.items()))): s.value
            for f in text_string_to_metric_families(text)
            for s in f.samples
            if s.name.endswith(("_sum", "_count"))
        }

    out = {}
    for pod in before.keys() | after.keys():
        if pod not in before or pod not in after:
            raise ValueError("metrics_missing")
        a, b = samples(before[pod]), samples(after[pod])
        values = {}
        unavailable = []
        for (name, labels), end in b.items():
            if not name.endswith("_sum"):
                continue
            count_key = (name[:-4] + "_count", labels)
            if (name, labels) not in a or count_key not in a or count_key not in b:
                unavailable.append(name[:-4] + str(dict(labels)))
                continue
            count, total = b[count_key] - a[count_key], end - a[(name, labels)]
            if count < 0 or total < 0:
                raise ValueError("metrics_reset")
            values[name[:-4] + str(dict(labels))] = {
                "count": count,
                "mean_seconds": total / count if count else None,
            }
        if not values:
            raise ValueError("metrics_missing")
        if unavailable:
            values["unavailable_series"] = unavailable
        out[pod] = values
    return out


def reconcile(config, raw, primary, replica):
    expected = {r["mid"]: r for r in raw["requests"] if r["status"] == "ack"}
    room_for = {cid: config["rooms"][i % 10] for i, cid in enumerate(config["ids"])}
    fields = ("cid", "room", "seq", "hash", "sender")
    ack_valid = (
        len(expected) == 1500
        and all(
            r["cid"] in room_for
            and r["room"] == room_for[r["cid"]]
            and r["hash"] == HASH
            and r["sender"] == ACTOR
            for r in expected.values()
        )
        and {r["cid"] for r in expected.values()} == set(room_for)
    )

    def match(rows):
        return (
            len(rows) == len(expected)
            and {r["mid"] for r in rows} == set(expected)
            and all(all(r[k] == expected[r["mid"]][k] for k in fields) for r in rows)
        )

    verdict = assess(expected, raw["peers"])
    result = {
        **verdict,
        "ack": len(expected),
        "planned": 1500,
        "pod_distribution": dict(Counter(r.get("pod_uid") for r in expected.values())),
        "kafka_partition_distribution": dict(
            Counter(r["partition"] for r in raw["kafka"])
        ),
        "ack_p99_seconds": p99([r["seconds"] for r in expected.values()]),
        "outbox_p99_seconds": p99(
            [r["outbox_seconds"] for r in primary if r["outbox_seconds"] is not None]
        ),
        "kafka_observed_p99_seconds": p99(
            [
                r["observed_monotonic"] - expected[r["mid"]]["started_monotonic"]
                for r in raw["kafka"]
                if r["mid"] in expected
            ]
        ),
        "states": dict(Counter(r["status"] for r in raw["requests"])),
        "primary": match(primary),
        "replica": match(replica),
        "kafka": match(raw["kafka"]),
        "published": all(
            r["outbox_seconds"] is not None and r["outbox_seconds"] >= 0
            for r in primary
        ),
    }
    result["correct"] = (
        ack_valid
        and len(raw["peers"]) == 20
        and not verdict["failures"]
        and all(result[k] for k in ("primary", "replica", "kafka", "published"))
    )
    result["pass"] = (
        result["correct"]
        and result["ack_p99_seconds"] <= 0.5
        and result["ws_p99_seconds"] <= 1
    )
    return result


def bootstrap_session(pod, user):
    # Response is captured in memory, never printed or written to artifacts.
    script = (
        "import httpx2 as h; r=h.post('http://127.0.0.1:18082/v1/dev/session',"
        "headers={'Origin':'http://127.0.0.1:18083'},json={'user':'"
        + user
        + "'},timeout=3);"
        "r.raise_for_status(); print(r.cookies['chat_session'])"
    )
    return command(KUBE + ["exec", pod, "--", "python", "-c", script]).strip()


async def main(*, modes=("pod", "service"), observer=None):
    if not modes or any(mode not in {"pod", "service"} for mode in modes):
        raise ValueError("unsupported_path")
    original = await asyncio.to_thread(deployment)
    fanout = await asyncio.to_thread(deployment, "fanout")
    if (
        original["resources"]["limits"]["cpu"] != "200m"
        or fanout["resources"]["limits"]["cpu"] != "200m"
    ):
        raise ValueError("unexpected_baseline")
    baseline = pods()
    guard(baseline)
    for kind, name in NAMES:
        if command(
            KUBE + ["get", kind, name, "--ignore-not-found", "-o", "name"]
        ).strip():
            raise ValueError("experiment_resource_exists")
    directory = ROOT / ".artifacts/cluster-path" / str(uuid4())
    directory.mkdir(parents=True)
    print(str(directory), flush=True)
    append(directory / "control.jsonl", {"relay": original, "fanout": fanout})
    process = None
    try:
        await asyncio.to_thread(set_cpu, "400m")
        source = command(
            KUBE
            + [
                "create",
                "configmap",
                "chat-cluster-probe-source",
                "--from-file=tests/load/chat-internal/cluster_driver.py",
                "--dry-run=client",
                "-o",
                "json",
            ]
        )
        command(KUBE + ["create", "-f", "-"], input=source)
        command(
            KUBE
            + ["create", "-f", str(ROOT / "infra/k8s/chat-external/cluster-probe.yaml")]
        )
        for _ in range(30):
            state = json.loads(
                command(KUBE + ["get", "pod", "chat-cluster-probe", "-o", "json"])
            )
            if state["status"].get("phase") == "Running":
                break
            await asyncio.sleep(1)
        else:
            raise ValueError("runner_not_ready")
        baseline = pods()
        data = json.loads(command(KUBE + ["get", "pods", "-o", "json"]))["items"]
        apis = {
            p["metadata"]["name"]: p["status"]["podIP"]
            for p in data
            if p["metadata"].get("labels", {}).get("app") == "chat"
        }
        gateways = sorted(
            p["status"]["podIP"]
            for p in data
            if p["metadata"].get("labels", {}).get("app") == "chat-gateway"
        )
        if len(apis) != 2 or len(gateways) != 2:
            raise ValueError("unexpected_topology")
        cookies = [bootstrap_session(min(apis), user) for user in ("user_a", "user_b")]
        all_passed = True
        for mode in modes:
            guard(baseline)
            if sql(
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;"
            ):
                raise ValueError("outbox_not_drained")
            run_dir = directory / mode
            run_dir.mkdir()
            rooms, ids = (
                [str(uuid4()) for _ in range(10)],
                [str(uuid4()) for _ in range(1500)],
            )
            config = {
                "api": f"http://{apis[min(apis)]}:18082"
                if mode == "pod"
                else "http://chat:18082",
                "apis": apis,
                "gateways": gateways,
                "cookies": cookies,
                "rate": 25,
                "rooms": rooms,
                "ids": ids,
            }
            (run_dir / "plan.json").write_text(
                json.dumps({k: v for k, v in config.items() if k != "cookies"})
            )
            fixture = "BEGIN;"
            for room in rooms:
                fixture += f"INSERT INTO chat.conversations(id,kind,last_seq) VALUES ('{room}','dm',0);"
                fixture += f"INSERT INTO chat.members(conversation_id,user_id) VALUES ('{room}','{ACTOR}'),('{room}','00000000-0000-4000-8000-000000000002');"
            sql(fixture + "COMMIT; SELECT to_json(true);")
            print("Running " + mode, flush=True)
            if observer is not None:
                (run_dir / "cost-before.json").write_text(
                    json.dumps(await asyncio.to_thread(observer))
                )
            process = await asyncio.create_subprocess_exec(
                *[
                    arg if arg != "--request-timeout=5s" else "--request-timeout=0"
                    for arg in KUBE
                ],
                "exec",
                "-i",
                "chat-cluster-probe",
                "--",
                "python",
                "/probe/cluster_driver.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            communication = asyncio.create_task(
                process.communicate(json.dumps(config).encode())
            )
            async with asyncio.timeout(200):
                while not communication.done():
                    await asyncio.wait({communication}, timeout=5)
                    append(
                        run_dir / "resources.jsonl",
                        await asyncio.to_thread(guard, baseline),
                    )
                    state = json.loads(
                        command(
                            KUBE + ["get", "pod", "chat-cluster-probe", "-o", "json"]
                        )
                    )
                    if state["status"]["phase"] != "Running" or any(
                        c.get("restartCount", 0)
                        for c in state["status"].get("containerStatuses", [])
                    ):
                        raise ValueError("runner_failed")
            stdout, _ = await communication
            if process.returncode != 0:
                raise ValueError("runner_exec_failed")
            raw = json.loads(stdout)
            if observer is not None:
                (run_dir / "cost-after.json").write_text(
                    json.dumps(await asyncio.to_thread(observer))
                )
            (run_dir / "raw.json").write_text(json.dumps(raw))
            if raw["status"] != "measured":
                raise ValueError("runner_measurement_incomplete")
            values = ",".join("'" + room + "'::uuid" for room in rooms)
            query = (
                "SELECT coalesce(json_agg(row_to_json(s)),'[]') FROM (SELECT m.id::text mid,m.client_message_id::text cid,m.conversation_id::text room,m.sender_id::text sender,m.seq,encode(sha256(convert_to(m.text,'UTF8')),'hex') hash,extract(epoch FROM o.published_at-o.created_at) outbox_seconds FROM chat.messages m JOIN chat.message_outbox o ON o.event_id=m.id WHERE m.conversation_id IN ("
                + values
                + ")) s;"
            )
            primary, replica = sql(query), sql(query, REPLICA)
            (run_dir / "primary.json").write_text(json.dumps(primary))
            (run_dir / "replica.json").write_text(json.dumps(replica))
            proof = reconcile(config, raw, primary, replica)
            (run_dir / "proof.json").write_text(json.dumps(proof, indent=2))
            proof["metrics"] = metric_deltas(
                raw["metrics_before"], raw["metrics_after"]
            )
            (run_dir / "proof.json").write_text(json.dumps(proof, indent=2))
            print(json.dumps({"mode": mode, **proof}), flush=True)
            all_passed = all_passed and proof["pass"]
            if not proof["correct"]:
                break
        return all_passed
    finally:
        if process and process.returncode is None:
            process.terminate()
            await process.wait()
        recovery = {}
        for kind, name in NAMES:
            try:
                command(
                    KUBE + ["delete", kind, name, "--ignore-not-found", "--wait=false"]
                )
            except Exception as exc:  # noqa: BLE001
                recovery[kind + "/" + name] = type(exc).__name__
        try:
            await asyncio.to_thread(set_cpu, "200m")
            recovery["relay_restored"] = deployment() == original
            recovery["fanout_unchanged"] = deployment("fanout") == fanout
            recovery["guard"] = guard(pods())
            recovery["pending"] = sql(
                "SELECT to_json(count(*)) FROM chat.message_outbox WHERE published_at IS NULL;"
            )
            recovery["cleanup_absent"] = all(
                not command(
                    KUBE + ["get", kind, name, "--ignore-not-found", "-o", "name"]
                ).strip()
                for kind, name in NAMES
            )
        except Exception as exc:  # noqa: BLE001
            recovery["error_type"] = type(exc).__name__
        (directory / "restoration.json").write_text(json.dumps(recovery, indent=2))
        print(json.dumps({"restoration": recovery}), flush=True)
        if not restoration_ok(recovery):
            raise ValueError("restore_unverified")


if __name__ == "__main__":
    try:
        sys.exit(0 if asyncio.run(main()) else 1)
    except Exception as exc:  # noqa: BLE001 — no credential-bearing traceback
        print(json.dumps({"status": "incomplete", "error_type": type(exc).__name__}))
        raise SystemExit(2) from None
