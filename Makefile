.PHONY: up down restart logs backup restore ssl-init maintenance-on maintenance-off

DOMAIN ?= yourdomain.com
EMAIL  ?= your@email.com

# ── Dev ───────────────────────────────────────────────────────────────────────
dev:
	uvicorn app:app --reload --port 8000

# ── Docker ───────────────────────────────────────────────────────────────────
up:
	docker compose up -d

down:
	docker compose down

restart:
	docker compose restart app

rebuild:
	docker compose up -d --build app

logs:
	docker compose logs -f app

# ── Database ──────────────────────────────────────────────────────────────────
backup:
	./backup.sh

restore:
	@if [ -z "$(FILE)" ]; then echo "Usage: make restore FILE=backups/avalant_20260407_120000.sql.gz"; exit 1; fi
	@echo "→ Restoring from $(FILE)..."
	gunzip -c $(FILE) | docker compose exec -T db psql -U wallet -d avalant
	@echo "✓ Done"

# ── SSL (Let's Encrypt) ───────────────────────────────────────────────────────
# Run once on first deploy, before starting nginx with HTTPS config.
# 1. Start nginx in HTTP-only mode first (comment out the 443 server block)
# 2. Run: make ssl-init DOMAIN=yourdomain.com EMAIL=your@email.com
# 3. Uncomment the 443 server block in nginx/nginx.conf
# 4. Replace yourdomain.com in nginx.conf with your actual domain
# 5. make restart-nginx
ssl-init:
	docker compose run --rm certbot certonly \
		--webroot -w /var/www/certbot \
		--email $(EMAIL) \
		--agree-tos \
		--no-eff-email \
		-d $(DOMAIN) -d www.$(DOMAIN)

restart-nginx:
	docker compose restart nginx

# ── Maintenance mode ──────────────────────────────────────────────────────────
# Activates the maintenance page without restarting the app — the middleware
# watches for /tmp/avalant_maintenance INSIDE the container.
maintenance-on:
	docker compose exec app touch /tmp/avalant_maintenance
	@echo "✓ Maintenance mode ON — site returns the maintenance page."

maintenance-off:
	docker compose exec app rm -f /tmp/avalant_maintenance
	@echo "✓ Maintenance mode OFF."
