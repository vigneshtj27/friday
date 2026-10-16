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
