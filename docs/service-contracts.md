# 서비스 계약 보완

Status: 기존 승인 규칙의 잔여 세부 선택 보존 · 2026-09-07

[공통 개발 기준](../external/backend-template/design/engineering.md)에 없는 기존 세부 선택만 보존합니다.
새로운 공통 규칙집이 아니며 해당 책임을 변경할 때만 읽습니다. 공통 정본 채택 버전이 아래 내용을 수용하거나
서비스 설계에서 명시적으로 변경하면 해당 조항을 제거합니다. 그 전에는 기존 의미를 유지합니다.

## Python 서비스 환경과 실행 (하네스)

2026-09-12 추가. `services/catalog-hub`가 자체 lock 없이 옆 서비스의 venv 바이너리로
검증된 사례를 계기로 명문화합니다. 공통 정본이 이 조항을 수용하면 제거합니다.

**이 repo는 uv workspace가 아닙니다.** 루트에 `pyproject.toml`이 없고 서비스마다
환경을 따로 둡니다. 새 Python 서비스는 `services/<name>/`에 다음을 갖춥니다.

- `pyproject.toml` — `build-backend = "uv_build"`, `requires-python` 고정,
  런타임 의존성과 `[dependency-groups] dev`(pytest·ruff·ty)
- `uv.lock` — **필수.** `uv lock`으로 생성하고 커밋합니다
- `tests/conftest.py` — `--import-mode=importlib`에서 `tests/`를 import 경로에 넣습니다.
  이게 있으면 `PYTHONPATH` 없이 표준 명령만으로 돌아갑니다

실행은 **전부 아래 형태**입니다. 전역 uv를 바꾸지 않고 고정 버전을 씁니다.

```sh
cd services/<name>
uv tool run --from uv==0.12.10 uv sync --locked
uv tool run --from uv==0.12.10 uv run --locked pytest -q
uv tool run --from uv==0.12.10 uv run --locked ruff check src tests
uv tool run --from uv==0.12.10 uv run --locked ruff format --check src tests
uv tool run --from uv==0.12.10 uv run --locked ty check
uv tool run --from uv==0.12.10 uv run --locked python -m <pkg>.run
```

금지 사항과 그 이유:

- **다른 서비스의 `.venv` 바이너리를 직접 호출하지 않습니다**
  (`../chat/.venv/bin/python …`). 선언한 의존성과 실제 실행 환경이 갈라지고,
  그 서비스의 lock을 바꾸면 조용히 깨집니다.
- **`PYTHONPATH`로 경로를 밀어넣지 않습니다.** `PYTHONPATH=src:tests`는
  `src/`가 `tests/`를 import하는 역방향 의존을 가립니다 — 실제로 catalog-hub의
  측정 도구가 `tests/oracle.py`를 import하고 있었고 `ty check`로 드러났습니다.
  시험과 구현이 함께 쓰는 헬퍼는 `src/` 안으로 옮깁니다.
- **lock 없이 검증 결과를 보고하지 않습니다.** `--locked` 없이 통과한 결과는
  재현을 보장하지 않습니다.

의존성 추가·갱신은 공식 명령(`uv add`, `uv lock`)을 쓰고 수동으로 lock을 편집하지
않습니다. 참조 구현은 [backend-template의 uv 프로젝트](../external/backend-template/python/fastapi/README.md)입니다.

## HTTP 표현과 업무 조합

- HTTP API는 MVC 역할 분리를 적용합니다. Router/Controller는 단일 업무 진입점을 호출하고 JSON 표현은 Response DTO·직렬화가, 화면은 FE가 소유합니다. 이름이 같은 클래스 계층을 강제하지 않습니다.
- 전송 계층 의존이 없고 업무 입력과 의미가 같은 값 Schema는 그대로 전달할 수 있습니다. 의미 차이가 있을 때만 내부 입력으로 변환합니다.
- 기존 변환 명명은 API의 `from_internal()`, Application의 `from_entity()`입니다. Router에서 `from_entity()`를 호출하지 않습니다. 내부 dict·ORM 객체를 공개 응답으로 그대로 노출하지 않습니다.
- JSON casing·성공/오류 표현·pagination은 서비스 계약에서 일관되게 정합니다. cursor 형식은 Schema/codec, 정렬·조회 조건은 Repository가 소유합니다.
- 내부 오류를 HTTP status·공개 code·사용자용 message로 변환하며 request ID로 진단 로그와 연결합니다. 외부 오류 원문을 공개 응답으로 내보내지 않습니다.
- Facade를 쓰는 경우 Service를 거치지 않고 Repository에 직접 접근하지 않습니다. 다른 도메인의 변경은 그 소유 도메인의 업무 계약을 통합니다.

## 상태와 영속화

- 상태만으로 정해지는 종료 여부·그룹은 Enum이 소유하고 사용자·시간·한도 정책을 넣지 않습니다. 업무 상수는 소유 도메인에 두고 특징 계산과 결정 정책을 구분합니다.
- 사건 시간 변환과 별개로 날짜 구간의 기준 시간대를 명시합니다. 현재 시각은 주입하거나 공통 clock으로 제공하여 테스트에서 제어합니다.
- Repository는 Domain 모델이나 명시적인 조회 타입을 반환하고 집계·projection은 별도 조회 DTO로 표현합니다. Enum annotation만으로 DB 값의 런타임 변환을 가정하지 않으며 인덱스·제약을 중복 정의하지 않습니다.
- transaction manager/Unit of Work가 commit·rollback·cleanup을 소유하고 참여 Service·Repository는 중간 commit을 하지 않습니다. 특정 데코레이터·전역 SessionProxy를 필수로 하지 않습니다.
- 독립 트랜잭션 사이에는 세션에 묶인 ORM 객체 대신 식별자·값을 전달합니다. savepoint와 독립 commit은 실패 전파 범위를 정한 경우에만 사용합니다.

## 외부 연계와 검증

- 공유 Client는 동시 사용 안전성·종료 처리를 확인하고 요청·트랜잭션 상태 객체를 singleton으로 공유하지 않습니다. 외부 성공 HTTP 코드만으로 업무 성공을 판정하지 않습니다.
- provider의 rate limit·`Retry-After` 계약을 확인합니다. 멱등 키를 지원하면 같은 업무 재시도에 같은 키를 사용하고 유효 기간·중복 응답 처리도 확인합니다.
- 검증은 테스트 순서·숨은 공유 상태에 의존하지 않습니다. fixture와 필요한 factory/Mother에는 유효한 기본값·명시적 변경값을 제공합니다. 병렬 테스트의 데이터 충돌을 방지합니다.
- 기본 테스트에서 실거래를 호출하지 않습니다. 실제 provider 시험은 sandbox smoke로 분리해 계정·비용·데이터 영향·권한을 확인합니다. API status·DTO·오류·pagination, 외부 변환 fixture, 통신 대역의 timeout·재시도·취소, 실제 DB 원자성을 책임별로 검증합니다.
- 설정된 lint·type check·test·필요한 빌드를 실행하고 반복 오류는 좁은 회귀 검증으로 남깁니다. 없는 도구의 통과를 주장하지 않습니다.

Python 작성 규칙은 [해당 구현 설계](../external/backend-template/design/implementations/fastapi.md#python-개발-규칙)를 따릅니다.
기존 예외인 공개 API 재노출의 상대 import 허용은 유지합니다. 새 Python 버전·라이브러리를 이 문서에서 고정하지 않습니다.
