// Per-venue "how to connect" copy — single source of truth used by both
// the /profile API Keys form and the /portfolio Add Wallet form.
// Each entry: where the keys live, what permissions to set, and what
// goes in each form field. Keep concise — this surfaces in a small
// help panel, not a long article.

window.VENUE_HELP = {
  // ── CEX ────────────────────────────────────────────────────────────
  binance: {
    where: 'binance.com → User Center → API Management → Create API',
    perms: '✅ Read · ✅ Enable Futures · ❌ Withdrawals · IP whitelist 37.60.252.32',
    fields: [
      ['API Key',    'Long string from Binance'],
      ['API Secret', 'Shown ONCE at creation — copy immediately'],
    ],
  },
  bybit: {
    where: 'bybit.com → API → New Key',
    perms: '✅ Read+Trade (Unified/Spot/Contract) · ❌ Withdraw · ❌ Transfer',
    fields: [
      ['API Key',    'From Bybit dashboard'],
      ['API Secret', 'Shown ONCE'],
    ],
  },
  okx: {
    where: 'okx.com → User Center → API → Create V5 API Key',
    perms: '✅ Read + Trade · ❌ Withdraw · pin IP 37.60.252.32 if possible',
    fields: [
      ['API Key',    'OKX API key (shown after Create)'],
      ['API Secret', 'OKX secret (shown ONCE at Create — copy immediately)'],
      ['Passphrase', 'The passphrase you typed when creating the key — case-sensitive'],
    ],
  },
  gate: {
    where: 'gate.io → API Management → Create',
    perms: '✅ Read + Perpetual Trade + Spot Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'Gate API key'],
      ['API Secret', 'Gate secret'],
    ],
  },
  mexc: {
    where: 'mexc.com → Account → API Management',
    perms: '✅ Read + Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'MEXC API key'],
      ['API Secret', 'MEXC secret'],
    ],
  },
  kucoin: {
    where: 'kucoin.com → API → Create General API',
    perms: '✅ General + Trade · ❌ Transfer · ❌ Withdraw',
    fields: [
      ['API Key',    'KuCoin API key'],
      ['API Secret', 'KuCoin secret'],
      ['Passphrase', 'The passphrase you set when creating the key'],
    ],
  },
  bitget: {
    where: 'bitget.com → API Management → Create',
    perms: '✅ Read + Spot Trade + Contracts Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'Bitget API key'],
      ['API Secret', 'Bitget secret'],
      ['Passphrase', 'The passphrase you set'],
    ],
  },
  bingx: {
    where: 'bingx.com → API Management → Create',
    perms: '✅ Read + Spot/Perp Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'BingX API key'],
      ['API Secret', 'BingX secret'],
    ],
  },
  whitebit: {
    where: 'whitebit.com → API → Generate',
    perms: '✅ Read + Trade (Margin/Spot) · ❌ Withdraw',
    fields: [
      ['API Key',    'WhiteBIT public key'],
      ['API Secret', 'WhiteBIT private key (NOT a crypto wallet privkey — venue-issued)'],
    ],
  },
  kraken: {
    where: 'futures.kraken.com → Settings → API → Generate Key (Futures API — NOT regular Kraken Spot API!)',
    perms: '✅ Read + Place / Cancel / Modify Orders · ❌ Withdraw',
    fields: [
      ['API Key',    'Kraken Futures key'],
      ['API Secret', 'Kraken Futures secret (base64-encoded — copy AS IS, do not decode)'],
    ],
  },
  htx: {
    where: 'huobi.com → API Management → Create',
    perms: '✅ Read + Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'HTX access key'],
      ['API Secret', 'HTX secret key'],
    ],
  },
  backpack: {
    where: 'backpack.exchange → Settings → API Keys → Generate (NOT EVM-style — Ed25519)',
    perms: 'Backpack permissions per-instruction (no global Read/Trade toggle); pin your IP if possible',
    fields: [
      ['API Key',    'Base64 Ed25519 PUBLIC key (looks like "AbC..." 44 chars, ends with "=")'],
      ['API Secret', 'Base64 Ed25519 PRIVATE key seed (32 bytes encoded — shown ONCE at Generate)'],
    ],
  },

  // ── Perp DEX ───────────────────────────────────────────────────────
  hyperliquid: {
    where: 'hyperliquid.xyz/agentWallet → 1) Generate, 2) Approve on-chain with your main wallet',
    perms: 'Agent Wallet can trade but CANNOT withdraw — safe to give us',
    fields: [
      ['Wallet Address', 'Your MAIN HL wallet 0x… (the account that owns the funds — NOT the agent wallet address)'],
      ['Private Key',    'Agent Wallet private key (shown ONCE at Generate). After Generate you MUST click Approve so the main wallet signs an on-chain tx — without it the agent can\'t trade.'],
    ],
  },
  aster: {
    where: 'asterdex.com/en/api-wallet → 1) Generate, 2) Authorize on-chain (most users miss step 2!)',
    perms: '✅ Read + Perp Trade + Spot Trade (NO withdraw — agent wallets can\'t)',
    fields: [
      ['Master Wallet Address',  'Your MAIN Aster login wallet 0x… (the account that owns the funds — NOT the API wallet)'],
      ['API Wallet Private Key', 'EVM privkey from the Generate popup (shown ONCE). Then you MUST click Authorize and sign an on-chain tx with your master wallet — without that step the key returns "No agent found".'],
    ],
  },
  ethereal: {
    where: 'ethereal.trade → log in with your wallet → Settings → API → Generate Linked Signer',
    perms: 'Linked signer trades your subaccount; CANNOT withdraw or transfer funds',
    fields: [
      ['Wallet Address', 'Your EVM wallet 0x… connected to Ethereal (the one with funds — subaccount auto-derived)'],
      ['Private Key',    'OPTIONAL for Portfolio. REQUIRED for Screener: linked-signer private key from the Generate popup (NOT your main wallet privkey).'],
    ],
  },
  lighter: {
    where: 'app.lighter.xyz → Account → API Management → Generate API Key (shows API Key Index + Public Key + Private Key, popup ONE TIME)',
    perms: 'API keys only authorize trade actions; withdrawals require main wallet signature',
    fields: [
      ['Wallet Address',  'Your EVM L1 wallet 0x… connected to Lighter (where you deposited)'],
      ['Private Key',     'OPTIONAL for Portfolio. REQUIRED for Screener: ZK signing key from the "Generate API Key" popup (NOT your EVM privkey).'],
      ['Account Index',   'Leave blank — we auto-derive the numeric account_index from your L1 address via Lighter\'s public REST.'],
      ['API Key Index',   'OPTIONAL for Portfolio. For Screener: the integer shown in the popup (e.g. 4); defaults to 255 if blank.'],
    ],
  },
  paradex: {
    where: 'paradex.trade → Account Security → Key Management → Generate Subkey (recommended — Subkey can\'t withdraw)',
    perms: 'Subkeys can trade but CANNOT withdraw or transfer funds',
    fields: [
      ['Starknet L2 Address', 'Your Paradex L2 address (starts with 0x… as a felt — longer than a regular EVM hex)'],
      ['L2 Private Key',      'OPTIONAL for Portfolio (read-only via JWT). REQUIRED for Screener: Subkey L2 private key. Avoid using your main L2 key — Subkey is the safe option.'],
      ['Subkey Public Key',   'OPTIONAL — only fill if you pasted a Subkey private key above. Leave blank if you used the main L2 key.'],
    ],
  },
  extended: {
    where: 'extended.exchange → API Management → Create API Key (gives you api_key + Stark L2 pubkey + Stark L2 privkey + vault id)',
    perms: '✅ Read + Trade. No-withdrawal by default on API keys',
    fields: [
      ['API Key',             'String from Extended UI (the only field needed for Portfolio).'],
      ['Stark L2 Public Key', 'Hex Stark L2 pubkey from API Management. ONLY for Screener — Portfolio works without it.'],
      ['Stark L2 Private Key','Hex Stark L2 privkey (shown ONCE at create). ONLY for Screener — used to sign orders.'],
      ['Vault',               'Numeric collateral_position_id (your subaccount id on Extended). ONLY for Screener.'],
    ],
  },

  // ── Chains ─────────────────────────────────────────────────────────
  // No private keys for chains — just public addresses.
};

// Render a help-card DOM fragment for a given venue. Returns a string of
// HTML (caller injects into the form). No styling assumptions beyond the
// .venue-help-* classes which both forms ship.
window.renderVenueHelp = function(venue) {
  const h = (window.VENUE_HELP || {})[venue];
  if (!h) {
    // Chains and any unmapped venue: short generic note.
    return `<div class="venue-help-empty">Public address only — no private keys needed.</div>`;
  }
  const fields = (h.fields || []).map(([k, v]) =>
    `<li><strong>${k}:</strong> ${v}</li>`
  ).join('');
  return `
    <div class="venue-help-line"><span class="venue-help-icon">📍</span><span>${h.where}</span></div>
    <div class="venue-help-line"><span class="venue-help-icon">🔒</span><span>${h.perms}</span></div>
    ${fields ? `<ul class="venue-help-fields">${fields}</ul>` : ''}
  `;
};
