# 로컬 편집기 실행 보고

검증자: 구현 worker self-review. 승인된 앱 구현·격리 실험을 완료했으며 사용자의 이해도는 아직 측정하지 않았다. canonical task와 Dae-Jeong 교재, DB worker 예약 파일은 수정하지 않았다.

## 실행 표면

- 앱: http://127.0.0.1:18120/
- 실습: http://127.0.0.1:18120/guide/
- 근거: http://127.0.0.1:18120/results/
- 교재: http://127.0.0.1:8010/practice/canvas-editor-20h-draft/
- 위 네 URL은 2026-09-18 13:22–13:24 UTC에 실제 HTTP 200 확인. 교재의 내용 소유권·학습 수용은 교재 담당과 coordinator에게 있다.

승인 예산 시작: canonical task의 `2026-09-18T13:07:05Z`. 최종 검사/보고서 HTTP 확인 관측 `2026-09-18T13:25:39Z`까지 실제 경과 18분 34초. 20시간은 상한이며 20시간 연속 실행·자동 재개·학습 완료를 주장하지 않는다.

서버는 포트가 비어 있음을 `lsof`로 확인한 후 Orca-owned terminal에서 시작했다. 관측 PID **36889**, 포트 **18120**, bind **127.0.0.1**, terminal **term_a788b5be-8135-4163-8c7e-f0100c972860**. 이 값은 현재 실행의 기록이며 재시작 시 달라진다.

시작:

```sh
cd /Users/marin/personal-workspace/editor-principles-lab
python3 server.py --port 18120
```

중지: 해당 Orca 서버 터미널에서 Ctrl+C. 다른 터미널에서는 PID와 명령을 먼저 확인한 뒤 아래 명령을 사용한다.

```sh
ps -p 36889 -o pid,command
kill -TERM 36889
```

기동을 유지한 채 인계한다. 데이터는 이 프로젝트의 ignored `data/`에만 저장한다. 테스트와 E2 대조군은 별도 임시 디렉터리에서 실행하고 제거했다. 새 DB 서버나 외부/유료 서비스를 사용하지 않았다. 브라우저 테스트 전용 Playwright만 로컬 ignored `node_modules/`에 설치했다.

## 구현과 검사

Python 3.13.2, Node v22.9.0, Playwright 1.63.0, 설치된 Chrome 153.0.8010.50, macOS. 앱 실행에는 Python 표준 라이브러리와 native DOM/SVG/ES modules만 필요하다.

| 검사 | 결과 | 근거 |
| --- | --- | --- |
| 순수 상태 Node 검사 | 6/6 통과 | `node --test tests/*.test.mjs`; immutable 전이, history 분기/상한, 선택 분리, 좌표 제한, 텍스트 undo |
| 앱 API Python 검사 | 9/9 통과 | `tests/test_api.py`; 생성/목록/저장/복원, 서버 재시작, 동시 1성공·1충돌, 503, 응답 유실, replace 실패 보존, 입력/정적 경계/Host/Origin |
| DB worker offline 검사 | 최초 통합 실행 6/6 통과 | `tests/test_index_compare.py`; DB 실행 결과는 아님 |
| Chrome 실제 브라우저 | 10/10 시나리오 통과 | [browser.json](browser.json), `node tests/browser.mjs` |
| E2 격리 대조군 | oracle 4/4 통과 | [conflict-failure.json](conflict-failure.json), `python3 experiments/run.py` |

브라우저 검사는 사용자 생성·제목/텍스트 편집·추가·keyboard 이동, 실제 mouse drag 한 undo 단위·redo, pointer cancellation, 저장/페이지 reload, 두 탭 409와 로컬 보존, 서버 확인과 명시적 교체, 503 후 재시도, 응답 유실의 결과 불명과 실제 서버 기록, 지연된 저장 응답 중 추가 편집 보존·문서 전환 제한을 확인했다. pageerror 0개. 오류 실험의 409/503/연결 종료는 의도한 결과다.

1440×1000 viewport의 [편집기](desktop-1440.png), [안내](guide-1440.png), [상태/sequence 그림](guide-save-diagrams.png)을 실제 렌더링해 시각 검수했다. 제목·도구·상태·캔버스와 텍스트 속성이 desktop 화면에 보이고 가로 overflow가 없다. diagram은 SVG 접근성 제목과 인접한 평문 설명을 포함한다. 화면 압축 과정에서 일시적으로 SVG 비율이 달라져 drag 좌표 검사가 실패했으며, 비율을 보존하는 max-width로 고친 뒤 10개 브라우저 시나리오를 다시 통과했다.

## E2 관찰

동일한 revision 1을 읽은 A/B가 저장할 때 보호군은 HTTP 의미상 `[200, 409]`이고 최종 제목은 `A edit`이다. 버전 검사를 뺀 임시 대조군은 B가 A를 덮어 `B edit`만 남긴다. 저장 전 실패는 기존 기록을 그대로 유지한다. 저장 후 응답 유실은 기록을 증가시키며 기존 revision으로 재시도하면 409다. HTTP 응답 실제 종료는 API 및 브라우저 검사로 별도 확인했다.

## API 계약

`GET /api/documents` → `{documents:[{id,title,revision}]}`; `POST /api/documents` + `{title?}` → 201 `{document,revision:1}`. `GET /api/documents/{id}` → `{document,revision}`. `PUT /api/documents/{id}` + `{document,expected_revision}` → 200 `{document,revision+1}` 또는 409 `{error:"conflict",document,revision}`. 잘못된 입력 400, 없음 404, 기록 전 장애 503. 학습 전용 `X-Lab-Fault: none|before|lost`를 사용한다. lost는 성공 기록 뒤 연결을 닫는다.

## 한계와 남은 범위

- DB index 실측은 담당자의 [offline readiness](index-readiness.md)에 따라 보류됐다. 실제 SQL·EXPLAIN·속도·쓰기 비용·rollback 검증 완료를 주장하지 않는다. 앱 worker의 완료와 DB live 측정 완료는 별개다.
- 선택 `async-job` MOCK은 생략했다. 실제 RabbitMQ/queue/worker/renderer/AI는 실행하지 않았다. 관련 원리는 연결된 기초 교재 소유다.
- 최대 200요소, 50개 snapshot history. 한 프로세스 lock과 atomic file replace이며 DB transaction·다중 프로세스 lock·정전 durability 보장이 아니다.
- 자동 병합·CRDT/OT·다중 선택·리사이즈·회전·JSON import·문서 삭제·인증·공개 서비스는 없다. 다운로드는 로컬 보존용이며 재가져오기 기능은 없다.
- 현재 입력은 blur/Tab에서 확정한다. 브라우저 탭을 닫으면 미저장 작업은 사라질 수 있어 beforeunload 경고와 JSON 다운로드를 제공한다. 서버 재시작 복원과 페이지 reload는 저장된 내용에 적용된다.
- fault controls는 로컬 합성 학습용이고 운영 장애 주입 체계가 아니다. Host/Origin·크기 제한은 운영 인증·권한 통제를 대신하지 않는다.

coordinator가 독립 수용과 canonical task 갱신을 소유한다. 학습자의 예상·조작·90초 자기 말 설명은 아직 남아 있다.
