#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gcloud_bin="${GCLOUD_BIN:-gcloud}"
region="${GCP_REGION:-asia-northeast3}"
repository="${ARTIFACT_REPOSITORY:-agent-rca-dev-workloads}"

for command_name in find shasum "$gcloud_bin"; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'required command is unavailable: %s\n' "$command_name" >&2
    exit 1
  }
done

project_id="${GCP_PROJECT_ID:-$($gcloud_bin config get-value project 2>/dev/null)}"
if [[ -z "$project_id" || "$project_id" == "(unset)" ]]; then
  printf 'GCP_PROJECT_ID or an active gcloud project is required.\n' >&2
  exit 1
fi

runtime_fingerprint="$({
  shasum -a 256 "$repo_root/requirements.txt"
  shasum -a 256 "$repo_root/platform/incident-platform/Dockerfile"
  shasum -a 256 "$repo_root/config/online-boutique-krca.yaml"
  find "$repo_root/src" "$repo_root/contracts" "$repo_root/db" \
    "$repo_root/knowledge" \
    -type f ! -name '*.pyc' -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 shasum -a 256
  shasum -a 256 "$repo_root/tools/run_stategraph_reconciler.py"
  shasum -a 256 "$repo_root/tools/run_incident_receiver.py"
  shasum -a 256 "$repo_root/tools/run_incident_worker.py"
  shasum -a 256 "$repo_root/tools/run_agent_worker.py"
  shasum -a 256 "$repo_root/tools/run_incident_viewer.py"
} | shasum -a 256 | cut -c1-12)"
image_tag="${IMAGE_TAG:-runtime-${runtime_fingerprint}}"
registry_prefix="${region}-docker.pkg.dev/${project_id}/${repository}"
builder_service_account="${BUILD_SERVICE_ACCOUNT:-projects/${project_id}/serviceAccounts/agent-rca-dev-image-builder@${project_id}.iam.gserviceaccount.com}"
source_staging_directory="gs://${project_id}-agent-rca-dev-cloudbuild-source/source"
build_context="$(mktemp -d)"
trap 'rm -rf "$build_context"' EXIT

cp "$repo_root/requirements.txt" "$build_context/requirements.txt"
cp "$repo_root/platform/incident-platform/Dockerfile" "$build_context/Dockerfile"
cp "$repo_root/platform/incident-platform/cloudbuild.yaml" "$build_context/cloudbuild.yaml"
cp -R "$repo_root/src" "$build_context/src"
cp -R "$repo_root/contracts" "$build_context/contracts"
cp -R "$repo_root/db" "$build_context/db"
cp -R "$repo_root/knowledge" "$build_context/knowledge"
mkdir -p "$build_context/config"
cp "$repo_root/config/online-boutique-krca.yaml" \
  "$build_context/config/online-boutique-krca.yaml"
mkdir -p "$build_context/tools"
cp "$repo_root/tools/run_stategraph_reconciler.py" \
  "$build_context/tools/run_stategraph_reconciler.py"
cp "$repo_root/tools/run_incident_receiver.py" \
  "$build_context/tools/run_incident_receiver.py"
cp "$repo_root/tools/run_incident_worker.py" \
  "$build_context/tools/run_incident_worker.py"
cp "$repo_root/tools/run_agent_worker.py" \
  "$build_context/tools/run_agent_worker.py"
cp "$repo_root/tools/run_incident_viewer.py" \
  "$build_context/tools/run_incident_viewer.py"
find "$build_context" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$build_context" -type f -name '*.pyc' -delete

$gcloud_bin builds submit "$build_context" \
  --project="$project_id" \
  --region="$region" \
  --config="$build_context/cloudbuild.yaml" \
  --gcs-source-staging-dir="$source_staging_directory" \
  --service-account="$builder_service_account" \
  --substitutions="_REGISTRY_PREFIX=${registry_prefix},_IMAGE_TAG=${image_tag}"

digest="$($gcloud_bin artifacts docker images describe \
  "${registry_prefix}/agent-rca-runtime:${image_tag}" \
  --project="$project_id" \
  --format='value(image_summary.digest)')"
printf 'IMAGE_TAG=%s\n' "$image_tag"
printf 'IMAGE_DIGEST=%s\n' "$digest"
