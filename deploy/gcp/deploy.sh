#!/usr/bin/env bash
# Deploy friday to one Compute Engine VM. Safe to re-run: existing resources are
# kept, and the app is rebuilt from GitHub's main branch (origin/main, fetched
# first; set DEPLOY_REF=HEAD to ship a local commit instead).
#
#   deploy/gcp/deploy.sh            # create or update everything, ship the code
#   deploy/gcp/deploy.sh ship       # only ship the code and restart
#   deploy/gcp/deploy.sh tunnel     # open the portal at http://127.0.0.1:8765
#   deploy/gcp/deploy.sh public     # serve the portal on https://<ip>.sslip.io (password-only protection)
#   deploy/gcp/deploy.sh destroy    # delete everything labelled app=rv-fri (asks first)
#
# Every billable resource carries the label app=rv-fri, so Billing reports can
# filter on it. Nothing listens on the internet: the only ingress is SSH from
# Google's IAP range, and the portal is reached through an IAP SSH tunnel.
#
# Needs: gcloud authenticated as someone who can administer the project, git,
# python3. The portal password and the Anthropic key are asked for once
# (or read from FRIDAY_PORTAL_PASSWORD / ANTHROPIC_API_KEY) and go straight into
# Secret Manager; they are never printed.
set -euo pipefail

PROJECT=${PROJECT:-rv-experiments}
REGION=${REGION:-asia-south1}          # Mumbai: an Indian egress IP, matching Asia/Kolkata
ZONE=${ZONE:-asia-south1-a}
MACHINE=${MACHINE:-e2-standard-2}      # 2 vCPU, 8 GB: Chrome plus the app with headroom
DATA_GB=${DATA_GB:-30}
TIMEZONE=${TIMEZONE:-Asia/Kolkata}
LOCALE=${LOCALE:-en-IN}
TAG=rv-fri
LABELS="app=$TAG"
VM=$TAG
NET=$TAG-net
SUBNET=$TAG-subnet
IP=$TAG-ip
DATA_DISK=$TAG-data
SA_NAME=$TAG-vm
SA="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
SECRETS=(rv-fri-master-key rv-fri-admin-password-hash rv-fri-anthropic-api-key)
OPTIONAL_SECRETS=(rv-fri-google-client-id rv-fri-google-client-secret)
DEPLOY_REF=${DEPLOY_REF:-origin/main}
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
G="gcloud --project=$PROJECT --quiet"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
exists() { "$@" >/dev/null 2>&1; }

ssh_vm() { $G compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap -- "$@"; }

ensure_apis() {
  say "Enabling APIs"
  $G services enable compute.googleapis.com secretmanager.googleapis.com iap.googleapis.com
}

ensure_network() {
  say "Network $NET (IAP SSH only)"
  exists $G compute networks describe "$NET" || $G compute networks create "$NET" --subnet-mode=custom
  exists $G compute networks subnets describe "$SUBNET" --region="$REGION" ||
    $G compute networks subnets create "$SUBNET" --network="$NET" --region="$REGION" --range=10.88.0.0/24 \
      --enable-private-ip-google-access
  exists $G compute firewall-rules describe "$TAG-allow-iap-ssh" ||
    $G compute firewall-rules create "$TAG-allow-iap-ssh" --network="$NET" --direction=INGRESS \
      --action=allow --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags="$TAG" \
      --description="SSH (and the portal tunnel) through Identity-Aware Proxy only"
  say "Static IP $IP (a stable egress address banks can get used to)"
  exists $G compute addresses describe "$IP" --region="$REGION" ||
    $G compute addresses create "$IP" --region="$REGION" --network-tier=PREMIUM
  $G compute addresses update "$IP" --region="$REGION" --update-labels="$LABELS"
}

ensure_service_account() {
  say "Service account $SA"
  exists $G iam service-accounts describe "$SA" ||
    $G iam service-accounts create "$SA_NAME" --display-name="friday VM ($TAG)"
  for role in roles/logging.logWriter roles/monitoring.metricWriter; do
    $G projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$SA" --role="$role" \
      --condition=None >/dev/null
  done
}

put_secret() {  # name, value on stdin
  local name=$1
  if ! exists $G secrets describe "$name"; then
    $G secrets create "$name" --replication-policy=user-managed --locations="$REGION" --labels="$LABELS"
  fi
  $G secrets versions add "$name" --data-file=- >/dev/null
}

ensure_secrets() {
  say "Secrets in Secret Manager"
  cd "$ROOT"
  if ! exists $G secrets versions access latest --secret=rv-fri-master-key; then
    python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode(),end="")' | put_secret rv-fri-master-key
    echo "  created rv-fri-master-key (back it up: gcloud secrets versions access latest --secret=rv-fri-master-key)"
  fi
  if ! exists $G secrets versions access latest --secret=rv-fri-admin-password-hash; then
    local pw=${FRIDAY_PORTAL_PASSWORD:-}
    if [ -z "$pw" ]; then
      read -rsp "  Choose a portal password (12+ characters): " pw; echo
    fi
    [ ${#pw} -ge 12 ] || { echo "  too short" >&2; exit 1; }
    FRIDAY_PW="$pw" uv run --quiet python -c \
      'import os,sys; from friday.web.auth import hash_password; sys.stdout.write(hash_password(os.environ["FRIDAY_PW"]))' |
      put_secret rv-fri-admin-password-hash
    echo "  stored the portal password hash"
  fi
  if ! exists $G secrets versions access latest --secret=rv-fri-anthropic-api-key; then
    local key=${ANTHROPIC_API_KEY:-}
    if [ -z "$key" ]; then
      read -rsp "  Anthropic API key: " key; echo
    fi
    printf '%s' "$key" | put_secret rv-fri-anthropic-api-key
    echo "  stored the Anthropic API key"
  fi
  for s in "${SECRETS[@]}"; do
    $G secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done
}

ensure_vm() {
  say "Data disk $DATA_DISK (${DATA_GB} GB, survives VM rebuilds)"
  exists $G compute disks describe "$DATA_DISK" --zone="$ZONE" ||
    $G compute disks create "$DATA_DISK" --zone="$ZONE" --size="${DATA_GB}GB" --type=pd-balanced --labels="$LABELS"

  say "VM $VM ($MACHINE in $ZONE)"
  if ! exists $G compute instances describe "$VM" --zone="$ZONE"; then
    $G compute instances create "$VM" --zone="$ZONE" --machine-type="$MACHINE" \
      --network-interface="subnet=$SUBNET,address=$IP,network-tier=PREMIUM" \
      --image-family=debian-12 --image-project=debian-cloud \
      --boot-disk-size=30GB --boot-disk-type=pd-balanced --boot-disk-device-name="$TAG-boot" \
      --disk="name=$DATA_DISK,device-name=friday-data,mode=rw,auto-delete=no" \
      --service-account="$SA" --scopes=cloud-platform \
      --tags="$TAG" --labels="$LABELS" \
      --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
      --metadata=enable-oslogin=TRUE,friday-timezone="$TIMEZONE",friday-locale="$LOCALE",friday-public-url=http://127.0.0.1:8765 \
      --metadata-from-file=startup-script="$HERE/startup.sh"
  else
    $G compute instances add-metadata "$VM" --zone="$ZONE" --metadata-from-file=startup-script="$HERE/startup.sh"
  fi
  # Instance labels do not flow to its disks; label the boot disk explicitly.
  $G compute disks add-labels "$VM" --zone="$ZONE" --labels="$LABELS"

  say "Waiting for first-boot setup (Docker, data disk)"
  for _ in $(seq 60); do
    if ssh_vm "test -f /var/lib/rv-fri-ready" 2>/dev/null; then echo "  ready"; return; fi
    sleep 10
  done
  echo "  timed out; check: gcloud compute ssh $VM --zone=$ZONE --tunnel-through-iap -- sudo tail /var/log/rv-fri-startup.log" >&2
  exit 1
}

ship() {
  # Ships DEPLOY_REF (origin/main by default). From Cloud Build (cloudbuild.yaml)
  # COMMIT_SHA is set and the pushed commit is already checked out, possibly
  # without .git, in which case the tree is tarred as is.
  local ref=$DEPLOY_REF rev full built tgz remote
  if [ -n "${COMMIT_SHA:-}" ]; then
    ref=HEAD
  elif [ "${ref#origin/}" != "$ref" ]; then
    git -C "$ROOT" fetch -q origin "${ref#origin/}"
  fi
  full=${COMMIT_SHA:-$(git -C "$ROOT" rev-parse "$ref")}
  rev=${full:0:7}
  built=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  say "Shipping $ref ($rev) and starting friday (the first build takes a few minutes)"
  tgz=$(mktemp "${TMPDIR:-/tmp}/friday-XXXXXX")
  if git -C "$ROOT" rev-parse --verify -q "$ref" >/dev/null 2>&1; then
    git -C "$ROOT" archive --format=tar.gz -o "$tgz" "$ref"
  else
    tar -czf "$tgz" -C "$ROOT" --exclude=./.git .
  fi
  remote="/tmp/friday-$rev-$RANDOM.tgz"
  $G compute scp "$tgz" "$VM:$remote" --zone="$ZONE" --tunnel-through-iap
  rm -f "$tgz"
  # flock: a push-triggered build and a manual ship never swap the app dir at once.
  ssh_vm "sudo flock -w 900 /var/lock/rv-fri-deploy bash -c 'rm -rf /opt/friday/app.new && mkdir -p /opt/friday/app.new && tar -xzf $remote -C /opt/friday/app.new \
    && printf %s '\''{\"sha\": \"$full\", \"ref\": \"$ref\", \"built_at\": \"$built\"}'\'' > /opt/friday/app.new/friday/BUILD_INFO \
    && rm -rf /opt/friday/app.old && { [ ! -d /opt/friday/app ] || mv /opt/friday/app /opt/friday/app.old; } \
    && mv /opt/friday/app.new /opt/friday/app && rm $remote && /usr/local/bin/rv-fri-up'"
  say "Health"
  for _ in $(seq 30); do
    if out=$(ssh_vm "curl -fsS http://127.0.0.1:8765/healthz" 2>/dev/null); then
      echo "  $out"
      case $out in *"\"$rev\""*) return ;; esac
      echo "  ...still the old version, waiting"  # the old container answers until the new one is up
    fi
    sleep 5
  done
  echo "  friday did not answer on 127.0.0.1:8765/healthz within 150s" >&2
  exit 1
}

public() {
  local ip domain
  ip=$($G compute addresses describe "$IP" --region="$REGION" --format='value(address)')
  domain="${DOMAIN:-${ip//./-}.sslip.io}"
  echo "This makes the portal reachable from the whole internet at https://$domain,"
  echo "protected only by the portal password (with login backoff)."
  say "Firewall: HTTP/HTTPS from anywhere (Caddy; port 80 is for the certificate challenge and redirects)"
  exists $G compute firewall-rules describe "$TAG-allow-web" ||
    $G compute firewall-rules create "$TAG-allow-web" --network="$NET" --direction=INGRESS \
      --action=allow --rules=tcp:80,tcp:443 --source-ranges=0.0.0.0/0 --target-tags="$TAG" \
      --description="friday portal over HTTPS (Caddy)"
  say "Pointing the app at https://$domain"
  $G compute instances add-metadata "$VM" --zone="$ZONE" \
    --metadata=friday-domain="$domain",friday-public-url="https://$domain" \
    --metadata-from-file=startup-script="$HERE/startup.sh"
  # Re-run the startup script so rv-fri-up learns about public mode.
  ssh_vm "sudo google_metadata_script_runner startup >/dev/null 2>&1 || true"
  ship
  say "Waiting for the certificate"
  for _ in $(seq 40); do
    if curl -fsS -o /dev/null "https://$domain/healthz" 2>/dev/null; then echo "  https://$domain is live"; return; fi
    sleep 6
  done
  echo "  not answering yet; Let's Encrypt can take a few minutes. Check: curl -v https://$domain/healthz" >&2
}

private() {
  say "Closing public access"
  $G compute firewall-rules delete "$TAG-allow-web" || true
  $G compute instances remove-metadata "$VM" --zone="$ZONE" --keys=friday-domain || true
  $G compute instances add-metadata "$VM" --zone="$ZONE" --metadata=friday-public-url=http://127.0.0.1:8765
  ssh_vm "sudo docker rm -f app-caddy-1 >/dev/null 2>&1 || true"
  ship
}

tunnel() {
  echo "Portal: http://127.0.0.1:8765  (Ctrl+C to close the tunnel)"
  $G compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap -- -N -L 8765:127.0.0.1:8765
}

destroy() {
  echo "This deletes the VM, its disks (including all browser profiles and statements), the IP, the network and the secrets."
  read -rp "Type rv-fri to confirm: " ok
  [ "$ok" = "$TAG" ] || exit 1
  $G compute instances delete "$VM" --zone="$ZONE" || true
  $G compute disks delete "$DATA_DISK" --zone="$ZONE" || true
  $G compute addresses delete "$IP" --region="$REGION" || true
  $G compute firewall-rules delete "$TAG-allow-iap-ssh" || true
  $G compute firewall-rules delete "$TAG-allow-web" || true
  $G compute networks subnets delete "$SUBNET" --region="$REGION" || true
  $G compute networks delete "$NET" || true
  for s in "${SECRETS[@]}" "${OPTIONAL_SECRETS[@]}"; do $G secrets delete "$s" || true; done
  $G iam service-accounts delete "$SA" || true
}

google_client() {
  say "Google OAuth client for Gmail codes and Sheets (see docs/google.md)"
  local id secret
  id=${FRIDAY_GOOGLE_CLIENT_ID:-}
  [ -n "$id" ] || read -rp "  OAuth client ID: " id
  secret=${FRIDAY_GOOGLE_CLIENT_SECRET:-}
  [ -n "$secret" ] || { read -rsp "  OAuth client secret: " secret; echo; }
  printf '%s' "$id" | put_secret rv-fri-google-client-id
  printf '%s' "$secret" | put_secret rv-fri-google-client-secret
  for s in rv-fri-google-client-id rv-fri-google-client-secret; do
    $G secrets add-iam-policy-binding "$s" --member="serviceAccount:$SA" \
      --role=roles/secretmanager.secretAccessor >/dev/null
  done
  echo "  stored; restarting friday so it picks them up"
  ssh_vm "sudo /usr/local/bin/rv-fri-up" >/dev/null
}

case "${1:-all}" in
  all) ensure_apis; ensure_network; ensure_service_account; ensure_secrets; ensure_vm; ship
       say "Done. Open the portal with: deploy/gcp/deploy.sh tunnel" ;;
  ship) ship ;;
  tunnel) tunnel ;;
  public) public ;;
  private) private ;;
  google-client) google_client ;;
  cli) shift; ssh_vm "cd /opt/friday/app && sudo docker compose exec -T friday friday $(printf '%q ' "$@")" ;;
  destroy) destroy ;;
  *) echo "usage: $0 [all|ship|tunnel|public|private|google-client|cli <friday args>|destroy]" >&2; exit 2 ;;
esac
