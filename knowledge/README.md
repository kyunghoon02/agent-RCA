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
localized Entity key와 lexical term이 모두 맞는 문서만 Top-K/문자/시간 budget 안에서
반환한다. 검색 결과는 `RetrievedReference`이며 `EvidenceItem`이 아니다.

문서 수정 시 `shasum -a 256 knowledge/documents/<file>` 결과를 `index.yaml`의
`sha256:<digest>`에 반영해야 한다. 상세 계약은
[`contracts/knowledge-retrieval.md`](../contracts/knowledge-retrieval.md)를 따른다.
