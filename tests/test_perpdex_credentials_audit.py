"""Task 1 audit — perpdex providers must declare which credentials they
need so the wallet-creation form can collect the right fields.

This test locks the metadata flags surfaced through /api/wallets/options.
Storage-layer plumbing for these creds is a follow-up — see
AUDIT_WALLETS.md.
"""


def test_perpdex_options_surface_trade_credential_flags(client, auth):
    r = client.get("/api/wallets/options", headers=auth)
    assert r.status_code == 200
    data = r.json()
    by_value = {p["value"]: p for p in data["perpdex_types"]}

    # Aster: api_key + api_secret (EVM private key in api_secret)
    aster = by_value.get("aster")
    assert aster is not None
    assert aster["needs_api_key"] is True
    # Aster does NOT have needs_private_key flag — its secret IS the PK,
    # named api_secret for backwards compat with EVM exchanges.

    # Hyperliquid: needs an EVM private key for EIP-712 signing
    hl = by_value.get("hyperliquid")
    assert hl is not None
    assert hl["needs_private_key"] is True
    assert hl["needs_api_key"] is False

    # Ethereal: same — EVM key for personal_sign
    et = by_value.get("ethereal")
    assert et is not None
    assert et["needs_private_key"] is True

    # Paradex: needs L2 stark private key + JWT
    px = by_value.get("paradex")
    assert px is not None
    assert px["needs_l2_private_key"] is True
    assert px["needs_api_token"] is True

    # Lighter: triple credentials — account_index, hex pk, api_key_index
    lh = by_value.get("lighter")
    assert lh is not None
    assert lh["needs_account_index"] is True
    assert lh["needs_private_key"] is True
    assert lh["needs_api_key_index"] is True


def test_cex_passphrase_flags_unchanged(client, auth):
    """OKX, KuCoin, Bitget require passphrase; the others don't."""
    r = client.get("/api/wallets/options", headers=auth)
    assert r.status_code == 200
    by_value = {p["value"]: p for p in r.json()["exchange_types"]}
    for ex in ("okx", "kucoin", "bitget"):
        assert by_value[ex]["needs_passphrase"] is True, f"{ex} should require passphrase"
    for ex in ("binance", "bybit", "gate", "mexc", "bingx"):
        assert by_value[ex]["needs_passphrase"] is False, f"{ex} should not require passphrase"
