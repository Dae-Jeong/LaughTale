# 오케스트레이션 연결

Status: 프로젝트 진입 설정 · 2026-09-10

Laughtale은 공통 Paperclip 회사의 **Laughtale** 프로젝트로 구분한다.
PM이 업무를 기록하고 실행할 때는 이 repo의 Orca workspace를 지정한다.
등록은 실행 권한이나 상시 agent 기동을 뜻하지 않는다.

## 시작

1. 로컬 `.local/source-roots.yaml`의 `roots.workspace` 아래 `orchestration`을
   `workspace:orchestration`으로 해석한다. 공통 모델 채택 버전은
   `07b718940df511f44af2ab1e53df9ddc2bb0f667`이다. 해당 checkout의 버전과 변경을 확인한다.
2. 그 repo의 `skills/squad-model/SKILL.md`와 요청에 필요한 reference를 읽는다.
   운영자의 Product Workflow 정본은 `wiki:meta/agents/product-workflow.md`다.
3. [README](../README.md), [문서 라우팅](README.md), 해당 `tasks/<work>.md`를 읽고
   기존 담당·활성 실행을 확인한다. 코드·인프라의 완료 기준은 해당 owner 문서를 따른다.
4. 소재를 받아 task별 입력·완료 조건·검증자·허용 효과·자원을 정하고 필요한 역할만 배정한다.
   Paperclip issue에 이 제품의 projectId를 연결하며 상세 기록은 기존 named task에 둔다.

예: “Laughtale PM으로 이 소재를 정리하고, 기존 작업과 겹치지 않는 최소 작업을
완료 기준·검증 방법과 함께 진행해줘: …”

## 제품별 경계

- 기획·디자인·개발·QA는 현재 채팅 제품의 승인된 범위와 고정 backend 기준을 소비한다.
- 디자인의 Pencil 원본은 실행 전에 파일·노드를 고정하고 같은 원본의 writer를 확인한다.
  UIBowl 인증과 runtime 도구 노출은 해당 실행에서 확인한다.
- 마케팅은 고객·메시지·측정이 필요한 작업에만 참여한다. HR은 수행 증거를 리뷰하며
  한 번의 납품을 제품 성과·모델 역량으로 일반화하지 않는다.
- `.local/orchestration.json`은 현재 Paperclip·Orca ID 연결만 소유한다. 실제 실행 전에
  CLI/API로 대상과 활성 상태를 다시 확인한다. 기존 세션에 자동으로 지시하지 않는다.

새 clone에서는 운영자가 공통 모델 checkout과 global wiki를 준비하고
ignored `.local/source-roots.yaml`에 `roots.workspace`, `roots.wiki`를 설정한다.
설정 파일이 없으면 경로를 추측해 다른 repo를 실행하지 않는다.
공통 모델을 복제·자동 갱신하지 않으며, 버전 변경은 차이와 제품 영향을 확인한 뒤 채택한다.
