"""실제 네트워크 부하 발생기 (Step 3).

Step 1·2의 `loadgen.py`와 달리 **인프로세스 ASGI가 아닙니다.** 서비스는 Pod 안의
별도 프로세스·컨테이너이고, 이 발생기는 클러스터 밖의 독립 프로세스로서 실제 TCP
소켓으로 요청합니다. 따라서:

  - 측정값에 실제 소켓 왕복·커널·HTTP 파싱이 포함됩니다
  - 발생기 CPU가 서비스 CPU와 **실제로 분리**됩니다 (다른 프로세스이므로)

Step 3 관측을 위해 응답 헤더에서 인스턴스·캐시·release를 읽어 **Pod별로 집계**합니다.

부하는 `--execute` 없이는 발생하지 않습니다.
"""

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from httpx2 import AsyncClient

# Step 1·2와 같은 계단에 상한 탐색용 연장 계단을 더합니다.
BASE_STEPS: tuple[float, ...] = (1, 2, 4, 6, 8, 12)
EXTENDED_STEPS: tuple[float, ...] = (16, 24, 32, 48)
ALL_STEPS: tuple[float, ...] = BASE_STEPS + EXTENDED_STEPS

MAX_ERROR_RATE = 0.01
BREAK_P95_MULTIPLE = 2.0
MIN_SUBGROUP_SAMPLES = 20


def cpu_seconds() -> float:
    """이 발생기 프로세스의 CPU 시간입니다. 서비스와 다른 프로세스입니다."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def percentile_ns(samples: list[int], fraction: float) -> int:
    """nearest-rank percentile입니다."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]


@dataclass(slots=True)
class Observation:
    """요청 1건의 관측입니다."""

    client_ns: int
    server_ns: int
    instance: str
    cache: str
    release_id: str


@dataclass(slots=True)
class StepOutcome:
    """한 계단의 결과입니다. Pod별 집계를 포함합니다."""

    target_rps: float
    duration_s: float
    scheduled: int
    started: int
    errors: int
    dropped: int
    late_starts: int
    observations: list[Observation] = field(default_factory=list)
    generator_cpu_s: float = 0.0

    def error_rate(self) -> float:
        return self.errors / self.started if self.started else 0.0

    def by_instance(self) -> dict[str, dict[str, object]]:
        """Pod별 요청 수·hit률·지연입니다 (Step 3 관측 1)."""
        grouped: dict[str, list[Observation]] = defaultdict(list)
        for row in self.observations:
            grouped[row.instance].append(row)
        result: dict[str, dict[str, object]] = {}
        for instance, rows in sorted(grouped.items()):
            hits = sum(1 for r in rows if r.cache == "hit")
            latencies = [r.client_ns for r in rows]
            result[instance] = {
                "requests": len(rows),
                "hits": hits,
                "misses": len(rows) - hits,
                "hit_rate": round(hits / len(rows), 4) if rows else None,
                "p50_ns": percentile_ns(latencies, 0.50),
                "p95_ns": percentile_ns(latencies, 0.95),
            }
        return result

    def summary(self) -> dict[str, object]:
        latencies = [row.client_ns for row in self.observations]
        server = [row.server_ns for row in self.observations]
        result: dict[str, object] = {
            "target_rps": self.target_rps,
            "duration_s": round(self.duration_s, 3),
            "scheduled": self.scheduled,
            "started": self.started,
            "completed": len(self.observations),
            "errors": self.errors,
            "dropped": self.dropped,
            "error_rate": round(self.error_rate(), 5),
            "achieved_rps": (
                round(len(self.observations) / self.duration_s, 3)
                if self.duration_s
                else None
            ),
            "late_starts": self.late_starts,
            "generator_cpu_s": round(self.generator_cpu_s, 3),
            "instances": self.by_instance(),
        }
        if latencies:
            result["client_ns"] = {
                "p50": percentile_ns(latencies, 0.50),
                "p95": percentile_ns(latencies, 0.95),
                "p99": percentile_ns(latencies, 0.99),
            }
            result["server_ns"] = {
                "p50": percentile_ns(server, 0.50),
                "p95": percentile_ns(server, 0.95),
            }
            # 실제 네트워크 왕복 = 클라이언트 전체 - 서버 처리.
            network = [row.client_ns - row.server_ns for row in self.observations]
            result["network_ns"] = {
                "p50": percentile_ns(network, 0.50),
                "p95": percentile_ns(network, 0.95),
                "median": int(statistics.median(network)),
            }
        for label in ("hit", "miss"):
            subset = [r.client_ns for r in self.observations if r.cache == label]
            entry: dict[str, object] = {"sample_count": len(subset)}
            if len(subset) >= MIN_SUBGROUP_SAMPLES:
                entry["p50"] = percentile_ns(subset, 0.50)
                entry["p95"] = percentile_ns(subset, 0.95)
            else:
                entry["note"] = "표본 부족으로 percentile 미판정"
            result[f"{label}_client_ns"] = entry
        return result


async def run_step(
    client: AsyncClient, *, url: str, target_rps: float, duration_s: float
) -> StepOutcome:
    """한 계단을 open-loop로 발사합니다."""
    interval_ns = int(1e9 / target_rps)
    scheduled = max(1, int(target_rps * duration_s))
    observations: list[Observation] = []
    errors = 0
    late_starts = 0

    async def one_request() -> None:
        nonlocal errors
        started = time.perf_counter_ns()
        try:
            response = await client.get(url)
        except Exception:
            errors += 1
            return
        elapsed = time.perf_counter_ns() - started
        if response.status_code != 200:
            errors += 1
            return
        observations.append(
            Observation(
                client_ns=elapsed,
                server_ns=int(response.headers.get("X-Server-Total-Ns", 0)),
                instance=response.headers.get("X-Instance", "unknown"),
                cache=response.headers.get("X-Cache", "unknown"),
                release_id=response.headers.get("X-Release-Id", "unknown"),
            )
        )

    cpu_start = cpu_seconds()
    wall_start = time.perf_counter_ns()
    tasks: list[asyncio.Task[None]] = []
    for index in range(scheduled):
        due = wall_start + index * interval_ns
        now = time.perf_counter_ns()
        if now < due:
            await asyncio.sleep((due - now) / 1e9)
        elif now - due > interval_ns:
            late_starts += 1
        tasks.append(asyncio.create_task(one_request()))
    await asyncio.gather(*tasks, return_exceptions=True)

    return StepOutcome(
        target_rps=target_rps,
        duration_s=(time.perf_counter_ns() - wall_start) / 1e9,
        scheduled=scheduled,
        started=len(tasks),
        errors=errors,
        dropped=0,
        late_starts=late_starts,
        observations=observations,
        generator_cpu_s=cpu_seconds() - cpu_start,
    )


async def run_ladder(
    *, base_url: str, lang: str, steps: tuple[float, ...], seconds: float, warmup: int
) -> dict[str, object]:
    """계단 전체를 돕니다. 꺾이거나 중단 조건에 걸리면 멈춥니다."""
    url = f"{base_url}/v1/catalog/full?lang={lang}"
    async with AsyncClient(timeout=60) as client:
        for _ in range(warmup):
            await client.get(url)

        baseline_samples: list[int] = []
        for _ in range(20):
            started = time.perf_counter_ns()
            response = await client.get(url)
            if response.status_code == 200:
                baseline_samples.append(time.perf_counter_ns() - started)
        baseline_p95 = percentile_ns(baseline_samples, 0.95)
        threshold = int(baseline_p95 * BREAK_P95_MULTIPLE)

        outcomes: list[dict[str, object]] = []
        break_rps: float | None = None
        stop_reasons: list[str] = []

        for target in steps:
            outcome = await run_step(
                client, url=url, target_rps=target, duration_s=seconds
            )
            summary = outcome.summary()
            client_stats = summary.get("client_ns")
            p95 = int(client_stats["p95"]) if isinstance(client_stats, dict) else None
            summary["p95_vs_baseline"] = round(p95 / baseline_p95, 3) if p95 else None
            summary["broke"] = bool(p95 and p95 > threshold)
            outcomes.append(summary)

            if outcome.error_rate() > MAX_ERROR_RATE:
                stop_reasons.append(
                    f"error_rate {outcome.error_rate():.4f} > {MAX_ERROR_RATE}"
                    f" at {target} RPS"
                )
                break
            if summary["broke"]:
                break_rps = target
                break

        return {
            "base_url": base_url,
            "baseline_p95_ns": baseline_p95,
            "break_threshold_ns": threshold,
            "steps": outcomes,
            "break_rps": break_rps,
            "stop_reasons": stop_reasons,
            "client": "external process over real TCP (httpx2) — 인프로세스 아님",
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.netload",
        description=(
            "실제 네트워크 부하 발생기입니다 (Step 3). 서비스와 다른 프로세스로 "
            "동작합니다. --execute 없이는 부하를 발생시키지 않습니다."
        ),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:18094")
    parser.add_argument("--lang", default="ko")
    parser.add_argument("--seconds", type=float, default=20)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument(
        "--extended",
        action="store_true",
        help="16/24/32/48 RPS 연장 계단까지 포함합니다",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    steps = ALL_STEPS if args.extended else BASE_STEPS

    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "base_url": args.base_url,
                    "steps_rps": list(steps),
                    "seconds_per_step": args.seconds,
                    "model": "open-loop arrival rate over real TCP",
                    "note": "--execute 에서만 부하를 발생시킵니다. SLO 판정 없음.",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    result = asyncio.run(
        run_ladder(
            base_url=args.base_url,
            lang=args.lang,
            steps=steps,
            seconds=args.seconds,
            warmup=args.warmup,
        )
    )
    result["platform"] = sys.platform
    result["python"] = sys.version.split()[0]

    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"raw written to {args.out}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
