# Read-only RCA Viewer Query Contract

> 상태: bounded repository query port와 list/detail service 구현

## 책임

Viewer query service는 저장된 Incident와 RCA artifact를 읽기 전용으로 조합한다.
Kubernetes, cloud, StateGraph 또는 Agent를 변경하거나 새로운 Evidence를 수집하지 않는다.

```text
IncidentRepository/PostgreSQL
→ bounded ViewerRepository query
→ IncidentViewerQueryService
→ schema-valid list 또는 detail document
→ future HTTP API/UI
```

## Incident 목록

`viewer-incident-query.schema.json`은 status, severity, namespace, 최대 100자의 search,
1~100 page size와 opaque cursor만 허용한다. 정렬은
`updated_at DESC, incident_id DESC`이며 cursor는 이 두 key와 filter hash를 가진다.
다음 페이지에서 filter를 변경한 cursor는 거부한다. offset pagination은 사용하지 않는다.

목록 결과는 Incident 전체 payload가 아니라 다음 summary만 반환한다.

- Incident ID, lifecycle status, severity와 source
- triggered/updated timestamp와 alert name
- normalized source Entity
- partial/failed/timed-out collector 개수

## Incident 상세

상세 결과는 다음 저장 artifact를 Incident ID 하나로 묶는다.

- redaction/hash/provenance가 검증된 Evidence 최대 500개
- Frozen Context 최대 50개
- JSON/Markdown RCA Report 최대 50개
- content-free Agent Run audit 최대 50개
- Incident lifecycle audit 최대 1,000개
- 합성 timeline 최대 2,000개

각 상한을 넘으면 조용히 완전한 결과처럼 보이지 않도록 `truncated` flag를 반환한다.
timeline은 detection/lifecycle audit, Evidence 관측, Context freeze, Agent completion과
Report generation을 시간순으로 합친다. Evidence 원문 source로 추가 조회하거나 raw Secret,
Agent prompt/reasoning trace 또는 Ground Truth를 반환하지 않는다.

## 구현 경계

현재 구현은 Python service/repository contract와 fixture test 범위다. HTTP route, 사용자
인증/인가, UI, Grafana/Loki/Hubble deep link allowlist와 production PostgreSQL query plan은
아직 구현하거나 runtime 검증하지 않았다.
