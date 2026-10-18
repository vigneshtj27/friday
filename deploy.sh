#!/usr/bin/env bash
# Deploy friday to GCP in one step:   ./deploy.sh
# Signs you in to Google if needed (browser opens), then ships the current commit.
set -euo pipefail
cd "$(dirname "$0")"

if ! gcloud auth print-access-token >/dev/null 2>&1; then
  gcloud auth login vignesh@deepholistics.com --force --brief
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "Note: uncommitted changes are NOT deployed (only the last commit is)."
fi

deploy/gcp/deploy.sh ship
echo "Live at https://34-14-129-151.sslip.io"
