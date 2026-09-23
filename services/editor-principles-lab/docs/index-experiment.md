# E3 — 소유자별 최근 문서와 PostgreSQL index

Status: 오프라인 checkpoint 완료, 테스트 6개 통과. 2026-09-18 coordinator 지시에 따라 면접 기초 P0를 우선하고 실제 DB 측정 P2는 보류했다. DB 접속·서비스 기동·성능 측정은 하지 않았다. 이 예제는 합성 데이터이며 MiriCanvas 내부 구현이나 개인 경력을 설명하지 않는다. 앱의 JSON 저장과 별개인 실험이다.

## 면접 첫 답변 — 짧은 기술 검토

“DB index는 특정 컬럼 값을 기준으로 원하는 행을 더 적은 탐색으로 찾도록 DB가 유지하는 별도 자료구조입니다. 예를 들어 소유자의 최근 문서 20개를 자주 읽는다면 owner_id와 정렬 컬럼에 맞춘 복합 B-tree를 검토합니다. 다만 index마다 공간과 쓰기 유지 비용이 생기고, 작은 테이블이나 쿼리와 맞지 않는 index에서는 planner가 index를 안 쓰거나 성능 이득이 없을 수 있습니다. 실제 쿼리의 실행 계획과 읽기·쓰기 비용을 비교해서 판단합니다.” [공식 index 설명](https://www.postgresql.org/docs/16/indexes-ordering.html)

| 용어 | 핵심 구분 | 이 예제에 대입 |
| --- | --- | --- |
| connection | client와 DB 서버 사이 통신 연결 | psql이 loopback TCP로 접속 |
| DB session | 접속 후 DB와 상호작용하는 문맥과 상태의 수명 | 같은 psql 실행에서 설정과 여러 SQL을 사용 |
| transaction | 변경을 함께 확정하거나 취소하는 작업 단위 | BEGIN부터 ROLLBACK까지 실험 DDL/DML을 묶음 |

“한 DB session에서 여러 transaction을 순서대로 실행할 수 있고 COMMIT은 보통 connection 종료가 아닙니다. transaction은 DB 작업의 원자적 경계이며 session이나 연결 그 자체와 같지 않습니다.” 여기의 session은 DB session이며 웹 로그인 session 또는 특정 ORM Session 클래스와 혼동하지 않는다. [PostgreSQL 연결 구조](https://www.postgresql.org/docs/16/tutorial-arch.html), [transaction 설명](https://www.postgresql.org/docs/16/tutorial-transactions.html)

위 문구는 개념 설명 초안이며 실제 이력에서 사용했다는 주장이 아니다. 사용자 자신의 사례와 연결하려면 별도 경력 근거 확인이 필요하다.

## 먼저 예측하기

정답/결과를 열기 전에 다음 빈칸을 자기 말로 적는다.

- 예상: 문서 1,000개와 10,000개에서 `owner_id=3 ORDER BY updated_at DESC,id DESC LIMIT 20`은 어떤 작업이 필요한가?
- 예상: PRIMARY KEY(id)만 있는 상태, 복합 B-tree, title B-tree 중 무엇을 고를까? 인덱스가 있어도 안 쓰는 경우는?
- 예상: 최신 시각이 같은 문서에서 id까지 정렬하지 않으면 어떤 문제가 생길까?
- 예상: updated_at을 바꾸면 조회 결과, 저장 공간, 쓰기 비용은 어떻게 달라질까?
- 관찰 후: plan의 scan/sort, actual time, buffers, ordered IDs 중 예상과 다른 증거를 하나 적는다.
- 대안: 인덱스를 추가하지 않기, 조회 조건/정렬 변경, 조회량 줄이기 중 무엇을 택할 조건인가? LIMIT을 크게 하거나 대부분의 행이 같은 owner라면?

## 실행 경계와 명령

Python 표준 라이브러리와 설치된 `psql`만 사용한다. Docker/컨테이너/DB를 생성하거나 기동하지 않는다. 기존 공유 테스트 인스턴스와 전용 논리 DB를 coordinator가 준비·승인한 뒤에만 `--run`한다. SQLite 대체는 없다.

```sh
python3 experiments/index_compare.py
python3 -m unittest discover -s tests -p test_index_compare.py -v
# 승인된 기존 role을 ROLE 자리에 넣는다. 비밀번호는 명령/문서에 넣지 않는다.
python3 experiments/index_compare.py --run --host 127.0.0.1 --port 5434 --database editor_principles_lab --user ROLE
```

`PGPASSFILE` 또는 `PGPASSWORD`는 실행 환경에서만 제공한다. 결과에 role/password/connection URL을 남기지 않는다. `--owner`는 0–9, `--limit`은 1–100의 정수만 허용한다. 허용 주소는 localhost/127.0.0.1/::1, 포트는 5434, DB는 `editor_principles_lab` 하나다. ambient PGHOSTADDR/PGSERVICE/PGOPTIONS를 제거하고 loopback 주소로 고정한다. 접속 후 `current_database()`를 재확인한다.

새 `index_lab_<random>` schema에서만 DDL/DML을 수행하고 단일 transaction을 rollback한다. 기존 schema를 덮어쓰지 않는다. statement timeout 15초, lock timeout 2초, 전체 psql timeout 180초이며 오류/접속 종료 때도 열린 transaction은 rollback된다. 성공 시 schema 부재까지 확인한다. rollback은 디스크/WAL/캐시 자원 소비가 없다는 뜻은 아니다. 공유 인스턴스의 다른 부하는 측정에 영향을 준다.

## 무엇을 비교하는가

| 조건 | 유지되는 index | 확인할 점 |
| --- | --- | --- |
| primary_only | PRIMARY KEY(id)의 B-tree | 보조 index가 없는 baseline. index 자체가 0개인 실험은 아니다 |
| suited | PK + `(owner_id, updated_at DESC, id DESC)` | owner 범위를 찾고 최근 순서의 앞부분을 읽을 수 있는가 |
| unsuitable | PK + `(title)` | 이번 WHERE/ORDER BY를 지원하지 않는 index도 저장·삽입 비용을 갖는가 |

입력은 ID 1..N, owner=(id−1)%10, 기준시각 2026-01-01 + ((id×37)%997)초, 고정 길이 title의 결정적 공식이다. 두 N은 1,000/10,000이고 각 owner는 10%다. 세 조건은 같은 데이터를 새 테이블에 넣고 ANALYZE한다. index를 먼저 만든 뒤 INSERT의 EXPLAIN ANALYZE로 유지 비용을 포함하며 index 생성 비용은 별도 측정하지 않는다.

각 조건 read warmup 1회 + 측정 5회 후 ordered IDs를 Python oracle과 비교한다. owner의 모든 updated_at에 2,000초를 더하되 가장 작은 ID는 99,999초로 옮기는 UPDATE를 실행한다. 이 변경이 선두 ID를 바꾸는지 확인하고, 수정 후에도 세 조건을 독립 oracle에 대조한다. PK는 그대로이고 suited의 indexed column은 바뀐다. unsuitable의 title은 수정하지 않는다. UPDATE 비용에는 행 검색과 heap 변경도 포함된다.

## 원리와 관찰 한계

B-tree는 정렬된 키를 통해 범위를 좁히는 구조다. 복합 키의 앞부분 owner equality가 후보 범위를 제한하고 나머지 키 순서가 정렬 요구와 맞는다. [PostgreSQL multicolumn index 문서](https://www.postgresql.org/docs/16/indexes-multicolumn.html) 참조.

ORDER BY와 LIMIT이 맞는 index를 만나면 별도 sort를 피할 수 있다. 작은 테이블이나 넓은 범위에서는 planner가 Seq Scan + Sort를 더 싸다고 판단할 수 있다. 순차 scan 선택도 관측 결과이며 `enable_seqscan=off` 같은 강제 옵션으로 성공을 만들지 않는다. [정렬과 index 공식 문서](https://www.postgresql.org/docs/16/indexes-ordering.html) 참조.

index는 별도 공간을 차지하고 INSERT/키 변경 때 갱신해야 한다. 이 실험은 write 각 1회이므로 정밀 benchmark가 아니다. read 5회도 통계적 성능 보장이 아니다. EXPLAIN ANALYZE의 계측 오버헤드, warm cache, 고정 조건 순서, ANALYZE 표본, host와 Docker VM 자원, 동시 부하가 영향을 준다. Execution Time은 서버 측 계측 시간으로 연결/결과 전송/commit 비용을 포함하지 않는다. BUFFERS는 각 plan에 저장하며 부모·자식 값을 무작정 합하지 않는다. [EXPLAIN 공식 문서](https://www.postgresql.org/docs/16/using-explain.html) 참조. 출처 확인: 2026-09-18.

`indexes_bytes`는 PK와 보조 index의 합, `total_bytes`는 table·index 및 부속 저장 공간이다. JSON에는 index별 정의/크기와 before/after 크기, 엔진 버전, 관련 설정, client OS/CPU 정보가 들어간다. Docker CPU/RAM/storage 할당을 알 수 없으면 Unknown으로 남긴다. row count 확대, 다른 분포, cold cache, 지속 workload는 이번 범위 밖이다.

## 실행 후 근거

성공한 실제 실행만 `results/index-results.json`과 `results/index-results.md`를 만든다. 기존 결과가 있으면 새 성공 실행이 두 파일을 갱신한다. 실패한 실행은 이전 성공 결과를 수정하지 않으므로 JSON의 `measured_at`을 확인한다. JSON은 전체 plan/actual timing/BUFFERS와 ordered IDs, markdown은 비교표다. 이 문서의 준비 상태만으로 실행했다고 판단하지 않는다.

관찰 후 자료를 닫고 90초 동안 “이 쿼리에 왜 이 index인가, 언제 불필요한가, 무엇을 대가로 지불하는가”를 설명한다. 검증은 작성 agent의 self-review이며 사용자 설명 점수는 미측정이다.
