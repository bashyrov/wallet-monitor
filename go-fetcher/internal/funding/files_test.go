package funding

import (
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestBuildRows_BasicShape(t *testing.T) {
	byEx := map[string]map[string]Tick{
		"binance": {
			"BTC": {Symbol: "BTC", Rate: 0.0001, MarkPrice: 60000, Volume24h: 1e9, IntervalH: 8},
		},
	}
	rows := buildRows(byEx)
	if len(rows) != 1 {
		t.Fatalf("rows: %d", len(rows))
	}
	r := rows[0]
	if r["symbol"] != "BTC" || r["exchange"] != "binance" {
		t.Errorf("symbol/ex: %+v", r)
	}
	if r["rate"] != 0.0001 {
		t.Errorf("rate: %v", r["rate"])
	}
	if r["price"] != float64(60000) {
		t.Errorf("price: %v", r["price"])
	}
	if r["volume_usd"] != 1e9 {
		t.Errorf("volume_usd: %v", r["volume_usd"])
	}
	if r["cross_listed"] != false {
		t.Errorf("single-venue should be cross_listed=false, got %v", r["cross_listed"])
	}
}

func TestBuildRows_CrossListedFlag(t *testing.T) {
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 8}},
		"bybit":   {"BTC": {Symbol: "BTC", Rate: 0.00012, IntervalH: 8}},
		"okx":     {"ETH": {Symbol: "ETH", Rate: 0.0002, IntervalH: 8}},
	}
	rows := buildRows(byEx)
	if len(rows) != 3 {
		t.Fatalf("rows: %d", len(rows))
	}
	for _, r := range rows {
		if r["symbol"] == "BTC" && r["cross_listed"] != true {
			t.Errorf("BTC on multiple venues should be cross_listed=true, got %v", r)
		}
		if r["symbol"] == "ETH" && r["cross_listed"] != false {
			t.Errorf("ETH on single venue should be cross_listed=false, got %v", r)
		}
	}
}

func TestBuildRows_APRComputation(t *testing.T) {
	// APR formula: rate * (8/interval_h) * (8760/8) * 100
	// For 8h interval @ rate=0.0001: APR = 0.0001 * 1 * 1095 * 100 = 10.95
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 8}},
	}
	rows := buildRows(byEx)
	apr := rows[0]["apr"].(float64)
	if math.Abs(apr-10.95) > 0.001 {
		t.Errorf("APR for rate=0.0001 @ 8h: want ~10.95 got %v", apr)
	}
}

func TestBuildRows_APRForFourHourInterval(t *testing.T) {
	// 4h interval: rate is paid twice as often → APR doubles
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 4}},
	}
	rows := buildRows(byEx)
	apr := rows[0]["apr"].(float64)
	if math.Abs(apr-21.9) > 0.001 {
		t.Errorf("APR for rate=0.0001 @ 4h: want ~21.9 got %v", apr)
	}
}

func TestBuildRows_DefaultIntervalIs8(t *testing.T) {
	// Missing IntervalH (0) defaults to 8h.
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 0}},
	}
	rows := buildRows(byEx)
	if rows[0]["interval_h"] != 8.0 {
		t.Errorf("interval default: %v", rows[0]["interval_h"])
	}
}

func TestBuildRows_NextTsZeroWhenNoNextFunding(t *testing.T) {
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 8}},
	}
	rows := buildRows(byEx)
	if rows[0]["next_ts"] != 0 {
		t.Errorf("missing NextFunding should yield next_ts=0, got %v", rows[0]["next_ts"])
	}
}

func TestBuildRows_NextTsEpochSeconds(t *testing.T) {
	// Per code comment: next_ts is in EPOCH SECONDS, not milliseconds.
	ts := time.UnixMilli(1718000028000)
	byEx := map[string]map[string]Tick{
		"binance": {"BTC": {Symbol: "BTC", Rate: 0.0001, IntervalH: 8, NextFunding: ts}},
	}
	rows := buildRows(byEx)
	if rows[0]["next_ts"] != int64(1718000028) {
		t.Errorf("next_ts should be epoch seconds, got %v", rows[0]["next_ts"])
	}
}

func TestTickToJSON_FieldShape(t *testing.T) {
	ts := time.UnixMilli(1718000001000)
	next := time.UnixMilli(1718000028000)
	out := tickToJSON(Tick{
		Symbol: "BTC", Rate: 0.0001, MarkPrice: 60000, IndexPrice: 59999,
		Volume24h: 1e9, OpenIntUSD: 5e8, IntervalH: 8, NextFunding: next,
		UpdatedAt: ts,
	})
	if out["rate"] != 0.0001 || out["mark_price"] != float64(60000) {
		t.Errorf("primary fields: %+v", out)
	}
	if out["updated_at"] != int64(1718000001000) {
		t.Errorf("updated_at (ms): %v", out["updated_at"])
	}
	if out["next_funding"] != int64(1718000028000) {
		t.Errorf("next_funding (ms): %v", out["next_funding"])
	}
}

func TestTickToJSON_OmitsNextFundingWhenZero(t *testing.T) {
	out := tickToJSON(Tick{Symbol: "BTC", Rate: 0.0001})
	if _, ok := out["next_funding"]; ok {
		t.Errorf("zero NextFunding should be omitted from JSON, got %v", out["next_funding"])
	}
}

func TestDumper_WritesPerVenueAndMerged(t *testing.T) {
	dir := t.TempDir()
	store := NewStore()
	store.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: 60000, Volume24h: 1e9, IntervalH: 8})
	store.Apply("bybit", Tick{Symbol: "BTC", Rate: 0.00012, MarkPrice: 60100, IntervalH: 8})

	d := NewDumper(store, dir, 100*time.Millisecond)
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	// Per-venue files
	for _, ex := range []string{"binance", "bybit"} {
		path := filepath.Join(dir, "funding."+ex+".json")
		if _, err := os.Stat(path); err != nil {
			t.Errorf("funding.%s.json missing: %v", ex, err)
		}
	}
	// Merged file with cross-venue rows
	raw, err := os.ReadFile(filepath.Join(dir, "funding.json"))
	if err != nil {
		t.Fatal(err)
	}
	var merged map[string]any
	_ = json.Unmarshal(raw, &merged)
	rows := merged["rows"].([]any)
	if len(rows) != 2 {
		t.Errorf("merged rows: want 2 got %d", len(rows))
	}
}

func TestDumper_OBSourceFillsMissingMark(t *testing.T) {
	// HTX-class case: rate from REST but mark missing → orderbook midprice fills in
	dir := t.TempDir()
	store := NewStore()
	store.Apply("htx", Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: 0, IntervalH: 8})

	d := NewDumper(store, dir, 100*time.Millisecond)
	d.SetOrderbookSource(func(ex, sym string) (float64, float64, bool) {
		if ex == "htx" && sym == "BTC" {
			return 59999, 60001, true
		}
		return 0, 0, false
	})
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	raw, _ := os.ReadFile(filepath.Join(dir, "funding.htx.json"))
	var doc map[string]any
	_ = json.Unmarshal(raw, &doc)
	btc := doc["BTC"].(map[string]any)
	mark := btc["mark_price"].(float64)
	// midprice of 59999/60001 = 60000
	if mark != 60000 {
		t.Errorf("OB midprice fill: want 60000 got %v", mark)
	}
}

func TestDumper_OBSourceDoesNotOverwriteExistingMark(t *testing.T) {
	dir := t.TempDir()
	store := NewStore()
	store.Apply("binance", Tick{Symbol: "BTC", Rate: 0.0001, MarkPrice: 60500, IntervalH: 8})

	d := NewDumper(store, dir, 100*time.Millisecond)
	d.SetOrderbookSource(func(ex, sym string) (float64, float64, bool) {
		return 59999, 60001, true
	})
	if err := d.dump(); err != nil {
		t.Fatal(err)
	}

	raw, _ := os.ReadFile(filepath.Join(dir, "funding.binance.json"))
	var doc map[string]any
	_ = json.Unmarshal(raw, &doc)
	btc := doc["BTC"].(map[string]any)
	// Existing mark 60500 should NOT be overwritten by midprice 60000
	if btc["mark_price"].(float64) != 60500 {
		t.Errorf("existing mark overwritten by OB source: %v", btc["mark_price"])
	}
}
