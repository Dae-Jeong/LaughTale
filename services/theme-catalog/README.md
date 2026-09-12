# theme-catalog

합성 테마 속성의 단건 조회·드문 갱신을 다루는 **독립 실험 서비스**입니다.
task: `LAUGH-KNOWLEDGE-READ-001`.

자주 읽고 드물게 수정하는 데이터의 조회 비용, 로컬 캐시, 최신성, 재현 가능한 성능
검증만 다룹니다. 실제 채팅 앱·제품 API에 연결하지 않고 `chat_service`를 runtime
import하지 않습니다. fixture의 theme_id·색상·간격은 전부 임의 생성 값이며 회사
원자료와 무관합니다.

## 현재 상태

| 항목 | 상태 |
| --- | --- |
| 원본(dict)·bounded 로컬 캐시 on/off | 구현·기능 검증 완료 |
| 갱신 무효화·늦은 fill 방어·취소/오류 정리 | 구현·기능 검증 완료 |
| 독립 oracle 대조·negative control | 통과 (주입 결함 8종 검출 확인) |
| FastAPI factory/lifespan·인프로세스 ASGI 시험 | 구현·기능 검증 완료 |
| 원본·HTTP 입력 계약 일치 (정수/색상) | 불일치 재현 후 수정·검증 완료 |
| 원본 상태 소유권 (관측으로 변경 불가) | 구현·기능 검증 완료 |
| F0 기준선(cache-off)·측정 유효성 | **보완 이전 코드** 기준 측정 완료, 아래 참고 |
| F1 cache on/off 성능 비교 | **미실행.** 임계치를 관리 검토로 고정한 뒤 수행 |
| 캐시 채택 여부 | **미정.** 자원 절감 증거 없이 채택하지 않음 |

## 중요한 제약

- **갱신은 이 프로세스 수명까지만 유지됩니다.** 원본은 시작 때 합성 fixture를 한 번
  적재한 메모리 dict이며, `PATCH`로 바꾼 값은 프로세스가 끝나면 사라지고 다음 시작 때
  fixture 초기값으로 돌아갑니다. 영속 DB·공유 원본은 이 버전의 범위가 아닙니다.
- **최신성 계약은 단일 프로세스 범위입니다.** 여러 worker·Pod에서는 로컬 lock과 캐시가
  공유되지 않으므로 다중 서버 최신성을 보장하지 않습니다. `run.py`는 `workers=1`입니다.
- Redis·공유 캐시·분산 lock·single-flight는 구현하지 않았습니다. 현재 단건 조회·드문
  갱신 요구에서 원자 연산 지원을 선택 기준으로 삼지 않습니다.
- 이 서비스의 수치는 합성 메모리 조회 비용입니다. 실제 DB/API 성능이나 운영 개선으로
  일반화하지 않습니다.

## 조회·갱신 계약

조회의 선형화 지점은 **유효 cache hit 또는 원본 snapshot 취득 시점**입니다.

- 갱신 성공 뒤 시작된 조회는 새 값을 반환합니다.
- 갱신과 겹친 조회는 그 지점의 순서에 맞는 옛 값 또는 새 값을 허용합니다.
- 응답 직전까지 항상 최신이라는 더 강한 보장은 주장하지 않습니다.

| 항목 | 계약 |
| --- | --- |
| 입력 | `spacing_px`는 정수만 허용. bool·문자열·실수를 변환하지 않으며 원본과 HTTP가 같은 규칙 |
| 원본 | 시작 때 구성한 authoritative dict. 불변 값만 반환하고 내부 dict를 노출하지 않음 |
| 상태 소유 | 값 교체와 카운터 증가는 `apply_update` 하나가 함께 수행. 관측은 live `read_only_view` 또는 detached `copy_of_themes` |
| cache-off | 짧은 공통 lock 아래 dict 단건 조회. 요청별 불필요한 가공 없음 |
| cache-on | 단일 프로세스 bounded LRU, key는 theme_id, 기본 최대 16개 항목 |
| miss | lock 아래 원본 snapshot과 key별 변경 카운터를 취득. fill 직전 같은 카운터를 재확인 |
| update | 같은 lock 아래 값 교체·카운터 증가·해당 key 무효화 |
| 늦은 fill | 옛 카운터의 fill은 거절. 새 값을 옛 snapshot으로 덮지 않음 |
| not-found | 명시적 404. negative caching하지 않음 |
| 실패·취소 | 실패값·옛 값을 캐시에 남기지 않고 lock을 해제. 자동 무한 재시도 없음 |
| TTL | 미사용. 모든 쓰기를 이 프로세스가 소유하므로 명시적 무효화로 검증 |

cache-off와 cache-on은 **같은 공통 lock 아래서 원본을 만집니다.** 캐시에 유리하도록
baseline만 복사·직렬화·지연시키지 않았습니다. 정상 조회 경로에는 I/O await, 전체 파일
재파싱, 전수 스캔, 인위적 sleep이 없습니다. 제어 지연(`ControlHooks`)은 기능 시험에서만
설치되며 성능 회차에서는 전부 `None`입니다.

## HTTP API

| 메서드 | 경로 | 설명 |
| --- | --- | --- |
| `GET` | `/v1/themes/{theme_id}` | 단건 조회. 미등록 ID는 404 `THEME_NOT_FOUND` |
| `PATCH` | `/v1/themes/{theme_id}` | 지정한 속성만 교체. 빈 본문은 422 |
| `GET` | `/v1/stats` | hit/miss/load/eviction·갱신·오류 계수 |
| `GET` | `/health/live` · `/health/ready` | 생존·준비 상태 |

성공 응답은 `{"data": ...}`, 오류는 `{"type","title","status","code","errors"}`입니다.
오류 본문에 입력값·내부 메시지를 넣지 않고 승인한 공개 위치와 고정 코드만 반환합니다.

`/v1/stats`의 계수는 기능 검증과 측정 분모 확인용입니다. **hit 비율이 높다는 사실만으로
자원 효율 개선을 선언하지 않습니다.**

## 실행

의존성은 FastAPI·pydantic-settings·uvicorn입니다. 현재 이 저장소에서 검증에 사용한
인터프리터는 기존에 설치된 `services/chat/.venv` (Python 3.14.7)이며, **새로 설치하거나
기존 환경을 변경하지 않았습니다.** 이 서비스 전용 환경을 따로 만들려면
`services/theme-catalog/`에서 `uv sync`를 실행하면 되지만 아직 수행하지 않았습니다.

```sh
cd services/theme-catalog
export PYTHONPATH=src:tests

# 기능 시험 (126개 + subtest 45) — 둘 다 확인했습니다.
../chat/.venv/bin/python -m pytest tests -q
../chat/.venv/bin/python -m unittest discover -s tests -t tests -p 'test_*.py'

# lint · format · 타입 검사
../chat/.venv/bin/ruff check src tests
../chat/.venv/bin/ruff format --check src tests
../chat/.venv/bin/ty check

# CLI 도움말·dry-run (측정 미실행)
../chat/.venv/bin/python -m theme_catalog.tools.measure --help
../chat/.venv/bin/python -m theme_catalog.tools.measure

# 서버 실행 — 필요할 때만 직접 실행합니다. 아래 명령은 아직 실행하지 않았습니다.
../chat/.venv/bin/python -m theme_catalog.run
```

기본 바인딩은 `127.0.0.1:18092`이며 loopback 실험용입니다. 공개 노출·다중 worker는
범위 밖입니다. 기존에 떠 있는 다른 서비스의 포트를 사용하지 않습니다.

설정은 환경 변수 또는 `.env`로 바꿉니다: `CACHE_ENABLED`, `CACHE_CAPACITY`,
`FIXTURE_PATH`, `SERVER_HOST`, `SERVER_PORT`.

## 측정

```sh
# 계측 on 회차 (요청별 지연 표본)
PYTHONPATH=src ../chat/.venv/bin/python -m theme_catalog.tools.measure \
  --execute --cache off --key-pattern cycle32 --warmup 1000 --reads 10000 \
  --timing per-read --out ../../.artifacts/theme-read/<attempt>/run.json

# 계측 off 회차 (batch 비용)
... --timing batch

# 메모리 회차 (오버헤드가 있으므로 지연 회차와 분리)
... --timing batch --trace-memory
```

`--execute` 없이는 측정을 실행하지 않습니다. raw sample(ns)·계수·CPU·RSS는 `--out`
경로에 남기며 저장소에서는 gitignore된 `.artifacts/theme-read/` 아래에 둡니다.

percentile은 raw sample에서 nearest-rank로 구합니다. **percentile을 평균하지 않고,
batch 평균을 요청 p95라고 부르지 않습니다.**

### 측정 유효성 — F0 결과 (2026-09-12, darwin/arm64, Python 3.14.7)

> **이 수치는 revision6 가독성 보완 이전 코드의 결과입니다.** raw는
> `.artifacts/theme-read/f0-20260912/`에 그대로 보존합니다. 보완 후 코드의 성능으로
> 재사용하지 않으며, 비교가 필요하면 새 회차를 따로 측정해야 합니다. 아래 계측
> 오버헤드·변동폭은 측정 가능성의 경계를 보여 주는 참고값입니다.

1프로세스·동시성1·32테마·cycle32·warmup 1000·측정 10000회·모드별 3회, cache-off.

| 항목 | 값 |
| --- | --- |
| 계측 on CPU/read (중앙값) | 807 ns |
| 계측 off CPU/read (중앙값) | 640 ns |
| 계측 오버헤드 | 167 ns/read (읽기 비용의 약 26%) |
| 회차간 변동폭 (계측 off) | 84 ns/read (약 13%) |
| `perf_counter` 해상도 / 호출쌍 비용 | 41.7 ns / 42 ns |
| 최대 RSS | 약 28 MiB (상한 128 MiB) |
| 전체 소요 | 약 1 초 (상한 120 초) |
| 정합성 위반·오류 | 0 |

**판정 한계:** per-read 계측이 읽기 비용의 약 26%를 차지하고 회차간 변동이 약 13%입니다.
**이보다 작은 자원 차이는 이 조건에서 판정할 수 없습니다.** cache-on 비교에서 차이가 이
변동 범위 안이면 우위 미확인으로 기록하며, 캐시 이득을 만들기 위해 baseline을 느리게
하거나 부하를 확대하지 않습니다.

이 모델은 이미 저렴한 메모리 dict lookup이므로 추가 캐시 계층이 총 CPU·메모리를 줄일
근거가 사전에 없습니다. **캐시가 자원상 무이득이거나 오히려 나쁘다는 결론도 유효한
결과입니다.** 캐시 항목은 원본 dict와 별개의 추가 보관이므로 메모리는 증가하는 방향이며,
hot working-set에서 hit이 늘어도 lock·계수·조회 분기 비용이 함께 듭니다.

## 파일 구조

```
services/theme-catalog/
├── pyproject.toml
├── README.md
├── src/theme_catalog/
│   ├── run.py                      실행 진입점 (프로세스 설정)
│   ├── bootstrap/app.py            create_app — 조립 지점
│   ├── bootstrap/lifespan.py       fixture 적재 → 업무 Theme 조립 → 자원 정리 등록
│   ├── core/theme.py               업무 타입 Theme과 값 규칙 (JSON·HTTP를 모름)
│   ├── core/store.py               authoritative 메모리 원본·상태 소유권
│   ├── core/cache.py               bounded LRU
│   ├── core/catalog.py             조회·갱신 진입점 (ThemeCatalog)
│   ├── core/counters.py            관측용 계수
│   ├── core/control_hooks.py       시험 전용 경쟁 제어 hook과 사용 경계
│   ├── core/fixture.py             합성 fixture 적재 (loader 전용)
│   ├── core/settings.py            설정
│   ├── dependencies/themes.py      provider (app.state 접근은 여기서만)
│   ├── routers/                    themes · stats · health
│   ├── schemas/                    외부 응답/입력 스키마
│   ├── http/errors.py              공개 오류 변환·업무 예외 등록표
│   ├── tools/measure.py            계측 CLI
│   └── fixtures/themes.json        합성 fixture (32테마, seed 41)
└── tests/
    ├── oracle.py                   독립 기대값 reference model
    ├── test_fixture.py             구조 검증·거절
    ├── test_catalog.py             원본·캐시 계약·oracle 대조·negative control
    ├── test_concurrency.py         update barrier·동시 miss·취소·오류 정리
    ├── test_input_contract.py      원본과 HTTP의 정수/색상 계약 대조 (실제 ASGI)
    ├── test_state_ownership.py     관측값으로 원본을 바꿀 수 없는지 확인
    ├── test_measure.py             표본·percentile·CLI help/dry-run
    └── test_api.py                 인프로세스 ASGI·수명·앱 상태 격리
```

### 모듈 경계의 이유

- `core/theme.py`가 업무 타입과 값 규칙을 소유하므로 `store`·`cache`·`catalog`·
  `schemas`는 **JSON fixture 적재 구현을 import하지 않습니다.** fixture는 입력 경로
  하나일 뿐이고, `ThemeStore`는 `Iterable[Theme]`만 받습니다. fixture → 업무 Theme
  조립은 `bootstrap/lifespan.py`(조립 경계)가 담당합니다.
- `ThemeCatalog`는 조회와 갱신을 **함께** 소유합니다. 둘이 같은 원본·같은 lock·같은
  무효화 규칙을 공유하므로 나누면 순서 계약이 흩어집니다.
- `counters.py`·`control_hooks.py`를 분리한 것은 `catalog.py`를 열었을 때 `get`·
  `update`가 먼저 읽히게 하기 위함입니다. 잠금·원본 갱신·무효화 순서 자체는 그대로
  `get`/`update` 본문에 남겨 두었고 helper 뒤로 감추지 않았습니다.
- 정상 경로에는 hook 실행을 감싸는 helper를 두지 않고 `if hook is not None` 분기를
  직접 씁니다. 가독성을 위해 조회마다 coroutine 생성·await를 더하지 않습니다.

### 요청 추적 — 보완 전후

`GET /v1/themes/{id}` 한 건을 따라가는 경로입니다. 파일 위치만 옮긴 것이 아니라,
읽는 순서에서 업무가 먼저 나오도록 바꾼 부분을 표시했습니다.

| 단계 | 보완 전 | 보완 후 |
| --- | --- | --- |
| 진입 | `routers/themes.py` → `ReaderDep` | 같음 (`CatalogDep`) |
| 업무 | `core/reader.py`를 열면 `ReadCounters`(40줄)와 `ControlHooks`가 먼저 나오고 그 뒤에 `ThemeReader.get` | `core/catalog.py`를 열면 `ThemeCatalog.get`·`update`가 파일 앞부분에 있고, 계수·hook 정의는 각자 모듈 |
| 이름 | `ThemeReader`가 `update`도 소유해 이름과 책임이 어긋남 | `ThemeCatalog` — 조회·갱신 모두를 뜻함 |
| 값 규칙 | `store`가 `core/fixture.py`의 `parse_spacing`을 호출 (업무가 JSON loader에 의존) | `core/theme.py`의 `ensure_spacing_px` (loader와 무관) |
| 원본 변경 | `store.themes[id] = ...`가 공개 경로로 가능 → 카운터·무효화 우회 | `_themes`는 비공개, `apply_update`만 값과 카운터를 함께 변경 |
| 잠금·순서 | `get`/`update` 본문에 인라인 | **같음** (의도적으로 유지) |

갱신 경로도 같습니다. `update`는 여전히 한 `async with self.lock:` 블록 안에서
`apply_update` → `invalidate` → 계수 증가를 순서대로 보여 줍니다.

## 검증 방법

`tests/oracle.py`는 **구현의 store·cache·reader·key 함수를 재사용하지 않습니다.**
fixture JSON을 직접 읽어 tuple로만 보관하는 별도 reference model이므로, 원본 dict와
캐시가 함께 틀려도 어긋나 실패합니다. cache-off/on 동등성만으로 통과시키지 않습니다.

검출력 확인을 위해 구현에 결함을 주입해 시험이 실패하는지 확인했습니다
(2026-09-12, 임시 사본에서 수행하고 서비스 소스는 변경하지 않음):

| 주입한 결함 | 검출한 시험 수 |
| --- | --- |
| 갱신 시 캐시 key 무효화 제거 | 7 |
| 늦은 fill 방어(변경 카운터 대조) 제거 | 1 |
| 용량 상한·eviction 제거 | 4 |
| 미등록 ID에 임의 값 반환 (not-found 소실) | 4 |
| `ThemePatch`의 strict 모드 해제 (입력 자동 변환 허용) | 9 |
| 갱신 시 변경 카운터 증가 누락 | 3 |
| `read_only_view`가 내부 dict를 그대로 반환 | 3 |
| `/v1/stats`의 캐시 존재 판정을 진위 판정으로 되돌림 | 2 |
