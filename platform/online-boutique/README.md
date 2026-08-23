# Online Boutique target workload

[Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) is
Google's sample e-commerce application for demonstrating and testing
microservice environments. It is not a production storefront or part of the
Agent RCA product. This repository uses it as a controlled incident target.

The pinned `v0.10.6` overlay deploys 11 application and Redis workloads. Their
service-to-service calls provide realistic paths such as frontend -> checkout
-> cart, catalog, currency, shipping, payment, and email. That makes failures
observable across more than one Kubernetes resource or telemetry provider.

## Environment boundary

- The upstream source is pinned to commit
  `5b3a712ab85ccb8f6f7cd5b720d36ba9a8d041eb`.
- Application images use the upstream `v0.10.6` tag. The upstream mutable
  `redis:alpine` reference is replaced with a recorded OCI digest.
- `frontend-external` and the bundled `loadgenerator` are removed.
- All 11 target services and the telemetry Collector service remain
  `ClusterIP`; no Ingress is created.
- Redis uses ephemeral pod storage. This target creates no PVC and does not
  preserve cart data across pod replacement.
- The target and Collector remain ephemeral. Traces are retained separately by
  Tempo in the `observability` namespace.

## Direct application telemetry

The pinned upstream source already contains explicit OpenTelemetry SDK tracing
paths that are disabled by default. This overlay enables those code paths on
`checkoutservice`, `currencyservice`, `emailservice`, `frontend`,
`paymentservice`, `productcatalogservice`, and `recommendationservice` with an
explicit `OTEL_SERVICE_NAME` and the internal OTLP gRPC endpoint. It does not
inject an auto-instrumentation agent, sidecar proxy, or eBPF HTTP inference.

```text
instrumented application code
  -> OTLP spans
  -> OpenTelemetry Collector
       -> Tempo (trace storage, 72 hours)
       -> span_metrics -> Prometheus RED metrics
       -> service_graph -> Prometheus dependency-edge metrics
```

The span metrics include request count, duration histogram, error status and
bounded RPC dimensions under the `agent_rca_*` namespace. They are derived from
application-created spans, not custom business metrics emitted directly by the
service. The service graph metrics use the `traces_service_graph_*` namespace.
Cardinality, retention and resource use are bounded in the checked-in Collector
and Tempo configurations.

`adservice`, `cartservice`, and `shippingservice` do not have a complete
upstream server-side tracing path in this pinned release. Instrumented callers
still produce client spans for their edges, but internal server latency and
child spans for those three services are an explicit coverage gap. Adding that
coverage requires a controlled source fork and rebuilt digest-pinned images.

## Operations

Render the exact manifest without applying it:

```bash
make render-online-boutique
```

Deploy and verify the target through Ansible:

```bash
make deploy-online-boutique
make verify-online-boutique
```

Verification requires all 12 deployments to complete rollout, the exact
internal service set, pinned images, the approved seven-service instrumentation
settings, stable container restart counts without OOM kills, a working
frontend, no target PVC, and a normalized `online-boutique` log stream in Loki.
It also requires a healthy Collector Prometheus target, a non-zero
`agent_rca_calls_total` result and at least one stored Tempo trace.
