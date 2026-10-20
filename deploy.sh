#!/usr/bin/env bash
# Deploy friday to GCP in one step:   ./deploy.sh
# Signs you in to Google if needed (browser opens), then ships GitHub's main branch
# (origin/main). Push to main first; local and unpushed commits are not deployed.
set -euo pipefail
cd "$(dirname "$0")"

if ! gcloud auth print-access-token >/dev/null 2>&1; then
  gcloud auth login vignesh@deepholistics.com --force --brief
fi

git fetch -q origin main
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/main)" ] || [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  echo "Note: deploying origin/main ($(git rev-parse --short origin/main)); your local checkout differs and is NOT deployed."
fi

deploy/gcp/deploy.sh ship
echo "Live at https://34-14-129-151.sslip.io"
