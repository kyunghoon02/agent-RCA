# Evaluation and Reliability Record

이 문서는 Agent RCA의 성능 수치를 홍보용 한 줄로 축약하지 않고, 어떤 runtime과
평가 경계에서 무엇을 측정했는지 기록한다. Incident ID, evaluation ID, cloud account
정보와 원본 로그는 공개 문서에 넣지 않고 `evaluation/runs/private/`에만 보관한다.

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

## Claim Boundary and Next Gate

현재 말할 수 있는 것은 “세 등록 fault가 post-correction targeted smoke에서 각각
통과했고, latest runtime no-fault control은 올바르게 abstain했으며, unsupported citation은
없었다”까지다. fault 세 건과 no-fault 한 건이 모두 동일 latest image의 반복 표본은
아니다. 이를 100% 정확도나 production 일반화로 표현해서는 안 된다. 다음 신뢰도 gate는
동일 latest commit/runtime으로 다음 20회를 새로 실행하는 것이다.

- OOMKilled 5회
- ImagePullBackOff 5회
- missing ConfigMap 5회
- no-fault control 5회

그 결과에서 fault Top-1, no-fault abstention, Gate rejection reason 분포, Evidence
precision/recall, end-to-end latency와 token 사용량을 다시 집계한다. corrected 20-run이
완료되기 전에는 historical 45%와 targeted 3/3을 하나의 전후 정확도 수치로 비교하지
않는다.

## Reproduce

```bash
make validate-core
make plan-evaluation-matrix
CONFIRM_EVALUATION_MATRIX="$(git rev-parse HEAD)" make evaluate-matrix
make summarize-evaluation-matrix \
  EVALUATION_MATRIX_MANIFEST=evaluation/runs/private/matrix/<run>/manifest.json
```

실행 계획, fault safety boundary와 scorer 계약의 source of truth는
[Evaluation Preregistration](../evaluation/preregistration.yaml)이다.
