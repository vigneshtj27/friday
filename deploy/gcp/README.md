# friday on Google Cloud

One Compute Engine VM in `asia-south1` (Mumbai), project `rv-experiments`.

```bash
gcloud auth login
deploy/gcp/deploy.sh
```

On the first run it asks for a portal password and your Anthropic API key (or reads `FRIDAY_PORTAL_PASSWORD` and `ANTHROPIC_API_KEY`). Both go straight into Secret Manager. Later runs keep what exists and ship the current `git HEAD`.

Open the portal:

```bash
deploy/gcp/deploy.sh tunnel
```

Then browse to http://127.0.0.1:8765.

## What it creates

Every billable resource has the label **`app=rv-fri`**. In Billing → Reports, filter by label `app` = `rv-fri`.

| Resource | Name | Labelled |
|---|---|---|
| VM, e2-standard-2 (2 vCPU, 8 GB), Debian 12, Shielded VM | `rv-fri` | yes |
| Boot disk, 30 GB pd-balanced | `rv-fri` | yes |
| Data disk, 30 GB pd-balanced (profiles, vault, statements; kept if the VM is deleted) | `rv-fri-data` | yes |
| Static external IP (egress only; a stable address for the banks) | `rv-fri-ip` | yes |
| Secrets: master key, portal password hash, Anthropic key | `rv-fri-*` | yes |
| VPC network, subnet, firewall rule (IAP SSH only) | `rv-fri-net`, `rv-fri-subnet`, `rv-fri-allow-iap-ssh` | can't be labelled; free |
| Service account, with logging/metrics writer roles and read access to its three secrets only | `rv-fri-vm` | can't be labelled; free |

Roughly $60–70 a month at on-demand prices, mostly the VM. Stop the VM when you're not using it and you pay only for the disks and the IP.

## Security

- Nothing listens on the internet. The firewall admits only SSH from Google's IAP range (35.235.240.0/20), and the portal is bound to the VM's localhost. You reach it through an IAP tunnel, which checks your Google identity.
- Persistent disks are encrypted at rest by Google, so the compose file asserts `FRIDAY_VOLUME_ENCRYPTED_ASSERT=true`.
- `.env` is written at boot from Secret Manager into `/opt/friday/app/.env`, mode 600, root-owned.
- **Back up the master key.** Losing it makes every stored credential unreadable:
  `gcloud secrets versions access latest --secret=rv-fri-master-key --project=rv-experiments`

## Operating it

| Task | Command |
|---|---|
| Deploy new code | `deploy/gcp/deploy.sh ship` |
| Logs | `gcloud compute ssh rv-fri --zone=asia-south1-a --tunnel-through-iap -- 'cd /opt/friday/app && sudo docker compose logs -f'` |
| First-boot log | `... -- sudo tail -100 /var/log/rv-fri-startup.log` |
| Stop / start | `gcloud compute instances stop rv-fri --zone=asia-south1-a` / `start` |
| Remove everything | `deploy/gcp/deploy.sh destroy` |

A datacenter IP is still a datacenter IP: banks may ask for more codes than they would at home. See "Home machine vs VPS" in the main README. If a bank keeps bouncing, run a home `friday egress-proxy` and point the profile's proxy at it.

## Public HTTPS URL (optional)

```bash
deploy/gcp/deploy.sh public
```

Serves the portal at `https://<static-ip-with-dashes>.sslip.io` (for 34.14.129.151 that is
`https://34-14-129-151.sslip.io`) through Caddy with an automatic Let's Encrypt certificate,
and opens ports 80/443 to the internet. **The portal password is then the only thing between
the internet and your logged-in bank sessions** (login attempts back off exponentially;
cookies become `Secure`). Use a long, unique password. Set `DOMAIN=friday.example.com` first
to use your own domain (point an A record at the static IP).

To go back to tunnel-only access:

```bash
deploy/gcp/deploy.sh private
```

## Push to deploy (Cloud Build)

A push to the chosen branch on GitHub deploys that commit to the VM. Cloud Build runs
[`cloudbuild.yaml`](../../cloudbuild.yaml), which runs `deploy/gcp/deploy.sh ship`: tar the
commit, copy it over an IAP SSH tunnel, swap `/opt/friday/app`, run `rv-fri-up`, and fail the
build if `/healthz` doesn't answer within 150 s. No GitHub Actions, and the build never sees the
app's secrets (the VM reads them from Secret Manager as before).

Set it up once, with your own gcloud login, naming the branch whose pushes should deploy:

```bash
deploy/gcp/setup-cloudbuild.sh main-c3c2du
```

**The one manual step.** On the first run the script creates the GitHub connection
`rv-fri-github` and stops with a URL. Open it, sign in to GitHub as the owner of
`vigneshtj27/friday`, authorize Cloud Build and install the Cloud Build GitHub app on that
repository (you can do the same from Console → Cloud Build → Repositories → 2nd gen →
`rv-fri-github`). Then run the script again; it links the repo and creates the trigger.
Re-running it later is safe, and running it with another branch moves the trigger there.

What it creates (all free):

| Resource | Name |
|---|---|
| Deploy service account: IAP tunnel to port 22, OS Admin Login on `rv-fri` only, Service Account User on `rv-fri-vm`, `compute.instances.get`/`compute.projects.get` (custom role `rvFriDeployer`), log writer. No secret access. | `rv-fri-deployer` |
| 2nd-gen GitHub connection and repository link, in `asia-south1` | `rv-fri-github` / `friday` |
| Trigger: push to the branch → `cloudbuild.yaml`, run as `rv-fri-deployer` | `rv-fri-deploy` |

The branch has to contain `cloudbuild.yaml`, so merge this into it before relying on the trigger.

Watch builds:

| Task | Command |
|---|---|
| Builds in the console | https://console.cloud.google.com/cloud-build/builds;region=asia-south1?project=rv-experiments |
| Recent builds | `gcloud builds list --region=asia-south1 --project=rv-experiments --limit=5` |
| Follow one | `gcloud builds log --stream BUILD_ID --region=asia-south1 --project=rv-experiments` |
| Deploy without pushing | `gcloud builds triggers run rv-fri-deploy --region=asia-south1 --branch=main-c3c2du --project=rv-experiments` |

**Builds don't overlap on the VM.** Cloud Build itself will run two builds at once if you push
twice quickly, but `deploy.sh ship` takes a lock (`flock /var/lock/rv-fri-deploy`) on the VM
around the swap and restart, so the second waits for the first (up to 15 minutes). The same lock
covers a manual `deploy/gcp/deploy.sh ship` from your laptop. Builds start in push order, so
the last push normally lands last; if in doubt, check `git rev-parse HEAD` against the build's
commit, or run the trigger again.
