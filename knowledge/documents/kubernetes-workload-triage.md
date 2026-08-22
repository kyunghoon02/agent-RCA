# Kubernetes Workload Triage

Use this runbook only for a Kubernetes Entity already present in the frozen localized
Context.

## Failed mount or missing ConfigMap

1. Read the Pod status and recent Kubernetes Event records inside the Incident window.
2. Check whether a `FailedMount` Event identifies a required ConfigMap by kind and name.
3. Read only ConfigMap existence and metadata; never copy `data` or `binaryData` values.
4. Correlate the Event and resource-state Evidence by cluster, namespace, resource name,
   and observation time.
5. If the Event is absent, expired, or the ConfigMap query failed, record the gap instead
   of concluding that a missing ConfigMap caused the Incident.

## Other workload symptoms

- For an OOMKilled hypothesis, combine container termination and restart state with a
  bounded memory metric summary from the same workload and time window.
- For image pull failures, combine Pod waiting state with Kubernetes Event Evidence.
- For scheduling or volume symptoms, distinguish an empty Event list from API failure or
  Event retention expiry.

This runbook selects checks. Only current runtime Evidence can support or refute a root
cause.
