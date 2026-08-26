# Read-only RCA Viewer Query Contract

> 상태: bounded repository query port, list/detail/work-state service와 인증된 GET transport 구현

## 책임

Viewer query service는 저장된 Incident와 RCA artifact를 읽기 전용으로 조합한다.
Kubernetes, cloud, StateGraph 또는 Agent를 변경하거나 새로운 Evidence를 수집하지 않는다.

```text
IncidentRepository/PostgreSQL
→ bounded ViewerRepository query
→ IncidentViewerQueryService
→ schema-valid list 또는 detail document
→ authenticated read-only HTTP API
→ future same-origin BFF/UI
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

## Work 상태

`GET /api/v1/incidents/{incident_id}/work`는 collection, localization, analysis의
현재 work item을 stage별로 반환한다. stage가 아직 생성되지 않았으면 `null`이다.
state, attempt count, lease/claim/completion timestamp, worker ID, last error code와
고정된 Context ID만 노출하며 fenced write 권한인 `claim_token`은 조회하거나 반환하지 않는다.

## HTTP API

`IncidentViewerHTTPAPI`와 WSGI adapter는 다음 GET route만 제공한다.

- `GET /healthz`: 인증이 필요 없는 process health
- `GET /api/v1/incidents`: 반복 가능한 `status`, `severity`와 단일
  `namespace`, `search`, `limit`, `cursor` query
- `GET /api/v1/incidents/{incident_id}`: bounded artifact detail
- `GET /api/v1/incidents/{incident_id}/work`: 안전한 work-state projection

Viewer route에는 최소 16자 bearer token이 필요하다. 알 수 없는 query, 중복된 단일 query,
mutation method, query/response size 상한 초과를 거부하고 응답에는 `no-store`를 지정한다.
이 token은 브라우저 공개 환경변수에 넣지 않고, UI 연동 시 server-side BFF 또는 동등한
same-origin backend가 보관하는 것을 전제로 한다.

## 구현 경계

현재 구현은 Python service/repository contract, PostgreSQL adapter, 인증된 bounded WSGI
transport와 fixture test 범위다. 아직 이 API를 cluster에 배포하거나 runtime 검증하지
않았으며 사용자 session/role 기반 인증, UI/BFF, DB-enforced read-only role,
Grafana/Loki/Hubble deep link allowlist와 production PostgreSQL query plan도 미구현이다.
