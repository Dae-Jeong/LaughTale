"""캐시 메모리 실측과 release 전환 민감도 (D3·D7).

D3 — "27MB라 전량 상주 가능 → 용량 관리 불필요"는 **잠정 가설**입니다.
27MB는 DB 크기이지 응답 캐시 크기가 아닙니다. 여기서 실측합니다:
    엔트리 1개당 메모리 / 원본 DB 대비 배율 / 프로세스 RSS 증가분
중단 임계는 **250 MiB** (기준 RSS 124.7 MiB의 2배, 관리 확정)입니다.
넘으면 실패가 아니라 **lang 선별(ko 우선) 또는 endpoint 선별로 축소 후 재측정** 신호입니다.

D7 — release 전환 실제 빈도는 확인 불가이므로 **민감도**만 잽니다:
    전환 직후 cold 구간 지속시간 / DB 쿼리 스파이크 / p99 영향
빈도를 몰라도 "시간당 N회면 총영향 = N x (스파이크 비용)"으로 외삽합니다.

부하를 발생시키지 않습니다. 측정은 `--execute`에서만 수행합니다.
"""

import argparse
import asyncio
import gc
import json
import resource
import statistics
import sys
import time
from pathlib import Path

from httpx2 import ASGITransport, AsyncClient

from catalog_hub.bootstrap.app import create_app
from catalog_hub.core.settings import SUPPORTED_LANGS, Settings
from catalog_hub.routers.catalog import CACHE_HEADER, SERVER_TIMING_HEADER

DEFAULT_DB_URL = "postgresql+asyncpg://thready:thready@127.0.0.1:5433/catalog_hub_lab"

# 관리 확정 중단 임계입니다 (기준 RSS 124.7 MiB의 2배).
RSS_LIMIT_BYTES = 250 * 1024 * 1024
BASELINE_RSS_BYTES = int(124.7 * 1024 * 1024)

# 원본 합성 DB 실측 크기입니다. 배율 계산의 분모입니다.
SOURCE_DB_BYTES = 25_099_287


def max_rss_bytes() -> int:
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def deep_size(obj: object, seen: set[int] | None = None) -> int:
    """객체 그래프의 대략적 바이트입니다.

    `sys.getsizeof`는 컨테이너 자체만 세므로 자식을 따라갑니다. 공유 객체를 두 번
    세지 않도록 id로 방문 표시합니다. **근사치**이며 할당자 오버헤드·내부 조각화는
    반영되지 않습니다. RSS 증가분을 함께 보고해 교차 확인합니다.
    """
    if seen is None:
        seen = set()
    marker = id(obj)
    if marker in seen:
        return 0
    seen.add(marker)
    size = sys.getsizeof(obj)
    if isinstance(obj, str | bytes | int | float | bool) or obj is None:
        return size
    if isinstance(obj, dict):
        for key, value in obj.items():
            size += deep_size(key, seen) + deep_size(value, seen)
        return size
    if isinstance(obj, list | tuple | set | frozenset):
        for item in obj:
            size += deep_size(item, seen)
        return size
    slots = getattr(type(obj), "__slots__", None)
    if slots:
        for name in slots:
            if hasattr(obj, name):
                size += deep_size(getattr(obj, name), seen)
        return size
    attrs = getattr(obj, "__dict__", None)
    if attrs:
        size += deep_size(attrs, seen)
    return size


async def measure_cache_memory(
    settings: Settings, *, langs: tuple[str, ...]
) -> dict[str, object]:
    """lang을 하나씩 채우며 엔트리당 메모리와 RSS 증가분을 잽니다."""
    app = create_app(settings)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=transport, base_url="http://catalog-hub.local", timeout=60
        ) as client:
            gc.collect()
            rss_before = max_rss_bytes()
            cache = app.state.response_cache
            if cache is None:
                raise RuntimeError(
                    "response cache must be enabled for this measurement"
                )

            progression: list[dict[str, object]] = []
            cache_bytes = 0
            rss_final = rss_before
            for index, lang in enumerate(langs, start=1):
                await client.get(f"/v1/catalog/full?lang={lang}")
                gc.collect()
                cache_bytes = deep_size(cache.entries)
                rss_final = max_rss_bytes()
                progression.append(
                    {
                        "langs_cached": index,
                        "lang": lang,
                        "entries": len(cache),
                        "cache_deep_bytes": cache_bytes,
                        "bytes_per_entry": cache_bytes // max(1, len(cache)),
                        "rss_bytes": rss_final,
                        "rss_delta_from_start": rss_final - rss_before,
                    }
                )
            return {
                "rss_before_bytes": rss_before,
                "progression": progression,
                "entries_final": len(cache),
                "cache_deep_bytes": cache_bytes,
                "bytes_per_entry": cache_bytes // max(1, len(cache)),
                "source_db_bytes": SOURCE_DB_BYTES,
                "cache_to_db_ratio": round(cache_bytes / SOURCE_DB_BYTES, 4),
                "rss_final_bytes": rss_final,
                "rss_delta_bytes": rss_final - rss_before,
                "rss_limit_bytes": RSS_LIMIT_BYTES,
                "rss_limit_exceeded": rss_final > RSS_LIMIT_BYTES,
                "baseline_rss_bytes": BASELINE_RSS_BYTES,
            }


async def measure_release_transition(
    settings: Settings, *, lang: str, probes: int
) -> dict[str, object]:
    """release 전환 직후 cold 구간을 잽니다 (D7 민감도).

    전환은 시험용 oracle 함수를 재사용합니다 — 구현이 아니라 DB를 직접 바꿉니다.
    """
    from catalog_hub.tools.release_transition import (  # noqa: PLC0415
        publish_successor_release,
        rollback_to_release,
    )

    app = create_app(settings)
    transport = ASGITransport(app=app)
    transition: tuple[int, int] | None = None

    try:
        async with app.router.lifespan_context(app):
            async with AsyncClient(
                transport=transport, base_url="http://catalog-hub.local", timeout=60
            ) as client:
                url = f"/v1/catalog/full?lang={lang}"
                cache = app.state.response_cache

                # 전환 전 warm 상태를 만듭니다.
                await client.get(url)
                warm_samples: list[int] = []
                for _ in range(probes):
                    started = time.perf_counter_ns()
                    response = await client.get(url)
                    warm_samples.append(time.perf_counter_ns() - started)
                    assert response.headers[CACHE_HEADER] == "hit"

                loads_before = 0
                if cache is not None:
                    loads_before = cache.misses

                transition = await publish_successor_release("d7")

                # 전환 직후 요청들을 관측합니다.
                post: list[dict[str, object]] = []
                post_ns: list[int] = []
                cold_ns = 0
                cold_requests = 0
                for index in range(probes):
                    started = time.perf_counter_ns()
                    response = await client.get(url)
                    elapsed = time.perf_counter_ns() - started
                    post_ns.append(elapsed)
                    is_hit = response.headers[CACHE_HEADER] == "hit"
                    if not is_hit:
                        cold_ns += elapsed
                        cold_requests += 1
                    post.append(
                        {
                            "index": index,
                            "cache": response.headers[CACHE_HEADER],
                            "client_ns": elapsed,
                            "server_ns": int(response.headers[SERVER_TIMING_HEADER]),
                        }
                    )

                misses_after = cache.misses - loads_before if cache is not None else 0
                entries_after = len(cache) if cache is not None else 0
                release_ids = list(cache.release_ids()) if cache is not None else []

                warm_p50 = int(statistics.median(warm_samples))
                warm_p99 = sorted(warm_samples)[
                    max(0, int(0.99 * len(warm_samples)) - 1)
                ]
                post_p99 = sorted(post_ns)[max(0, int(0.99 * len(post_ns)) - 1)]

                return {
                    "probes": probes,
                    "warm_before": {"p50_ns": warm_p50, "p99_ns": warm_p99},
                    "cold_requests_after_transition": cold_requests,
                    "cold_total_ns": cold_ns,
                    "extra_db_queries": misses_after,
                    "post_transition": post[: min(10, len(post))],
                    "post_p99_ns": post_p99,
                    "p99_inflation_vs_warm": (
                        round(post_p99 / warm_p99, 2) if warm_p99 else None
                    ),
                    "cache_entries_after": entries_after,
                    "release_ids_after": release_ids,
                    "note": (
                        "cold 구간은 lang 1종 기준입니다. 전량(7 lang)이면 "
                        "cold 비용은 lang 수만큼 곱해집니다. 전환 빈도를 몰라도 "
                        "'시간당 N회 x 이 비용'으로 외삽합니다 (D7)."
                    ),
                }
    finally:
        if transition is not None:
            old_id, new_id = transition
            await rollback_to_release(old_id, new_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.cachemem",
        description=(
            "캐시 메모리 실측(D3)과 release 전환 민감도(D7)입니다. 부하를 발생시키지 "
            "않으며 --execute 없이는 측정하지 않습니다."
        ),
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--lang", default="ko")
    parser.add_argument("--probes", type=int, default=20)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "measures": ["cache memory (D3)", "release transition (D7)"],
                    "langs": list(SUPPORTED_LANGS),
                    "rss_limit_bytes": RSS_LIMIT_BYTES,
                    "rss_limit_mib": RSS_LIMIT_BYTES // (1024 * 1024),
                    "note": "--execute 에서만 측정합니다. 부하는 발생시키지 않습니다.",
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    settings = Settings(
        _env_file=None,
        db_url=args.db_url,
        response_cache_enabled=True,
        stage_timing_enabled=False,
    )
    memory = asyncio.run(measure_cache_memory(settings, langs=SUPPORTED_LANGS))
    transition = asyncio.run(
        measure_release_transition(settings, lang=args.lang, probes=args.probes)
    )
    payload = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "d3_cache_memory": memory,
        "d7_release_transition": transition,
    }
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"raw written to {args.out}", file=sys.stderr)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
