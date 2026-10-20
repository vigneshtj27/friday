#!/usr/bin/env bash
# Route one profile's browser through a home internet connection, in one command.
#
#   deploy/egress.sh <profile_id> <home-tailscale-ip> [port]
#   deploy/egress.sh amex.me 100.101.102.103
#
# What it does, from your laptop:
#   1. installs Tailscale on the VM and joins your tailnet (TS_AUTHKEY=tskey-... for no
#      prompt; otherwise it prints a login URL to open);
#   2. prints the exact `friday egress-proxy` command to run on the home machine;
#   3. waits until that proxy answers from the VM;
#   4. sets the profile's proxy (password into the vault, never on a command line) and
#      switches it to the patchright driver (NO_PATCHRIGHT=1 to skip);
#   5. runs `friday preflight <profile> --fresh` and refuses to finish if the IP the site
#      will see is a datacenter/hosting network or not in the profile's country;
#   6. runs `friday browser-check <profile>` (IPv4/IPv6/WebRTC leaks, languages, timezone).
#
# One proxy per profile, never rotated. The home machine needs Tailscale (same tailnet)
# and a friday checkout (`uv sync`). Same PROJECT/ZONE defaults as deploy/gcp/deploy.sh.
set -euo pipefail

PROFILE=${1:?usage: $0 <profile_id> <home-tailscale-ip> [port]}
HOME_IP=${2:?usage: $0 <profile_id> <home-tailscale-ip> [port]}
PORT=${3:-3128}
PROJECT=${PROJECT:-rv-experiments}
ZONE=${ZONE:-asia-south1-a}
VM=${VM:-rv-fri}
G="gcloud --project=$PROJECT --quiet"
SERVER="http://$HOME_IP:$PORT"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ssh_vm() { $G compute ssh "$VM" --zone="$ZONE" --tunnel-through-iap -- "$@"; }
in_app() { ssh_vm "cd /opt/friday/app && sudo docker compose exec -T friday friday $*"; }

[[ $HOME_IP =~ ^100\.([0-9]{1,3}\.){2}[0-9]{1,3}$|^fd7a: ]] ||
  { echo "$HOME_IP doesn't look like a Tailscale address (100.x.y.z); use the home machine's tailnet IP." >&2; exit 2; }

say "1. Tailscale on $VM"
ssh_vm "command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sudo sh"
if ssh_vm "sudo tailscale status >/dev/null 2>&1"; then
  echo "already on the tailnet as $(ssh_vm 'tailscale ip -4' | tr -d '\r')"
elif [[ -n ${TS_AUTHKEY:-} ]]; then
  printf '%s' "$TS_AUTHKEY" | ssh_vm "sudo tailscale up --hostname=$VM --auth-key=file:/dev/stdin"
else
  echo "Open the login URL below to add the VM to your tailnet (or re-run with TS_AUTHKEY=tskey-...)."
  ssh_vm "sudo tailscale up --hostname=$VM"
fi

say "2. Start the proxy on the home machine"
PW=${FRIDAY_EGRESS_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')}
cat <<EOF
On the always-on home machine (Tailscale up, in the friday checkout), run and leave running:

  FRIDAY_EGRESS_PASSWORD='$PW' uv run friday egress-proxy --listen $HOME_IP:$PORT

(Keep that password; re-running this script with FRIDAY_EGRESS_PASSWORD set reuses it.)
EOF

say "3. Waiting for $SERVER to answer from the VM"
probe() {
  printf 'proxy = "%s"\nproxy-user = "friday:%s"\n' "$SERVER" "$PW" |
    ssh_vm "curl -sS --max-time 10 -K - https://ipinfo.io/json" 2>/dev/null
}
for i in $(seq 1 120); do
  if out=$(probe) && [[ $out == *'"ip"'* ]]; then
    echo "proxy answers; egress: $(printf '%s' "$out" | tr -d '\n' | sed -E 's/.*"ip": *"([^"]*)".*"org": *"([^"]*)".*/\1 \2/')"
    break
  fi
  [[ $i == 120 ]] && { echo "No answer from $SERVER after 10 minutes. Is the proxy running and both machines on the tailnet?" >&2; exit 1; }
  sleep 5
done

say "4. Pointing $PROFILE at $SERVER"
flags="--server $SERVER --username friday"
[[ ${NO_PATCHRIGHT:-} == 1 ]] || flags+=" --patchright"
printf '%s\n' "$PW" | in_app "profile set-proxy $PROFILE $flags"

say "5. Verifying what the site will see"
report=$(in_app "preflight $PROFILE --fresh --json" | grep '^{' | tail -1 || true)
[[ -n $report ]] || { echo "preflight produced no report" >&2; exit 1; }
REPORT=$report python3 - <<'EOF'
import json, os, sys
r = json.loads(os.environ["REPORT"])
eg = r.get("egress") or {}
for i in r["items"]:
    print("  [%s] %s: %s" % ("ok" if i["ok"] else "FAIL", i["name"], i["detail"]))
    if not i["ok"]:
        for f in i.get("fix") or []:
            print("         fix: " + f)
egress = [i for i in r["items"] if i["name"] in ("egress", "network")][0]
if not egress["ok"]:
    sys.exit("\nREFUSING: the egress (%s %s) is not a residential connection in the expected country." % (eg.get("ip"), eg.get("org")))
print("\nEgress OK: %s AS%s (%s) via %s." % (eg.get("ip"), eg.get("asn"), eg.get("country"), eg.get("proxy")))
if not r["ok"]:
    print("Other preflight items still fail; fix them before the next sign-in (see above).")
EOF

say "6. Browser self-check (neutral IP-echo pages only, never the bank)"
in_app "browser-check $PROFILE" || { echo "browser-check found problems (above); fix them before the next sign-in." >&2; exit 1; }
