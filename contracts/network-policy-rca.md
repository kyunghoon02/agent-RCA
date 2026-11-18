# Network policy dependency diagnosis

상태: 2026-10-13 로컬 구현·fixture 검증. 신규 cloud 배포, 장애 주입, 실제 LLM 호출 없음.

## Claim

`network.policy-denied-dependency`는 등록된 caller → destination:port/TCP 통신에서
실제 policy denial과 해당 caller RPC error가 함께 관측됐다는 제한된 진단이다.
개별 frontend HTTP 요청의 원인, 모든 서비스 오류의 유일한 원인, 특정 정책 객체의 UID를
입증하지 않는다. 두 observation이 시간상 겹치는 것은 transaction-level causal proof와
다르므로 Report는 `PARTIAL`로 제한한다. 과거 네트워크 실험과 기존 정확도 matrix는
신규 Rule의 runtime 증거로 재분류하지 않는다.

## Flow

1. 운영자가 관리하는 [dependency config](../config/network-dependency-paths.json)를 읽는다.
   Alert와 Agent는 path, port, operation, PromQL을 임의 지정할 수 없다.
2. Hubble은 caller root의 `from` 방향을 time/flow/byte budget 안에서 조회한다.
   같은 namespace의 정확한 Deployment workload와 Pod prefix, destination port와 TCP,
   `DROPPED` 및 `POLICY_DENIED/POLICY_DENY`를 모두 대조한다. UUID 중복은 한 번만 센다.
3. Prometheus는 `SPAN_KIND_CLIENT`와 `STATUS_CODE_ERROR`인 caller operation의 counter를
   `increase(...[2m])`, 15초 query step으로 조회한다. 2분은 30초 scrape에서 increase를
   계산할 counter 표본을 확보하기 위한 고정 lookback이다. step은 scrape 주기가 아니다.
   RPC 완료 error span이 생성되지 않은 timeout은 놓칠 수 있으며 no-data는 정상으로 바꾸지 않는다.
4. 각각 Hubble/Prometheus Evidence를 만든다. raw flow, IP, L7 payload, source port는
   저장하지 않는다. destination port는 config에 등록된 값만 보존한다. Counter increase는
   Prometheus extrapolation 때문에 소수일 수 있으므로 실패 요청 수로 주장하지 않는다.
5. Projector는 caller/destination logical Service, configured `CALLS` relation, 관측 Event를
   만든다. configured relation은 실제 RPC 발생 증거로 사용할 수 없다.
6. Gate는 인용·inspection·Frozen Context 검사에 더해 Incident/cluster/namespace/path/
   caller operation이 일치하는지 검증한다. 실제 denial timestamp 하나 이상이 양수 RPC error
   sample의 2분 lookback 안에 있어야 한다. 전체 시간창의 maximum 두 개만 비교하지 않는다.
7. Root-cause Entity는 caller Service여야 한다. retention UNKNOWN과 aggregate attribution
   한계 때문에 `CONCLUSIVE`는 거절하고, matching proof가 있을 때 `PARTIAL`을 허용한다.

현재 path는 `frontend → productcatalogservice:3550/TCP`, caller operation
`hipstershop.ProductCatalogService/GetProduct`이다. frontend가 GetProduct를 호출하는 관계는
[pinned upstream source](https://github.com/GoogleCloudPlatform/microservices-demo/blob/5b3a712ab85ccb8f6f7cd5b720d36ba9a8d041eb/src/frontend/rpc.go)에서 확인했다.
실제 exporter가 반환하는 span name 및 status는 재배포 preflight에서 별도로 확인해야 한다.
CLIENT telemetry가 없으면 server Metric이나 KRCA correlation으로 대신 확정하지 않는다.

## Implementation and activation

- [Provider](../src/incident_platform/providers/network_policy.py)
- [Semantic validation and proof](../src/incident_platform/network_policy.py)
- [Projector](../src/incident_platform/projectors/network_policy.py)
- [Local pipeline and negative tests](../tests/test_network_policy_rca.py)

기본 collector는 이전 Hubble summary를 유지한다. 새 worker image를 빌드한 뒤 운영자가
`INCIDENT_WORKER_NETWORK_PATHS_CONFIG=/app/config/network-dependency-paths.json`을 설정하면
같은 `hubble` collector 안에서 신규 path Evidence를 추가한다. 두 수집은 하나의 provider
deadline과 Evidence item cap을 공유한다. path 수는 최대 8, path당 retained flow는 최대 100,
metric sample은 최대 1000이다. path 조회 실패 시 성공한 기존 summary는 `PARTIAL`로 유지한다.
API/RBAC write 권한이나 LLM tool은 추가하지 않는다.

RCA draft/report v1.2와 taxonomy v1.1을 추가했다. 과거 v1.1 draft/report JSON Schema는
변경하지 않고 기존 저장 artifact를 계속 읽는다. 새 network cause는 v1.1 출력 계약에서
거절된다. structured-output-v2 preregistration의 runtime hash는 그대로이며, 변경된 Agent
runtime은 `verify-structured-output-boundary`를 통과할 수 없다. 기존 ID로 재평가하지 말고
새 source/image/prompt/contract를 고정한 별도 등록을 만들어야 한다.

## Local checks and next runtime gate

```bash
make test-network-policy-rca
make validate-core
```

로컬 pipeline test는 mock Hubble/Prometheus와 fake Agent를 사용한다. 실제 Agent SDK의
model output 및 실제 Cilium/Prometheus 동작은 별도 검증 대상이다.

재배포 후 다음을 실행 전에 동결한 별도 scenario로 검증해야 한다.

- 정상 baseline: 실제 CLIENT span/error series, workload readiness와 Hubble 조회 확인.
- 정확히 한 정책 경로 차단: 승인된 namespace, 별도 lock/watchdog, 절대 deadline 및
  소유권을 확인한 exact rollback을 사용한다. 다른 workload 변경은 보존한다.
- Agent 보고: matching dependency Evidence → Frozen Context → tool inspection → Gate →
  PARTIAL Report를 확인하고, 실패와 rejection도 그대로 보존한다.
- 다른 destination/port의 drop, 시간 불일치, 정상 RPC, 다른 원인의 RPC failure,
  telemetry 유실·no-data에서는 네트워크 원인을 잘못 확정하지 않는지 확인한다.
- 복구: 정책 제거 후 동일 RPC 및 frontend request 성공을 확인한다. 자연 Alert와
  synthetic Alert 평가를 구분하고 detection latency와 ingest-to-report를 따로 측정한다.

과거 GCP 환경은 철거돼 있어 이 문서 작성 시 runtime gate는 실행하지 않았다.

## Production coverage direction

모든 Incident를 새로운 cause ID로 열거하지 않는다. 네트워크 정책, 인증, resource 압박,
잘못된 설정, dependency failure 등 재사용 가능한 원인군에 대해 Evidence predicate를 정의하고,
서비스·경로·threshold는 versioned profile로 관리한다. 새로운 telemetry 의미가 필요한 경우에는
Provider와 증명 계약을 추가해야 한다.

현재 구조에서는 미등록 원인을 `cause_id=null` 가설과 missing Evidence로 보고할 수 있지만
confirmed root cause로 승인할 수 없다. 후속 설계에서는 verified observation, supported
hypothesis, confirmed cause를 분리하고 공통 Gate가 citation/scope/time/identity를 검사하게 한다.
미지의 가설의 semantic truth를 구조 검증이나 높은 LLM confidence만으로 확정해서는 안 된다.
이 일반화된 Gate는 이번 변경에서 구현하지 않았다.
