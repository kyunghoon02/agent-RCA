# Operational Knowledge

이 디렉터리는 Agent RCA가 bounded retrieval로 참고할 운영 문서 corpus다.
`index.yaml`이 승인 상태, 유효 기간, Entity key와 SHA-256 content hash를 고정하고,
`GitReferenceDocumentRepository`가 매 조회 시 index와 원문을 검증한다.

허용하는 문서 종류:

- architecture
- service catalog
- runbook
- SLO
- read-only tool guide

금지하는 데이터:

- evaluation Ground Truth와 fault injection 정답
- credential, token, private key와 Secret value
- raw incident telemetry dump
- 미검증 Agent 출력과 reasoning trace

문서는 `approved` review 상태, version, 유효 기간, Entity key와 content hash를 가진
index entry를 통해서만 Retriever에 노출한다. Retriever는 Frozen Context에서 파생한
localized Entity key와 metadata/time/hash 조건을 먼저 검사하고 lexical, vector 또는
RRF Hybrid 순위를 Top-K/문자/시간 budget 안에서 적용한다. 검색 결과는
`RetrievedReference`이며 `EvidenceItem`이 아니다.

Git 문서가 source of truth이고 PostgreSQL+pgvector는 opt-in derived index다. pgvector
동기화는 승인된 test/runtime DB와 `.env`의 `POSTGRES_DSN`, `OPENAI_API_KEY`를 준비한 뒤
`make sync-knowledge-vectors`로 실행한다. 정확도 비교는 같은 corpus와 query set에서
`make evaluate-knowledge-retrieval`로 실행하며 초기 2문서 benchmark 수치는 성과로
게시하지 않는다.

문서 수정 시 `shasum -a 256 knowledge/documents/<file>` 결과를 `index.yaml`의
`sha256:<digest>`에 반영해야 한다. 상세 계약은
[`contracts/knowledge-retrieval.md`](../contracts/knowledge-retrieval.md)를 따른다.
