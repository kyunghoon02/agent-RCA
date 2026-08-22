# Operational Knowledge와 Incident Memory Retrieval Contract

> 상태: Git index, lexical baseline, pgvector adapter, Hybrid Retriever와 Agent
> reference tool 연결 구현. live pgvector/embedding benchmark는 미검증
>
> 목적: Agent가 참고 지식과 runtime Evidence를 혼동하지 않도록 입력, retrieval과
> citation 경계를 고정한다.

## Knowledge 계층

| 계층 | 저장 내용 | 초기 구현 |
|---|---|---|
| Graph Knowledge | Entity, 상태·관계 시간 구간, Event 집계와 Evidence reference | Neo4j repository와 local fixture 구현 |
| Operational Knowledge | architecture, service catalog, runbook, SLO와 tool guide | Git source, lexical baseline와 opt-in pgvector 구현 |
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
source_class               operational-knowledge
source_kind                git-path
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
context_id
investigation_scope
localized_entity_keys
allowed_document_types
query_terms
retrieval_method           entity-key+lexical | entity-key+vector |
                           entity-key+lexical+vector-rrf
top_k
character_budget
timeout_seconds
request_id
requested_at
```

- `localized_entity_keys`는 Frozen Context 또는 승인된 다음 seed에서만 가져온다.
- domain, time window와 entity scope가 없는 corpus 전체 자유 검색은 허용하지 않는다.
- MVP 기본 상한은 `top_k <= 5`, 전체 reference text `<= 12,000` characters다.
- query term은 최대 16개, timeout은 최대 5초, index scan은 최대 500개 문서다.
- timeout, no match, stale-only result와 retriever failure를 구분한다.
- metadata/entity, review, valid time과 content hash는 모든 방식에서 먼저 적용한다.
- Vector index는 이 hard filter를 통과한 `reference_document_id + content_hash` 후보만
  검색할 수 있다.
- `entity-key+lexical`을 baseline, `entity-key+vector`를 semantic-only ablation,
  `entity-key+lexical+vector-rrf`를 target variant로 비교한다.

## Vector index와 Hybrid ranking

Git corpus가 source of truth이며 pgvector는 hash-pinned derived index다. 문서는 bounded
overlap chunk로 나누고 embedding model과 content hash를 함께 저장한다. Retriever는
query embedding과 cosine distance로 문서별 최고 chunk score를 얻되 Vector raw score와
lexical count score를 직접 더하지 않는다.

```text
RRF(document) = sum(1 / (60 + rank_source(document)))
```

Vector index가 후보에 없는 ID, 현재 Git index와 다른 hash 또는 중복 ID를 반환하면
fail closed로 `REPOSITORY_UNAVAILABLE`을 기록한다. Reference는 여전히 현재 Incident
Evidence가 아니며 검색 방식과 무관하게 root cause를 증명할 수 없다.

## Retrieval evaluation

동일한 Frozen Context와 query set에 lexical/vector/hybrid를 실행하고 다음을 기록한다.

- Hit@K와 Recall@K
- MRR@K와 nDCG@K
- p95 retrieval latency
- corpus document ID/content hash, benchmark와 embedding model을 묶은 fingerprint
- no match, timeout과 repository failure 상태

초기 `evaluation/knowledge-retrieval`의 2개 문서/12개 query는 pipeline pilot이다. 승인
문서 20개와 frozen query 30개 이상에서 다시 실행하기 전에는 정확도 개선 수치를
portfolio 성과로 사용하지 않는다.

## RetrievedReference

각 검색 결과는 다음 provenance를 보존한다.

```text
retrieval_id
request_id
reference_document_id
source_class              operational-knowledge
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

현재 `reference-document`, `reference-index`, retrieval query/result/audit JSON Schema와
hash-pinned Git corpus, `GitReferenceDocumentRepository`, `BoundedKnowledgeRetriever`를
구현했다. Retriever는 `strategy=stategraph`인 Frozen Context의 Graph Entity에서만 key를
파생하며 caller가 Entity key를 직접 주입할 수 없다. approved/type/entity/time/hash
조건과 Top-K, character, timeout, query-term, index-scan budget을 적용하고 성공, no match,
stale only, timeout, repository failure를 구분한다. 기존 lexical baseline에 더해 opt-in
PostgreSQL/pgvector chunk index, OpenAI embedding adapter, vector-only와 RRF Hybrid ranking,
동기화 도구와 평가 지표를 구현했다.

RetrievedReference는 `evidence_id`를 가지지 않으며 현재 Incident에 관한 사실을 새로
만들지 않는다. Agent는 이 retrieval run에 포함된 문서 ID만
`inspect_reference(reference_document_id)`로 열 수 있다. Evidence Gate는 실제로 검사한
Reference만 별도 `reference_document_ids`로 인용하게 하고, Reference를 runtime Evidence
수나 distinct Evidence source 수에 포함하지 않는다. Agent message/tool loop와 Gate의
fixture contract는 구현했지만 API credit 부족으로 live model 성공은 아직 검증하지
못했다. pgvector live sync와 embedding benchmark도 아직 실행하지 않았으며 초기
2문서 pilot 결과는 portfolio claim으로 간주하지 않는다. Factual/Experiential Memory와
runtime evaluation도 아직 보류한다.

## Reference

- Jiang et al., “KRCA: An Efficient Root Cause Analysis System in Hyper-Scale
  Microservice Systems via Agentic AI,” ASE 2026,
  <https://arxiv.org/abs/2607.01788>
- Xiang et al., “Simplifying Root Cause Analysis in Kubernetes with StateGraph
  and LLM,” <https://arxiv.org/abs/2506.02490>
