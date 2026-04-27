/**
 * Single source of truth for exchange labels + brand colours, plus the
 * live venue lists fetched from /api/meta/venues. Loaded before any
 * other app script on every page that renders exchange names, so
 * screener.html / arb.html / navbar.js never drift.
 *
 * Use:
 *   window.EX.labels.binance      → 'Binance'
 *   window.EX.colors.binance      → '#F0B90B'
 *   window.EX.chip('binance')     → '<span class="ex-chip">…</span>'
 *   window.EX.dot('binance')      → '<span class="ex-dot" data-ex="binance"></span>'
 *   window.EX.lists.screener_cex  → ['binance','bybit',…]   (after loadVenues)
 *   window.EX.counts.portfolio_chains → 14                  (after loadVenues)
 *
 * loadVenues() is called automatically on script load. Pages that need
 * the data immediately should `await window.EX.ready` (a Promise that
 * resolves once venues meta is fetched) before rendering filters or
 * count strings. The labels/colors maps are static and available
 * synchronously — they cover every venue id we currently know about.
 */
(function () {
  const labels = {
    binance: 'Binance', bybit: 'Bybit', okx: 'OKX', gate: 'Gate',
    kucoin: 'KuCoin', mexc: 'MEXC', bitget: 'Bitget',
    hyperliquid: 'Hyperliquid', aster: 'Aster', ethereal: 'Ethereal',
    whitebit: 'WhiteBIT', bingx: 'BingX', backpack: 'Backpack',
    lighter: 'Lighter', paradex: 'Paradex',
    htx: 'HTX', extended: 'Extended', ourbit: 'Ourbit',
    kraken: 'Kraken',
  };
  const colors = {
    binance: '#F0B90B', bybit: '#F0842D', okx: '#C8C8C8', gate: '#17C684',
    kucoin: '#09BA86', mexc: '#17D854', bitget: '#00D2C8',
    hyperliquid: '#64B4FF', aster: '#8A63D2', ethereal: '#C864C8',
    whitebit: '#2DCCCD', bingx: '#1DB8F2', backpack: '#4ADE80',
    lighter: '#A78BFA', paradex: '#FF6A6A',
    htx: '#2E7DF6', extended: '#E879F9', ourbit: '#FFB84D',
    kraken: '#7C5CFF',
    // Chain palette — used by source-chip grids on /landing and /
    ethereum: '#627EEA', bsc: '#F3BA2F', polygon: '#8247E5',
    arbitrum: '#28A0F0', optimism: '#FF0420', base: '#0052FF',
    avalanche: '#E84142', tron: '#C2A633', solana: '#9945FF',
    zksync: '#1C9BEF', linea: '#7B61FF', scroll: '#FFEEDA',
    mantle: '#27E5C7', blast: '#FCFC03', fantom: '#13B5EC',
  };
  // Chain labels (case-aware) so grids render "BSC" / "zkSync" properly.
  const chainLabels = {
    ethereum: 'Ethereum', bsc: 'BSC', polygon: 'Polygon',
    arbitrum: 'Arbitrum', optimism: 'Optimism', base: 'Base',
    avalanche: 'Avalanche', tron: 'Tron', solana: 'Solana',
    zksync: 'zkSync', linea: 'Linea', scroll: 'Scroll',
    mantle: 'Mantle', blast: 'Blast', fantom: 'Fantom',
  };
  Object.assign(labels, chainLabels);

  function dot(ex) {
    const key = (ex || '').toLowerCase();
    return `<span class="ex-dot" data-ex="${key}"></span>`;
  }
  function chip(ex, opts) {
    const key = (ex || '').toLowerCase();
    const label = labels[key] || ex || '—';
    const extra = (opts && opts.suffix) ? ' ' + opts.suffix : '';
    return `<span class="ex-chip">${dot(key)}<span class="ex-name">${label}${extra}</span></span>`;
  }

  if (!document.getElementById('ex-shared-css')) {
    const rules = [
      '.ex-dot{width:7px;height:7px;border-radius:50%;display:inline-block;flex-shrink:0;vertical-align:baseline;box-shadow:0 0 0 1px rgba(0,0,0,.25) inset}',
      '.ex-chip{display:inline-flex;align-items:center;gap:6px;font-weight:600;font-size:12px;color:var(--text,#E6E8E3);}',
      '.ex-chip .ex-name{letter-spacing:.01em}',
    ];
    Object.entries(colors).forEach(([k, v]) => {
      rules.push(`.ex-dot[data-ex="${k}"]{background:${v}}`);
    });
    const el = document.createElement('style');
    el.id = 'ex-shared-css';
    el.textContent = rules.join('\n');
    document.head.appendChild(el);
  }

  // Live venue lists — populated by loadVenues() on script start.
  const lists = {
    screener_cex: [],
    screener_perp_dex: [],
    screener_spot: [],
    portfolio_cex: [],
    portfolio_perp_dex: [],
    portfolio_chains: [],
    // Convenience union — what the /screener and /arb pages historically
    // listed in their dropdowns. Filled after loadVenues().
    screener_all: [],
  };
  const counts = {
    screener_cex: 0,
    screener_perp_dex: 0,
    screener_spot: 0,
    portfolio_cex: 0,
    portfolio_perp_dex: 0,
    portfolio_chains: 0,
  };

  let _resolveReady;
  const ready = new Promise((res) => { _resolveReady = res; });

  async function loadVenues() {
    try {
      const r = await fetch('/api/meta/venues', { credentials: 'omit' });
      if (!r.ok) throw new Error('http ' + r.status);
      const j = await r.json();
      const sc = j.screener || {};
      const po = j.portfolio || {};
      lists.screener_cex      = (sc.cex      || []).map(x => x.id);
      lists.screener_perp_dex = (sc.perp_dex || []).map(x => x.id);
      lists.screener_spot     = (sc.spot     || []).map(x => x.id);
      lists.portfolio_cex     = (po.cex      || []).map(x => x.id);
      lists.portfolio_perp_dex= (po.perp_dex || []).map(x => x.id);
      lists.portfolio_chains  = (po.chains   || []).map(x => x.id);
      lists.screener_all = lists.screener_cex.concat(lists.screener_perp_dex);
      Object.assign(counts, j.counts || {});
      // Backfill labels for any ids the API surfaces that weren't in our
      // static map (new venues added server-side without a frontend release).
      [sc.cex, sc.perp_dex, sc.spot, po.cex, po.perp_dex, po.chains].forEach(arr => {
        (arr || []).forEach(({id, label}) => {
          if (id && label && !labels[id]) labels[id] = label;
        });
      });
      // Render any data-meta="<count_key>" placeholder text on the page.
      document.querySelectorAll('[data-meta]').forEach(el => {
        const key = el.getAttribute('data-meta');
        if (key && counts[key] != null) {
          el.textContent = String(counts[key]);
        }
      });
      // Render any data-venues="<list_key>" placeholder as a comma-joined
      // label list, fed by the actual provider labels returned by the API.
      const labelMap = {};
      [sc.cex, sc.perp_dex, sc.spot, po.cex, po.perp_dex, po.chains].forEach(arr => {
        (arr || []).forEach(({id, label}) => { if (id) labelMap[id] = label || labels[id] || id; });
      });
      const venuesByKey = {
        screener_cex: lists.screener_cex,
        screener_perp_dex: lists.screener_perp_dex,
        screener_spot: lists.screener_spot,
        portfolio_cex: lists.portfolio_cex,
        portfolio_perp_dex: lists.portfolio_perp_dex,
        portfolio_chains: lists.portfolio_chains,
      };
      document.querySelectorAll('[data-venues]').forEach(el => {
        const key = el.getAttribute('data-venues');
        const ids = venuesByKey[key];
        if (Array.isArray(ids) && ids.length) {
          el.textContent = ids.map(id => labelMap[id] || labels[id] || id).join(', ');
        }
      });
      // data-meta-sum="a+b" — sum of the named counters (e.g. total
      // screener venues = screener_cex + screener_perp_dex).
      document.querySelectorAll('[data-meta-sum]').forEach(el => {
        const expr = el.getAttribute('data-meta-sum') || '';
        const total = expr.split('+').map(k => k.trim()).reduce((s, k) => s + (counts[k] || 0), 0);
        if (total > 0) el.textContent = String(total);
      });
      // data-venues-grid="<key>" — render a source-chip per venue id. The
      // key may be one of the venuesByKey labels OR a "+" expression
      // (e.g. portfolio_cex+portfolio_perp_dex+portfolio_chains).
      document.querySelectorAll('[data-venues-grid]').forEach(el => {
        const expr = el.getAttribute('data-venues-grid') || '';
        const ids = [];
        expr.split('+').forEach(k => {
          const part = venuesByKey[k.trim()];
          if (Array.isArray(part)) ids.push(...part);
        });
        if (!ids.length) return;
        const cls = el.getAttribute('data-chip-class') || 'source-chip';
        const dotCls = el.getAttribute('data-chip-dot-class') || 'source-chip-dot';
        el.innerHTML = ids.map(id => {
          const lbl = labelMap[id] || labels[id] || id;
          const col = colors[id] || 'var(--text3)';
          return `<div class="${cls}"><span class="${dotCls}" style="background:${col}"></span>${lbl}</div>`;
        }).join('');
      });
    } catch (e) {
      console.warn('EX.loadVenues failed:', e);
    } finally {
      _resolveReady();
    }
  }
  loadVenues();

  window.EX = { labels, colors, dot, chip, lists, counts, ready, loadVenues };
})();
