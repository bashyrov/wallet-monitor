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
    where: 'okx.com → API → Create V5 API Key',
    perms: '✅ Read + Trade · ❌ Withdraw',
    fields: [
      ['API Key',    'OKX API key'],
      ['API Secret', 'OKX secret'],
      ['Passphrase', 'The passphrase you set when creating the key'],
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
    where: 'kraken.com → Futures → Settings → API → Generate Key (Futures API!)',
    perms: '✅ Read + Place / Cancel / Modify Orders · ❌ Withdraw',
    fields: [
      ['API Key',    'Kraken Futures key'],
      ['API Secret', 'Kraken Futures secret (base64)'],
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
    where: 'backpack.exchange → Settings → API Keys → Generate',
    perms: '✅ All trade actions (Backpack splits perms by instruction, no global toggle)',
    fields: [
      ['API Key',    'Base64-encoded Ed25519 public key'],
      ['API Secret', 'Base64-encoded Ed25519 seed (32 bytes)'],
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
    where: 'ethereal.trade → API → Generate Linked Signer',
    perms: 'Linked signer trades on your subaccount — no withdrawal capability',
    fields: [
      ['Wallet Address', 'Subaccount address 0x…'],
      ['Private Key',    'Linked signer private key (EVM hex)'],
    ],
  },
  lighter: {
    where: 'app.lighter.xyz → Settings → API → Generate API Key',
    perms: 'API keys only authorize trade actions; withdrawals require main wallet sig',
    fields: [
      ['Wallet Address',  'Your EVM wallet 0x… connected to Lighter'],
      ['Private Key',     'ZK signing key from the "Generate" popup (NOT EVM privkey)'],
      ['Account Index',   'OPTIONAL — we auto-derive from your wallet address'],
      ['API Key Index',   'Number from the popup (e.g. 4) — defaults to 255 if blank'],
    ],
  },
  paradex: {
    where: 'paradex.trade → Account Security → Key Management → Generate Subkey',
    perms: 'Subkeys can trade but CANNOT withdraw or transfer funds',
    fields: [
      ['Starknet L2 Address', 'Your Paradex L2 address'],
      ['L2 Private Key',      'Subkey private key (much safer than main key)'],
      ['Subkey Public Key',   'OPTIONAL — only if you used a subkey above'],
    ],
  },
  extended: {
    where: 'extended.exchange → API Management → Create API Key',
    perms: '✅ Read + Trade. No-withdrawal by default on API keys',
    fields: [
      ['API Key',             'String from Extended UI'],
      ['Stark L2 Public Key', 'Stark L2 pubkey (also from API Management) — only for trading'],
      ['Stark L2 Private Key','Stark L2 privkey (shown once) — only for trading'],
      ['Vault',               'collateral_position_id — int subaccount id — only for trading'],
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
