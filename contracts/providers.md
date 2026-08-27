# Provider interface와 GCP/kubeadm runtime 경계

> 상태: Phase 0 contract + Prometheus/Kubernetes adapter code
>
> 원칙: Reasoning 계층은 provider 원본 응답을 직접 읽지 않고 정규화된 contract만 사용한다.

## 공통 호출 규칙

모든 provider 호출은 다음 입력을 가진다.

```text
incident_id
time_window
resource_scope
query_budget
request_id
```

공통 결과 envelope:

```json
{
  "request_id": "req-...",
  "status": "SUCCEEDED | PARTIAL | FAILED | TIMED_OUT",
  "items": [],
  "next_page_token": null,
  "started_at": "RFC3339",
  "ended_at": "RFC3339",
  "error": null
}
```

- timeout, pagination과 최대 결과 수를 호출 전에 지정한다.
- provider 실패는 빈 성공 결과로 바꾸지 않는다.
- 모든 원본 위치와 query는 `EvidenceItem.provenance`로 남긴다.
- Reasoning 계층은 임의 query string을 실행하지 않고 allowlisted provider method만
  호출한다.
- GCP infrastructure API와 observability provider를 분리한다. RCA runtime은
  cloud resource를 생성·변경·삭제하지 않는다.

## interface

### MetricsProvider

```text
query_range(metric_query, time_window, step, resource_scope, sample_limit)
summarize_sli(service, baseline_window, incident_window, recovery_window)
```

반환값은 원본 sample 전체가 아니라 `metric-summary` EvidenceItem과 필요한 원본
series reference다.

- query에는 namespace와 workload selector를 포함한다.
- Pod 이름은 교체되므로 workload identity와 Pod UID를 함께 보존한다.
- 애플리케이션 metric이 없으면 kube-state-metrics/cAdvisor 기반 상태·resource
  Evidence만 제공하고 누락 source를 명시한다.

목표 runtime adapter는 in-cluster Prometheus HTTP API를 사용한다.

현재 `PrometheusMetricProvider`는 allowlisted PromQL template, namespace/resource
selector, time window, sample/response byte limit을 강제하고 resource별 summary를
반환한다. Incident worker는 in-cluster Prometheus endpoint에 request rate, failure rate,
p95 latency와 baseline p95 latency query 4개를 연결한다. trusted runtime `cluster_id`를
Evidence subject에 추가한 뒤 `PrometheusMetricEvidenceProjector`가 allowlisted scalar
summary만 logical Service의 time-bounded Event로 변환한다. raw sample과 query text는 Graph
attribute가 되지 않으며 metric Event는 `recent_change_evidence_ids`에서 제외한다.

Pod 이름이 rollout마다 달라지는 workload metric은 별도
`PrometheusWorkloadMetricProvider`가 처리한다. Incident의 exact Service root에서 만든
prefix만 PromQL selector에 허용하며, 반환된 각 series가 그 prefix 범위에 속하는지 다시
검사한다. `agent_rca_pod_memory_working_set_ratio` recording rule은 cAdvisor working set과
kube-state-metrics memory limit을 container별로 비교한다.
`agent_rca_pod_restart_count_delta`는 kube-state-metrics의 누적 restart counter를 30분
bounded increase로 변환한다. 두 rule 모두 `kube_pod_info`의 Pod UID를 결합한다. Provider는
UID가 없는 series나 범위 밖 Pod를 거부하고, series가 없으면 가상의 Pod Evidence를 만들지
않는다. Kubernetes API의 현재 `restartCount`를 시간 delta로 오인하지 않으며,
`OOMKilledRule`은 동일 UID의 Kubernetes `OOMKilled` 또는 검증된 kernel memcg OOM
Evidence 중 하나와 Prometheus restart delta가 함께 있을 때만 원인을 `PROVEN`으로
판정한다. 30초 scrape의 memory working-set ratio는 순간 OOM peak를 놓칠 수 있으므로
보조 관측으로 보존하지만 필수 gate나 supporting citation으로 사용하지 않는다. `Error`와
exit code 137만으로는 OOM을 판정하지 않는다.
`PrometheusWorkloadMetricEvidenceProjector`는 정규화된 summary를 동일 UID의 Kubernetes
Pod Entity에만 투영한다.

### PrometheusAPIFeatureProvider

```text
collect(
  allowlisted_api_dependencies,
  failure_rate_query,
  latency_query,
  qps_query,
  latency_baseline_query,
  time_window,
  edge/query/sample_budget
)
```

API dependency는 Agent가 만드는 문자열이 아니라 versioned `APIDependencySpec`으로
주입한다. Provider는 namespace/service/operation label exact match, 단일 series,
time window, edge/query/sample limit을 강제한다. 호출 전에 예상 query 수가 budget을
넘는지 확인하고, 원본 sample 대신 계산 feature와 계산 구간·선택 lag·reason code만
`metric-summary` Evidence로 반환한다.

`APIEdgeEvidenceProjector`는 `HAS_DATA`인 contract-valid Evidence만
`APIEdgeSignal`로 바꾼다. 필수 series 누락, sample truncation과 정렬 표본 부족은
`INSUFFICIENT_DATA` Evidence로 남고 KRCA signal이 되지 않는다. Incident worker는 Alert의
명시적 `krca_profile`만 버전 관리되는 profile로 해석하며, `prometheus-api` collector에
profile 전용 Service scope, 15분 lookback, edge/query/sample budget을 부여한다. profile
누락 시 기존 Service 수집만 수행하고, 알 수 없는 profile이나 Alert source와 alerting API가
다르면 fail-closed한다.

`KRCAPIEdgeEvidenceProjector`는 trusted runtime `cluster_id`가 있는 feature Evidence만
logical Service 간 `CALLS` relation으로 변환한다. 원본 sample, PromQL과 계산 feature를
Graph attribute로 복사하지 않으며, 이 metric-derived relation은
`recent_change_evidence_ids`에서 제외한다. 정확히 하나의 profile edge set이 모두 저장된
경우에만 KRCA drilldown을 실행한다. 완전히 resolve된 Top-N은 multi-seed scope가 되고,
Top-N이 없거나 budget이 소진되거나 Entity resolution이 불완전하면 exact source Service
scope로 fallback한다. 선택한 profile, seed source와 fallback reason은 Frozen Context의
correlation key로 고정한다. bounded BFS가 spanning tree를 만들더라도 선택된 subgraph에서
실제로 관측한 수렴·교차 relation의 현재 Incident Evidence는 버리지 않고 Context 전체
Evidence 집합에 포함한다.

### LogsProvider

```text
search(query_template_id, time_window, resource_scope, line_limit)
aggregate_patterns(time_window, resource_scope, group_limit)
```

query에는 namespace, service/Pod, severity와 time window를 반드시 포함한다.
credential과 개인정보를 redaction한 `log-pattern` EvidenceItem을 반환한다.

- label 또는 시간 범위가 없는 cluster-wide query는 거부한다.
- 원본 log는 Loki retention에 남기고 Graph에는 저장하지 않는다.
- 로그가 없다는 사실과 LogsProvider 실패를 구분한다.

현재 특수 목적 `LokiKernelOOMProvider`는 Alloy가 read-only host journal에서 수집한
`CONSTRAINT_MEMCG` kernel OOM만 Loki query API로 읽는다. 일반 application log pattern
adapter는 후속 범위다.

- 먼저 Kubernetes API에서 exact Service root로 파생된 Pod name/UID를 bounded list하고,
  그 UID regex를 Loki stream selector에 넣는다.
- Alloy가 추출한 `pod_uid` label과 원본 kernel `oom_memcg` cgroup path에서 Provider가
  다시 파싱한 UID가 일치해야만 `log-pattern` Evidence를 만든다.
- PID, container ID와 원본 kernel line은 Evidence/Graph에 복사하지 않고 match count,
  constraint, first/last match 시각만 정규화한다.
- 이미 삭제·교체되어 현재 Kubernetes UID mapping이 없는 과거 Pod는 추측으로
  reconciliation하지 않는다. 이 경우 수집은 fail-closed하며 장기 historical UID
  inventory는 후속 coverage 과제다.
- `LokiKernelOOMEvidenceProjector`는 검증된 aggregate만 동일 UID Pod의
  `KERNEL_CGROUP_OOM` Event로 변환한다.

### KubernetesStateProvider

```text
initial_list(resource_kinds, namespace, page_size)
watch(resource_kind, namespace, resource_version, timeout)
get(api_version, kind, namespace, name)
full_snapshot(resource_kinds, namespace, page_size)
```

- Kubernetes API verb는 `get/list/watch`뿐이다.
- Secret resource와 Secret value는 조회 대상이 아니다.
- Watch 종료나 compaction 발생 시 마지막 `resourceVersion` 이후 재연결하고 Full
  Snapshot으로 보정한다.
- Watch checkpoint는 namespace와 resource kind 단위로 저장한다.

목표 runtime adapter는 read-only ServiceAccount를 사용하는 in-cluster
Kubernetes API client다.

현재 `KubernetesStateProvider`는 allowlisted resource GET과 field-selected,
paged core/v1 Event list를 구현하고 만료된 pagination snapshot은 한 번 재시작한다.
Secret 조회와 write method는 제공하지 않으며
ConfigMap value와 Pod spec 원문을 Evidence로 복사하지 않는다. Watch/checkpoint와
실제 cluster ServiceAccount/CA 연결은 후속 runtime 작업이다. 각 adapter instance는
Alert 입력이 아닌 trusted runtime 설정의 `cluster_id`를 요구하고 모든 Kubernetes
Evidence subject에 포함한다.

### NetworkFlowProvider

```text
summarize_flows(time_window, resource_scope, direction, verdict, flow_limit)
find_drops(time_window, source_scope, destination_scope, reason_limit)
```

- Cilium/Hubble의 L3/L4/L7 flow는 namespace, workload와 time window로 제한한다.
- 원본 flow 전체를 StateGraph에 복사하지 않고 집계와 source reference만 저장한다.
- flow 없음, Hubble retention 만료와 provider 실패를 구분한다.
- Hubble evidence만으로 application root cause를 단정하지 않는다.

### DeploymentHistoryProvider

```text
list_revisions(deployment, namespace, time_window)
diff_revisions(deployment, namespace, from_revision, to_revision)
```

MVP에서는 Kubernetes Deployment/ReplicaSet 상태만 사용한다. GitHub와 Argo CD
이력은 2차 provider다. 현재 adapter는 exact Deployment GET과 namespace-bounded
ReplicaSet pagination만 수행하고 Deployment UID가 정확히 일치하는 owner revision만
retained history로 인정한다. Incident window 안 revision은 이전 retained pod template과
비교하며 image 원문은 basename과 fingerprint로, resource 설정은 CPU·memory·ephemeral
storage requests/limits로 제한한다. 변경이 없으면 `NO_CHANGES`, retention/truncation 때문에
판단이 불완전하면 `HISTORY_INCOMPLETE` Evidence를 반환한다.

### StateGraphRepository

```text
ingest(graph_records)
find_entities(exact_bounded_lookup)
find_state_paths(investigation_scope)
reconcile_projection(complete_graph_records, bounded_ownership_scope, observed_at)
```

현재 Port는 Evidence projection 저장, exact/time-bounded Entity resolution과 bounded
localization에 필요한 최소 연산만
노출한다. `ingest` 구현은 Entity를 먼저 upsert하고 연속된 동일 상태/관계만 병합해야
한다. Reasoning 계층은 Graph query language를 직접 생성하지 않고 repository method만
사용한다. Core record는 domain-neutral하며 Kubernetes와 다른 서비스 의미는 Evidence
projector가 변환한다.

`find_entities`는 trusted `cluster_id`, namespace, exact name, time window와 result
limit을 요구한다. `ServiceToEntityResolver`는 논리 Service를 우선하고 Kubernetes
Service를 fallback으로 사용하며, 복수 후보를 `AMBIGUOUS`로 반환한다. Incident history
pin과 garbage collection은 persistent backend 단계에서 별도 capability로 확장한다.
아직 구현되지 않은 capability를 현재 Port의 runtime 보장으로 표현하지 않는다.

`StateGraphReconciliationRepository`는 일반 `ingest`와 별도 capability다. Kubernetes
inventory가 `SUCCEEDED`이고 비어 있지 않을 때만 현재 projection을 반영하고, 동일한
`cluster_id + namespace + exact roots/root-derived prefixes + projector + managed types`
경계 안에서 사라진 Entity의 open Snapshot과 Relation interval을 한 transaction으로
닫는다. `PARTIAL`, timeout, 빈 결과나 범위 위반은 삭제로 해석하지 않으며 Graph를
변경하기 전에 실패한다.

### StateGraphObservationRepository

```text
stage_cycle(cycle, normalized_evidence)
mark_cycle_applied(cycle_id, reconciliation_result, applied_at)
get_cycle(cycle_id)
list_cycle_evidence(cycle_id)
prune_observations(now, batch_size)
```

주기 inventory Evidence는 Incident artifact와 수명이 다르므로
`IncidentRepository`에 저장하지 않는다. Reconciler는 정규화된 Evidence와 cycle을 먼저
`STAGED`로 저장한 뒤에만 StateGraph를 변경하고, Graph transaction 성공 뒤 `APPLIED`와
결과를 기록한다. Graph와 PostgreSQL 사이의 분산 transaction 대신 다음 fail-closed
재시도 규칙을 사용한다.

- journal 선저장이 실패하면 Graph를 변경하지 않는다.
- Graph 적용이 실패하면 cycle은 `STAGED`로 남는다.
- `STAGED` 재시도는 Provider를 다시 호출하지 않고 저장된 normalized Evidence를 투영한다.
- Graph 성공 뒤 `APPLIED` 기록이 실패하면 같은 cycle을 idempotent하게 재적용한다.
- 이미 `APPLIED`인 같은 cycle은 저장된 Evidence와 결과를 반환하고 Provider/Graph를 다시
  실행하지 않는다.
- 같은 cycle/request ID에 다른 scope, 시각 또는 Evidence가 들어오면 collision으로
  거부한다.

`APPLIED` cycle과 Evidence의 기본 보존은 72시간이고 abandoned `STAGED` cycle은
24시간이다. Graph ordinary history를 먼저 정리한 뒤 journal을 batch 단위로 정리한다.
complete-set reconciliation이 연장한 open Entity/Snapshot/Relation은 최신 cycle의
Evidence ID로 교체하며 일반 `ingest`의 Evidence ID merge 의미는 바꾸지 않는다. 현재
InMemory adapter와 PostgreSQL migration/adapter를 모두 구현했고, cluster-local
PostgreSQL journal과 Neo4j를 5분 Kubernetes CronJob에 연결했다. CronJob은
`concurrencyPolicy=Forbid`이며 각 실행이 migration을 idempotent하게 확인한 뒤 full
inventory cycle을 수행한다.

| 단계 | 구현 |
|---|---|
| Port | `StateGraphRepository` Protocol |
| fixture test | `InMemoryStateGraphRepository` adapter |
| Incident 연결 | `IncidentLocalizationService`가 Projector, Port와 Context 저장 연결 |
| seed resolution | `ResolvedIncidentLocalizationService`가 exact resolver 결과로 scope 생성 |
| complete-set reconciliation | `KubernetesStateGraphReconciler`와 InMemory/Neo4j atomic adapter 구현 |
| observation journal | InMemory/PostgreSQL adapter와 `STAGED -> APPLIED` retry contract 구현; cluster-local PostgreSQL live 배포 |
| GCP/kubeadm runtime | PostgreSQL journal→Neo4j projection을 수행하는 5분 serialized CronJob 배포 및 one-shot 검증 |

### IncidentRepository

```text
create_or_get_by_deduplication_key(incident)
transition(incident_id, expected_status, next_status)
store_evidence(incident_id, evidence_items)
freeze_context(context_package)
store_report(rca_report)
append_audit_event(incident_id, audit_event)
```

현재 MVP runtime은 cluster 내부 PostgreSQL adapter와 `agent-rca-local` 5Gi PVC다.
managed database는 범위가 아니며 single-node local-path `Retain`은 backup이 아니다.
VM disk 장애를 포함하는 backup/restore와 HA는 별도 운영 과제다.

### KnowledgeRetriever

```text
retrieve(
  incident_id,
  investigation_scope,
  localized_entity_keys,
  allowed_document_types,
  query_terms,
  top_k,
  character_budget,
  request_id
)
```

- StateGraph가 localization한 Entity 또는 승인한 다음 seed 범위만 조회한다.
- `approved`이고 Incident 시점에 유효한 reference 문서만 반환한다.
- 문서 ID, version, content hash, retrieval method와 rank를 보존한다.
- Ground Truth, fault injection 정답과 미검증 Agent 출력은 index에 포함하지 않는다.
- retrieved reference는 조사 힌트이며 `EvidenceItem`으로 변환하지 않는다.
- MVP는 metadata/entity/lexical retrieval을 사용하며 Vector DB를 요구하지 않는다.

상세 contract는 [`knowledge-retrieval.md`](knowledge-retrieval.md)를 따른다. Retriever
runtime과 ReferenceDocument schema는 아직 구현하지 않았다.

### LLMProvider

```text
generate_structured(task_type, agent_context, tool_schemas, output_schema, budget)
```

- `agent_context`는 Frozen Context Package와 bounded RetrievedReference만 포함한다.
- Context Package에 포함되지 않은 Evidence를 인용할 수 없다.
- reference citation과 Evidence citation을 서로 다른 ID로 출력한다.
- structured output은 저장 전에 JSON Schema와 evidence reference validation을
  통과해야 한다.
- Kubernetes/cloud write tool과 shell은 제공하지 않는다.
- model/provider 선택은 core contract와 분리하며 token, latency와 비용을 기록한다.

## 책임 분리

```text
Provider
-> 외부 시스템 조회, timeout, pagination, provenance

Collector
-> provider 결과 정규화, redaction, EvidenceItem 생성

Graph Localizer
-> 조사할 entity와 statepath 범위 결정

Knowledge Retriever
-> localized entity와 허용된 문서 종류에 맞는 versioned reference만 선택

Reasoning Controller
-> 동결된 Context Package와 bounded reference 안에서 tool-calling 조사와 가설 평가
```

Graph Localizer는 어디를 조사할지 결정하고 Knowledge Retriever는 어떤 운영 문서를
참고할지 제한한다. Reasoning Controller는 LLM API와 read-only tools를 조율하고,
Evidence Gate는 실제 Evidence가 어떤 원인을 지지하는지 검증한다.
