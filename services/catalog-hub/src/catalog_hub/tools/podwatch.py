"""Pod 다중화 관측 (Step 3).

세 가지를 연속 요청하며 관측합니다. 부하 계단과 분리된 **기능 관측**입니다.

1. **release 전파 지연** — D4는 요청 시점 DB 조회이므로 전파 지연이 최대 1요청이어야
   합니다. Pod 2개 이상에서 전환 후 각 Pod가 몇 번째 요청에 새 release를 보는지 셉니다.

2. **Pod 간 응답 불일치** — 전파 중 서로 다른 Pod가 다른 release를 응답하는 구간입니다.
   **이것은 캐시 오염이 아닙니다.** release_id가 키에 있으므로 옛 값을 새 값이라
   속이는 일은 구조적으로 불가능합니다. 만약 같은 release_id에 다른 내용이 오면
   그것이 오염이며 즉시 중단 사유입니다.

3. **rolling update 구간** — 신규 Pod는 캐시가 비어 cold, 기존 Pod는 warm이므로
   교체 중 같은 시점 요청의 응답 시간이 갈립니다. 그 구간의 지속시간·p99·miss 수를 잽니다.

부하를 발생시키지 않습니다. `--execute` 없이는 관측하지 않습니다.
"""

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from httpx2 import AsyncClient


@dataclass(slots=True)
class Probe:
    """요청 1건의 관측입니다."""

    elapsed_ns: int
    at_ns: int
    instance: str
    cache: str
    release_id: str
    body_digest: str


async def probe_once(client: AsyncClient, url: str, origin_ns: int) -> Probe | None:
    started = time.perf_counter_ns()
    try:
        response = await client.get(url)
    except Exception:
        return None
    if response.status_code != 200:
        return None
    body = response.json()
    # 같은 release_id에 다른 내용이 오는지 확인할 지문입니다 (오염 검출).
    digest = (
        f"{body['release_id']}:{body['category_count']}:{body['item_count']}:"
        f"{body['roots'][0]['label'] if body['roots'] else ''}"
    )
    return Probe(
        elapsed_ns=time.perf_counter_ns() - started,
        at_ns=time.perf_counter_ns() - origin_ns,
        instance=response.headers.get("X-Instance", "unknown"),
        cache=response.headers.get("X-Cache", "unknown"),
        release_id=response.headers.get("X-Release-Id", "unknown"),
        body_digest=digest,
    )


async def sample_continuously(
    client: AsyncClient, url: str, *, seconds: float, interval_s: float
) -> list[Probe]:
    """일정 간격으로 계속 찔러 봅니다."""
    origin = time.perf_counter_ns()
    probes: list[Probe] = []
    deadline = origin + int(seconds * 1e9)
    while time.perf_counter_ns() < deadline:
        probe = await probe_once(client, url, origin)
        if probe is not None:
            probes.append(probe)
        await asyncio.sleep(interval_s)
    return probes


def analyse(probes: list[Probe]) -> dict[str, object]:
    """관측을 Pod별·release별로 정리합니다."""
    by_instance: dict[str, list[Probe]] = defaultdict(list)
    for probe in probes:
        by_instance[probe.instance].append(probe)

    # 오염 검출: 같은 release_id인데 본문 지문이 다르면 설계 결함입니다.
    digests_per_release: dict[str, set[str]] = defaultdict(set)
    for probe in probes:
        digests_per_release[probe.release_id].add(probe.body_digest)
    contamination = {
        release: sorted(digests)
        for release, digests in digests_per_release.items()
        if len(digests) > 1
    }

    instances: dict[str, object] = {}
    for instance, rows in sorted(by_instance.items()):
        hits = sum(1 for r in rows if r.cache == "hit")
        instances[instance] = {
            "requests": len(rows),
            "hits": hits,
            "misses": len(rows) - hits,
            "hit_rate": round(hits / len(rows), 4) if rows else None,
            "releases_seen": sorted({r.release_id for r in rows}),
            "first_seen_ms": round(rows[0].at_ns / 1e6, 1),
            "last_seen_ms": round(rows[-1].at_ns / 1e6, 1),
        }

    return {
        "total_probes": len(probes),
        "instances": instances,
        "releases_seen": sorted({p.release_id for p in probes}),
        "contamination_detected": bool(contamination),
        "contamination_detail": contamination,
    }


async def observe_release_propagation(
    base_url: str, lang: str, *, probes_per_phase: int
) -> dict[str, object]:
    """release 전환 전후를 Pod별로 관측합니다 (관측 2·5)."""
    # 저장소 레이아웃(services/catalog-hub/tests)과 컨테이너 레이아웃(/app/tests)
    # 전환은 구현(repository·조립·캐시)이 아니라 DB를 직접 바꾸는 경로입니다.
    from catalog_hub.tools.release_transition import (  # noqa: PLC0415
        publish_successor_release,
        rollback_to_release,
    )

    url = f"{base_url}/v1/catalog/full?lang={lang}"
    transition: tuple[int, int] | None = None
    try:
        async with AsyncClient(timeout=60) as client:
            # 두 Pod 모두 warm으로 만듭니다.
            for _ in range(probes_per_phase):
                await client.get(url)

            origin = time.perf_counter_ns()
            before = [
                p
                for _ in range(probes_per_phase)
                if (p := await probe_once(client, url, origin)) is not None
            ]

            transition = await publish_successor_release("step3")
            old_id, new_id = transition

            origin_after = time.perf_counter_ns()
            after = [
                p
                for _ in range(probes_per_phase * 2)
                if (p := await probe_once(client, url, origin_after)) is not None
            ]

            # Pod별로 새 release를 몇 번째 요청에 처음 봤는지 셉니다.
            propagation: dict[str, object] = {}
            seen_per_instance: dict[str, int] = {}
            for index, probe in enumerate(after):
                if (
                    probe.release_id == str(new_id)
                    and probe.instance not in seen_per_instance
                ):
                    seen_per_instance[probe.instance] = index
            instance_request_index: dict[str, int] = defaultdict(int)
            first_new_per_instance: dict[str, int] = {}
            for probe in after:
                idx = instance_request_index[probe.instance]
                instance_request_index[probe.instance] += 1
                if (
                    probe.release_id == str(new_id)
                    and probe.instance not in first_new_per_instance
                ):
                    first_new_per_instance[probe.instance] = idx
            propagation = {
                "old_release": old_id,
                "new_release": new_id,
                "first_new_release_at_global_index": seen_per_instance,
                "first_new_release_at_per_instance_request": first_new_per_instance,
                "max_requests_until_new_release": (
                    max(first_new_per_instance.values())
                    if first_new_per_instance
                    else None
                ),
            }

            # 전파 중 Pod 간 불일치 구간 (오염 아님).
            mismatch_windows = 0
            for index in range(1, len(after)):
                if after[index].release_id != after[index - 1].release_id:
                    mismatch_windows += 1

            return {
                "before": analyse(before),
                "after": analyse(after),
                "propagation": propagation,
                "release_switch_observations": mismatch_windows,
                "note": (
                    "Pod 간 서로 다른 release 응답은 전파 지연이며 캐시 오염이 "
                    "아닙니다. 오염은 같은 release_id에 다른 내용이 오는 경우이며 "
                    "contamination_detected로 별도 판정합니다."
                ),
            }
    finally:
        if transition is not None:
            await rollback_to_release(transition[0], transition[1])


async def observe_window(
    base_url: str, lang: str, *, seconds: float, interval_s: float
) -> dict[str, object]:
    """일정 시간 계속 관측합니다. rolling update 중 호출합니다 (관측 5)."""
    url = f"{base_url}/v1/catalog/full?lang={lang}"
    async with AsyncClient(timeout=60) as client:
        probes = await sample_continuously(
            client, url, seconds=seconds, interval_s=interval_s
        )
    result = analyse(probes)
    latencies = sorted(p.elapsed_ns for p in probes)
    if latencies:
        result["latency_ns"] = {
            "p50": latencies[max(0, len(latencies) // 2 - 1)],
            "p99": latencies[max(0, int(0.99 * len(latencies)) - 1)],
            "max": latencies[-1],
        }
    # cold 구간: miss가 연속으로 나온 지점과 그 시각입니다.
    misses = [
        {
            "at_ms": round(p.at_ns / 1e6, 1),
            "instance": p.instance,
            "elapsed_ms": round(p.elapsed_ns / 1e6, 1),
        }
        for p in probes
        if p.cache == "miss"
    ]
    result["miss_events"] = misses
    result["miss_count"] = len(misses)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.podwatch",
        description=(
            "Pod 다중화 관측입니다 (Step 3). 부하를 발생시키지 않습니다. "
            "--execute 없이는 관측하지 않습니다."
        ),
    )
    parser.add_argument("--base-url", default="http://catalog-hub:8000")
    parser.add_argument("--lang", default="ko")
    parser.add_argument(
        "--mode", choices=("propagation", "window"), default="propagation"
    )
    parser.add_argument("--probes", type=int, default=30)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print(json.dumps({"mode": "dry-run", "observe": args.mode}, ensure_ascii=False))
        return 0

    if args.mode == "propagation":
        result = asyncio.run(
            observe_release_propagation(
                args.base_url, args.lang, probes_per_phase=args.probes
            )
        )
    else:
        result = asyncio.run(
            observe_window(
                args.base_url,
                args.lang,
                seconds=args.seconds,
                interval_s=args.interval,
            )
        )

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
