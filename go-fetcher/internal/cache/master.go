package cache

import (
	"path/filepath"
)

// dumpMaster writes a "master" subset of the orderbook cache to
// books.master.json. The master file mirrors Python's behaviour: it holds
// keys that aren't also written to a per-venue books.<ex>.json by a
// separate worker — i.e. spot venues, paradex, lighter, backpack, etc.
//
// In the Go fetcher every venue's WS adapter writes to the same in-process
// _book_cache, so per-venue files already cover everything. The master
// file is kept as a forward-compatibility hand-off for Python's merger,
// which UNIONs books.<ex>.json files plus books.master.json into the
// final books.json fed to the screener.
//
// Heuristic: anything with `_spot` suffix OR matching the exotic-venue
// allowlist below goes into master. Matches what Python's master process
// dumps so the contract on Python's side stays unchanged.
var masterVenues = map[string]struct{}{
	"binance_spot": {}, "bybit_spot": {}, "okx_spot": {}, "gate_spot": {},
	"kucoin_spot": {}, "bitget_spot": {}, "bingx_spot": {}, "htx_spot": {},
	"mexc_spot": {},
	"paradex":   {},
	"lighter":   {},
	"backpack":  {},
	"hyperliquid": {},
	"extended":  {}, // future
}

// writeMasterFile dumps just the master-eligible keys. Called from
// Dumper.dump() after the full snapshot is taken.
func (d *Dumper) writeMasterFile(snap map[string]Entry) error {
	out := make(map[string]any, 256)
	for k, v := range snap {
		idx := indexColon(k)
		if idx <= 0 {
			continue
		}
		ex := k[:idx]
		if _, ok := masterVenues[ex]; !ok {
			continue
		}
		out[k] = entryToJSON(v)
	}
	if len(out) == 0 {
		return nil
	}
	return writeAtomic(filepath.Join(d.cacheDir, "books.master.json"), out)
}

func indexColon(s string) int {
	for i, c := range s {
		if c == ':' {
			return i
		}
	}
	return -1
}
