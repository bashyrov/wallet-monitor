"""Health, providers endpoint, security headers."""


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_providers_counts(client):
    """Assert lower bounds — counts grow as we add providers, and gating CI
    on exact numbers makes every provider addition need a test bump."""
    r = client.get("/api/providers")
    assert r.status_code == 200
    data = r.json()
    assert data["exchanges"] >= 8,  f"Expected ≥8 exchanges, got {data['exchanges']}"
    assert data["chains"] >= 13,    f"Expected ≥13 chains, got {data['chains']}"
    assert data["perp_dexes"] >= 4, f"Expected ≥4 perp dexes, got {data['perp_dexes']}"


def test_providers_keys(client):
    data = client.get("/api/providers").json()
    assert set(data.keys()) == {"exchanges", "chains", "perp_dexes"}


def test_security_headers(client):
    r = client.get("/api/health")
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert r.headers.get("X-XSS-Protection") == "1; mode=block"
    assert "server" not in r.headers


def test_hidden_openapi(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404
