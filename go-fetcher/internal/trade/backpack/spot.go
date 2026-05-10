// Backpack is a spot-native exchange (no futures yet). The existing
// PlaceOrder / ClosePosition already implement spot semantics. This file
// wires them to trade.SpotAdapter so the dispatcher routes
// market_type=spot requests here instead of erroring.
package backpack

import (
	"context"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/trade"
)

func (a *Adapter) PlaceSpotOrder(ctx context.Context, creds trade.Creds, req trade.OpenRequest) (*trade.Result, error) {
	return a.PlaceOrder(ctx, creds, req)
}

func (a *Adapter) CloseSpotPosition(ctx context.Context, creds trade.Creds, req trade.CloseRequest) (*trade.Result, error) {
	return a.ClosePosition(ctx, creds, req)
}
