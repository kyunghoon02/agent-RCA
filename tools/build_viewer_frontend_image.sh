#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gcloud_bin="${GCLOUD_BIN:-gcloud}"
region="${GCP_REGION:-asia-northeast3}"
repository="${ARTIFACT_REPOSITORY:-agent-rca-dev-workloads}"
viewer_root="$repo_root/frontend/viewer"

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

viewer_fingerprint="$({
  shasum -a 256 \
    "$viewer_root/package.json" \
    "$viewer_root/package-lock.json" \
    "$viewer_root/next.config.mjs" \
    "$viewer_root/postcss.config.mjs" \
    "$viewer_root/tsconfig.json" \
    "$viewer_root/Dockerfile" \
    "$viewer_root/cloudbuild.yaml"
  find "$viewer_root/src" -type f -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 shasum -a 256
} | shasum -a 256 | cut -c1-12)"
image_tag="${IMAGE_TAG:-viewer-${viewer_fingerprint}}"
registry_prefix="${region}-docker.pkg.dev/${project_id}/${repository}"
builder_service_account="${BUILD_SERVICE_ACCOUNT:-projects/${project_id}/serviceAccounts/agent-rca-dev-image-builder@${project_id}.iam.gserviceaccount.com}"
source_staging_directory="gs://${project_id}-agent-rca-dev-cloudbuild-source/source"
build_context="$(mktemp -d)"
trap 'rm -rf "$build_context"' EXIT

cp \
  "$viewer_root/package.json" \
  "$viewer_root/package-lock.json" \
  "$viewer_root/next.config.mjs" \
  "$viewer_root/postcss.config.mjs" \
  "$viewer_root/tsconfig.json" \
  "$viewer_root/Dockerfile" \
  "$viewer_root/cloudbuild.yaml" \
  "$build_context/"
cp -R "$viewer_root/src" "$build_context/src"

"$gcloud_bin" builds submit "$build_context" \
  --project="$project_id" \
  --region="$region" \
  --config="$build_context/cloudbuild.yaml" \
  --gcs-source-staging-dir="$source_staging_directory" \
  --service-account="$builder_service_account" \
  --substitutions="_REGISTRY_PREFIX=${registry_prefix},_IMAGE_TAG=${image_tag}"

digest="$("$gcloud_bin" artifacts docker images describe \
  "${registry_prefix}/agent-rca-viewer:${image_tag}" \
  --project="$project_id" \
  --format='value(image_summary.digest)')"
printf 'IMAGE_TAG=%s\n' "$image_tag"
printf 'IMAGE_DIGEST=%s\n' "$digest"
