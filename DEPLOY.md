# Deploy guide — Avalant

Two principles drive every deploy decision:

1. **Users should never see a 502.** Two web replicas behind nginx with
   round-robin + `proxy_next_upstream`; one rebuilds while the other
   serves.
2. **Don't restart what didn't change.** A frontend tweak shouldn't bounce
   the data plane. A migration shouldn't rebuild the fetcher.

Run everything below **on the prod server** (`ssh root@avalant.xyz`,
`cd /root/wallet-monitor`).

---

## Quick reference

| Scope | Command | What happens | User impact |
|---|---|---|---|
| Frontend (HTML/JS/CSS) | `./scripts/deploy.sh frontend` | git pull only — files are bind-mounted | new code on next request; cached browsers refresh ≤60 s |
| Backend (web) | `./scripts/deploy.sh backend` | rebuilds `app` then `app2` rolling | zero downtime, in-flight WS reconnects |
| Fetcher (data plane) | `./scripts/deploy.sh fetcher` | rebuilds the fetcher sidecar | screener feed rewarms 10–20 s |
| Migrations | `./scripts/deploy.sh migrations` | `alembic upgrade head` then rolling app | brief window of mixed schema — pair with maintenance for breaking changes |
| nginx config | `./scripts/deploy.sh nginx` | hot-reload, recreate if mount-inode flipped | none |
| Everything | `./scripts/deploy.sh all` | full rolling-deploy (web + fetcher) | zero downtime |
| Auto-detect | `./scripts/deploy.sh` | runs only what `git diff origin/main` shows changed | depends on scope |

---

## Auto mode

The default `./scripts/deploy.sh` (no argument) inspects `git diff HEAD
origin/main` and picks the narrowest deploy that covers the changes:

```
.changed file…              picked deploy
backend/foo.py               backend
app.py                       backend
fetcher/__main__.py          fetcher
frontend/screener.html       frontend
alembic/versions/abc.py      migrations
nginx/nginx.conf             nginx
docker-compose.yml           all
```

Multiple touched scopes ⇒ multiple actions in sequence (e.g.
`backend/` + `fetcher/` runs both, but skips frontend if it hasn't
changed). If nothing matches, it falls back to a full rolling-deploy.

---

## Frontend hot-swap

`./frontend` is bind-mounted into every container as `/app/frontend:ro`.
That means:

- `git pull` updates the host directory → containers see the new files
  on the very next FastAPI request.
- No Docker action is needed for HTML / JS / CSS / SVG-only changes.
- Cache-Control on JS/CSS is `max-age=60`, so returning users with old
  code refresh within ~1 minute. For a hard cut-over (e.g. a JS↔backend
  contract change you can't make backwards-compatible) flip full-site
  maintenance on, deploy, flip it back — see below.

---

## Maintenance modes

Three independent scopes, each with an optional ETA + countdown rendered
on the user-facing lockout page. All controlled from
`/admin → Maintenance` or via `POST /api/admin/maintenance`.

| Scope | Blocks | Stays open |
|---|---|---|
| `site` | every page | `/api/health`, `/api/maintenance/status`, admin API |
| `screener` | `/screener`, `/arb`, `/watchlist`, `/api/screener/*` | portfolio + admin + pricing |
| `portfolio` | `/app`, `/archive`, `/profile`, `/avashare`, wallet/alert/trade APIs | screener + pricing + checkout (so users can renew) |

Lockout pages auto-poll `/api/maintenance/status` every 15 s and reload
themselves when the scope flips back off — no manual F5.

**Set ETA in one round-trip:**
```bash
curl -X POST https://avalant.xyz/api/admin/maintenance \
  -H "Authorization: Bearer $ADMIN_JWT" \
  -H 'Content-Type: application/json' \
  -d '{"scope":"screener","enabled":true,"duration_minutes":45}'
```
Server computes `ends_at = now + 45 min` and stores it; the page renders
`Ends at 14:30 (Europe/Warsaw) — 44m 50s remaining`.

Display timezone: `/admin → Maintenance → Display timezone` (default
`Europe/Warsaw`, IANA name).

---

## Real scenarios

### "I want to fix the screener UI"
1. Edit `frontend/screener.html` locally, commit, push.
2. On prod: `./scripts/deploy.sh frontend` (or just the auto run).
3. Done — backend, fetcher, WS feeds untouched.

### "I'm rolling out a new arb-compute pipeline that's 10× faster"
1. Toggle screener maintenance with ETA: 30 minutes.
2. `./scripts/deploy.sh fetcher` — only the data plane rebuilds.
3. Watch `docker compose logs --since=5m fetcher` until the new compute
   loop is settled.
4. Toggle screener maintenance off — pages auto-reload, users see the
   live screener within 15 s.

### "I'm shipping a breaking schema migration"
1. Toggle full-site maintenance with a generous ETA.
2. `./scripts/deploy.sh migrations` — runs `alembic upgrade head` on
   app1, then rebuilds both replicas with the new model code.
3. Verify `/api/health/feeds` is green.
4. Toggle full-site maintenance off.

### "I need to update an env var (e.g. CRYPTOCLOUD_*)"
1. Edit `/root/wallet-monitor/.env` on the host.
2. `docker compose up -d app app2 fetcher` — `up` (without `--build`)
   recreates containers so the new env is picked up. No image rebuild.
3. Verify with `docker compose exec app env | grep <KEY>`.

### "I need to change docker-compose.yml"
Compose changes always require a full deploy because the container
runtime spec changes. `./scripts/deploy.sh all` (or auto-detect picks
this).

---

## Rollback

Every deploy is a git commit. To roll back:

```bash
cd /root/wallet-monitor
git log --oneline -5                        # find the last good SHA
git checkout <sha>                          # detached HEAD is fine
./scripts/deploy.sh all                     # rebuild against that SHA
```

For migrations, `alembic downgrade -1` from inside the app container.
**Avoid downgrading auto-generated migrations** that altered data — they
typically don't restore content, only schema.

---

## Things that DON'T need a deploy

- **Maintenance toggles** — runtime via /admin.
- **Plan / promo / popup / billing-period CRUD** — runtime via /admin.
- **Hidden symbols / disabled exchanges** — runtime via /admin → Screener.
- **Trade enable/disable per exchange** — runtime via /admin → Screener.
- **Expiry-reminder schedule** — runtime via /admin → Communications.
- **Admin broadcast** — runtime via /admin → Communications.
- **User block/unblock, plan grant** — runtime via /admin → Users.

---

## What the rolling-deploy script actually does

`scripts/rolling-deploy.sh` (called by `deploy.sh all` or as the legacy
entry point):

1. `git pull --ff-only`.
2. `docker compose up -d --build app` → wait for `localhost:8000` to
   accept TCP. While `app` rebuilds, nginx routes everything to `app2`
   via `proxy_next_upstream error timeout invalid_header http_502
   http_503 http_504` with `proxy_connect_timeout 2s`.
3. `docker compose up -d --build app2` → wait for healthy.
4. `docker compose up -d --build fetcher` (no replica; brief restart).
5. `nginx -s reload` (or full recreate if `nginx.conf` inode changed).
6. Smoke-test `/api/health` and `/api/plans`.

Any single step that fails leaves the previous container running, so a
broken build never replaces the working one.
