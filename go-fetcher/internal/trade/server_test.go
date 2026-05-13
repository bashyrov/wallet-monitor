package trade

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestDecodeJSON_RejectsGETMethod(t *testing.T) {
	req := httptest.NewRequest(http.MethodGet, "/x", nil)
	w := httptest.NewRecorder()
	var into map[string]any
	if decodeJSON(w, req, &into) {
		t.Errorf("GET should be rejected")
	}
	if w.Code != http.StatusMethodNotAllowed {
		t.Errorf("status: want 405 got %d", w.Code)
	}
}

func TestDecodeJSON_RejectsUnknownFields(t *testing.T) {
	body := bytes.NewBufferString(`{"exchange":"binance","unknown_field":42}`)
	req := httptest.NewRequest(http.MethodPost, "/x", body)
	w := httptest.NewRecorder()
	var into openBody
	if decodeJSON(w, req, &into) {
		t.Errorf("unknown field should be rejected (DisallowUnknownFields)")
	}
	if w.Code != http.StatusBadRequest {
		t.Errorf("status: want 400 got %d", w.Code)
	}
}

func TestDecodeJSON_AcceptsValidShape(t *testing.T) {
	body := bytes.NewBufferString(`{"exchange":"binance","creds":{"api_key":"k"},"request":{}}`)
	req := httptest.NewRequest(http.MethodPost, "/x", body)
	w := httptest.NewRecorder()
	var into openBody
	if !decodeJSON(w, req, &into) {
		t.Fatalf("valid shape rejected: %d %s", w.Code, w.Body.String())
	}
	if into.Exchange != "binance" || into.Creds.APIKey != "k" {
		t.Errorf("decoded: %+v", into)
	}
}

func TestLookupOrFail_UnknownExchangeWrites501(t *testing.T) {
	w := httptest.NewRecorder()
	got := lookupOrFail(w, "no-such-venue-xyz123")
	if got != nil {
		t.Errorf("unknown venue should return nil")
	}
	if w.Code != http.StatusNotImplemented {
		t.Errorf("status: want 501 got %d", w.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["kind"] != "user" {
		t.Errorf("kind field: want 'user' got %q", body["kind"])
	}
}

func TestLookupOrFail_KnownExchangeReturnsAdapter(t *testing.T) {
	Register("found-test", &stubAdapter{name: "found-test"})
	w := httptest.NewRecorder()
	got := lookupOrFail(w, "found-test")
	if got == nil {
		t.Errorf("registered adapter not found")
	}
	if w.Code != 0 && w.Code != http.StatusOK {
		t.Errorf("should not write response on hit: %d", w.Code)
	}
}

func TestWriteJSON_SetsContentTypeAndEncodes(t *testing.T) {
	w := httptest.NewRecorder()
	writeJSON(w, http.StatusOK, map[string]any{"hello": "world"})
	if w.Code != http.StatusOK {
		t.Errorf("status: %d", w.Code)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("content-type: %q", ct)
	}
	if !strings.Contains(w.Body.String(), `"hello":"world"`) {
		t.Errorf("body: %s", w.Body.String())
	}
}

func TestWriteError_TradeUserKindMapsTo400(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errUser("bad input"))
	if w.Code != http.StatusBadRequest {
		t.Errorf("KindUser → 400, got %d", w.Code)
	}
}

func TestWriteError_TradeExchangeKindMapsTo422(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errExchange("60011", "auth"))
	if w.Code != http.StatusUnprocessableEntity {
		t.Errorf("KindExchange → 422, got %d", w.Code)
	}
}

func TestWriteError_TradeRateLimitMapsTo429(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errRateLimit("too fast", nil))
	if w.Code != http.StatusTooManyRequests {
		t.Errorf("KindRateLimit → 429, got %d", w.Code)
	}
}

func TestWriteError_TradeTransientMapsTo502(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errTransient("conn reset", nil))
	if w.Code != http.StatusBadGateway {
		t.Errorf("KindTransient → 502, got %d", w.Code)
	}
}

func TestWriteError_TradeInternalMapsTo500(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errInternal("oops", nil))
	if w.Code != http.StatusInternalServerError {
		t.Errorf("KindInternal → 500, got %d", w.Code)
	}
}

func TestWriteError_StdlibErrorMapsTo500AsInternal(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errors.New("plain error"))
	if w.Code != http.StatusInternalServerError {
		t.Errorf("stdlib error → 500, got %d", w.Code)
	}
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["kind"] != "internal" {
		t.Errorf("stdlib should map to kind=internal, got %q", body["kind"])
	}
}

func TestWriteError_IncludesCodeField(t *testing.T) {
	w := httptest.NewRecorder()
	writeError(w, errExchange("E60011", "auth required"))
	var body map[string]string
	_ = json.Unmarshal(w.Body.Bytes(), &body)
	if body["code"] != "E60011" {
		t.Errorf("code field: %q", body["code"])
	}
}
