#!/bin/bash
# VM startup script for friday on Compute Engine. Runs as root on every boot.
#   1. formats (first boot only) and mounts the persistent data disk
#   2. installs Docker Engine + the compose plugin (first boot only)
#   3. installs rv-fri-up, which writes .env from Secret Manager and starts the stack
#   4. starts the stack if the app has been shipped already
set -euo pipefail
exec > >(tee -a /var/log/rv-fri-startup.log) 2>&1
echo "== rv-fri startup $(date -Is)"

md() { curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }

# ---- data disk ----------------------------------------------------------------
DEV=/dev/disk/by-id/google-friday-data
MNT=/mnt/friday-data
for _ in $(seq 30); do [ -e "$DEV" ] && break; sleep 2; done
if ! blkid "$DEV" >/dev/null 2>&1; then
  mkfs.ext4 -m 0 -E lazy_itable_init=0,lazy_journal_init=0,discard "$DEV"
fi
mkdir -p "$MNT"
grep -q "$DEV" /etc/fstab || echo "$DEV $MNT ext4 discard,defaults,nofail 0 2" >> /etc/fstab
mountpoint -q "$MNT" || mount "$MNT"
mkdir -p "$MNT/data"
chown 1000:1000 "$MNT/data"   # uid of the friday user inside the image
chmod 700 "$MNT/data"

# ---- no IPv6 -------------------------------------------------------------------
# A browser behind an IPv4 proxy must not reach sites over IPv6 directly (it would show the VM's
# own address). The subnet is IPv4-only today; this keeps it so if that ever changes.
cat > /etc/sysctl.d/90-rv-fri-no-ipv6.conf <<'SYS'
net.ipv6.conf.all.disable_ipv6 = 1
net.ipv6.conf.default.disable_ipv6 = 1
SYS
sysctl --system >/dev/null || true

# ---- docker -------------------------------------------------------------------
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $VERSION_CODENAME stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
fi

# ---- rv-fri-up: secrets -> .env -> docker compose up --------------------------------
cat > /usr/local/bin/rv-fri-up <<'UP'
#!/bin/bash
# Write /opt/friday/app/.env from Secret Manager and (re)start friday.
set -euo pipefail
md() { curl -fsS -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/$1"; }
attr() { md "instance/attributes/$1" 2>/dev/null || true; }
secret() { gcloud secrets versions access latest --secret="$1" --project="$(md project/project-id)"; }
APP=/opt/friday/app
DOMAIN=$(attr friday-domain)
cd "$APP"
umask 077
{
  echo "FRIDAY_MASTER_KEY=$(secret rv-fri-master-key)"
  echo "FRIDAY_ADMIN_PASSWORD_HASH='$(secret rv-fri-admin-password-hash)'"
  echo "ANTHROPIC_API_KEY=$(secret rv-fri-anthropic-api-key)"
  # Optional: Google OAuth client for Gmail codes and Sheets (deploy/gcp/deploy.sh google-client).
  if gid=$(secret rv-fri-google-client-id 2>/dev/null); then
    echo "FRIDAY_GOOGLE_CLIENT_ID=$gid"
    echo "FRIDAY_GOOGLE_CLIENT_SECRET=$(secret rv-fri-google-client-secret)"
  fi
  echo "FRIDAY_HOST_DATA=/mnt/friday-data/data"
  echo "FRIDAY_DEFAULT_TIMEZONE=$(attr friday-timezone)"
  echo "FRIDAY_DEFAULT_LOCALE=$(attr friday-locale)"
  echo "TZ=$(attr friday-timezone)"
  if [ -n "$DOMAIN" ]; then
    echo "FRIDAY_DOMAIN=$DOMAIN"
    echo "FRIDAY_PUBLIC_BASE_URL=https://$DOMAIN"
    echo "FRIDAY_COOKIE_SECURE=true"
    echo "FRIDAY_CADDY_DATA=/mnt/friday-data/caddy"
  else
    echo "FRIDAY_PUBLIC_BASE_URL=$(attr friday-public-url)"
    echo "FRIDAY_COOKIE_SECURE=false"
  fi
} > .env.new
mv .env.new .env
if [ -n "$DOMAIN" ]; then
  # Public HTTPS mode: Caddy in front (certificates kept on the data disk).
  mkdir -p /mnt/friday-data/caddy
  docker compose -f docker-compose.yml -f deploy/gcp/compose.public.yml up -d --build --remove-orphans
else
  docker compose up -d --build --remove-orphans
fi
docker image prune -f >/dev/null
UP
chmod 755 /usr/local/bin/rv-fri-up

if [ -f /opt/friday/app/docker-compose.yml ]; then
  /usr/local/bin/rv-fri-up
fi
touch /var/lib/rv-fri-ready
echo "== rv-fri startup done $(date -Is)"
