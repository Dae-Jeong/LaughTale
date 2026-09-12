"""CPU·메모리·지연 계측 도구 (LAUGH-KNOWLEDGE-READ-001).

서비스 core를 그대로 재사용합니다. 구현을 두 경로에 복제하지 않습니다.
인위적 지연·불필요한 작업으로 캐시 이득을 만들지 않습니다.
기본은 dry-run이며 `--execute` 없이는 측정 회차를 돌리지 않습니다.

판정 한계: 여기서 얻은 수치는 단일 프로세스·동시성1의 합성 조회 비용이며
실제 DB/API·운영 성능·대규모 처리량으로 일반화하지 않습니다.
"""

import argparse
import asyncio
import json
import resource
import sys
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path

from theme_catalog.core.cache import DEFAULT_CACHE_CAPACITY
from theme_catalog.core.catalog import ThemeCatalog, build_catalog
from theme_catalog.core.fixture import (
    DEFAULT_FIXTURE_PATH,
    Fixture,
    FixtureError,
    load_fixture,
)

KEY_PATTERNS = ("cycle32", "hot4")


def key_sequence(
    theme_ids: tuple[str, ...], *, pattern: str, count: int
) -> tuple[str, ...]:
    """측정용 key 순서입니다. hot4와 순환32는 서로 다른 조건이므로 분리합니다."""
    if not theme_ids:
        raise ValueError("theme_ids must not be empty")
    if pattern == "cycle32":
        pool = theme_ids
    elif pattern == "hot4":
        pool = theme_ids[:4]
    else:
        raise ValueError(f"unknown key pattern: {pattern}")
    return tuple(pool[index % len(pool)] for index in range(count))


def max_rss_bytes() -> int:
    """플랫폼별 단위를 맞춘 최대 RSS입니다. darwin은 byte, linux는 KiB입니다."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


@dataclass(frozen=True, slots=True)
class RunResult:
    """한 회차의 결과입니다. percentile은 raw sample에서 직접 구합니다."""

    cache_enabled: bool
    key_pattern: str
    sample_count: int
    samples_ns: tuple[int, ...]
    wall_ns: int
    cpu_ns: int
    counters: dict[str, object]
    timed: bool
    update_every: int
    max_rss_bytes: int
    traced_peak_bytes: int | None

    def percentile_ns(self, fraction: float) -> int:
        """nearest-rank percentile입니다. 평균을 percentile이라고 부르지 않습니다."""
        if not self.samples_ns:
            raise ValueError("no samples")
        ordered = sorted(self.samples_ns)
        rank = max(
            1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000))
        )
        return ordered[rank - 1]

    def summary(self) -> dict[str, object]:
        """보고용 요약입니다. 표본이 없으면 batch 평균만 남깁니다."""
        result: dict[str, object] = {
            "cache_enabled": self.cache_enabled,
            "key_pattern": self.key_pattern,
            "sample_count": self.sample_count,
            "timed_samples": self.timed,
            "update_every": self.update_every,
            "wall_ns": self.wall_ns,
            "cpu_ns": self.cpu_ns,
            "cpu_ns_per_read": (
                self.cpu_ns / self.sample_count if self.sample_count else None
            ),
            "batch_mean_wall_ns": (
                self.wall_ns / self.sample_count if self.sample_count else None
            ),
            "max_rss_bytes": self.max_rss_bytes,
            "traced_peak_bytes": self.traced_peak_bytes,
            "counters": self.counters,
        }
        if self.timed and self.samples_ns:
            result["p50_ns"] = self.percentile_ns(0.50)
            result["p95_ns"] = self.percentile_ns(0.95)
            result["min_ns"] = min(self.samples_ns)
            result["max_ns"] = max(self.samples_ns)
        return result


async def run_reads(
    catalog: ThemeCatalog,
    keys: tuple[str, ...],
    *,
    timed: bool,
    update_every: int = 0,
    trace_memory: bool = False,
) -> RunResult:
    """조회 회차입니다.

    `timed=False`는 계측 off 회차이며 batch 비용만 잽니다. 개별 조회가 clock 비용과
    비슷할 때 이 회차로 보완하되 batch 평균을 요청 p95라고 부르지 않습니다.

    `update_every`>0이면 완료 조회 N회마다 한 key를 갱신합니다. 조회 전용 조건과
    분리해 실행합니다.

    `trace_memory`는 오버헤드가 있으므로 별도 메모리 측정 회차에서만 켭니다.
    """
    samples: list[int] = []
    completed = 0
    update_index = 0
    theme_ids = catalog.store.theme_ids()

    if trace_memory:
        tracemalloc.start()

    wall_start = time.perf_counter_ns()
    cpu_start = time.process_time_ns()
    if timed:
        for theme_id in keys:
            started = time.perf_counter_ns()
            await catalog.get(theme_id)
            samples.append(time.perf_counter_ns() - started)
            completed += 1
            if update_every and completed % update_every == 0:
                target = theme_ids[update_index % len(theme_ids)]
                update_index += 1
                await catalog.update(target, spacing_px=(update_index * 3) % 65)
    else:
        for theme_id in keys:
            await catalog.get(theme_id)
            completed += 1
            if update_every and completed % update_every == 0:
                target = theme_ids[update_index % len(theme_ids)]
                update_index += 1
                await catalog.update(target, spacing_px=(update_index * 3) % 65)
    cpu_ns = time.process_time_ns() - cpu_start
    wall_ns = time.perf_counter_ns() - wall_start

    traced_peak: int | None = None
    if trace_memory:
        _, traced_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

    return RunResult(
        cache_enabled=catalog.cache_enabled,
        key_pattern="",
        sample_count=completed,
        samples_ns=tuple(samples),
        wall_ns=wall_ns,
        cpu_ns=cpu_ns,
        counters=catalog.counters.snapshot(),
        timed=timed,
        update_every=update_every,
        max_rss_bytes=max_rss_bytes(),
        traced_peak_bytes=traced_peak,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m theme_catalog.tools.measure",
        description=(
            "theme-catalog 조회 비용 계측 도구 (LAUGH-KNOWLEDGE-READ-001). "
            "기본은 dry-run이며 --execute 없이는 측정 회차를 실행하지 않습니다."
        ),
    )
    parser.add_argument(
        "--fixture", type=Path, default=DEFAULT_FIXTURE_PATH, help="합성 fixture 경로"
    )
    parser.add_argument(
        "--cache", choices=("off", "on"), default="off", help="비교용 캐시 사용 여부"
    )
    parser.add_argument(
        "--cache-capacity",
        type=int,
        default=DEFAULT_CACHE_CAPACITY,
        help="bounded LRU 항목 수",
    )
    parser.add_argument(
        "--key-pattern", choices=KEY_PATTERNS, default="cycle32", help="key 분포"
    )
    parser.add_argument("--warmup", type=int, default=0, help="측정 밖 warmup 조회 수")
    parser.add_argument("--reads", type=int, default=10_000, help="측정 조회 수")
    parser.add_argument(
        "--timing",
        choices=("per-read", "batch"),
        default="per-read",
        help="per-read는 계측 on, batch는 계측 off 회차",
    )
    parser.add_argument(
        "--update-every",
        type=int,
        default=0,
        help="완료 조회 N회마다 한 key 갱신 (0이면 조회 전용 조건)",
    )
    parser.add_argument(
        "--trace-memory",
        action="store_true",
        help="tracemalloc 회차. 오버헤드가 있으므로 지연 회차와 분리합니다.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="실제 측정 회차를 실행합니다. 없으면 계획만 출력하는 dry-run입니다.",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="결과 JSON 기록 경로 (--execute와 함께)"
    )
    return parser


def dry_run_plan(args: argparse.Namespace, fixture: Fixture) -> dict[str, object]:
    """실행 없이 조건만 확인합니다. fixture 구조 오류는 여기서 이미 거절됩니다."""
    return {
        "mode": "dry-run",
        "fixture_path": str(args.fixture),
        "fixture_version": fixture.fixture_version,
        "seed": fixture.seed,
        "theme_count": len(fixture.themes),
        "fixture_update_count": len(fixture.updates),
        "cache": args.cache,
        "cache_capacity": args.cache_capacity if args.cache == "on" else None,
        "key_pattern": args.key_pattern,
        "distinct_keys": 4 if args.key_pattern == "hot4" else len(fixture.themes),
        "warmup": args.warmup,
        "reads": args.reads,
        "timing": args.timing,
        "update_every": args.update_every,
        "trace_memory": args.trace_memory,
        "external_calls": 0,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "baseline_max_rss_bytes": max_rss_bytes(),
        "note": (
            "측정은 --execute 에서만 실행합니다. "
            "성능 채택 임계치는 미확정이며 이 도구가 PASS를 판정하지 않습니다."
        ),
    }


async def execute(args: argparse.Namespace, fixture: Fixture) -> dict[str, object]:
    """한 회차를 실행합니다. 제어 hook 없이 정상 경로만 사용합니다."""
    catalog = build_catalog(
        fixture.themes,
        cache_enabled=(args.cache == "on"),
        capacity=args.cache_capacity,
    )
    theme_ids = catalog.store.theme_ids()

    if args.warmup:
        warmup_keys = key_sequence(
            theme_ids, pattern=args.key_pattern, count=args.warmup
        )
        await run_reads(catalog, warmup_keys, timed=False)
        # warmup 계수는 측정 회차와 섞지 않습니다.
        catalog.reset_counters()

    keys = key_sequence(theme_ids, pattern=args.key_pattern, count=args.reads)
    result = await run_reads(
        catalog,
        keys,
        timed=(args.timing == "per-read"),
        update_every=args.update_every,
        trace_memory=args.trace_memory,
    )
    summary = result.summary()
    summary["key_pattern"] = args.key_pattern
    summary["platform"] = sys.platform
    summary["python"] = sys.version.split()[0]
    summary["units"] = {"time": "ns", "memory": "bytes"}
    payload: dict[str, object] = {"mode": "execute", "summary": summary}
    if args.timing == "per-read":
        payload["samples_ns"] = list(result.samples_ns)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        fixture = load_fixture(args.fixture)
    except FixtureError as error:
        print(f"fixture rejected: {error}", file=sys.stderr)
        return 2

    if not args.execute:
        print(json.dumps(dry_run_plan(args, fixture), indent=2, ensure_ascii=False))
        return 0

    payload = asyncio.run(execute(args, fixture))
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"raw written to {args.out}", file=sys.stderr)
    print(json.dumps(payload["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
