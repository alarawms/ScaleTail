# ScaleTail for Synology

Synology-optimized Docker Compose stacks with Tailscale userspace mode. All 98 services from ScaleTail, ready for Synology DSM 7.x + Portainer CE.

## What's Different

| | Mainline ScaleTail | Synology variant |
|---|---|---|
| Tailscale mode | Kernel (`/dev/net/tun`, `cap_add: net_admin`) | Userspace (`TS_USERSPACE=true`) |
| Volume paths | Relative (`./data`) | Parameterized (`${DATA_ROOT}/...`) |
| PUID/PGID | Hardcoded `1000` | Parameterized (default `1026/100`) |
| Auth key | Per-service only | Shared key + per-service override |
| Portainer | Not targeted | Labels + app template catalog |

## Prerequisites

- Synology NAS (x86, DSM 7.2+)
- Docker via Container Manager (Package Center)
- [Portainer CE](https://docs.portainer.io/start/install-ce) installed
- Tailscale account with an [auth key](https://login.tailscale.com/admin/settings/keys)

## Method 1: Portainer App Templates (Recommended)

1. In Portainer, go to **Settings > App Templates**
2. Set the URL to:
   ```
   https://raw.githubusercontent.com/alarawms/ScaleTail/synology/synology/portainer-templates.json
   ```
3. Go to **App Templates**, browse the catalog, pick a service
4. Fill in the environment variables:
   - `TS_AUTHKEY` — your Tailscale auth key
   - `DATA_ROOT` — base path for data (default: `/volume1/docker`)
   - `PUID` / `PGID` — your user/group ID (run `id` via SSH to find yours)
   - `TZ` — your timezone (e.g., `Asia/Riyadh`)
5. Click **Deploy the stack**

## Method 2: Portainer Git Stack

1. In Portainer, go to **Stacks > Add Stack > Repository**
2. Repository URL: `https://github.com/alarawms/ScaleTail`
3. Reference: `synology`
4. Compose path: `synology/services/<service-name>/compose.yaml`
5. Set environment variables in the UI
6. Deploy

## Method 3: CLI

```bash
git clone https://github.com/alarawms/ScaleTail.git
cd ScaleTail
git checkout synology

# Set your shared auth key
echo "TS_AUTHKEY=tskey-auth-xxxxx" > synology/.env.shared

# Deploy a service
./tools/deploy-synology.sh adguardhome up
```

## Shared vs Per-Service Auth Keys

All services load `synology/.env.shared` first, then their own `.env`. To use one key for everything, set it in `.env.shared`. To override for a specific service, uncomment `TS_AUTHKEY=` in that service's `.env`.

## Finding Your PUID and PGID

SSH into your Synology and run:

```bash
id
# uid=1026(youruser) gid=100(users) ...
```

Use `1026` for `PUID` and `100` for `PGID`.

## Verifying Deployment

1. Check Portainer **Stacks** view — all containers should show "running"
2. Visit `https://<service-name>.<your-tailnet>.ts.net`
3. Check the [Tailscale admin console](https://login.tailscale.com/admin/machines) for the new device

## Regenerating Stacks

If upstream ScaleTail adds new services, regenerate:

```bash
pip install ruamel.yaml
python tools/generate.py --all --portainer
```

## Troubleshooting

**Tailscale won't connect**: Check `docker logs tailscale-<service>`. Common causes: expired auth key, key already used (use reusable keys).

**Permission errors on volumes**: Verify PUID/PGID match your Synology user. Create directories first: `mkdir -p /volume1/docker/<service>/`.

**Port conflicts with DSM**: Synology uses ports 5000/5001. If a service conflicts, change the app port in the compose file, not DSM.

**Portainer can't find .env**: When deploying via git stack, set variables in Portainer's UI instead of relying on the `.env` file.
