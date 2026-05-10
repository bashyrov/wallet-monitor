// HTTP transport for the trade package.
//
// Routes (mounted at /internal/trade/* by main.go):
//
//	POST /internal/trade/open    {exchange, creds, request}      → Result
//	POST /internal/trade/close   {exchange, creds, request}      → Result
//	POST /internal/trade/leverage {exchange, creds, request}     → 204
//	POST /internal/trade/positions {exchange, creds, symbol?}    → []Position
//	POST /internal/trade/balance {exchange, creds}               → Balance
//	GET  /internal/trade/health  → {supported: ["binance", ...]}
//
// Security: this listener is reachable ONLY from the Python web role
// over the docker compose network — nginx never forwards /internal/*.
// We additionally require the X-Internal-Auth header to match
// $AVALANT_INTERNAL_SECRET so a bug or accidental nginx misconfig
// can't expose the endpoint to the public internet.
//
// Why POST for everything: creds (api_key + secret) ride in the JSON
// body. Putting them in query strings would land them in nginx access
// logs. A GET that takes a body is also unfriendly to clients.
package trade

import (
	"encoding/json"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
)

// Routes — call this from main.go after the WS broadcaster routes are
// installed. Adds 5 endpoints to the existing mux. The shared-secret
// is read once at registration time so a new value requires a restart
// (intentional — secrets shouldn't hot-swap).
func Routes(mux *http.ServeMux) {
	secret := strings.TrimSpace(os.Getenv("AVALANT_INTERNAL_SECRET"))
	if secret == "" {
		log.L().Warn().Msg("AVALANT_INTERNAL_SECRET unset — /internal/trade/* refuses every request")
	}
	guard := func(next http.HandlerFunc) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			if secret == "" || r.Header.Get("X-Internal-Auth") != secret {
				http.Error(w, "forbidden", http.StatusForbidden)
				return
			}
			next(w, r)
		}
	}
	mux.HandleFunc("/internal/trade/open", guard(handleOpen))
	mux.HandleFunc("/internal/trade/close", guard(handleClose))
	mux.HandleFunc("/internal/trade/leverage", guard(handleLeverage))
	mux.HandleFunc("/internal/trade/positions", guard(handlePositions))
	mux.HandleFunc("/internal/trade/balance", guard(handleBalance))
	mux.HandleFunc("/internal/trade/health", guard(handleHealth))
}

// ── Request envelopes ────────────────────────────────────────────────────

type openBody struct {
	Exchange string      `json:"exchange"`
	Creds    Creds       `json:"creds"`
	Request  OpenRequest `json:"request"`
}

type closeBody struct {
	Exchange string       `json:"exchange"`
	Creds    Creds        `json:"creds"`
	Request  CloseRequest `json:"request"`
}

type leverageBody struct {
	Exchange string          `json:"exchange"`
	Creds    Creds           `json:"creds"`
	Request  LeverageRequest `json:"request"`
}

type positionsBody struct {
	Exchange string `json:"exchange"`
	Symbol   string `json:"symbol,omitempty"`
	Creds    Creds  `json:"creds"`
}

type balanceBody struct {
	Exchange string `json:"exchange"`
	Creds    Creds  `json:"creds"`
}

// ── Handlers ─────────────────────────────────────────────────────────────

func handleOpen(w http.ResponseWriter, r *http.Request) {
	t0 := time.Now()
	var b openBody
	if !decodeJSON(w, r, &b) {
		return
	}
	a := lookupOrFail(w, b.Exchange)
	if a == nil {
		return
	}
	tBeforePlace := time.Now()
	res, err := a.PlaceOrder(r.Context(), b.Creds, b.Request)
	tAfterPlace := time.Now()
	totalMs := tAfterPlace.Sub(t0).Milliseconds()
	venueMs := tAfterPlace.Sub(tBeforePlace).Milliseconds()
	if err != nil {
		log.L().Warn().
			Str("ex", b.Exchange).Str("sym", b.Request.Symbol).
			Int64("total_ms", totalMs).Int64("venue_ms", venueMs).
			Err(err).Msg("trade open failed")
		writeError(w, err)
		return
	}
	log.L().Info().
		Str("ex", b.Exchange).Str("sym", b.Request.Symbol).
		Int64("total_ms", totalMs).Int64("venue_ms", venueMs).
		Str("order_id", res.OrderID).
		Msg("trade open")
	writeJSON(w, http.StatusOK, res)
}

func handleClose(w http.ResponseWriter, r *http.Request) {
	var b closeBody
	if !decodeJSON(w, r, &b) {
		return
	}
	a := lookupOrFail(w, b.Exchange)
	if a == nil {
		return
	}
	res, err := a.ClosePosition(r.Context(), b.Creds, b.Request)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

func handleLeverage(w http.ResponseWriter, r *http.Request) {
	var b leverageBody
	if !decodeJSON(w, r, &b) {
		return
	}
	a := lookupOrFail(w, b.Exchange)
	if a == nil {
		return
	}
	if err := a.SetLeverage(r.Context(), b.Creds, b.Request); err != nil {
		writeError(w, err)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func handlePositions(w http.ResponseWriter, r *http.Request) {
	var b positionsBody
	if !decodeJSON(w, r, &b) {
		return
	}
	a := lookupOrFail(w, b.Exchange)
	if a == nil {
		return
	}
	out, err := a.ListPositions(r.Context(), b.Creds, b.Symbol)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, out)
}

func handleBalance(w http.ResponseWriter, r *http.Request) {
	var b balanceBody
	if !decodeJSON(w, r, &b) {
		return
	}
	a := lookupOrFail(w, b.Exchange)
	if a == nil {
		return
	}
	bal, err := a.GetBalance(r.Context(), b.Creds)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bal)
}

func handleHealth(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"supported": SupportedExchanges(),
	})
}

// ── Helpers ──────────────────────────────────────────────────────────────

func decodeJSON(w http.ResponseWriter, r *http.Request, into any) bool {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return false
	}
	r.Body = http.MaxBytesReader(w, r.Body, 32*1024) // creds are tiny; cap big to refuse abuse.
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(into); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": err.Error(), "kind": "user"})
		return false
	}
	return true
}

func lookupOrFail(w http.ResponseWriter, name string) Adapter {
	a := Lookup(name)
	if a == nil {
		writeJSON(w, http.StatusNotImplemented, map[string]string{
			"error": "exchange not supported by Go trade engine",
			"kind":  "user",
		})
	}
	return a
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, err error) {
	te, ok := err.(*Error)
	if !ok || te == nil {
		writeJSON(w, http.StatusInternalServerError,
			map[string]string{"error": err.Error(), "kind": "internal"})
		return
	}
	status := http.StatusBadRequest
	switch te.Kind {
	case KindUser:
		status = http.StatusBadRequest
	case KindExchange:
		status = http.StatusUnprocessableEntity
	case KindRateLimit:
		status = http.StatusTooManyRequests
	case KindTransient:
		status = http.StatusBadGateway
	case KindInternal:
		status = http.StatusInternalServerError
	}
	writeJSON(w, status, map[string]string{
		"error": te.Message,
		"kind":  string(te.Kind),
		"code":  te.Code,
	})
}
