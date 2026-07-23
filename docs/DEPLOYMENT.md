# Production Deployment Guide

The official deploy target for v1 is a **single VM running Docker Compose**,
fronted by **nginx with TLS**. This fits the project's scale (a small research
team) without Kubernetes-grade operational overhead; the compose file is
structured so a future move to a bigger orchestrator is mechanical.

```
                    ┌──────────────────────── VM ────────────────────────┐
 Internet ── 443 ──▶ nginx (host, TLS) ── 127.0.0.1:8000 ──▶ web (gunicorn)
                    │                                          │
                    │            ┌── worker (celery) ──┐       │
                    │            └── beat  (celery) ───┤       │
                    │                                  ▼       ▼
                    │                   redis ◀────────┴── postgres
                    └────────────────── (internal network only) ─────────┘
```

Postgres and Redis are **not** published on any host port — only the web app
is, and only on loopback. nginx is the single public entry point.

## 1. Prerequisites

- A VM (2 vCPU / 2 GB RAM is comfortable) with Docker Engine + the compose
  plugin, and nginx installed on the host.
- A domain pointing at the VM (for TLS).
- An SMTP account for outbound email (see `.env.example` for provider
  examples: Resend, SES, Gmail).

## 2. First deployment

```bash
git clone https://github.com/AlperEnesErsu/ScrapeMind.git
cd ScrapeMind

# 1. Configure — .env.prod is gitignored, never commit it
cp .env.prod.example .env.prod
python3 -c "import secrets; print(secrets.token_hex(32))"   # → SECRET_KEY
python3 -c "import secrets; print(secrets.token_hex(32))"   # → JWT_SECRET_KEY
# edit .env.prod: keys above, POSTGRES_PASSWORD, MAIL_*, …

# 2. Build + start everything
docker compose -f docker/docker-compose.prod.yml --env-file .env.prod up -d --build

# 3. Watch first boot — migrations run before gunicorn starts
docker compose -f docker/docker-compose.prod.yml logs -f web
```

**Startup order is load-bearing**: the web container's entrypoint runs
`flask db upgrade` *before* starting gunicorn, because plugin discovery needs
the `modules` table to exist (see `wsgi.py`). The worker/beat containers skip
the entrypoint — the web service owns migrations, so two containers never
race the same DDL.

The app **refuses to start** in production with an empty `SECRET_KEY` or
`DATABASE_URL` — a half-configured deployment fails loudly at boot instead of
serving forgeable sessions.

### Seed the first admin

```bash
docker compose -f docker/docker-compose.prod.yml exec web python scripts/seed.py
```

Then log in and immediately change the seeded password (Profile → Password —
this also revokes any API tokens).

## 3. nginx + TLS

`/etc/nginx/sites-available/scrapemind`:

```nginx
server {
    server_name scrapemind.example.com;

    client_max_body_size 3m;   # avatar uploads are capped at 2 MB

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;             # matches gunicorn --timeout
    }

    listen 80;
}
```

```bash
ln -s /etc/nginx/sites-available/scrapemind /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
# TLS — certbot rewrites the server block for 443 and sets up auto-renewal
apt install certbot python3-certbot-nginx
certbot --nginx -d scrapemind.example.com
```

Production config already sets `SESSION_COOKIE_SECURE=True`, so cookies only
travel over the TLS side.

## 4. Health & monitoring

- `GET /api/v1/health` → `{"status": "ok"}` — unauthenticated, no external
  calls. The compose healthcheck polls it every 30 s; point your uptime
  monitor at `https://your-domain/api/v1/health` too.
- `docker compose -f docker/docker-compose.prod.yml ps` shows per-service
  health. Celery liveness is visible in-app at `/admin/tasks` (a heartbeat
  task runs every minute).
- Logs are JSON in production (structlog) — `docker compose … logs` pipes
  cleanly into any aggregator.

## 5. Upgrades

```bash
git pull
docker compose -f docker/docker-compose.prod.yml --env-file .env.prod up -d --build
```

That's the whole procedure: the entrypoint applies new migrations before the
new code serves traffic. Compose replaces containers one service at a time;
for a zero-downtime bar you'd add a second web container behind nginx, which
is out of scope for v1.

Roll back = check out the previous tag and run the same command. Migrations
are written to be additive; verify a migration's `downgrade()` before relying
on it in anger.

## 6. Backups

The only irreplaceable state is **Postgres** and the **uploads volume**
(avatars). Nightly cron on the host:

```bash
# /etc/cron.d/scrapemind-backup  (03:00 daily, keep 14 days)
0 3 * * * root docker compose -f /opt/ScrapeMind/docker/docker-compose.prod.yml \
  exec -T db pg_dump -U scrapemind scrapemind | gzip \
  > /var/backups/scrapemind-$(date +\%F).sql.gz \
  && find /var/backups -name 'scrapemind-*.sql.gz' -mtime +14 -delete
```

Restore: `gunzip -c backup.sql.gz | docker compose … exec -T db psql -U scrapemind scrapemind`.

Redis needs no backup — it holds only the Celery queue and the permission
cache, both rebuilt automatically.

## 7. Secret rotation

| Secret            | Effect of rotating                                          |
|-------------------|-------------------------------------------------------------|
| `SECRET_KEY`      | All web sessions invalidated — every user logs in again. If `JWT_SECRET_KEY` is empty, API tokens die too. |
| `JWT_SECRET_KEY`  | All API tokens invalidated; web sessions untouched.         |
| `POSTGRES_PASSWORD` | Change it in Postgres (`ALTER USER`) *and* `.env.prod`, then restart. |
| SMTP / API keys   | Restart the stack; no user-visible effect.                  |

Rotation is just: edit `.env.prod` → `docker compose … up -d`. The session
and token invalidation effects are by design, not collateral damage — that's
what makes rotation the right response to a suspected leak.

## 8. Production checklist

- [ ] `SECRET_KEY` and `JWT_SECRET_KEY` freshly generated, not dev values
- [ ] `POSTGRES_PASSWORD` strong and unique
- [ ] `FLASK_ENV=production` (fail-fast guard + JSON logs + secure cookies)
- [ ] Mail configured and a password-reset email actually received
- [ ] TLS certificate issued and auto-renewal timer active (`systemctl list-timers | grep certbot`)
- [ ] Backup cron installed and a restore **tested once**
- [ ] Seeded admin password changed
- [ ] `docker compose ps` shows every service healthy
- [ ] Uptime monitor watching `/api/v1/health`
