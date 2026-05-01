package wsbroadcast

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"testing"
	"time"
)

func makeToken(t *testing.T, secret string, payload map[string]any) string {
	t.Helper()
	hb, _ := json.Marshal(map[string]any{"alg": "HS256", "typ": "JWT"})
	pb, _ := json.Marshal(payload)
	h := base64.RawURLEncoding.EncodeToString(hb)
	p := base64.RawURLEncoding.EncodeToString(pb)
	signing := h + "." + p
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(signing))
	sig := base64.RawURLEncoding.EncodeToString(mac.Sum(nil))
	return signing + "." + sig
}

func TestJWTValidatorAcceptsValid(t *testing.T) {
	v := NewJWTValidator("test-secret")
	tok := makeToken(t, "test-secret", map[string]any{
		"sub": "42",
		"exp": time.Now().Add(time.Hour).Unix(),
	})
	uid, err := v.Decode(tok)
	if err != nil {
		t.Fatalf("expected ok, got err: %v", err)
	}
	if uid != 42 {
		t.Fatalf("expected uid=42, got %d", uid)
	}
}

func TestJWTValidatorRejectsExpired(t *testing.T) {
	v := NewJWTValidator("test-secret")
	tok := makeToken(t, "test-secret", map[string]any{
		"sub": "1",
		"exp": time.Now().Add(-time.Hour).Unix(),
	})
	if _, err := v.Decode(tok); err == nil {
		t.Fatalf("expected expired-token error, got nil")
	}
}

func TestJWTValidatorRejectsBadSig(t *testing.T) {
	v := NewJWTValidator("test-secret")
	tok := makeToken(t, "OTHER-secret", map[string]any{
		"sub": "1",
		"exp": time.Now().Add(time.Hour).Unix(),
	})
	if _, err := v.Decode(tok); err == nil {
		t.Fatalf("expected sig-mismatch error, got nil")
	}
}

func TestJWTValidatorRejectsScope(t *testing.T) {
	v := NewJWTValidator("test-secret")
	tok := makeToken(t, "test-secret", map[string]any{
		"sub":   "1",
		"exp":   time.Now().Add(time.Hour).Unix(),
		"scope": "totp_challenge",
	})
	if _, err := v.Decode(tok); err == nil {
		t.Fatalf("expected scoped-token rejection, got nil")
	}
}
