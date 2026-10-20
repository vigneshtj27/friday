#!/usr/bin/env bash
# Push-to-deploy for friday with Cloud Build (no GitHub Actions). Safe to re-run.
#
#   deploy/gcp/setup-cloudbuild.sh <branch>     # normally main
#
# Creates, or keeps if present:
#   - the Cloud Build, IAP and OS Login APIs
#   - service account rv-fri-deployer, allowed only to SSH/scp to the rv-fri VM
#     over IAP (and write build logs); it cannot read the app's secrets
#   - a 2nd-gen GitHub connection rv-fri-github and a link to vigneshtj27/friday
#   - trigger rv-fri-deploy: a push to <branch> runs cloudbuild.yaml as that account
#
# One step is manual: the first run prints a URL where you authorize the Cloud
# Build GitHub app. Do that in the browser, then run this script again.
#
# Needs: gcloud logged in as a project Owner (or equivalent), and the VM from
# deploy/gcp/deploy.sh already in place.
set -euo pipefail

BRANCH=${1:-}
[ -n "$BRANCH" ] || { echo "usage: $0 <branch>   (the branch whose pushes deploy, normally main)" >&2; exit 2; }

PROJECT=${PROJECT:-rv-experiments}
REGION=${REGION:-asia-south1}
ZONE=${ZONE:-asia-south1-a}
GITHUB_REPO=${GITHUB_REPO:-vigneshtj27/friday}
TAG=rv-fri
VM=$TAG
VM_SA="$TAG-vm@$PROJECT.iam.gserviceaccount.com"
DEPLOYER_NAME=$TAG-deployer
DEPLOYER="$DEPLOYER_NAME@$PROJECT.iam.gserviceaccount.com"
ROLE_ID=rvFriDeployer
CONNECTION=$TAG-github
REPO=friday
TRIGGER=$TAG-deploy
G="gcloud --project=$PROJECT --quiet"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

say "APIs"
$G services enable cloudbuild.googleapis.com compute.googleapis.com iap.googleapis.com \
  oslogin.googleapis.com secretmanager.googleapis.com

PROJECT_NUMBER=$($G projects describe "$PROJECT" --format='value(projectNumber)')
CB_AGENT="service-$PROJECT_NUMBER@gcp-sa-cloudbuild.iam.gserviceaccount.com"

say "Cloud Build service agent (stores the GitHub token in Secret Manager)"
$G beta services identity create --service=cloudbuild.googleapis.com >/dev/null 2>&1 || true
$G projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$CB_AGENT" \
  --role=roles/secretmanager.admin --condition=None >/dev/null

say "Deploy service account $DEPLOYER"
exists $G iam service-accounts describe "$DEPLOYER" ||
  $G iam service-accounts create "$DEPLOYER_NAME" --display-name="friday push-to-deploy (Cloud Build)"
# A brand-new account takes a moment to become visible to IAM.
for _ in $(seq 10); do exists $G iam service-accounts describe "$DEPLOYER" && break; sleep 3; done

# gcloud compute ssh/scp reads the instance and the project (for OS Login settings).
PERMS=compute.instances.get,compute.projects.get
if exists $G iam roles describe "$ROLE_ID" --project="$PROJECT"; then
  $G iam roles update "$ROLE_ID" --project="$PROJECT" --permissions="$PERMS" >/dev/null
else
  $G iam roles create "$ROLE_ID" --project="$PROJECT" --title="friday deployer" \
    --description="Read the rv-fri VM and project metadata for gcloud compute ssh" \
    --permissions="$PERMS" --stage=GA >/dev/null
fi

bind_project() {  # role [condition]
  $G projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$DEPLOYER" \
    --role="$1" --condition="${2:-None}" >/dev/null
}
bind_project "projects/$PROJECT/roles/$ROLE_ID"
bind_project roles/logging.logWriter
# IAP tunnels can't be granted per instance from gcloud; limit the grant to SSH.
bind_project roles/iap.tunnelResourceAccessor \
  'expression=destination.port == 22,title=ssh-only,description=IAP TCP forwarding to port 22 only'
# sudo on this VM only (rv-fri-up and the app directory are root's).
$G compute instances add-iam-policy-binding "$VM" --zone="$ZONE" \
  --member="serviceAccount:$DEPLOYER" --role=roles/compute.osAdminLogin >/dev/null
# OS Login on a VM that runs as a service account needs actAs on that account.
$G iam service-accounts add-iam-policy-binding "$VM_SA" \
  --member="serviceAccount:$DEPLOYER" --role=roles/iam.serviceAccountUser >/dev/null

say "GitHub connection $CONNECTION ($REGION)"
if ! exists $G builds connections describe "$CONNECTION" --region="$REGION"; then
  $G builds connections create github "$CONNECTION" --region="$REGION"
fi
state=$($G builds connections describe "$CONNECTION" --region="$REGION" \
  --format='value(installationState.stage)')
if [ "$state" != COMPLETE ]; then
  echo
  echo "  The connection is waiting for you ($state). Open this in a browser, sign in to GitHub"
  echo "  as the owner of $GITHUB_REPO, authorize Cloud Build and install its app on the repo:"
  echo
  $G builds connections describe "$CONNECTION" --region="$REGION" \
    --format='value(installationState.actionUri)' | sed 's/^/    /'
  echo
  echo "  Then run this script again: $0 $BRANCH"
  exit 0
fi

say "Repository link $REPO -> github.com/$GITHUB_REPO"
exists $G builds repositories describe "$REPO" --connection="$CONNECTION" --region="$REGION" ||
  $G builds repositories create "$REPO" --connection="$CONNECTION" --region="$REGION" \
    --remote-uri="https://github.com/$GITHUB_REPO.git"

say "Trigger $TRIGGER: push to $BRANCH -> cloudbuild.yaml"
trigger_flags=(
  --region="$REGION"
  --repository="projects/$PROJECT/locations/$REGION/connections/$CONNECTION/repositories/$REPO"
  --branch-pattern="^${BRANCH}\$"
  --build-config=cloudbuild.yaml
  --service-account="projects/$PROJECT/serviceAccounts/$DEPLOYER"
  --description="Deploy friday to the rv-fri VM on every push to $BRANCH"
)
if exists $G builds triggers describe "$TRIGGER" --region="$REGION"; then
  $G builds triggers update github "$TRIGGER" "${trigger_flags[@]}"
else
  $G builds triggers create github --name="$TRIGGER" "${trigger_flags[@]}"
fi

say "Done"
echo "  Every push to $BRANCH now deploys. The branch must contain cloudbuild.yaml."
echo "  Run it now without pushing: gcloud builds triggers run $TRIGGER --region=$REGION --branch=$BRANCH --project=$PROJECT"
echo "  Watch:  https://console.cloud.google.com/cloud-build/builds;region=$REGION?project=$PROJECT"
