# T4 offline checkpoint — 2026-09-18

완료: Python stdlib + 기존 psql 실행기, localhost:5434/전용 DB guard, transaction 전용 schema/rollback, 결정적 1,000/10,000행의 세 index 조건, ordered ID oracle, 측정 JSON/Markdown 생성 코드와 학습 질문을 준비했다. `python3 -m unittest discover -s tests -p test_index_compare.py -v`의 6개 테스트 및 `python3 experiments/index_compare.py` 오프라인 준비 명령이 통과했다.

실제 DB 접속/서비스 변경/성능 측정은 **0회**다. coordinator의 P0 면접 기초 우선 지시로 live SQL·EXPLAIN·실제 엔진 정합성·rollback 검증은 P2로 보류했다. 따라서 `index-results.json`/`.md` 실측 파일은 아직 없고, 이 파일은 성능 결과가 아니다. 향상 배수나 DB 실행 성공을 주장하지 않는다.

후속 실행 계약: coordinator가 기존 DB와 role의 사용을 확인하고 P2를 재개한 뒤 다음 명령에서 `ROLE`을 실제 기존 role로 바꾼다. 비밀번호는 `PGPASSFILE`/`PGPASSWORD` 실행 환경으로만 전달한다.

```sh
python3 experiments/index_compare.py --run --host 127.0.0.1 --port 5434 --database editor_principles_lab --user ROLE
```

성공하면 `results/index-results.json`과 `results/index-results.md`가 생성된다. SQL은 실제 엔진에서 아직 검증하지 않았으므로 첫 live run 결과를 확인해야 한다. Docker/DB 생성·기동은 스크립트에 없다.

[실습 및 면접 첫 답변: index 정의·용도·두 한계, transaction/session/connection 구분](../docs/index-experiment.md) · [기계 판독 checkpoint](index-readiness.json)

검증자: 작성 agent self-review. 사용자 이해도, 회사 내부 구현, 경력 사용 사실은 검증하지 않았다. canonical task 및 다른 worker 소유 파일은 변경하지 않았다.
