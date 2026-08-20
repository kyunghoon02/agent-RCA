# Operational Knowledge와 Incident Memory Retrieval Contract

> 상태: design contract; runtime 미구현
>
> 목적: Agent가 참고 지식과 runtime Evidence를 혼동하지 않도록 입력, retrieval과
> citation 경계를 고정한다.

## Knowledge 계층

| 계층 | 저장 내용 | 초기 구현 |
|---|---|---|
| Graph Knowledge | Entity, 상태·관계 시간 구간, Event 집계와 Evidence reference | StateGraph fixture 일부 구현 |
| Operational Knowledge | architecture, service catalog, runbook, SLO와 tool guide | Git 기반 문서 corpus 계획 |
| Incident Memory | 검증된 과거 Incident/RCA와 일반화된 진단 경험 | 후속 단계 |

Graph Knowledge는 Evidence에서 projection된 조사 인덱스다. Operational Knowledge는
다음 조사를 선택하기 위한 reference다. 둘 다 그 자체만으로 현재 Incident의 원인을
증명하지 않는다.

## ReferenceDocument

모든 Operational Knowledge 문서는 ingestion 시 다음 metadata를 가져야 한다.

```text
reference_document_id
document_type              architecture | service-catalog | runbook | slo | tool-guide
title
source_path_or_uri
version
valid_from / valid_to
entity_keys
content_hash
review_status              approved | draft | retired
sensitivity                public | internal
```

- `approved` 문서만 Agent runtime에 제공한다.
- `valid_to`가 지난 문서는 historical query가 아닌 현재 Incident retrieval에서
  제외한다.
- secret, credential, raw Secret value와 개인정보를 corpus에 저장하지 않는다.
- content hash는 retrieval 시점의 문서와 index가 같은 버전인지 확인하는 데 쓴다.

## RetrievalQuery

Retriever는 StateGraph localization 이후 다음 입력만 받는다.

```text
incident_id
investigation_scope
localized_entity_keys
allowed_document_types
query_terms
top_k
character_budget
request_id
```

- `localized_entity_keys`는 Frozen Context 또는 승인된 다음 seed에서만 가져온다.
- domain, time window와 entity scope가 없는 corpus 전체 자유 검색은 허용하지 않는다.
- MVP 기본 상한은 `top_k <= 5`, 전체 reference text `<= 12,000` characters다.
- timeout, no match, stale-only result와 retriever failure를 구분한다.
- MVP retrieval은 metadata/entity match와 lexical search로 충분하다. Vector DB는
  corpus 규모와 평가 결과가 필요성을 입증할 때 추가한다.

## RetrievedReference

각 검색 결과는 다음 provenance를 보존한다.

```text
retrieval_id
request_id
reference_document_id
document_version
content_hash
matched_entity_keys
retrieval_method
rank
bounded_excerpt
```

Agent 입력은 `Frozen Context Package + RetrievedReference[]`로 구성한다. Reference
인용은 `reference_document_id`를 사용하고 원인 지지·반박은 `evidence_id`를 사용한다.
Evidence Gate는 reference만 인용한 root-cause 결론을 거부한다.

## Incident Memory

KRCA의 계층형 memory 개념을 다음처럼 제한적으로 적용한다.

- Working Memory: 현재 Incident의 tool call, 관측, 가설과 budget 상태다. Incident
  종료 후 자동으로 장기 지식이 되지 않는다.
- Factual Memory: 운영자 또는 평가 절차가 검증한 과거 Incident와 최종 RCA다.
- Experiential Memory: 여러 Incident에서 일반화한 진단 경험이며, source Incident와
  human review를 반드시 참조한다.

Factual/Experiential Memory는 실제 Agent runtime과 평가 baseline이 안정된 뒤
활성화한다. 미검증 Agent 출력의 자동 self-ingestion은 금지한다.

## 격리와 금지 입력

다음 데이터는 Retriever와 Agent가 접근할 수 없다.

- fault injection의 실제 주입 설정과 정답
- evaluation Ground Truth label과 grader note
- 미공개 credential, token, private key와 Secret value
- 검증되지 않은 Agent reasoning trace 또는 초안 RCA
- Incident scope 밖의 raw log, metric과 resource dump

Ground Truth 저장 위치와 runtime service account 권한은 문서 필터가 아니라 별도
storage/RBAC 경계로 격리한다.

## 구현 상태

현재 구현된 것은 domain-neutral StateGraph fixture뿐이다. ReferenceDocument schema,
ingestion/index, bounded Retriever, Agent input assembly, Factual/Experiential Memory와
runtime evaluation은 아직 구현되지 않았다.

## Reference

- Jiang et al., “KRCA: An Efficient Root Cause Analysis System in Hyper-Scale
  Microservice Systems via Agentic AI,” ASE 2026,
  <https://arxiv.org/abs/2607.01788>
- Xiang et al., “Simplifying Root Cause Analysis in Kubernetes with StateGraph
  and LLM,” <https://arxiv.org/abs/2506.02490>
