# Evaluation and Reliability Record

이 문서는 Agent RCA의 성능 수치를 홍보용 한 줄로 축약하지 않고, 어떤 runtime과
평가 경계에서 무엇을 측정했는지 기록한다. evaluation ID, cloud account 정보, Ground
Truth 원본과 원본 로그는 공개 문서에 넣지 않고 `evaluation/runs/private/`에만 보관한다.
공개 화면은 synthetic Incident artifact만 사용하며 runtime identifier는 일관된 별칭으로
치환한다. 판정, Evidence 수와 인용 관계는 변경하지 않는다.

## Method

- Variant C는 bounded read-only LLM Agent이며, Variant A는 같은 Frozen Context에
  등록된 원인별 증명 규칙을 적용하는 deterministic baseline이다.
- Ground Truth는 Agent Pod와 모델 입력에서 격리하고, 실행이 끝난 Prediction에만
  사후 결합한다.
- root cause는 자유 텍스트가 아니라 등록된 `cause_id`로 채점한다.
- Evidence precision/recall은 causal role을 기준으로 계산하며, 대체 가능한 동등
  Evidence는 같은 role로 묶는다.
- harness `PASSED`는 fault 주입, artifact 생성과 자동 복구가 성공했다는 뜻이다.
  Agent의 root cause가 맞았다는 뜻은 아니다.
- targeted smoke 한 번은 wiring regression을 찾는 검증이지 반복 정확도 통계가 아니다.

## Historical Baseline Before Corrections

2026-09-02에 4개 scenario를 각각 5회 실행한 frozen matrix는 20회 모두 harness와
cleanup을 완료했다. Agent Variant C의 결과는 다음과 같았다.

| Scenario | Expected-outcome match | Root-cause Top-1 | Outcome |
|---|---:|---:|---|
| checkoutservice OOMKilled | 0/5 | 0/5 | Gate rejected 5 |
| paymentservice ImagePullBackOff | 4/5 | 4/5 | Report 4, Gate rejected 1 |
| checkoutservice missing ConfigMap | 0/5 | 0/5 | Gate rejected 5 |
| frontend no-fault control | 5/5 | not applicable | correct `ABSTAIN` 5 |
| **Overall** | **9/20 (45%)** | **4/15 faults (26.7%)** | unsupported citation 0 |

Variant A는 같은 등록 scenario에서 20/20 expected outcome을 냈다. 이는 LLM이 근거를
날조했다는 결과가 아니라, Evidence가 충분한 경우에도 Agent와 Gate 사이의 인터페이스
및 결정 계약 때문에 유용한 Report가 탈락했다는 결과다. 전체 Agent 사용량은 LLM
41회, read-only tool 53회, 321,206 tokens였다. 당시 frozen model rate card가 없어서
비용은 계산하지 않았다.

## Failure Analysis

반복 결과와 후속 targeted run을 보존한 채 실패 경계를 추적해 다음 구조적 문제를
확인했다.

1. 모든 Gate rejection이 일반 reason 하나로 저장돼 운영자가 실패 원인을 구분하기
   어려웠다.
2. Kubernetes Event 조회가 resource name 중심이어서 오래된 Event나 재생성된 resource가
   bounded output을 먼저 소비할 수 있었다.
3. 모델이 긴 opaque Evidence ID를 tool argument로 다시 입력하면서 한 글자를 잘못
   복사할 수 있었다. tool은 이를 허용하지 않고 정상적으로 거부했지만 조사는
   `INCONCLUSIVE`로 끝났다.
4. Context completeness의 conclusive threshold는 Gate에만 있고 모델 hard rule에는 없어,
   Agent가 허용되지 않는 `CONCLUSIVE` 결정을 만든 뒤 사후 거절될 수 있었다.
5. root cause가 없는 no-fault Report에서 supporting Evidence가 비어 있는 `competing`
   가설도 evaluator가 예측 원인으로 계산해, 안전한 `INCONCLUSIVE`를 `AMBIGUOUS`로
   잘못 점수화할 수 있었다.

## Corrections

- Gate 실패를 내용이 노출되지 않는 machine-readable reason code로 분리했다.
- Event를 kind, name과 가능하면 exact Kubernetes UID로 제한하고, Incident window 적용과
  중복/series 집계를 output cap보다 먼저 수행한다.
- 모델에는 `E1`부터 시작하는 짧은 candidate reference만 보여주며, read-only tool이 이를
  exact Evidence ID로 해석한다. Report에는 tool이 반환한 실제 ID만 인용할 수 있다.
- collector failure와 Context completeness 정책을 모델 hard rule에 함께 전달한다.
  증명은 완전하지만 coverage가 부족하면 root cause를 보존한 `PARTIAL`, 증명이
  불완전하면 root cause가 없는 `INCONCLUSIVE`를 선택한다.
- `supported`와 `competing` hypothesis는 inspected supporting Evidence가 있을 때만
  허용하고, generic symptom이나 근거 없는 후보는 `unresolved`로 남긴다. evaluator도
  supporting Evidence가 없는 hypothesis를 predicted cause로 세지 않는다.
- Gate threshold는 낮추지 않았고, unknown/uninspected/out-of-scope citation 차단도
  그대로 유지했다.

## Corrected Runtime Targeted Verification

Event, short-reference와 Context-policy 수정을 포함한 동일한 fault-verification runtime
image에서 세 fault를 새 Incident로 한 번씩 재실행했다.

| Scenario | Agent decision | Top-1 | Evidence P/R | Unsupported citations | LLM/tool calls | Tokens | Agent wall time |
|---|---|---:|---:|---:|---:|---:|---:|
| OOMKilled | `CONCLUSIVE` | 1.0 | 1.0 / 1.0 | 0.0 | 2 / 3 | 14,720 | 16.47 s |
| missing ConfigMap | `CONCLUSIVE` | 1.0 | 1.0 / 1.0 | 0.0 | 2 / 2 | 13,813 | 15.87 s |
| ImagePullBackOff | `CONCLUSIVE` | 1.0 | 1.0 / 1.0 | 0.0 | 2 / 3 | 15,097 | 14.89 s |

각 실행은 fault-specific postcondition, Alertmanager 수신, Evidence 수집, StateGraph
localization, Agent tool 조사, Evidence Gate, Ground Truth scoring과 exact rollback을
통과했다. OOM은 StressChaos와 resource patch, ImagePull은 invalid image, missing
ConfigMap은 volume reference를 모두 제거했고 workload rollout 복구를 확인했다.

short-reference 수정 직후 첫 missing ConfigMap targeted run은 정답 Evidence 두 개를
인용했지만 `GATE_CONTEXT_INCOMPLETE`로 실패했다. 이 결과를 성공으로 재분류하지 않고
남겼고, 네 번째 원인인 hidden Context policy mismatch를 수정한 뒤 새 Incident에서
재검증했다.

이후 첫 no-fault targeted run에서 다섯 번째 평가 의미 문제를 발견했다. 이 run은
root cause가 없는 `inconclusive` Report와 unsupported citation 0을 저장했고 workload
postcondition도 통과했지만, supporting Evidence가 없는 `competing` 가설 두 개 때문에
evaluator가 `AMBIGUOUS`로 점수화했다. 이 결과도 덮어쓰지 않고 실패 artifact로 남겼다.

hypothesis status와 evaluator 조건을 함께 수정한 latest runtime에서 새 900초 no-fault
control을 실행한 결과는 다음과 같다.

| Agent Report | Evaluation outcome | Abstention correctness | Unsupported citations | LLM/tool calls | Tokens | Agent wall time |
|---|---|---:|---:|---:|---:|---:|
| `INCONCLUSIVE`, root cause null | `ABSTAIN` | 1.0 | 0.0 | 2 / 3 | 15,107 | 17.68 s |

최종 Report의 세 hypothesis는 모두 `unresolved`였고 supporting Evidence 수는 각각 0이었다.
Deployment와 Pod identity, restart snapshot은 900초 전후 동일했고, workload 종료와 lock,
tunnel, watchdog cleanup 후 모든 workload가 Ready인 것을 확인했다.

## Corrected Repeated Matrix

2026-09-03에 entity-bound Agent runtime을 고정하고, 동일 source commit에서 네 scenario를
각각 5회씩 새로 실행했다. 이 matrix는 이전 20회 결과를 재채점하거나 중간 회차를
재개한 것이 아니라, 수정된 runtime으로 처음부터 수행한 독립 실행이다.

| Scenario | Harness | Expected outcome | Top-1 / abstention | Evidence P/R | Unsupported citation | Median ingest-to-report | Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| checkoutservice OOMKilled | 5/5 | 5/5 `ROOT_CAUSE` | Top-1 5/5 | 1.0 / 1.0 | 0/5 | 22 s | 77,404 |
| paymentservice ImagePullBackOff | 5/5 | 5/5 `ROOT_CAUSE` | Top-1 5/5 | 1.0 / 1.0 | 0/5 | 21 s | 80,677 |
| checkoutservice missing ConfigMap | 5/5 | 5/5 `ROOT_CAUSE` | Top-1 5/5 | 1.0 / 1.0 | 0/5 | 22 s | 72,927 |
| frontend no-fault control | 5/5 | 5/5 `ABSTAIN` | abstain 5/5 | not applicable | 0/5 | 23 s | 83,992 |
| **Overall** | **20/20** | **20/20** | **fault Top-1 15/15; no-fault abstain 5/5** | **1.0 / 1.0 on 15 faults** | **0/20** | **23.25 s mean** | **315,000** |

전체 사용량은 LLM 41회와 read-only tool 58회였다. frozen model rate card가 없어 비용은
계산하지 않았다. 각 scenario의 deterministic bootstrap interval은 관측된 성공률에 대해
1.0–1.0, unsupported citation rate에 대해 0.0–0.0이었지만, scenario당 표본이 5개뿐인
동일 환경 반복이므로 이를 실제 장애 모집단에 대한 좁은 신뢰구간으로 해석하지 않는다.

fresh matrix 전에 두 preflight failure도 성공 결과와 분리해 보존했다. 첫 no-fault 실패는
애플리케이션이 아니라 장시간 controller-to-target SSH tunnel의 전송 오류 때문이었다.
target-local 대조 조회로 경계를 분리한 뒤 reconnecting supervisor를 추가했고, workload
시간에 종속된 절대 오류 개수 대신 사전 등록한 성공률 99% 이상, transport error rate 1%
이하 조건으로 availability gate를 고정했다. 다음 no-fault는 workload와 abstention은
정상이었지만 `GATE_ENTITY_OUT_OF_SCOPE`로 fail-closed 됐다. 모델이 보는 정확한 Entity ID
allowlist와 Gate가 검사하는 catalog를 일치시킨 뒤에만 새 20회를 시작했다. 실패 Report를
자동 수정하거나 Gate 조건을 낮추지는 않았다.

matrix 종료 후 12개 Online Boutique Deployment가 모두 Ready이고, fault lock과 활성
StressChaos가 없으며 checkoutservice와 paymentservice가 원래 replica 상태로 복구된 것을
별도 확인했다.

## Holdout v1 Preregistration

corrected matrix의 20/20이 같은 등록 fixture 반복에 과적합된 결과인지 확인하기 위해
Holdout v1 계약을 2026-09-03에 실행 전에 동결했다. 기존 세 root-cause family와 no-fault
control에서 각각 미사용 surface variant 3개, 총 12개를 한 번씩만 실행한다. memory limit,
observation window, invalid image reference, missing ConfigMap identity와 workload seed/rate를
변경하지만 새 Provider, cause ID, Agent prompt와 Evidence Gate 규칙은 추가하지 않는다.

각 scenario는 SHA-256으로 matrix에 고정된다. Agent는 scenario manifest와 Ground Truth를
받지 않으며 Alert name, summary, generator와 verification marker도 원인을 드러내지 않는
중립 case ID로 제한한다. Ground Truth는 실행 종료 후 Prediction에만 결합한다. 첫 시도 후
Agent prompt나 Gate를 수정해야 하면 Holdout v1을 이어서 실행하지 않고 새 Holdout v2를
등록한다. preregistration의 `frozen-unexecuted` 상태는 실행 전에 계약을 고정한 시점을
기록하며, 이후 runtime 결과는 아래에 분리해 기록한다.

## Holdout v1 Runtime Result

2026-09-03에 preregistration과 12개 scenario SHA-256을 검증한 뒤 같은 pinned Agent
runtime으로 matrix를 순차 실행했다. 실행 중 retry, resume, Agent prompt, Provider,
Evidence Gate와 taxonomy 변경은 없었다. Agent에는 중립 case metadata만 노출했고,
Ground Truth는 각 Agent Prediction이 완료된 뒤 scorer에서 결합했다.

| Family | Harness | Expected outcome | Top-1 / abstention | Evidence P/R | Unsupported citation | Mean ingest-to-report | Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| checkoutservice OOMKilled | 3/3 | 3/3 `ROOT_CAUSE` | Top-1 3/3 | 1.0 / 1.0 | 0/3 | 21.33 s | 44,149 |
| paymentservice ImagePullBackOff | 3/3 | 3/3 `ROOT_CAUSE` | Top-1 3/3 | 1.0 / 1.0 | 0/3 | 23.67 s | 46,719 |
| checkoutservice missing ConfigMap | 3/3 | 3/3 `ROOT_CAUSE` | Top-1 3/3 | 1.0 / 1.0 | 0/3 | 20.00 s | 43,110 |
| frontend no-fault control | 3/3 | 3/3 `ABSTAIN` | abstain 3/3 | not applicable | 0/3 | 26.00 s | 55,964 |
| **Overall** | **12/12** | **12/12** | **fault Top-1 9/9; no-fault abstain 3/3** | **1.0 / 1.0 on 9 faults** | **0/12** | **22.75 s** | **189,942** |

전체 사용량은 LLM 25회와 bounded read-only tool 34회였다. frozen model rate card가
없어 비용은 계산하지 않았다. matrix 종료 후 12개 Online Boutique Deployment가 모두
Ready이고, 활성 Chaos 객체, fault lock과 controlled-fault annotation이 없으며 변경했던
checkoutservice resource와 paymentservice image가 기준값으로 복구된 것을 별도 확인했다.

이 결과는 regression에 쓰지 않은 parameter와 workload surface variation에서도 등록된
세 원인군과 no-fault 분기가 기대대로 동작했다는 증거다. 그러나 family당 3개, variant당
1회, 같은 reference environment에서 얻은 결과이므로 알려지지 않은 원인이나 실제 장애
모집단의 정확도를 추정하는 수치로 해석하지 않는다.

## Holdout v1 Temporal Replication

최초 Holdout 결과를 Agent 수정에 사용하지 않고 같은 날 뒤 시점에 동일 scenario 12개를
clean `main`에서 다시 순차 실행했다. scenario digest와 pinned runtime은 최초 실행과 같았고,
retry, resume, prompt, Provider, Evidence Gate와 taxonomy 변경은 없었다. 이 실행은 더 이상
미관측 holdout이 아니므로 최초 Holdout 정확도에 합치지 않고 시간적 재현성 기록으로만 남긴다.

| Family | Harness | Expected outcome | Top-1 / abstention | Evidence P/R | Unsupported citation | Mean ingest-to-report | Tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| checkoutservice OOMKilled | 3/3 | 3/3 `ROOT_CAUSE` | Top-1 3/3 | 1.0 / 1.0 | 0/3 | 22.00 s | 46,176 |
| paymentservice ImagePullBackOff | 3/3 | 3/3 `ROOT_CAUSE` | Top-1 3/3 | 1.0 / 1.0 | 0/3 | 32.67 s | 48,343 |
| checkoutservice missing ConfigMap | 3/3 | 2/3 `ROOT_CAUSE`; 1 `FAILED` | Top-1 2/3 | 1.0 / 1.0 | 0/3 | 21.50 s on 2 reports | 44,383 |
| frontend no-fault control | 3/3 | 3/3 `ABSTAIN` | abstain 3/3 | not applicable | 0/3 | 22.33 s | 44,847 |
| **Overall** | **12/12** | **11/12** | **fault Top-1 8/9; no-fault abstain 3/3** | **1.0 / 1.0 on 9 faults** | **0/12** | **24.91 s on 11 reports** | **183,749** |

전체 사용량은 LLM 24회와 bounded read-only tool 32회였다. 모든 12개 run이 terminal
상태에 도달한 평균 시간은 24.75초였다. frozen model rate card가 없어 비용은 계산하지
않았다. 종료 후 12개 Deployment Ready, 활성 Chaos 객체 0, fault lock 0,
controlled-fault annotation 0과 변경 resource/image의 exact rollback을 확인했다.

불일치 1건은 missing ConfigMap variant에서 발생했다. Agent는 Ground Truth 역할과 일치하는
Evidence 두 개를 검사·인용해 Evidence precision/recall은 1.0/1.0이었지만, 최종 draft가
`agent-rca-draft.schema.json`을 만족하지 않아 Evidence Gate가
`GATE_DRAFT_CONTRACT_INVALID`로 fail-closed했다. root cause Report는 저장되지 않았고 실패를
재실행하거나 성공으로 재분류하지 않았다. 당시 감사 record는 원본 model draft와 schema
validation path를 보존하지 않아 어느 field가 위반됐는지는 사후 특정할 수 없다. 이후 runtime은
새 contract failure에 한해 원본 draft, 잘못된 값과 validation message 없이 schema 이름,
instance/schema JSON Pointer, keyword와 오류 개수만 audit와 Viewer에 저장하도록 보강했다.
이 변경으로 과거 실패 원인이 복원되거나 해당 11/12 결과가 바뀌지는 않는다.

## Claim Boundary and Next Gate

현재 말할 수 있는 것은 “고정된 한 runtime과 한 3-domain reference environment에서,
등록 regression 20/20과 별도 최초 Holdout surface variant 12/12가 기대 outcome을 냈지만,
동일 Holdout temporal replication은 11/12였고 unsupported citation은 두 Holdout 모두
없었다”까지다. 이는 같은 Evidence를 사용해도 LLM output contract의 비결정성이 전체
신뢰도를 제한할 수 있음을 보여준다. production 정확도나 알려지지 않은 장애에 대한
일반화 성능을 뜻하지 않는다.

현재 CI는 regression과 Holdout matrix 계약, runner, scorer와 safety policy를 core test로
검증한다. 실제 fault suite는 private runtime과 명시적 승인이 필요하므로 CI에서 자동
실행하지 않고 수동 release gate로 유지한다. privacy-safe contract failure telemetry는
구현·배포했다. SDK strict structured output은 명시적으로 고정했고 API가 지원하는 ID pattern과
array bound를 frozen draft contract에 맞췄다. `uniqueItems`와 조건부 의미 규칙은 API schema에
넣지 않고 독립 Evidence Gate가 계속 검증한다. 이 경계의 temporal reliability는
`structured-output-v1-preregistration.yaml`의 20회 계획은 3회 완료 후 운영자 요청으로
중단했으며 결과값을 주장하지 않는다. 후속 `structured-output-v2-preregistration.yaml`은
기존 4개 regression scenario를 2회씩 총 8회 재사용하도록 실행 전에 고정했다. 이는 output
contract의 작은 표본 반복 검증이지 새 사례 정확도나 일반화 평가가 아니다. 새 fault,
multi-factor 원인과 다른 cluster topology에 대한 평가는 별도 preregistration과 수치 경계를
사용하며 현재 결과와 합치지 않는다.

## Structured Output v2 Runtime Result

2026-09-03에 clean `main`과 pinned runtime에서 사전 등록한 8회를 순차 실행했다. 실행 중
retry, resume, Agent prompt, Provider, Evidence Gate와 taxonomy 변경은 없었다.

| Boundary | Observed result |
|---|---:|
| Completed and scored | 8/8 |
| Agent terminal reason | `REPORT_ACCEPTED` 8/8 |
| Model execution failure | 0/8 |
| Draft contract rejection | 0/8 |
| Unsupported Evidence citation | 0/8 |
| Secondary expected outcome | 8/8; fault Top-1 6/6, no-fault `ABSTAIN` 2/2 |
| Mean ingest-to-report | 25.00 s |
| Usage | LLM 17회, read-only tool 24회, 131,429 tokens |

frozen model rate card가 없어 비용은 계산하지 않았다. 평가 종료 후 fault target Deployment
12개가 모두 Available이고, 활성 Chaos 객체, fault lock과 controlled-fault marker가 모두
0개임을 별도 확인했다. 이 결과는 strict structured output과 독립 Evidence Gate 조합이
등록된 네 시나리오에서 두 번씩 재현됐다는 작은 표본 증거다. production reliability,
미관측 원인 정확도 또는 통계적 일반화를 뜻하지 않는다.

## Native Alert Runtime Check

### Sustained Impact Rule: Detection Failure

2026-09-05에 기존 checkout OOM scenario를 한 번 실행했다. 15분 정상 traffic으로 KRCA
6개 profile의 coverage를 먼저 확인한 뒤 resource limit 변경과 Chaos Mesh `StressChaos`로
실제 `OOMKilled`와 restart를 확인했다. 기존 matrix와 달리 firing/resolved Alert를 직접
제출하지 않고, 배포된 `OnlineBoutiqueCheckoutHighFailureRate` 규칙을 그대로 관찰했다.
Agent runtime, Provider, prompt, Gate, Alert 임계값과 `for: 2m`는 변경하지 않았다.

| Boundary | Observed result |
|---|---|
| Fault postcondition | 같은 대상 Pod에서 `OOMKilled`, restart 1회 이상 확인 |
| Native detection | `pending` 관측, `firing` 없음; 검증 실패 |
| Metric history | checkout 오류율 최대 약 7.09%; 5% 아래로 떨어져 2분 연속 유지 조건 미충족 |
| Incident / Agent / Report | 해당 실행 구간의 native Incident 0, Agent run 0, Report 0 |
| Cleanup | Deployment 12/12 Ready, Chaos 0, fault lock 0, controlled-fault marker 0, resource exact rollback |

이는 RCA가 잘못된 원인을 골랐다는 결과가 아니라, 짧은 OOM 증상과 현재 지속 오류율
Alert 정책의 조합이 Agent를 시작시키지 못한 결과다. 실패를 덮어쓰거나 자동 재실행하지
않았다. 감지 기록, 원본 OOM postcondition, Prometheus 시계열과 사후 read-only 검증은
ignored `evaluation/runs/private/native-prometheus/`에 보관한다. 기존 정확도 matrix에
합산하지 않으며, 이 실행에서는 자동 감지부터 Report까지의 성공을 주장할 수 없다.

이 결과로 지속적인 사용자 영향 감지와 짧은 OOM/restart 이벤트 감지의 목적을
분리했다. 또한 이 영향 기반 Alert의 source는 `frontend`이고
Kubernetes·Loki collector는 초기 source 범위만 조회하므로, KRCA가 지목한 하위 서비스의
원인 Evidence를 추가 수집하는 경로도 검증 대상이다. 이번에는 Agent가 시작되지 않아
그 경로의 runtime 성공·실패를 측정하지 못했다.

후속 정책은 기존 오류율 규칙을 유지하고 `OnlineBoutiqueRecentOOMRestart`를 별도로
구현·배포했다. 2026-09-05에 pinned promtool로 28개 격리 시나리오를 검증했고 중앙
Prometheus에서 rule health `ok`, `inactive`와 application Service 11개의 UID-bound
ownership mapping을 확인했다. 새 규칙의 Alert label은 Service 기준으로 정규화되어
해당 Service의 기존 collector 범위로 연결되는 것을 로컬 contract test로 검증했다.
이 배포 단계에서는 장애 재주입·평가용 Alert 제출·Agent runtime 변경을 하지 않았다.
이후 명시적으로 승인받은 별도 실행의 결과는 아래와 같다.

### OOM Event Rule: One Native End-to-End Run

2026-09-05에 `OnlineBoutiqueRecentOOMRestart`를 선택해 기존 checkout OOM fault를
**1회만** 실행했다. 기존 Agent image, model, Provider, prompt와 Evidence Gate는 유지했고,
firing/resolved Alert를 직접 제출하지 않았다. 새 규칙은 `checkoutservice`를 source로
사용하며 `krca_profile`이 없으므로 15분 API coverage warmup은 `--skip-tags warmup`으로
생략했다. 정상 workload, fresh Ready Pod, 장애 전 65초 metric 수집 대기, 최대 120초
StressChaos, OOM 확인 후 75초 관측과 기존 watchdog·exact rollback은 유지했다.

| Boundary | Observed result |
|---|---|
| Native detection | 실제 Pod `OOMKilled`·restart 1회 → Prometheus `firing` → Alertmanager → 정확히 매칭된 Incident |
| Agent / Gate | `gpt-5.6-luna`, `SUCCEEDED` / `REPORT_ACCEPTED`, `conclusive`, `kubernetes.container-oomkilled` |
| Supporting Evidence | 동일 fault Pod UID의 Kubernetes `OOMKilled`/exit 137, Prometheus restart delta 1, Loki cgroup OOM signature; 인용 3건 모두 조회·Context 포함 확인 |
| Latency / usage | Incident 수신 → Report 27초; Agent 약 15초; LLM 2회, read-only tool 3회, 15,722 tokens |
| Recovery | Deployment 12/12 Ready, Chaos·fault lock·marker 0, 원래 resource 복원, 새 Pod Ready/restart 0, 규칙 `inactive`와 자연 resolved webhook 확인 |

UTC 기준 Incident 수신 `06:31:37`, Context freeze `06:31:45`, Agent 실행
`06:31:49–06:32:04`, Report 저장 `06:32:04`, alert window 종료 `06:33:06`이다.
27초는 **Incident 수신부터**의 시간이며 장애 발생부터의 지연이나 반복 측정 SLO가 아니다.
resolved webhook은 alert window를 닫았고 Incident는 `REPORTED` 상태를 유지했다.
서비스 복원은 Agent 자동 조치가 아니라 fault harness가 수행했다.

이 결과는 OOM 신호를 가진 Alert에서 동일 Service의 원인 Evidence를 확인한 단일 연결성
검증이다. frontend 영향만으로 하위 서비스 원인을 찾는 능력, 메모리 고갈을 유발한
애플리케이션 동작, 원인 독립적인 정확도나 production 일반화를 입증하지 않는다.
앞선 영향 규칙의 감지 실패와 기존 matrix는 그대로 보존하며, 이번 성공을 합산하지 않는다.
원본 detection/bundle/result/recovery와 별도 postcheck는 ignored private 경로에 보관한다.

## Reproduce

### Native Alert Check

명시적 승인으로 **새 장애 1회를 주입**하는 명령은 다음과 같다. 기존 실행의 조회 명령이 아니다.

```bash
CONFIRM_CONTROLLED_FAULT=yes make verify-prometheus-rca
# Separate event-rule check; also injects one new real fault:
CONFIRM_CONTROLLED_FAULT=yes NATIVE_ALERT_NAME=OnlineBoutiqueRecentOOMRestart \
  make verify-prometheus-rca
```

위 Make target은 기본적으로 warmup을 포함한다. 위에 기록한 event-rule 실행의
정확한 warmup 생략 조건을 재현하려면 다음 명령을 사용한다. **새 장애를 주입하므로**
기존 결과를 확인하기 위한 조회 명령으로 실행하지 않는다.

```bash
ANSIBLE_CONFIG=automation/ansible/ansible.cfg .venv-ansible/bin/ansible-playbook \
  -i automation/ansible/inventories/dev.yml \
  -i automation/ansible/inventories/chaos-eval.yml \
  -i automation/ansible/inventories/observability.yml \
  automation/ansible/playbooks/verify-prometheus-rca.yml \
  --skip-tags warmup \
  --extra-vars native_alert_name=OnlineBoutiqueRecentOOMRestart \
  --extra-vars confirm_controlled_fault=yes \
  --extra-vars controlled_fault_environment=development
```

### Registered Matrices

```bash
make validate-core
make plan-structured-output-evaluation
CONFIRM_STRUCTURED_OUTPUT_EVALUATION="$(git rev-parse HEAD)" \
  make evaluate-structured-output-evaluation
make score-structured-output-evaluation \
  EVALUATION_MATRIX_MANIFEST=evaluation/runs/private/matrix/<run>/manifest.json
make plan-evaluation-matrix
CONFIRM_EVALUATION_MATRIX="$(git rev-parse HEAD)" make evaluate-matrix
make summarize-evaluation-matrix \
  EVALUATION_MATRIX_MANIFEST=evaluation/runs/private/matrix/<run>/manifest.json
make plan-holdout-matrix
CONFIRM_HOLDOUT_EVALUATION_MATRIX="$(git rev-parse HEAD)" \
  make evaluate-holdout-matrix
```

regression 실행 계획의 source of truth는
[Evaluation Preregistration](../evaluation/preregistration.yaml), 독립 holdout 경계는
[Holdout v1 Preregistration](../evaluation/holdout-v1-preregistration.yaml)이다.
