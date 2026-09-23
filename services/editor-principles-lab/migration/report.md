# Editor lab 이관 보고

Status: 전환 완료, 기존 과대본문 전송 실패 한계 유지. 확인: 2026-09-18 13:58:45 UTC.

## 귀속·실행

- 유일한 활성 코드/실행 경로: `/Users/marin/personal-workspace/laughtale/services/editor-principles-lab/`.
- 앱 http://127.0.0.1:18120/ · 안내 http://127.0.0.1:18120/guide/ · 기존 결과 http://127.0.0.1:18120/results/ . 세 페이지와 `/api/documents` HTTP 200 확인.
- 새 PID `28137`, Python `3.13.2`, cwd는 위 서비스 경로, 데이터는 그 안의 `data/`. Orca terminal `term_ee4b647c-7300-4192-a149-cf9f42d2ab06`, Laughtale worktree 소유.
- 원본 비활성 백업: `/Users/marin/personal-workspace/editor-principles-lab/`. 원본 파일·Git·ignored 의존성 전체를 그대로 보존했다. 이 백업에서 서버/작업을 재개하지 않는다. 원본 서버 PID `36889`의 명령·cwd·18120 listener를 확인한 뒤 해당 Orca terminal만 닫았고 프로세스 부재 확인 후 새 서버를 시작했다.
- 기존 `platform-mock`, `theme-catalog`, `catalog-hub`과 같은 kebab-case 이름을 유지한다. chat import·라우팅·배포와 연결하지 않았다. 별도 제품 기능이나 프레임워크를 추가하지 않았다.
- 사용자 지정 범위가 기존 작은 stdlib 앱의 최소 이관이므로 packaging/uv 서비스 전환은 하지 않았다. 이는 기존 서비스 표준을 모두 충족한다는 선언이 아니다. 해당 표준 적용이 필요하면 관리 owner가 별도 범위로 판단한다.

## 보존 증거

[source-before.json](source-before.json)은 모든 파일·디렉터리·symlink의 종류/권한/파일 SHA-256와 Git status·ignored·tracked·HEAD·diff·staged·remote inventory다. [source-after-copy.json](source-after-copy.json), [source-after-cutover.json](source-after-cutover.json)의 전체 285항목 비교에서 변경 0건, Git 상태 동일이다.

- 원본은 커밋 없는 `main`, tracked 파일 0개, untracked 코드/문서/시험/결과 25개, remote 없음. 미커밋 파일을 제거하거나 commit/stash/reset하지 않았다.
- 원본 `.git`, `node_modules`, `__pycache__`/pyc 등 ignored까지 백업에서 보존. 활성 서비스에는 `.git`·`node_modules`를 중첩 복사하지 않았다. 브라우저 재검증 의존성만 `/tmp/editor-migration-deps.yuPGdu`에 공식 npm 명령으로 Playwright `1.63.0`을 설치했다.
- 제외 항목 외 25개 파일을 먼저 byte-identical 복사하고 SHA-256 대조 통과. 이후 의도적으로 바꾼 파일은 `AGENTS.md`(owner/귀속), `README.md`(경로/명령), `tests/browser.mjs`(별도 결과·Python·검증 의존성 경로), `experiments/run.py`(별도 결과 경로)뿐이다. 추가 파일은 이 migration 기록·검증 도구다.
- `server.py`, `web/*`, `tests/test_api.py`, index 실험 코드는 원본 그대로다. 기존 `results/*`는 전부 해시 일치하며 덮어쓰지 않았다. 신규 시험 결과는 `migration/verification-1/`에만 생성했다.
- 전환 전후 실제 `data/` 파일 수는 0개로 일치했다. 원본 서버를 종료한 뒤 마지막 data 복사를 했으며 같은 data를 두 서버가 동시에 쓰지 않았다.
- Laughtale의 기존 광범위한 dirty 변경은 그대로 두고 서비스 디렉터리만 추가했다. task/교재/다른 서비스는 편집하지 않았다. 전체 Laughtale 동시 편집을 잠근 것은 아니므로 타 agent가 수행한 변경까지 불변으로 주장하지 않는다.

주요 SHA-256 (전체 목록은 inventory):

| 파일 | SHA-256 |
| --- | --- |
| server.py | `7092e3ee2918cd540a771f6bb98b2b7768056dfdd697ad6865b1d3f4765b8c46` |
| web/core.mjs | `53dd40b3e3867088448481e98fb7b9680892d29c1fb794f09f5fc4924b13ff12` |
| 기존 results/report.md | `431ee05bf09d046f108743d5ebc44eef38bd902f9ae58ace7726c74b98901edf` |
| source-before.json | `890d7d36c5721831017421ea9c971c6d9e49bdff3f41c853a3f19e03eaeecc5d` |

## 재검증과 남은 실패

[summary.json](verification-1/summary.json)에 실행 argv와 시작/종료 시각을 보존했다. 모든 API/브라우저는 임시 data와 localhost 임의 포트(`--port 0`)를 사용했고 live 18120에는 읽기만 했다.

| 검사 | 이번 독립 실행 결과 |
| --- | --- |
| Python 3.13.2 unittest | 15개 중 14통과/1오류: API 8/9, index offline 6/6 |
| Python 3.9 unittest | 15/15 통과: API 9/9, index offline 6/6 |
| Node core | 6/6 통과 |
| index 기본 CLI | offline 준비 통과, DB 접속 없음 |
| E2 충돌/실패 oracle | 4/4 통과 |
| 실제 Chrome 브라우저 | 10/10 통과, pageerror 0; 두 탭 충돌·응답 유실·저장 중 편집·undo/redo·복원 포함 |
| 시각 검사 | 새 1440×1000 screenshot을 직접 읽어 기존 화면·작업 영역·상태 패널 확인 |

과대본문 실패 원시 기록: [python313.log](verification-1/python313.log). `test_origin_host_and_size`의 `conn.request` 도중 `ConnectionResetError: [Errno 54] Connection reset by peer`; 서버는 400을 기록했다. 이전 이관 전 root 검수에서도 같은 실패가 있었다. 이번에는 3.9가 통과했지만 특정 Python 버전에만 발생한다고 일반화하지 않는다. 본문 전체 전송과 조기 거절/연결 종료 사이의 간헐 전송 한계이며 정확한 OS 레벨 인과는 추가 검증하지 않았다. 기존 server/test byte-identical로 이 실패를 보존했으며 테스트를 지우거나 성공으로 바꾸지 않았다. 따라서 전체 green 또는 오류 해결 완료가 아니다. 정상·실패 UI/API 동등 동작 확인에 근거해 경로만 전환했다.

새 근거: [core.log](verification-1/core.log), [python39.log](verification-1/python39.log), [index-offline.log](verification-1/index-offline.log), [conflict.json](verification-1/conflict.json), [browser.json](verification-1/browser/browser.json), [화면](verification-1/browser/desktop-1440.png). 최종 runtime/복사/등록 확인은 [final-check.json](final-check.json).

관측 가능한 보존 inventory 시작 `13:54:54Z` → 최종 확인 `13:58:45Z`: 3분 51초. 이 구간 이전 규칙/명령 조사 시간은 포함하지 않으며 총 연속 작업시간으로 부르지 않는다. 20시간 실행 주장은 없다.

## Orca 정리

- `orca repo --help`에는 repo rm이 없다. `orca worktree rm --help`는 Git worktree/branch까지 제거한다고 명시하므로 사용하지 않았다.
- `orca project setup-delete --help`는 repo-backed setup의 registered repo compatibility record 제거를 명시한다. 이를 근거로 `orca project setup-delete --setup e1f1e63b-522a-455c-8377-0bb351e2a81b --json` 실행 성공.
- 이후 `orca repo list`와 `orca project setups` 양쪽에서 기존 ID가 없음을 확인. 파일은 위 전후 전체 해시로 불변 확인. 새 서비스 별도 repo/Run/agent는 만들지 않았다.
- 기존 두 worker Dispatch는 settled/completed였다. DB terminal은 idle 확인, 앱 terminal idle 조회는 timeout이라 idle로 단정하지 않았다. 완료된 lab task의 잔여 두 terminal을 명시적으로 닫아 `ptyKilled:true`를 확인한 후 inventory를 시작했다. 서버 terminal은 검증 이후 별도로 종료했다.
- 삭제한 것은 기존 lab Orca 등록 및 세 terminal 표면뿐이다. 원본 파일/Git 이력은 삭제하지 않았다. 필요 시 원본 경로를 공식 `repo add`로 재등록할 수 있으나 활성 서비스와 이중 실행하면 안 된다.

## 교재 writer에게 전달할 링크 변경 목록

- 로컬 프로젝트 base 경로: `/Users/marin/personal-workspace/editor-principles-lab/` → `/Users/marin/personal-workspace/laughtale/services/editor-principles-lab/`.
- 실행 cd 명령도 위 새 경로. 현재 PID/Orca terminal 정보는 이 보고서를 사용한다. 과거 결과의 원본 경로는 역사로 유지한다.
- 앱/안내/결과의 18120 HTTP URL은 변경 없음. 기초교재 8010 URL도 변경하지 않았다.
- 교재 writer 수정 대상: `practice/mapping.md:39`의 `workspace:editor-principles-lab` → `workspace:laughtale/services/editor-principles-lab`. `practice/start.md:75`~77 및 `practice/mapping.md:40,44`의 18120 URL은 그대로 유지 가능하다. 이 목록만 전달했으며 교재를 직접 수정하지 않았다.
- Dae `wiki/products/interview-learning/**/*.md` 제한 검색에서 이전 절대 lab 경로 참조는 발견하지 못했다. 교재 전체 저장소 검색 결과나 모든 링크 검증으로 확대 해석하지 않는다. 교재 수정은 Dae writer만 수행한다.

DB 새 인스턴스 vs 기존 PG 내 DB 선택은 여전히 대기다. Docker/DB 기동·SQL/EXPLAIN 실측·공유 서비스 변경·원격/commit/push·추가기능 없음. task 갱신은 Obsidian 관리 owner에게 인계하며 이 보고서가 task writer를 대체하지 않는다.
