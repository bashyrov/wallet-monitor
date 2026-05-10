// Lighter zk-perp adapter — partial port (read-only).
//
// Lighter is a ZK-rollup; trade authority is delegated to API keys whose
// signatures are computed by a native CGO library shipped per-platform
// with the official `lighter-sdk` Python wrapper. There is no
// Go-native equivalent for that signer, and bringing the same shared
// libs in over CGO would tie this package to the upstream's release
// cadence + per-arch builds.
//
// What this adapter does:
//
//   - GetBalance / ListPositions hit the unsigned REST endpoints
//     (/api/v1/account) and run entirely in Go.
//   - PlaceOrder / ClosePosition / SetLeverage return a clean
//     KindUnsupported error so the caller can fall back to the
//     Python adapter (which keeps lighter-sdk in-process).
//
// Operationally: keep "lighter" out of GO_TRADE_VENUES. If it ends up
// there by mistake, the user-facing error makes the misconfig obvious
// without crashing the trade flow.
//
// Credentials mapping (matches Python):
//
//	APIKey     → account_index   (numeric string)
//	APISecret  → api_private_key (hex, 0x optional)
//	Passphrase → api_key_index   (default "255")
package lighter

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

const baseURL = "https://mainnet.zklighter.elliot.ai"

type Adapter struct {
	httpClient *http.Client
}

func New() *Adapter {
	return &Adapter{
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
			Transport: &http.Transport{
				ForceAttemptHTTP2:   true,
				MaxIdleConns:        200,
				MaxIdleConnsPerHost: 32,
				MaxConnsPerHost:     64,
				IdleConnTimeout:     300 * time.Second,
				TLSHandshakeTimeout: 5 * time.Second,
			},
		},
	}
}

func init() { trade.Register("lighter", New()) }

func (a *Adapter) Name() string { return "lighter" }

// ── REST (no signing) ────────────────────────────────────────────────────

func (a *Adapter) get(ctx context.Context, path string, qs map[string]string) (json.RawMessage, error) {
	u := baseURL + path
	if len(qs) > 0 {
		first := true
		for k, v := range qs {
			sep := "&"
			if first {
				sep = "?"
				first = false
			}
			u += sep + k + "=" + v
		}
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	resp, err := a.httpClient.Do(req)
	if err != nil {
		return nil, &trade.Error{Kind: trade.KindTransient, Message: err.Error(), Cause: err}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		return nil, &trade.Error{Kind: trade.KindExchange, Message: strings.TrimSpace(string(raw))}
	}
	return raw, nil
}

func (a *Adapter) accountIndex(creds trade.Creds) (string, error) {
	idx := strings.TrimSpace(creds.APIKey)
	if idx == "" {
		return "", errUser("lighter requires the numeric account_index in api_key")
	}
	if _, err := strconv.Atoi(idx); err != nil {
		return "", errUser("lighter account_index must be an integer")
	}
	return idx, nil
}

func (a *Adapter) GetBalance(ctx context.Context, creds trade.Creds) (*trade.Balance, error) {
	idx, err := a.accountIndex(creds)
	if err != nil {
		return nil, err
	}
	body, err := a.get(ctx, "/api/v1/account", map[string]string{"by": "index", "value": idx})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Accounts []struct {
			Assets []struct {
				Symbol        string      `json:"symbol"`
				Balance       json.Number `json:"balance"`
				LockedBalance json.Number `json:"locked_balance"`
			} `json:"assets"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse account", err)
	}
	if len(resp.Accounts) == 0 {
		return &trade.Balance{}, nil
	}
	var total float64
	for _, asset := range resp.Accounts[0].Assets {
		sym := strings.ToUpper(asset.Symbol)
		if sym != "USDC" && sym != "USDT" {
			continue
		}
		bal, _ := asset.Balance.Float64()
		locked, _ := asset.LockedBalance.Float64()
		total += bal + locked
	}
	return &trade.Balance{TotalUSD: total, AvailableUSD: total}, nil
}

func (a *Adapter) ListPositions(ctx context.Context, creds trade.Creds, symbol string) ([]trade.Position, error) {
	idx, err := a.accountIndex(creds)
	if err != nil {
		return nil, err
	}
	body, err := a.get(ctx, "/api/v1/account", map[string]string{"by": "index", "value": idx})
	if err != nil {
		return nil, err
	}
	var resp struct {
		Accounts []struct {
			Positions []struct {
				Symbol           string      `json:"symbol"`
				Position         json.Number `json:"position"`
				Sign             int         `json:"sign"`
				AvgEntryPrice    json.Number `json:"avg_entry_price"`
				UnrealizedPnl    json.Number `json:"unrealized_pnl"`
				RealizedFunding  json.Number `json:"realized_funding"`
				AllocatedMargin  json.Number `json:"allocated_margin"`
			} `json:"positions"`
		} `json:"accounts"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		return nil, errInternal("parse positions", err)
	}
	if len(resp.Accounts) == 0 {
		return nil, nil
	}
	want := strings.ToUpper(symbol)
	out := make([]trade.Position, 0, len(resp.Accounts[0].Positions))
	for _, p := range resp.Accounts[0].Positions {
		qty, _ := p.Position.Float64()
		if qty == 0 {
			continue
		}
		sym := strings.ToUpper(p.Symbol)
		if want != "" && sym != want {
			continue
		}
		side := trade.SideBuy
		if p.Sign != 1 && qty <= 0 {
			side = trade.SideSell
		}
		entry, _ := p.AvgEntryPrice.Float64()
		upnl, _ := p.UnrealizedPnl.Float64()
		funding, _ := p.RealizedFunding.Float64()
		levF, _ := p.AllocatedMargin.Float64()
		out = append(out, trade.Position{
			Symbol:        sym,
			Side:          side,
			Quantity:      abs(qty),
			EntryPrice:    entry,
			UnrealizedPnL: upnl,
			FundingPnL:    funding,
			Leverage:      int(levF),
			MarginMode:    trade.MarginCross,
		})
	}
	return out, nil
}

// ── Trade actions: ZK signing not implemented in Go ──────────────────────

var errZK = &trade.Error{
	Kind: trade.KindUser,
	Message: "lighter trade actions require ZK signing (lighter-sdk CGO) — " +
		"not yet ported to Go; route this venue through the Python adapter " +
		"(remove from GO_TRADE_VENUES).",
}

func (a *Adapter) SetLeverage(_ context.Context, _ trade.Creds, _ trade.LeverageRequest) error {
	return errZK
}
func (a *Adapter) PlaceOrder(_ context.Context, _ trade.Creds, _ trade.OpenRequest) (*trade.Result, error) {
	return nil, errZK
}
func (a *Adapter) ClosePosition(_ context.Context, _ trade.Creds, _ trade.CloseRequest) (*trade.Result, error) {
	return nil, errZK
}

// ── Helpers ──────────────────────────────────────────────────────────────

func abs(f float64) float64 {
	if f < 0 {
		return -f
	}
	return f
}

func errUser(msg string, args ...any) *trade.Error {
	return &trade.Error{Kind: trade.KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *trade.Error {
	return &trade.Error{Kind: trade.KindInternal, Message: msg, Cause: cause}
}

var _ trade.Adapter = (*Adapter)(nil)
