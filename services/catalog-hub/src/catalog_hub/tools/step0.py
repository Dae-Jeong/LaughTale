"""Step 0 — 요청 지연 구간 분해 (Laughtale 캐싱 실험).

측정하는 것:

    클라이언트 전체 지연 (L3-in-process)
      = 네트워크·프레임워크 바깥 구간
      + 서버 처리 시간 (X-Server-Total-Ns 헤더)
          = 라우팅 + release조회 + DB쿼리 + 조립/i18n + 직렬화

**타이머 비용 보정**: 같은 회차 구성으로 타이머 on/off 두 벌을 돌리고 차이를
보고합니다. 구간 값과 전체 서버 시간은 **같은 요청 안에서 쌍으로** 기록하므로,
별도 회차 간 p95 차이로 타이머 비용을 귀속하지 않습니다.

**한계**: 여기서 쓰는 클라이언트는 인프로세스 ASGI입니다. 실제 TCP 왕복이 아니므로
"네트워크" 구간은 ASGI·TestClient 오버헤드이며 실제 네트워크 지연이 아닙니다.
실제 HTTP 측정은 별도 승인 범위입니다.

기본은 dry-run이며 `--execute` 없이는 측정하지 않습니다.
"""

import argparse
import json
import resource
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from catalog_hub.bootstrap.app import create_app
from catalog_hub.core.settings import DEFAULT_LANG, Settings
from catalog_hub.core.stage_timer import (
    STAGE_ORDER,
    STAGES_SURVIVING_CACHE_HIT,
)
from catalog_hub.routers.catalog import SERVER_TIMING_HEADER, STAGE_HEADER

DEFAULT_DB_URL = "postgresql+asyncpg://thready:thready@127.0.0.1:5433/catalog_hub_lab"


def max_rss_bytes() -> int:
    """플랫폼별 단위를 맞춘 최대 RSS입니다. darwin은 byte, linux는 KiB입니다."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw if sys.platform == "darwin" else raw * 1024


def percentile_ns(samples: list[int], fraction: float) -> int:
    """nearest-rank percentile입니다. 평균을 percentile이라고 부르지 않습니다."""
    if not samples:
        raise ValueError("no samples")
    ordered = sorted(samples)
    rank = max(1, min(len(ordered), -(-int(fraction * len(ordered) * 1000) // 1000)))
    return ordered[rank - 1]


@dataclass(slots=True)
class RoundResult:
    """한 회차의 결과입니다. 구간과 전체를 쌍으로 보관합니다."""

    timing_enabled: bool
    client_total_ns: list[int]
    server_total_ns: list[int]
    stages_ns: dict[str, list[int]]
    errors: int
    max_rss_bytes: int

    def summary(self) -> dict[str, object]:
        client = self.client_total_ns
        server = self.server_total_ns
        result: dict[str, object] = {
            "timing_enabled": self.timing_enabled,
            "sample_count": len(client),
            "errors": self.errors,
            "max_rss_bytes": self.max_rss_bytes,
            "client_total_ns": {
                "p50": percentile_ns(client, 0.50),
                "p95": percentile_ns(client, 0.95),
                "p99": percentile_ns(client, 0.99),
                "median": int(statistics.median(client)),
            },
            "server_total_ns": {
                "p50": percentile_ns(server, 0.50),
                "p95": percentile_ns(server, 0.95),
                "median": int(statistics.median(server)),
            },
        }
        # 네트워크·프레임워크 바깥 구간 = 클라이언트 전체 - 서버 처리.
        outside = [c - s for c, s in zip(client, server, strict=True)]
        result["outside_server_ns"] = {
            "median": int(statistics.median(outside)),
            "p95": percentile_ns(outside, 0.95),
        }
        if self.stages_ns:
            stage_summary: dict[str, object] = {}
            for stage in STAGE_ORDER:
                values = self.stages_ns.get(stage)
                if values:
                    stage_summary[stage] = {
                        "median": int(statistics.median(values)),
                        "p95": percentile_ns(values, 0.95),
                        "share_of_server_pct": round(
                            100 * statistics.median(values) / statistics.median(server),
                            2,
                        ),
                    }
            result["stages_ns"] = stage_summary
        return result


def run_round(
    settings: Settings, *, requests: int, warmup: int, lang: str
) -> RoundResult:
    """앱을 새로 띄워 한 회차를 돌립니다. 회차마다 새 프로세스 상태를 씁니다."""
    app = create_app(settings)
    client_total: list[int] = []
    server_total: list[int] = []
    stages: dict[str, list[int]] = {}
    errors = 0

    with TestClient(app) as client:
        url = f"/v1/catalog/full?lang={lang}"
        for _ in range(warmup):
            client.get(url)
        client.post("/v1/stats/reset")

        for _ in range(requests):
            started = time.perf_counter_ns()
            response = client.get(url)
            elapsed = time.perf_counter_ns() - started
            if response.status_code != 200:
                errors += 1
                continue
            client_total.append(elapsed)
            server_total.append(int(response.headers[SERVER_TIMING_HEADER]))
            raw_stages = response.headers.get(STAGE_HEADER)
            if raw_stages:
                for stage, value in json.loads(raw_stages).items():
                    stages.setdefault(stage, []).append(value)

    return RoundResult(
        timing_enabled=settings.stage_timing_enabled,
        client_total_ns=client_total,
        server_total_ns=server_total,
        stages_ns=stages,
        errors=errors,
        max_rss_bytes=max_rss_bytes(),
    )


def server_median_ns(round_summary: dict[str, object]) -> int:
    """회차 요약에서 서버 처리 median을 꺼냅니다.

    요약이 `dict[str, object]`이므로 중첩 접근을 한 곳에 모아 타입을 좁힙니다.
    """
    server = round_summary["server_total_ns"]
    if not isinstance(server, dict):
        raise TypeError("server_total_ns must be a mapping")
    return int(server["median"])


def cache_ceiling(
    stage_summary: dict[str, object], server_median: int
) -> dict[str, object]:
    """캐시 이득 상한입니다.

    D1·D4에 따라 라우팅·release조회·직렬화는 캐시 hit에도 남으므로 제외하고,
    DB쿼리 + 조립/i18n만 상한으로 잡습니다.
    """
    removable = 0
    surviving = 0
    for stage in STAGE_ORDER:
        entry = stage_summary.get(stage)
        if not isinstance(entry, dict):
            continue
        median = int(entry["median"])
        if stage in STAGES_SURVIVING_CACHE_HIT:
            surviving += median
        else:
            removable += median
    return {
        "removable_ns_median": removable,
        "surviving_ns_median": surviving,
        "ceiling_pct_of_server": (
            round(100 * removable / server_median, 2) if server_median else None
        ),
        "note": (
            "상한은 DB쿼리+조립/i18n입니다. 라우팅·release조회·직렬화는 캐시 hit에도 "
            "남으므로 제외했습니다 (D1·D4). 실제 이득은 이보다 작습니다."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m catalog_hub.tools.step0",
        description=(
            "Step 0 구간 분해입니다. 기본은 dry-run이며 --execute 없이는 측정하지 "
            "않습니다. 부하 발생·다중 인스턴스는 이 도구의 범위가 아닙니다."
        ),
    )
    parser.add_argument("--db-url", default=DEFAULT_DB_URL, help="합성 DB 접속 URL")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="조회 언어")
    parser.add_argument("--requests", type=int, default=200, help="회차당 측정 요청 수")
    parser.add_argument("--warmup", type=int, default=20, help="측정 밖 warmup 요청 수")
    parser.add_argument(
        "--rounds", type=int, default=3, help="타이머 on/off 각 반복 수"
    )
    parser.add_argument("--execute", action="store_true", help="실제로 측정합니다")
    parser.add_argument("--out", type=Path, default=None, help="결과 JSON 기록 경로")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.execute:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "db_url": args.db_url.rsplit("/", 1)[-1],
                    "lang": args.lang,
                    "requests_per_round": args.requests,
                    "warmup": args.warmup,
                    "rounds_per_mode": args.rounds,
                    "modes": ["timing_off", "timing_on"],
                    "stages": list(STAGE_ORDER),
                    "note": (
                        "측정은 --execute 에서만 실행합니다. SLO 판정을 하지 않으며 "
                        "관측값만 보고합니다 (D6)."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    rounds: dict[str, list[dict[str, object]]] = {"timing_off": [], "timing_on": []}
    for enabled in (False, True):
        key = "timing_on" if enabled else "timing_off"
        for _ in range(args.rounds):
            settings = Settings(
                _env_file=None,
                db_url=args.db_url,
                stage_timing_enabled=enabled,
                response_cache_enabled=False,
            )
            result = run_round(
                settings, requests=args.requests, warmup=args.warmup, lang=args.lang
            )
            rounds[key].append(result.summary())

    off_medians = [server_median_ns(r) for r in rounds["timing_off"]]
    on_medians = [server_median_ns(r) for r in rounds["timing_on"]]
    off_median = int(statistics.median(off_medians))
    on_median = int(statistics.median(on_medians))

    last_on = rounds["timing_on"][-1]
    raw_stage_summary = last_on.get("stages_ns")
    stage_summary: dict[str, object] = (
        raw_stage_summary if isinstance(raw_stage_summary, dict) else {}
    )

    # 타이머 비용은 회차 변동폭과 비교해야 의미가 있습니다. 차이가 변동폭보다
    # 작으면 "측정 한계 미만"이며, 음수로 나와도 타이머가 시간을 줄였다는 뜻이
    # 아닙니다. 부호를 그대로 보고하되 해석을 함께 남깁니다.
    off_spread = max(off_medians) - min(off_medians)
    timer_delta = on_median - off_median
    below_noise = abs(timer_delta) < off_spread

    payload: dict[str, object] = {
        "mode": "execute",
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "client": "in-process ASGI (TestClient) — 실제 네트워크 왕복 아님",
        "rounds": rounds,
        "timer_overhead": {
            "server_median_ns_timing_off": off_median,
            "server_median_ns_timing_on": on_median,
            "delta_ns": timer_delta,
            "delta_pct": (
                round(100 * timer_delta / off_median, 2) if off_median else None
            ),
            "timing_off_spread_ns": off_spread,
            "below_noise_floor": below_noise,
            "interpretation": (
                "타이머 비용이 회차 변동폭보다 작아 측정되지 않았습니다. "
                "음수는 타이머가 빨라졌다는 뜻이 아니라 노이즈입니다."
                if below_noise
                else "타이머 비용이 회차 변동폭을 넘습니다. 보정이 필요합니다."
            ),
        },
        "round_spread_pct": {
            "timing_off": (
                round(100 * off_spread / off_median, 2) if off_median else None
            ),
            "timing_on": (
                round(100 * (max(on_medians) - min(on_medians)) / on_median, 2)
                if on_median
                else None
            ),
        },
        "cache_ceiling": cache_ceiling(stage_summary, on_median)
        if stage_summary
        else None,
        "limits": [
            "인프로세스 ASGI이며 실제 네트워크 왕복이 아닙니다.",
            "워커 1개 고정 조건입니다 (D2). 워커 증설로도 처리량은 늘 수 있습니다.",
            "SLO 판정이 아니라 관측값입니다 (D6).",
        ],
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
