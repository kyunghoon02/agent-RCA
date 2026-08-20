# Operational Knowledge

이 디렉터리는 Agent RCA가 bounded retrieval로 참고할 운영 문서 corpus를 위한
경계다. 현재는 contract만 확정했으며 runtime index와 Retriever는 구현하지 않았다.

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
index entry를 통해서만 runtime에 노출한다. 상세 계약은
[`contracts/knowledge-retrieval.md`](../contracts/knowledge-retrieval.md)를 따른다.
