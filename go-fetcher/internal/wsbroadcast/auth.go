// Package wsbroadcast — Go-side WS fan-out for /api/screener/ws/*.
//
// Why this exists: Python uvicorn + asyncio handles WS broadcast today
// but the GIL turns N concurrent clients × M-byte messages into a CPU
// bottleneck. Go's per-goroutine sends scale linearly with cores.
// nginx splits /api/screener/ws/* to this binary; everything else
// stays on the Python app.
//
// Wire compatibility: the on-the-wire frame format must match the
// previous Python broadcaster byte-for-byte so the existing frontend
// doesn't need any change. See parseframe / build*Snapshot for the
// shapes (snapshot + diff, ping/pong text frames).
//
// auth.go — JWT validation. HS256 only, same SECRET_KEY env var the
// Python side uses. Returns user_id from `sub` claim or 0 for anon.
package wsbroadcast

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"strconv"
	"strings"
	"time"
)

// JWTValidator — minimal HS256-only validator, matches the Python
// auth_service.decode_token contract (returns sub or None/0).
type JWTValidator struct {
	secret []byte
}

func NewJWTValidator(secret string) *JWTValidator {
	return &JWTValidator{secret: []byte(secret)}
}

type jwtPayload struct {
	Sub   string `json:"sub"`
	Exp   int64  `json:"exp"`
	Scope string `json:"scope,omitempty"`
}

// Decode validates the token and returns the user_id from `sub`.
// Returns (0, nil) for the anonymous case caller may map to "guest".
// Returns (user_id, nil) on success. Returns (0, err) on bad token.
//
// Tokens with a `scope` claim are rejected — those are short-lived
// challenge tokens (e.g. TOTP) and must not grant WS access. The
// Python `get_current_user` enforces the same rule.
func (v *JWTValidator) Decode(token string) (int, error) {
	if token == "" {
		return 0, errors.New("empty token")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return 0, errors.New("malformed token")
	}
	signingInput := parts[0] + "." + parts[1]
	gotSig, err := base64.RawURLEncoding.DecodeString(parts[2])
	if err != nil {
		return 0, errors.New("bad sig encoding")
	}
	mac := hmac.New(sha256.New, v.secret)
	mac.Write([]byte(signingInput))
	if !hmac.Equal(gotSig, mac.Sum(nil)) {
		return 0, errors.New("sig mismatch")
	}
	payloadJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return 0, errors.New("bad payload encoding")
	}
	var p jwtPayload
	if err := json.Unmarshal(payloadJSON, &p); err != nil {
		return 0, errors.New("bad payload json")
	}
	if p.Exp > 0 && time.Now().Unix() > p.Exp {
		return 0, errors.New("expired")
	}
	if p.Scope != "" {
		// Scope-tagged tokens (totp_challenge, etc.) must not grant
		// general session access. Same rule the Python side enforces
		// in get_current_user.
		return 0, errors.New("scoped token not allowed")
	}
	uid, err := strconv.Atoi(p.Sub)
	if err != nil {
		return 0, errors.New("bad sub")
	}
	return uid, nil
}
