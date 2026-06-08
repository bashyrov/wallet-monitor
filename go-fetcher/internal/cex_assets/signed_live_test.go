package cex_assets

import (
	"context"
	"os"
	"strings"
	"testing"
	"time"
)

// Signed adapter live tests. Run only when CEX_ASSETS_LIVE=1 AND the
// per-venue read-only credentials are present in the env. Each test
// skips when its venue's keys are missing — same hybrid policy the
// manager uses in prod, so dev/CI without keys silently skips.
//
// Usage:
//   CEX_ASSETS_LIVE=1 \
//     CEX_BINANCE_READ_KEY=... CEX_BINANCE_READ_SECRET=... \
//     CEX_BYBIT_READ_KEY=...   CEX_BYBIT_READ_SECRET=... \
//     CEX_OKX_READ_KEY=...     CEX_OKX_READ_SECRET=...     CEX_OKX_READ_PASSPHRASE=... \
//     CEX_MEXC_READ_KEY=...    CEX_MEXC_READ_SECRET=... \
//     CEX_BINGX_READ_KEY=...   CEX_BINGX_READ_SECRET=... \
//     go test ./internal/cex_assets/... -v -run TestSignedLive
func TestSignedLive_Binance(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run")
	}
	creds := LoadSignedCreds("binance")
	if !creds.HasKey() {
		t.Skip("binance: no CEX_BINANCE_READ_KEY/_SECRET in env")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	assets, err := FetchBinance(ctx, nil, creds)
	if err != nil {
		t.Fatalf("binance: %v", err)
	}
	checkCanary(t, "binance", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestSignedLive_Bybit(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run")
	}
	creds := LoadSignedCreds("bybit")
	if !creds.HasKey() {
		t.Skip("bybit: no CEX_BYBIT_READ_KEY/_SECRET in env")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	assets, err := FetchBybit(ctx, nil, creds)
	if err != nil {
		t.Fatalf("bybit: %v", err)
	}
	checkCanary(t, "bybit", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestSignedLive_OKX(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run")
	}
	creds := LoadSignedCreds("okx")
	if !creds.HasKey() || creds.Passphrase == "" {
		t.Skip("okx: no CEX_OKX_READ_KEY / _SECRET / _PASSPHRASE in env")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	assets, err := FetchOKX(ctx, nil, creds)
	if err != nil {
		t.Fatalf("okx: %v", err)
	}
	checkCanary(t, "okx", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestSignedLive_MEXC(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run")
	}
	creds := LoadSignedCreds("mexc")
	if !creds.HasKey() {
		t.Skip("mexc: no CEX_MEXC_READ_KEY/_SECRET in env")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	assets, err := FetchMEXC(ctx, nil, creds)
	if err != nil {
		t.Fatalf("mexc: %v", err)
	}
	checkCanary(t, "mexc", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

func TestSignedLive_BingX(t *testing.T) {
	if os.Getenv("CEX_ASSETS_LIVE") != "1" {
		t.Skip("set CEX_ASSETS_LIVE=1 to run")
	}
	creds := LoadSignedCreds("bingx")
	if !creds.HasKey() {
		t.Skip("bingx: no CEX_BINGX_READ_KEY/_SECRET in env")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	assets, err := FetchBingX(ctx, nil, creds)
	if err != nil {
		t.Fatalf("bingx: %v", err)
	}
	checkCanary(t, "bingx", assets, "USDT", "ethereum", "0xdac17f958d2ee523a2206206994597c13d831ec7")
}

// TestSignedCreds_LoadFromEnv — verifies the env reader. No live calls.
func TestSignedCreds_LoadFromEnv(t *testing.T) {
	os.Setenv("CEX_FOOTEST_READ_KEY", "k1")
	os.Setenv("CEX_FOOTEST_READ_SECRET", "s1")
	defer os.Unsetenv("CEX_FOOTEST_READ_KEY")
	defer os.Unsetenv("CEX_FOOTEST_READ_SECRET")
	c := LoadSignedCreds("footest")
	if !c.HasKey() {
		t.Fatalf("HasKey false despite env set")
	}
	if c.APIKey != "k1" || c.APISecret != "s1" {
		t.Errorf("creds wrong: %+v", c)
	}
	// Lowercase venue input must also work — manager passes "binance"/"bybit"/etc.
	os.Setenv("CEX_BARTEST_READ_KEY", "k2")
	defer os.Unsetenv("CEX_BARTEST_READ_KEY")
	c2 := LoadSignedCreds("bartest")
	if c2.HasKey() {
		t.Errorf("HasKey should be false without secret")
	}
}

// TestSigning_Helpers — pure unit tests for the HMAC functions.
func TestSigning_Helpers(t *testing.T) {
	// HMAC-SHA256 sanity vector — cross-checked against Python's hmac
	// library (`hmac.new(secret, payload, sha256).hexdigest()`). Catches
	// any future regression where the wrong digest function is wired.
	secret := "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
	payload := "timestamp=1499827319559"
	want := "2222d49722f6af5da13f6da6bfc0d7de19ca2815ebc98bbc49e4942268472f3f"
	got := HMACHex(secret, payload)
	if !strings.EqualFold(got, want) {
		t.Errorf("HMACHex Binance vector mismatch:\n got %s\nwant %s", got, want)
	}
	// SortedQuery — keys must appear in lex order.
	q := SortedQuery(map[string]string{"zebra": "1", "apple": "2", "mango": "3"})
	want2 := "apple=2&mango=3&zebra=1"
	if q != want2 {
		t.Errorf("SortedQuery: got %q want %q", q, want2)
	}
}
