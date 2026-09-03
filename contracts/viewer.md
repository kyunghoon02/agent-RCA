# Read-only RCA Viewer Query Contract

> 상태: bounded query service, private API Deployment, same-origin BFF/UI와 DB-enforced read-only role 구현 및 live 검증

## 책임

Viewer query service는 저장된 Incident와 RCA artifact를 읽기 전용으로 조합한다.
Kubernetes, cloud, StateGraph 또는 Agent를 변경하거나 새로운 Evidence를 수집하지 않는다.

```text
IncidentRepository/PostgreSQL
→ bounded ViewerRepository query
→ IncidentViewerQueryService
→ schema-valid list 또는 detail document
→ authenticated read-only HTTP API
→ same-origin server-side BFF
→ read-only operator UI
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
- content-free Agent Run audit 최대 50개. 새 draft schema 실패는 원본 draft, 잘못된 값과
  validation message 없이 schema 이름, instance/schema JSON Pointer, keyword와 오류 개수만 포함
- Incident lifecycle audit 최대 1,000개
- 합성 timeline 최대 2,000개

각 상한을 넘으면 조용히 완전한 결과처럼 보이지 않도록 `truncated` flag를 반환한다.
timeline은 detection/lifecycle audit, Evidence 관측, Context freeze, Agent completion과
Report generation을 시간순으로 합친다.

`EVIDENCE_OBSERVED` event의 `occurred_at`은 Evidence `observed_at`이며 signal이 나타내는
시각이다. 수집 실행 시각이 아니므로 Incident 생성보다 앞설 수 있다. 같은 event의
`details.collected_at`은 저장된 `provenance.collected_at`을 그대로 노출하며 Provider
수집 pass를 식별한다. 두 값 모두 이미 저장된 non-secret 값이고 `claim_token`이나 lease
값은 노출하지 않는다. 이 field가 없는 과거 payload는 pass를 알 수 없는 것으로 취급하며
retry가 분리되었다고 표시하지 않는다. Evidence 원문 source로 추가 조회하거나 raw Secret,
Agent prompt/reasoning trace 또는 Ground Truth를 반환하지 않는다.

`GATE_DRAFT_CONTRACT_INVALID`인 새 Agent Run은 같은 제한된 contract failure 좌표를
상세 payload와 `AGENT_RUN_COMPLETED` timeline event에 노출한다. 이 정보는 어떤 계약 위치에서
fail-closed했는지 진단하기 위한 metadata이며 model content는 아니다. 이 필드가 없는 과거
Agent Run은 사후에 원본 field를 추정하지 않는다.

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
transport, Next.js UI와 server-side BFF를 포함한다. API는 private ClusterIP로 배포하고
별도 PostgreSQL role에 table `SELECT`만 부여하며 mutation 권한과 실제 mutation query를
모두 거부하는지 검증한다. authenticated list/detail/work request와 local BFF를 통한 live
Incident/Evidence 조회도 확인했다.

아직 UI의 cluster Deployment, public ingress/domain과 사용자 session/role 기반 인증은
없다. Grafana/Loki/Hubble deep link의 runtime allowlist와 production PostgreSQL query plan,
외부 노출을 전제로 한 rate limit도 미구현이다.
