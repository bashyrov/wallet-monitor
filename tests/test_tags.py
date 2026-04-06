"""Tag CRUD + attaching tags to wallets."""


def _make_tag(client, auth, name="DeFi", color="#1AFFAB"):
    return client.post("/api/tags", json={"name": name, "color": color}, headers=auth)


def _make_wallet(client, auth):
    return client.post("/api/wallets", json={
        "name": "test wallet",
        "wallet_type": "chain",
        "type_value": "ethereum",
        "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
    }, headers=auth).json()["id"]


# ── Tags CRUD ─────────────────────────────────────────────────────────────────

def test_create_tag(client, auth):
    r = _make_tag(client, auth)
    assert r.status_code in (200, 201)
    data = r.json()
    assert data["name"] == "DeFi"
    assert data["color"] == "#1AFFAB"
    assert "id" in data


def test_list_tags_empty(client, auth):
    r = client.get("/api/tags", headers=auth)
    assert r.status_code == 200
    assert r.json() == []


def test_list_tags(client, auth):
    _make_tag(client, auth, "DeFi")
    _make_tag(client, auth, "CEX")
    tags = client.get("/api/tags", headers=auth).json()
    assert len(tags) == 2


def test_duplicate_tag_name(client, auth):
    _make_tag(client, auth, "Unique")
    r = _make_tag(client, auth, "Unique")
    assert r.status_code == 409


def test_update_tag(client, auth):
    tid = _make_tag(client, auth, "Old").json()["id"]
    r = client.put(f"/api/tags/{tid}", json={"name": "New", "color": "#F87171"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["name"] == "New"
    assert r.json()["color"] == "#F87171"


def test_update_tag_partial(client, auth):
    tid = _make_tag(client, auth, "Tag", "#ffffff").json()["id"]
    r = client.put(f"/api/tags/{tid}", json={"color": "#000000"}, headers=auth)
    assert r.status_code == 200
    assert r.json()["name"] == "Tag"
    assert r.json()["color"] == "#000000"


def test_delete_tag(client, auth):
    tid = _make_tag(client, auth).json()["id"]
    r = client.delete(f"/api/tags/{tid}", headers=auth)
    assert r.status_code == 204
    assert client.get("/api/tags", headers=auth).json() == []


def test_delete_nonexistent_tag(client, auth):
    r = client.delete("/api/tags/9999", headers=auth)
    assert r.status_code == 404


def test_tag_invalid_color(client, auth):
    r = client.post("/api/tags", json={"name": "Bad", "color": "red"}, headers=auth)
    assert r.status_code == 422


def test_tag_empty_name(client, auth):
    r = client.post("/api/tags", json={"name": "", "color": "#ffffff"}, headers=auth)
    assert r.status_code == 422


# ── Wallet ↔ Tag ──────────────────────────────────────────────────────────────

def test_add_tag_to_wallet(client, auth):
    wid = _make_wallet(client, auth)
    tid = _make_tag(client, auth).json()["id"]
    r = client.post(f"/api/wallets/{wid}/tags/{tid}", headers=auth)
    assert r.status_code == 200
    wallet = r.json()
    assert any(t["id"] == tid for t in wallet["tags"])


def test_remove_tag_from_wallet(client, auth):
    wid = _make_wallet(client, auth)
    tid = _make_tag(client, auth).json()["id"]
    client.post(f"/api/wallets/{wid}/tags/{tid}", headers=auth)
    r = client.delete(f"/api/wallets/{wid}/tags/{tid}", headers=auth)
    assert r.status_code == 200
    assert r.json()["tags"] == []


def test_wallet_lists_its_tags(client, auth):
    wid = _make_wallet(client, auth)
    t1 = _make_tag(client, auth, "A").json()["id"]
    t2 = _make_tag(client, auth, "B").json()["id"]
    client.post(f"/api/wallets/{wid}/tags/{t1}", headers=auth)
    client.post(f"/api/wallets/{wid}/tags/{t2}", headers=auth)
    wallets = client.get("/api/wallets", headers=auth).json()
    wallet = next(w for w in wallets if w["id"] == wid)
    tag_ids = {t["id"] for t in wallet["tags"]}
    assert tag_ids == {t1, t2}


def test_tag_deleted_removed_from_wallet(client, auth):
    wid = _make_wallet(client, auth)
    tid = _make_tag(client, auth).json()["id"]
    client.post(f"/api/wallets/{wid}/tags/{tid}", headers=auth)
    client.delete(f"/api/tags/{tid}", headers=auth)
    wallets = client.get("/api/wallets", headers=auth).json()
    wallet = next(w for w in wallets if w["id"] == wid)
    assert wallet["tags"] == []


# ── Auth guards ───────────────────────────────────────────────────────────────

def test_tags_require_auth(client):
    assert client.get("/api/tags").status_code == 401
    assert client.post("/api/tags", json={"name": "x", "color": "#ffffff"}).status_code == 401
