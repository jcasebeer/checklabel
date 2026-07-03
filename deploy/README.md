# Deploying to a VPS behind a Cloudflare Tunnel

Architecture: one small VPS, zero inbound ports. The app container publishes
nothing on the host; a `cloudflared` sidecar joins the same Docker network and
makes **outbound-only** connections to Cloudflare's edge, which serves the
public hostname (TLS included). The firewall denies all inbound traffic except
SSH.

```
browser ── TLS ──> Cloudflare edge (+ optional Access auth)
                        │  tunnel (outbound from VPS)
                   ┌────▼──────────── VPS ────────────┐
                   │  cloudflared ──> app:8000        │
                   │  (no published ports, ufw deny)  │
                   └──────────────────────────────────┘
```

## One-time setup

**1. Provision the VPS** (any provider, Ubuntu 22.04/24.04, 1 GB RAM is
plenty). Paste `cloud-init.yaml` into the provider's *user data* field after
inserting your SSH public key. This creates a `deploy` user with Docker,
enables unattended security upgrades, and locks the firewall down to SSH only.

**2. Create the tunnel** in the [Cloudflare Zero Trust dashboard](https://one.dash.cloudflare.com/):
*Networks → Tunnels → Create a tunnel (Cloudflared)*.

- Copy the connector **token** — it goes in `deploy/.env` as `TUNNEL_TOKEN`.
- Under *Public Hostname*, route your hostname (e.g. `labelcheck.example.com`)
  to service **`http://app:8000`** — that's the app container's name on the
  compose network, resolvable by the cloudflared container.

**3. Decide the access posture** (all in `deploy/.env`, no code changes):

- **Open demo (default):** no login anywhere. The app's built-in per-network
  spend cap (`LABEL_CHECK_SPEND_CAP_PER_IP`, default $2/24h, IPv6 grouped at
  the provider /32) protects the API budget from scripted abuse.
- **Gated API:** set `LABEL_CHECK_API_KEY`; batch callers must then send
  `Authorization: Bearer <key>` (or `X-API-Key`). The UI stays open.
- Cloudflare Access (email OTP / SSO on the hostname) remains available as an
  extra layer for non-demo deployments, configured entirely on the Cloudflare
  side.

**4. Seed the server's secrets** (never synced or overwritten by deploys):

```bash
ssh deploy@YOUR_VPS 'mkdir -p /opt/label-check/deploy'
scp deploy/.env.example deploy@YOUR_VPS:/opt/label-check/deploy/.env
ssh deploy@YOUR_VPS 'nano /opt/label-check/deploy/.env'   # fill in both secrets
```

## Every deploy

```bash
DEPLOY_HOST=deploy@YOUR_VPS ./scripts/deploy.sh
```

The script rsyncs the source, rebuilds the image on the server, restarts the
stack, and waits for the app's healthcheck to go green (printing logs if it
doesn't). Restarts forfeit the in-memory manifest state of any `mode=queued`
batch still in flight — pollers receive `409` and should resubmit.

## Operations

```bash
ssh deploy@YOUR_VPS 'cd /opt/label-check/deploy && docker compose logs -f app'
ssh deploy@YOUR_VPS 'cd /opt/label-check/deploy && docker compose ps'
ssh deploy@YOUR_VPS 'cd /opt/label-check/deploy && docker compose restart cloudflared'
```

Outbound network requirements: `api.anthropic.com` (the app) and Cloudflare's
edge (`cloudflared`). Nothing else.
