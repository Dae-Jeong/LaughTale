# 작은 편집기 · 원리 실험실

합성 문서로 상태 모델, undo/redo, 저장·충돌·실패를 관찰하는 로컬 학습 앱입니다. 실제 미리캔버스 구현을 재현하지 않습니다.

## 시작

Python 3.9 이상과 최신 Chrome을 사용합니다. 앱 실행에 외부 패키지나 build는 필요 없습니다.

```sh
cd /Users/marin/personal-workspace/laughtale/services/editor-principles-lab
lsof -nP -iTCP:18120 -sTCP:LISTEN
python3 server.py --port 18120
```

포트가 이미 점유되어 있으면 기존 프로세스를 무작정 종료하지 말고 [이관 보고](migration/report.md)의 현재 앱 PID와 비교합니다. 서버는 127.0.0.1에만 bind합니다. 중지는 실행 터미널의 Ctrl+C입니다. `results/report.md`는 이관 전 증거로 수정하지 않습니다.

- 앱: http://127.0.0.1:18120/
- 실습 안내(모델·상태·저장 순서 그림과 글 설명): http://127.0.0.1:18120/guide/
- 실행 근거: http://127.0.0.1:18120/results/
- 별도 기초 교재: http://127.0.0.1:8010/practice/canvas-editor-20h-draft/

문서를 만들고 요소를 추가한 뒤 저장하세요. 동일한 `#문서ID` URL을 두 탭에서 열면 409 충돌을 재현할 수 있습니다. 제목과 텍스트 입력은 blur/Tab에서 확정합니다. 문서 JSON, undo/redo 개수, 서버 revision, 마지막 요청을 화면에서 확인할 수 있습니다. `서버 확인`은 로컬을 보존하고, `서버 내용으로 교체`만 현재 미저장 작업을 버립니다. 필요한 경우 먼저 JSON을 내려받으세요.

저장 파일은 ignored `data/*.json`입니다. 같은 디렉터리를 두 서버가 동시에 사용하지 마세요. 새 격리 실습은 다음처럼 별도 디렉터리로 시작할 수 있습니다(기존 파일 삭제 없음).

```sh
python3 server.py --port 18121 --data-dir data-practice
```

`data-practice/`를 쓰면 본인이 관리하는 로컬 데이터입니다. 원격 push는 하지 않습니다. 초기화가 필요하면 서버 중지 후 기존 `data/`를 다른 이름으로 이동해 보존하고 다시 시작하세요. 앱은 JSON import·자동 병합·실시간 공동편집·인증·공개 배포를 제공하지 않습니다.

## 검사와 재현

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.test.mjs
python3 experiments/run.py --output /tmp/editor-new-evidence.json
```

API/실패 검사는 임시 디렉터리와 임의 localhost 포트를 사용하고 종료 후 지웁니다. 앱 데이터는 건드리지 않습니다. index 실험은 별도 담당자의 [안내](docs/index-experiment.md)를 따릅니다. `python3 experiments/index_compare.py`는 기본적으로 offline 검증이며 DB 측정을 의미하지 않습니다.

브라우저 검사만 Playwright가 필요합니다. 설치된 Google Chrome을 사용하므로 브라우저 다운로드는 없습니다.

```sh
npm install --no-save --package-lock=false playwright@1.63.0
LAB_RESULTS_DIR=/tmp/editor-browser-new-evidence node tests/browser.mjs
```

브라우저 검사는 자체 임시 서버·데이터를 쓰며 `LAB_RESULTS_DIR`에 JSON과 화면을 기록합니다. 기존 증거를 보존하려면 새 출력 경로를 지정하세요. `LAB_PYTHON`으로 Python 실행 파일, `PLAYWRIGHT_MODULE`로 별도 설치한 Playwright의 절대 module 경로를 지정할 수 있습니다. Node 내장 test runner는 순수 상태 모델을 검사합니다. 이전 [보고서](results/report.md)와 새 [이관 보고](migration/report.md)는 별도 증거입니다.

## 코드 읽기와 경계

`web/core.mjs`는 문서 전이와 최대 50개의 snapshot history, `web/app.mjs`는 DOM/SVG와 저장 UI, `server.py`는 입력 검증과 조건부 JSON 저장을 담당합니다. 선택과 서버 revision은 history의 문서 내용 밖에 있습니다. 저장 중 편집은 남겨 두며 중복 저장·문서 전환은 제한합니다.

HTTP/JSON/UUID/파일 시스템/렌더링은 표준 도구에 맡겼습니다. 모델/history/충돌·실패 시나리오/oracle은 직접 구현했습니다. React, 편집 라이브러리, DB 서버, 브로커는 앱 의존성이 아닙니다. atomic replace와 한 프로세스 lock은 DB transaction·다중 프로세스 동시성·정전 durability를 대신하지 않습니다. 선택 async-job MOCK은 생략했으며 실제 RabbitMQ 보장을 검증했다고 주장하지 않습니다.
