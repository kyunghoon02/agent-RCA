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
- All 11 services remain `ClusterIP`; no Ingress is created.
- Redis uses ephemeral pod storage. This target creates no PVC and does not
  preserve cart data across pod replacement.
- The current upstream base emits container logs but does not configure
  application Prometheus metrics or OpenTelemetry export. Kubernetes state,
  container logs, and Cilium/Hubble flow evidence are available now; explicit
  application telemetry integration is a later step.

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

Verification requires all deployments and pods to be ready, the exact internal
service set, pinned images, stable container restart counts without OOM kills, a
working frontend, no PVC, and a normalized `online-boutique` log stream in Loki.
