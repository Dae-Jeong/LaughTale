"""계단식 부하 발생기 (Step 1·2, D5).

**open-loop 도착률 모델**입니다. 이전 요청의 완료를 기다리지 않고 목표 간격마다
요청을 시작합니다. closed-loop(직렬 반복)은 서버가 느려지면 도착률이 함께 줄어
"꺾이는 지점"을 감출 수 있습니다.

구분해 기록하는 것 (설계서 Step 1 요구):
    목표 도착률   scheduled — 계단이 요구한 요청 수
    실제 시작     started   — 실제로 발사된 요청 수
    완료          completed — 200을 받은 수
    거절/오류     errors    — 비-200
    dropped       미발사분 — 발생기가 제때 못 쏜 수 (발생기 병목 신호)
    지연 출발     late_starts — 목표 시각보다 늦게 출발한 수와 그 지연

**발생기 자원을 서비스와 분리**해 보고합니다. 같은 프로세스 안에서 도는 인프로세스
ASGI이므로 CPU를 완전히 분리할 수는 없고, 대신 발생기 자체의 스케줄링 오버헤드와
지연 출발을 관측해 병목 여부를 판단합니다. 이 한계를 결과에 명시합니다.

부하는 `--execute` 없이는 발생하지 않습니다.
"""

import argparse
import asyncio
import json
import resource
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from httpx2 import ASGITransport, AsyncClient

from catalog_hub.bootstrap.app import create_app
from catalog_hub.core.settings import DEFAULT_LANG, Settings
from catalog_hub.routers.catalog import CACHE_HEADER, SERVER_TIMING_HEADER

DEFAULT_DB_URL = "postgresql+asyncpg://thready:thready@127.0.0.1:5433/catalog_hub_lab"

# D5 정정판 계단입니다. 단건 약 124ms이므로 단일 워커 이론 상한은 약 8 RPS입니다.
COARSE_STEPS: tuple[float, ...] = (1, 2, 4, 6, 8, 12)
COARSE_SECONDS = 30
FINE_SECONDS = 60
FINE_INTERVAL_RPS = 0.5

# 중단 조건 (관리 확정).
MAX_ERROR_RATE = 0.01
BREAK_P95_MULTIPLE = 2.0


def max_rss_bytes() -> int:
    """플랫폼별 단위를 맞춘 최대 RSS입니다."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def cpu_seconds() -> float:
    """프로세스 CPU 시간(user+sys)입니다."""
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime + usage.ru_stime


def percentile_ns(samples: list[int], fraction: float) -> int:
    """nearest-rank percentile입니다. 평균을 percentile이라고 부르지 않습니다."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]


@dataclass(slots=True)
class StepOutcome:
    """한 계단의 결과입니다."""

    target_rps: float
    duration_s: float
    scheduled: int
    started: int
    completed: int
    errors: int
    dropped: int
    late_starts: int
    late_ns: list[int]
    client_ns: list[int]
    server_ns: list[int]
    hit_client_ns: list[int]
    miss_client_ns: list[int]
    generator_cpu_s: float
    max_rss_bytes: int

    def error_rate(self) -> float:
        return self.errors / self.started if self.started else 0.0

    def summary(self) -> dict[str, object]:
        result: dict[str, object] = {
            "target_rps": self.target_rps,
            "duration_s": self.duration_s,
            "scheduled": self.scheduled,
            "started": self.started,
            "completed": self.completed,
            "errors": self.errors,
            "dropped": self.dropped,
            "error_rate": round(self.error_rate(), 5),
            "achieved_rps": (
                round(self.completed / self.duration_s, 3) if self.duration_s else None
            ),
            "late_starts": self.late_starts,
            "late_ns_median": (
                int(statistics.median(self.late_ns)) if self.late_ns else 0
            ),
            "generator_cpu_s": round(self.generator_cpu_s, 3),
            "max_rss_bytes": self.max_rss_bytes,
        }
        if self.client_ns:
            result["client_ns"] = {
                "p50": percentile_ns(self.client_ns, 0.50),
                "p95": percentile_ns(self.client_ns, 0.95),
                "p99": percentile_ns(self.client_ns, 0.99),
            }
        if self.server_ns:
            result["server_ns"] = {
                "p50": percentile_ns(self.server_ns, 0.50),
                "p95": percentile_ns(self.server_ns, 0.95),
            }
        # hit/miss별 지연은 표본 수를 함께 보고합니다 (판정 규칙 3).
        for label, samples in (
            ("hit", self.hit_client_ns),
            ("miss", self.miss_client_ns),
        ):
            entry: dict[str, object] = {"sample_count": len(samples)}
            if len(samples) >= 20:
                entry["p50"] = percentile_ns(samples, 0.50)
                entry["p95"] = percentile_ns(samples, 0.95)
            else:
                entry["note"] = "표본 부족으로 percentile 미판정"
            result[f"{label}_client_ns"] = entry
        return result


@dataclass(slots=True)
class StopCondition:
    """중단 조건 판정 결과입니다."""

    tripped: bool = False
    reasons: list[str] = field(default_factory=list)

    def check(self, name: str, condition: bool, detail: str) -> None:
        if condition:
            self.tripped = True
            self.reasons.append(f"{name}: {detail}")


async def run_step(
    client: AsyncClient,
    *,
    target_rps: float,
    duration_s: float,
    lang: str,
) -> StepOutcome:
    """한 계단을 open-loop로 발사합니다."""
    url = f"/v1/catalog/full?lang={lang}"
    interval_ns = int(1e9 / target_rps)
    scheduled = max(1, int(target_rps * duration_s))

    client_ns: list[int] = []
    server_ns: list[int] = []
    hit_ns: list[int] = []
    miss_ns: list[int] = []
    errors = 0
    late_starts = 0
    late_ns: list[int] = []

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
        client_ns.append(elapsed)
        raw_server = response.headers.get(SERVER_TIMING_HEADER)
        if raw_server:
            server_ns.append(int(raw_server))
        if response.headers.get(CACHE_HEADER) == "hit":
            hit_ns.append(elapsed)
        else:
            miss_ns.append(elapsed)

    cpu_start = cpu_seconds()
    wall_start = time.perf_counter_ns()
    tasks: list[asyncio.Task[None]] = []
    started_count = 0

    for index in range(scheduled):
        due = wall_start + index * interval_ns
        now = time.perf_counter_ns()
        if now < due:
            await asyncio.sleep((due - now) / 1e9)
        else:
            # 목표 시각을 이미 지났습니다 — 발생기가 따라가지 못하고 있습니다.
            behind = now - due
            if behind > interval_ns:
                late_starts += 1
                late_ns.append(behind)
        tasks.append(asyncio.create_task(one_request()))
        started_count += 1

    await asyncio.gather(*tasks, return_exceptions=True)
    wall_ns = time.perf_counter_ns() - wall_start
    generator_cpu = cpu_seconds() - cpu_start

    return StepOutcome(
        target_rps=target_rps,
        duration_s=wall_ns / 1e9,
        scheduled=scheduled,
        started=started_count,
        completed=len(client_ns),
        errors=errors,
        dropped=scheduled - started_count,
        late_starts=late_starts,
        late_ns=late_ns,
        client_ns=client_ns,
        server_ns=server_ns,
        hit_client_ns=hit_ns,
        miss_client_ns=miss_ns,
        generator_cpu_s=generator_cpu,
        max_rss_bytes=max_rss_bytes(),
    )


async def measure_baseline(
    client: AsyncClient, *, lang: str, requests: int
) -> dict[str, int]:
    """부하 전 단건 기준선입니다. 꺾임 판정(p95 2배)의 기준입니다."""
    url = f"/v1/catalog/full?lang={lang}"
    samples: list[int] = []
    for _ in range(requests):
        started = time.perf_counter_ns()
        response = await client.get(url)
        if response.status_code == 200:
            samples.append(time.perf_counter_ns() - started)
    return {
        "sample_count": len(samples),
        "p50": percentile_ns(samples, 0.50),
        "p95": percentile_ns(samples, 0.95),
    }


async def run_ladder(
    settings: Settings,
    *,
    steps: tuple[float, ...],
    seconds: float,
    lang: str,
    baseline_requests: int,
    warmup: int,
) -> dict[str, object]:
    """한 구성(캐시 on 또는 off)으로 계단 전체를 돕니다."""
    app = create_app(settings)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport, base_url="http://catalog-hub.local", timeout=60
        ) as client:
            url = f"/v1/catalog/full?lang={lang}"
            for _ in range(warmup):
                await client.get(url)

            baseline = await measure_baseline(
                client, lang=lang, requests=baseline_requests
            )
            break_threshold = int(baseline["p95"] * BREAK_P95_MULTIPLE)

            outcomes: list[dict[str, object]] = []
            stop = StopCondition()
            break_rps: float | None = None

            for target in steps:
                outcome = await run_step(
                    client, target_rps=target, duration_s=seconds, lang=lang
                )
                summary = outcome.summary()
                client_stats = summary.get("client_ns")
                p95 = (
                    int(client_stats["p95"]) if isinstance(client_stats, dict) else None
                )
                summary["p95_vs_baseline"] = (
                    round(p95 / baseline["p95"], 3) if p95 else None
                )
                summary["broke"] = bool(p95 and p95 > break_threshold)
                outcomes.append(summary)

                stop.check(
                    "error_rate",
                    outcome.error_rate() > MAX_ERROR_RATE,
                    f"{outcome.error_rate():.4f} > {MAX_ERROR_RATE} at {target} RPS",
                )
                if stop.tripped:
                    break
                if summary["broke"]:
                    break_rps = target
                    break

            return {
                "cache_enabled": settings.response_cache_enabled,
                "baseline_ns": baseline,
                "break_threshold_ns": break_threshold,
                "steps": outcomes,
                "break_rps": break_rps,
                "stopped": stop.tripped,
                "stop_reasons": stop.reasons,
            }


def fine_steps(break_rps: float, steps: tuple[float, ...]) -> tuple[float, ...]:
    """꺾인 계단과 직전 계단 사이를 0.5 RPS 간격으로 나눕니다 (D5 2단계)."""
    index = steps.index(break_rps)
    if index == 0:
        return ()
    previous = steps[index - 1]
    values: list[float] = []
    current = previous + FINE_INTERVAL_RPS
    while current < break_rps:
        values.append(round(current, 2))
        current += FINE_INTERVAL_RPS
    return tuple(values)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.loadgen",
        description=(
            "계단식 부하 발생기입니다 (D5 정정판: 1,2,4,6,8,12 RPS). 기본은 dry-run "
            "이며 --execute 없이는 부하를 발생시키지 않습니다. SLO 판정을 하지 않습니다."
        ),
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--lang", default=DEFAULT_LANG)
    parser.add_argument("--cache", choices=("off", "on"), default="off")
    parser.add_argument("--seconds", type=float, default=COARSE_SECONDS)
    parser.add_argument("--baseline-requests", type=int, default=20)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--fine", action="store_true", help="세분화 계단으로 실행")
    parser.add_argument("--fine-from", type=float, default=None)
    parser.add_argument("--fine-to", type=float, default=None)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    return parser


def resolve_steps(args: argparse.Namespace) -> tuple[float, ...]:
    if not args.fine:
        return COARSE_STEPS
    if args.fine_from is None or args.fine_to is None:
        raise SystemExit("--fine requires --fine-from and --fine-to")
    values: list[float] = []
    current = args.fine_from + FINE_INTERVAL_RPS
    while current < args.fine_to:
        values.append(round(current, 2))
        current += FINE_INTERVAL_RPS
    return tuple(values)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    steps = resolve_steps(args)

    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "cache": args.cache,
                    "steps_rps": list(steps),
                    "seconds_per_step": args.seconds,
                    "model": "open-loop arrival rate",
                    "stop_conditions": {
                        "error_rate_above": MAX_ERROR_RATE,
                        "p95_break_multiple": BREAK_P95_MULTIPLE,
                    },
                    "note": (
                        "--execute 에서만 부하를 발생시킵니다. 관측값만 보고하며 "
                        "SLO 판정을 하지 않습니다 (D6)."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    settings = Settings(
        _env_file=None,
        db_url=args.db_url,
        response_cache_enabled=(args.cache == "on"),
        stage_timing_enabled=False,
    )
    result = asyncio.run(
        run_ladder(
            settings,
            steps=steps,
            seconds=args.seconds,
            lang=args.lang,
            baseline_requests=args.baseline_requests,
            warmup=args.warmup,
        )
    )
    result["platform"] = sys.platform
    result["python"] = sys.version.split()[0]
    result["client"] = "in-process ASGI (httpx2 ASGITransport) — 실제 네트워크 아님"

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
