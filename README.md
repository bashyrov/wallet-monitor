# avalant_

**All your crypto. One dashboard.**

Self-hosted portfolio tracker that aggregates balances across CEX exchanges, perpetual DEXes, and on-chain wallets — in one place, with no cloud dependency.

---

## Features

- **8 CEX exchanges** — Binance, OKX, Bybit, Gate, MEXC, KuCoin, Bitget, Backpack
- **5 Perp DEXes** — Hyperliquid, Lighter, Ethereal, Aster, Paradex
- **13 blockchain networks** — Ethereum, BSC, Polygon, Arbitrum, Optimism, Base, Avalanche, zkSync, Linea, Scroll, Mantle, Blast, Tron
- **Address book** — label your on-chain addresses, get highlighted matches in transaction history
- **Transaction history** — last 5 transactions per wallet, normalized across all providers
- **Admin panel** — user management, block/unblock, request anomaly detection
- **Encrypted credentials** — Fernet + PBKDF2, keys never leave your server

---

## Stack

| Layer | Technology |
|-------|-----------|
| Backend | FastAPI, SQLAlchemy, Alembic |
| Auth | JWT (HS256), bcrypt |
| Database | PostgreSQL (prod) / SQLite (dev) |
| Frontend | Vanilla JS, no framework |
| Infra | Docker, nginx, Let's Encrypt |

---

## Quick Start

### Local (SQLite)

```bash
git clone https://github.com/bashyrov/wallet-monitor.git
cd wallet-monitor

python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env   # edit SECRET_KEY and ENCRYPTION_KEY

uvicorn app:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000). The first registered user automatically becomes admin.

### Docker + PostgreSQL

```bash
cp .env.sample .env   # fill in all required variables
docker compose up -d --build
```

---

## Environment Variables

```env
# Required
SECRET_KEY=...           # generate: python -c "import secrets; print(secrets.token_hex(32))"
ENCRYPTION_KEY=...       # same as above

# Database (default: SQLite)
DATABASE_URL=postgresql://user:pass@localhost:5432/avalant

# Optional — enables full EVM token balances and tx history
ANKR_KEY=

# Optional — higher Tron rate limits
TRON_KEY=

# CORS (empty = same-origin only)
ALLOWED_ORIGINS=https://yourdomain.com
```

See [`.env.sample`](.env.sample) for the full list.

---

## Production Deploy

1. Point your domain DNS to the server
2. Get SSL certificate: `certbot certonly --standalone -d yourdomain.com`
3. Copy certs to `nginx/certs/`
4. Set `server_name` in [`nginx/nginx.conf`](nginx/nginx.conf)
5. Fill in `.env` and run `docker compose up -d --build`

Auto-renew SSL:
```bash
# crontab -e
0 3 * * * certbot renew --quiet && cp /etc/letsencrypt/live/yourdomain.com/*.pem /opt/avalant/nginx/certs/ && docker compose -f /opt/avalant/docker-compose.yml exec nginx nginx -s reload
```

---

## Project Structure

```
├── app.py                  # FastAPI entry point
├── settings.py             # Config via environment variables
├── frontend/               # Multi-page vanilla JS frontend
├── backend/
│   ├── api/v1/             # REST endpoints
│   ├── providers/          # Exchange, chain, perp dex integrations
│   ├── services/           # Business logic
│   ├── db/                 # SQLAlchemy models & session
│   ├── domain/             # Data models & enums
│   └── schemas/            # Pydantic schemas
├── alembic/                # Database migrations
├── nginx/                  # Reverse proxy config
└── docker-compose.yml
```

---

## Adding a Provider

**New exchange:**
1. Create `backend/providers/exchanges/newexchange_provider.py`, inherit `BaseWalletProvider`
2. Set `name`, `label`, `enabled = True`, `needs_passphrase`
3. Implement `fetch_balance(wallet) → BalanceResult`
4. Register in `EXCHANGE_PROVIDERS` and `ExchangeType` enum

**New chain:**
1. Create provider in `backend/providers/chains/`, inherit `BaseChainProvider`
2. Add to `CHAIN_PROVIDERS` and `CHAIN_META`
3. Add to `ChainType` enum

**Disable a provider** — set `enabled = False` on the class (or in `CHAIN_META`). It disappears from the UI automatically.

---

## License

MIT
