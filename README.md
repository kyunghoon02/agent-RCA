# Cloud-Native Operations & RCA Lab

KT Cloud의 VM 위에 재현 가능한 self-managed Kubernetes 운영환경을 만들고,
Prometheus·Loki·Alertmanager·Kubernetes API·Cilium/Hubble에서 수집한 Evidence로
Google Online Boutique 장애를 분석하는 인프라/SRE 포트폴리오다.

Temporal StateGraph와 budget-bounded Agent RCA는 플랫폼의 목적 자체가 아니라,
운영자가 조사할 범위를 줄이고 모든 결론을 실제 Evidence로 추적하기 위한
자동화 계층이다.

## 확정된 실행 경계

- cloud target: KT Cloud
- 우선 대상 zone: `DX-M1`
- Kubernetes: VM 기반 self-managed cluster
- cluster bootstrap/configuration: Ansible
- CNI 및 network evidence: Cilium + Hubble
- target application: Google Online Boutique `v0.10.6`
- RCA 권한: read-only, bounded query, 근거 부족 시 `ABSTAIN`

KT Cloud 계정에서 사용할 인증·Compute·Network·Block Storage API와 표준
OpenStack provider 호환성은 아직 확인되지 않았다. 따라서 provider 이름과
Terraform 리소스를 추측해서 구현하지 않고 capability gate 뒤에 둔다.

## 포트폴리오에서 보여줄 역량

1. capability가 확인된 KT Cloud API를 Terraform으로 재현한다.
2. Ansible로 Kubernetes control plane/worker, Cilium과 Hubble을 구성하고 검증한다.
3. metric, log, event, resource state와 network flow를 Incident time window로 묶는다.
4. 반복 가능한 fault fixture와 수동 troubleshooting 결과를 자동 RCA와 비교한다.
5. 최소 권한, redaction, timeout, partial failure, 비용과 destroy 경계를 검증한다.
6. 모든 RCA 결론을 `evidence_id`로 추적하고 근거가 부족하면 판단을 보류한다.

AI/LLM 기능보다 인프라 생성, 관측, 장애 재현, troubleshooting과 운영 검증
Evidence를 먼저 제시한다.

## 현재 상태

- Cloud target: KT Cloud/DX-M1로 확정
- Provider capability: 계정별 API 및 Terraform provider 호환성 확인 대기
- Terraform: KT Cloud 구현은 capability gate로 차단; 이전 GCP/GKE HCL은 공개 baseline에서 제거
- Ansible: 공통 dependency만 유지; self-managed cluster 역할은 미구현
- Core: Alertmanager 입력, Incident lifecycle, Collector orchestration, Evidence,
  deterministic RCA, Fast Path JSON/Markdown report 구현
- Runtime proof: 아직 KT Cloud VM/Kubernetes에 배포하거나 검증하지 않음

현재 코드와 fixture의 정적 검증 성공은 cloud, Kubernetes, Prometheus, Loki 또는
Alertmanager의 실제 연결 성공을 의미하지 않는다.

## 데이터 수집 경계

- query에는 `online-boutique` namespace, workload와 Incident time window가 반드시
  포함된다.
- 원본 metric/log/network flow는 각 관측 시스템의 retention에 두고, Evidence와
  StateGraph에는 요약, provenance, content hash와 필요한 state만 저장한다.
- 검색 결과 없음, retention 만료, timeout과 provider 실패를 구분한다.
- eBPF/Hubble은 보조 evidence이며 모든 장애의 기본 원인을 대신 판정하지 않는다.

## 문서

- [KT Cloud 전환 로드맵](docs/roadmap/kt-cloud-plan.md)
- [KT Cloud 목표 아키텍처](docs/architecture/kt-cloud-overview.md)
- [KT Cloud capability matrix](docs/provider/kt-cloud-capability-matrix.md)
- [Phase 0 범위](docs/scope/project-scope.md)
- [현재 구현된 Core 아키텍처와 Flow](docs/architecture/implemented-core-flow.md)
- [Provider contract](contracts/providers.md)
- [AI-assisted 개발 원칙](docs/development/ai-assisted-workflow.md)

이전 GCP/GKE 실행물, 로컬 작업 계획과 개인 면접 메모는 공개 저장소에서
제외한다.

## 저장소 구조

```text
automation/ansible/  self-managed cluster 자동화 경계와 공통 dependency
config/              프로젝트 범위, KT capability, RCA routing 정책
contracts/           Incident, Evidence, Graph, RCA 및 provider 계약
docs/                아키텍처, ADR, runbook, 진행 기록
evaluation/          평가 사전등록과 Ground Truth 격리 정책
infra/terraform/     KT Cloud capability 확인 후 구현할 provisioning 경계
platform/            cloud-neutral Kubernetes manifest와 Kustomize base
src/                 Incident/Evidence/RCA core
tests/               deterministic fixture와 core unit test
tools/               정적 검증 도구
```

## 현재 가능한 검증

초기 1회 로컬 검증 환경을 만든다.

```bash
make bootstrap-dev
```

`requirements.txt`는 Core가 직접 import하는 런타임 라이브러리,
`requirements-dev.txt`는 위 의존성과 YAML 기반 로컬 검증 도구를 설치한다.

그다음 Core 검증을 실행한다.

```bash
make validate-core
```

이 검증은 contract, Alertmanager 입력 정규화, Incident lifecycle, Collector의
병렬 실행·timeout·retry·partial failure, Evidence redaction/hash, deterministic
RCA와 Fast Path report를 fixture로 확인한다.

KT Cloud capability gate 상태는 다음 파일에서 확인한다.

```bash
make ktcloud-readiness
```

필수 API가 확인되기 전에는 이 명령이 의도적으로 준비 미완료 상태를 반환한다.

Online Boutique remote base render에는 GitHub 접근이 필요하다.

```bash
kubectl kustomize platform/online-boutique
```
