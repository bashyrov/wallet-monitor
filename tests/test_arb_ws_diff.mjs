// Frontend unit test for _applyArbPayload — mirrors the backend unit tests.
// Run: node tests/test_arb_ws_diff.mjs

import { readFileSync } from 'node:fs';

const html = readFileSync(new URL('../frontend/screener.html', import.meta.url), 'utf8');

// Extract the block that defines _arbRows / _arbRowsByKey / _arbKey / _applyArbPayload.
const startMarker = 'let _arbRows = [];';
const endMarker = '_arbRows = Array.from(_arbRowsByKey.values());';
const startIdx = html.indexOf(startMarker);
const endIdx = html.indexOf(endMarker, startIdx);
if (startIdx === -1 || endIdx === -1) {
  console.error('FAIL: could not locate _applyArbPayload block in screener.html');
  process.exit(1);
}
// Close the enclosing function (…})
const block = html.slice(startIdx, html.indexOf('}', endIdx) + 1);
// Swap 'let'/'const' for 'var' so the top-level identifiers are reachable
// from code we append below.
const module = block
  .replace(/^let\s+/gm, 'var ')
  .replace(/^const\s+/gm, 'var ');

// Boot the module + assertions in the same scope
const harness = `
${module}

function assert(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } else { console.log('  ok — ' + msg); } }

// 1. Initial snapshot populates state
_applyArbPayload({ type: 'snapshot', opportunities: [
  { symbol: 'BTC', long_exchange: 'binance', short_exchange: 'okx', net_profit: 1.0 },
  { symbol: 'ETH', long_exchange: 'binance', short_exchange: 'okx', net_profit: 2.0 },
]});
assert(_arbRows.length === 2, 'snapshot seeds 2 rows');
assert(_arbRowsByKey.has('BTC|binance|okx'), 'BTC key present after snapshot');

// 2. Diff applies added + updated + removed
_applyArbPayload({ type: 'diff',
  added:   [{ symbol: 'SOL', long_exchange: 'binance', short_exchange: 'okx', net_profit: 3.0 }],
  updated: [{ symbol: 'BTC', long_exchange: 'binance', short_exchange: 'okx', net_profit: 1.5 }],
  removed: [['ETH', 'binance', 'okx']],
});
assert(_arbRows.length === 2, 'after diff: 2 rows (BTC + SOL)');
assert(_arbRowsByKey.get('BTC|binance|okx').net_profit === 1.5, 'BTC updated in place');
assert(_arbRowsByKey.has('SOL|binance|okx'), 'SOL added');
assert(!_arbRowsByKey.has('ETH|binance|okx'), 'ETH removed');

// 3. Empty diff = no-op
_applyArbPayload({ type: 'diff' });
assert(_arbRows.length === 2, 'empty diff leaves state intact');

// 4. Reconnect snapshot wipes and re-seeds
_applyArbPayload({ type: 'snapshot', opportunities: [
  { symbol: 'DOGE', long_exchange: 'binance', short_exchange: 'okx', net_profit: 0.5 },
]});
assert(_arbRows.length === 1, 'reconnect snapshot wipes old state');
assert(_arbRowsByKey.has('DOGE|binance|okx'), 'DOGE present after snapshot');
assert(!_arbRowsByKey.has('BTC|binance|okx'), 'old BTC gone after snapshot');

// 5. Legacy untyped payload is treated as snapshot
_applyArbPayload({ opportunities: [
  { symbol: 'LINK', long_exchange: 'binance', short_exchange: 'okx', net_profit: 0.8 },
]});
assert(_arbRows.length === 1, 'legacy payload treated as snapshot');
assert(_arbRowsByKey.has('LINK|binance|okx'), 'LINK present from legacy snapshot');

// 6. Removed accepts both array-form and string-form keys (belt-and-suspenders)
_applyArbPayload({ type: 'snapshot', opportunities: [
  { symbol: 'A', long_exchange: 'x', short_exchange: 'y', net_profit: 1 },
  { symbol: 'B', long_exchange: 'x', short_exchange: 'y', net_profit: 1 },
  { symbol: 'C', long_exchange: 'x', short_exchange: 'y', net_profit: 1 },
]});
_applyArbPayload({ type: 'diff', removed: [['A', 'x', 'y'], 'B|x|y'] });
assert(_arbRows.length === 1 && _arbRows[0].symbol === 'C', 'array-form and string-form removed keys both work');

// 7. Fees / exchanges meta tracked separately
_applyArbPayload({ type: 'snapshot', fees: { binance: 0.04 }, exchanges: ['binance'],
  opportunities: [{ symbol: 'Z', long_exchange: 'binance', short_exchange: 'okx', net_profit: 1 }]
});
assert(_arbMeta.fees.binance === 0.04, 'snapshot records fees');
_applyArbPayload({ type: 'diff', fees: { binance: 0.05 } });
assert(_arbMeta.fees.binance === 0.05, 'diff updates fees dict');

console.log('\\n✓ all frontend _applyArbPayload cases pass');
`;

eval(harness);
