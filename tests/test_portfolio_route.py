"""Task 6 — /app renamed to /portfolio. Old /app must 301 to /portfolio
to keep TG-bot deep links and existing bookmarks working forever."""


def test_app_redirects_to_portfolio_301(client):
    """GET /app → 301 → /portfolio. No body, just the location header."""
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 301, f"expected 301, got {r.status_code}"
    assert r.headers.get("location") == "/portfolio"


def test_app_redirect_preserves_query(client):
    """GET /app?ref=foo → 301 → /portfolio?ref=foo. We must preserve
    query strings so referral / next-redirect parameters survive."""
    r = client.get("/app?ref=alice&next=/profile", follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"].startswith("/portfolio?")
    assert "ref=alice" in r.headers["location"]
    assert "next=" in r.headers["location"]


def test_portfolio_route_unauth_redirects_to_login(client):
    """Without session cookie, /portfolio → 302 → /login?next=/portfolio."""
    r = client.get("/portfolio", follow_redirects=False)
    assert r.status_code == 302
    assert "/login?next=/portfolio" in r.headers.get("location", "")


def test_portfolio_html_file_exists():
    """The HTML file was renamed from app.html. Make sure portfolio.html
    is on disk and app.html is gone."""
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.isfile(os.path.join(here, "frontend", "portfolio.html")), \
        "frontend/portfolio.html missing"
    assert not os.path.isfile(os.path.join(here, "frontend", "app.html")), \
        "frontend/app.html still exists — rename incomplete"
