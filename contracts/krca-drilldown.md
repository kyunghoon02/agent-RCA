# KRCA-style API Drilldown Contract

이 계약은 KRCA 논문의 API-level drilldown을 이 프로젝트의 Evidence 경계에 맞게
적용한다. 논문의 production system 전체를 재현하거나 동일 정확도를 주장하지 않는다.

## 입력

각 호출 관계는 `APIEdgeSignal` 하나로 정규화한다.

```text
parent API
child API
failure-rate correlation + p-value
latency anomaly
latency fluctuation contribution
latency correlation
supporting evidence_ids
```

시계열 정렬, dynamic window, 최대 time lag와 p-value 계산은 metric feature provider의
책임이다. Core scorer는 실제 Evidence ID가 연결된 feature만 입력받으며 fixture 값을
live Prometheus 결과로 취급하지 않는다.

## 표기와 관측 구간

| 표기 | 의미 |
|---|---|
| `P` | 장애가 관측된 upstream Parent API |
| `C` | `P`가 호출하는 downstream Child API |
| `p_t`, `c_t` | 시점 `t`에서 `P`, `C`의 failure-rate 또는 latency 값 |
| `t_e` | Alert 발생 시점 |
| `t_s` | Alert 이전에 정상 상태에서 장애 상태로 전환됐다고 판단한 변곡점 |
| `tau` | downstream 신호가 upstream으로 전파되는 시차 |
| `L` | 평가할 최대 시차 |

논문은 고정된 최근 N분 전체가 아니라 `[t_s, t_e]`를 dynamic observation window로
사용한다. `t_s`는 `t_e` 이전 failure-rate 1차 차분의 부호가 마지막으로 바뀐
시점이다. `L=5`는 5초를 뜻하지 않고 metric sample interval 5개를 뜻한다.

## API edge 점수

### 식 1: 최종 전파 점수

$$
Score(P,C)=\max\left(Score_f(P,C), Score_l(P,C)\right)
$$

장애가 error 또는 latency 중 한 형태로만 강하게 나타날 수 있으므로 두 점수를
평균하지 않고 큰 값을 사용한다. 점수가 propagation threshold 이상일 때만 `C`를
의심 경로로 유지하고 `C`의 downstream을 계속 탐색한다.

### 식 2: failure-rate 점수

$$
Score_f(P,C)=
\max_{\tau \in [0,L]}
Corr\left(p[t_s,t_e], c[t_s-\tau,t_e-\tau]\right)
$$

downstream 장애가 upstream에 늦게 나타날 수 있으므로 `C`의 failure-rate 시계열을
최대 `L` sample까지 이동시키며 가장 높은 Pearson correlation을 선택한다. 논문은
`alpha=0.05` p-value 검정을 통과하지 못한 correlation을 0으로 만든다. 이 프로젝트의
Core scorer는 음의 correlation도 장애 전파 근거로 사용하지 않고 0으로 제한한다.

### 식 3: latency 점수

$$
Score_l(P,C)=w_1 A(C)+w_2 F(P,C)+w_3 C(P,C)
$$

- `A(C)`: downstream API 자체가 정상 latency baseline에서 벗어난 정도
- `F(P,C)`: downstream latency 변동이 upstream 변동에 기여할 가능성
- `C(P,C)`: 시차를 고려한 두 latency 추세의 Pearson correlation

논문의 production weight `(w_1, w_2, w_3)`는 `(0.2, 0.5, 0.3)`이다. Latency는
network jitter와 실행시간 변화의 영향을 함께 받기 때문에 correlation 하나만으로
판단하지 않는다.

### 식 4: latency anomaly degree

$$
A(C)=
\frac{1}{t_e-t_s+1}
\sum_{t=t_s}^{t_e}
\frac{|c_t-c_{base,t}|}{c_t}
$$

`C`의 현재 latency가 historical minimum baseline에서 평균적으로 얼마나 벗어났는지
측정한다. 논문의 `c_base,t`는 1시간, 1일, 1주 multi-granularity look-back으로 계산한
historical minimum latency다.

### 식 5: latency fluctuation contribution

$$
F(P,C)=
\frac{QPS_C}{QPS_P}
\cdot
\frac{\sum_{t=t_s}^{t_e}|c_t-c_{t-1}|}
{\sum_{t=t_s}^{t_e}|p_t-p_{t-1}|}
$$

첫 항은 upstream 요청 중 downstream으로 전달된 traffic 비중이고, 두 번째 항은
downstream과 upstream latency의 누적 변동량 비율이다. `C(P,C)`는 식 2와 동일한
time-lagged Pearson correlation을 latency 시계열에 적용한다.

이 식들은 인과관계를 증명하지 않는다. 높은 점수는 `C`의 변화가 `P`에 전파된
형태와 유사하다는 의미이며, 공통 원인과 correlated noise도 높은 점수를 만들 수
있다.

## 프로젝트 적용값

```text
failure_score = p_value <= 0.05 ? max(0, failure_rate_correlation) : 0

latency_score =
    0.2 * latency_anomaly
  + 0.5 * latency_fluctuation_contribution
  + 0.3 * max(0, latency_correlation)

edge_score = max(failure_score, latency_score)
```

기본 threshold는 `0.8`, maximum time lag는 `5`, service 후보는 Top-3다. 이 값은
논문의 production 설정을 초기 fixture default로 사용한 것이며 Online Boutique
fault evaluation 전에는 운영 threshold나 SLO로 간주하지 않는다.

논문 본문만으로는 `A`와 `F`의 clipping 또는 rescaling, 0인 QPS/변동량 분모,
missing sample 처리 정책이 완전히 정의되지 않는다. 이 프로젝트의 초기 adapter는
다음 보수적 정책을 고정했다.

- 모든 필수 series의 timestamp 교집합만 사용하고 최소 4개 aligned sample을 요구한다.
- Parent failure-rate 1차 차분의 마지막 부호 변화 이후를 dynamic window로 사용한다.
  남은 표본이 4개 미만이면 reason code를 남기고 전체 bounded window로 fallback한다.
- 최대 5 sample lag에서 가장 큰 Pearson correlation을 선택한다.
- failure correlation의 양측 p-value는 Student t 분포로 계산한다.
- anomaly와 fluctuation contribution은 `[0, 1]`로 clip한다.
- Parent QPS 또는 Parent latency variation 분모가 0이면 contribution을 0으로 만들고
  reason code를 남긴다.
- 필수 series 누락, truncation 또는 aligned sample 부족이면 feature Evidence는
  `INSUFFICIENT_DATA`이며 `APIEdgeSignal`을 생성하지 않는다.

위 정책과 threshold는 Ground Truth와 분리된 fault fixture 결과로 재조정해야 한다.

## 탐색과 fallback

1. Alert API에서 시작해 threshold 이상인 downstream edge만 재귀 탐색한다.
2. 모든 평가 edge와 supporting Evidence ID를 audit record에 남긴다.
3. API 후보를 점수순으로 정렬하고 service 단위 Top-N을 StateGraph seed 후보로
   반환한다.
4. Top-N 밖 후보는 버리지 않고 `next_ranked_candidates`로 보존한다.
5. missing observability, traversal budget exhaustion, Evidence 충돌, 복수 가설 또는
   복합 원인 의심 시에만 next-ranked 후보를 adaptive fallback에 제공한다.
6. fallback resolver가 승인하지 않은 Graph Entity로는 점프할 수 없다.
7. hard cap 안에서 충분한 Evidence를 얻지 못하면 root cause를 강제하지 않고
   `ABSTAIN`한다.

Adaptive fallback은 위 수식을 대체하거나 threshold를 임의로 낮추지 않는다. KRCA가
metric 누락으로 일찍 종료되거나 여러 cascading anomaly를 분리하지 못했을 때,
StateGraph의 탐색 seed, depth와 entity budget만 제한적으로 확장한다.

## 현재 구현 범위

| 단계 | 상태 |
|---|---|
| 식 1의 failure/latency maximum과 threshold 판정 | Core 구현·fixture 검증 |
| 식 3의 latency weight 계산 | Core 구현·fixture 검증 |
| p-value significance gate | Core 구현·fixture 검증 |
| threshold 기반 재귀 traversal과 service Top-N | Core 구현·fixture 검증 |
| 식 2의 dynamic window, max-lag Pearson와 p-value | adapter 구현·fixture 검증 |
| 식 4의 anomaly 계산 | supplied baseline series 기반 adapter 구현·fixture 검증 |
| 식 4의 live multi-look-back baseline PromQL | query template/runtime 미검증 |
| 식 5의 QPS·latency fluctuation 계산 | adapter 구현·fixture 검증 |
| feature Evidence → Top-N → Entity seed | in-memory 연결 fixture 검증 |

따라서 현재 fixture 검증은 scoring과 traversal code의 결정성을 증명하지만, 실제
Prometheus 시계열에서 feature가 올바르게 생성된다는 runtime proof는 아니다.

## Runtime 미구현 경계

- live API metric/operation label과 baseline PromQL 검증
- API dependency graph discovery, versioning과 `APIDependencySpec` 공급 adapter
- Agent assessment와 adaptive 재수집 loop
- KRCA skeleton graph와 memory-augmented multi-agent의 전체 재현

## Reference

- Jiang et al., “KRCA: An Efficient Root Cause Analysis System in Hyper-Scale
  Microservice Systems via Agentic AI,” ASE 2026,
  <https://doi.org/10.1145/3832783.3834468>
