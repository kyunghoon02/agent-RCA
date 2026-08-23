#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
gcloud_bin="${GCLOUD_BIN:-gcloud}"
region="${GCP_REGION:-asia-northeast3}"
repository="${ARTIFACT_REPOSITORY:-agent-rca-dev-workloads}"
upstream_repository="https://github.com/GoogleCloudPlatform/microservices-demo.git"
upstream_tag="v0.10.6"
upstream_commit="5b3a712ab85ccb8f6f7cd5b720d36ba9a8d041eb"

for command_name in git shasum "$gcloud_bin"; do
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

patch_fingerprint="$(
  cd "$repo_root/platform/online-boutique/source-patches"
  shasum -a 256 ./*.patch | LC_ALL=C sort | shasum -a 256 | cut -c1-12
)"
image_tag="${IMAGE_TAG:-${upstream_tag}-otel-${patch_fingerprint}}"
registry_prefix="${region}-docker.pkg.dev/${project_id}/${repository}"
builder_service_account="${BUILD_SERVICE_ACCOUNT:-projects/${project_id}/serviceAccounts/agent-rca-dev-image-builder@${project_id}.iam.gserviceaccount.com}"
source_staging_directory="gs://${project_id}-agent-rca-dev-cloudbuild-source/source"
worktree="$(mktemp -d)"
trap 'rm -rf "$worktree"' EXIT

git clone --quiet --depth 1 --branch "$upstream_tag" "$upstream_repository" "$worktree/upstream"
actual_commit="$(git -C "$worktree/upstream" rev-parse HEAD)"
if [[ "$actual_commit" != "$upstream_commit" ]]; then
  printf 'upstream tag moved: expected %s, got %s\n' "$upstream_commit" "$actual_commit" >&2
  exit 1
fi

for patch_file in "$repo_root"/platform/online-boutique/source-patches/*.patch; do
  git -C "$worktree/upstream" apply --check "$patch_file"
  git -C "$worktree/upstream" apply "$patch_file"
done

$gcloud_bin builds submit "$worktree/upstream" \
  --project="$project_id" \
  --region="$region" \
  --config="$repo_root/platform/online-boutique/cloudbuild-otel.yaml" \
  --gcs-source-staging-dir="$source_staging_directory" \
  --service-account="$builder_service_account" \
  --substitutions="_REGISTRY_PREFIX=${registry_prefix},_IMAGE_TAG=${image_tag}"

printf 'IMAGE_TAG=%s\n' "$image_tag"
for service in adservice cartservice shippingservice; do
  digest="$($gcloud_bin artifacts docker images describe \
    "${registry_prefix}/${service}:${image_tag}" \
    --project="$project_id" \
    --format='value(image_summary.digest)')"
  printf '%s_digest=%s\n' "$service" "$digest"
done
