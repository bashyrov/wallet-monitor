package ws

import (
	"errors"
	"time"

	"github.com/gorilla/websocket"
)

// Policy-violation close codes that mean "the server is rejecting our
// subscribe / behaviour" — fast retry just deepens the ban (bug #2, #3).
//
//	1008 — Binance "Pong timeout" / generic policy violation
//	3001 — Aster "Server error or session policy violation"
//	1011 — Hyperliquid funding "keepalive timeout"
//	4400 — generic 4xxx range used by several venues for invalid frames
//	4401 — auth-required (we hit this when subscribe goes through before
//	       the JWT first-frame on /ws/funding)
var policyCodes = map[int]struct{}{
	1008: {},
	1011: {},
	3001: {},
	4400: {},
	4401: {},
}

// IsPolicyClose reports whether `err` is a websocket-CloseError matching
// one of the policy-violation codes. The caller (runner) uses this to pick
// the long backoff timer instead of the short transient one.
func IsPolicyClose(err error) bool {
	if err == nil {
		return false
	}
	var ce *websocket.CloseError
	if errors.As(err, &ce) {
		_, ok := policyCodes[ce.Code]
		return ok
	}
	return false
}

// Backoff is a two-tier backoff. transient.next() handles ordinary network
// errors with a fast curve (resets to 0.3s on first frame). policy.next()
// handles the policy codes above with a much longer curve that *only*
// resets when we successfully receive a data frame, not just connect.
type Backoff struct {
	transientCur time.Duration
	policyCur    time.Duration
}

const (
	transientStart = 300 * time.Millisecond
	transientCap   = 30 * time.Second
	policyStart    = 30 * time.Second
	policyCap      = 5 * time.Minute
)

func (b *Backoff) NextTransient() time.Duration {
	if b.transientCur == 0 {
		b.transientCur = transientStart
	}
	d := b.transientCur
	if b.transientCur < transientCap {
		b.transientCur *= 2
		if b.transientCur > transientCap {
			b.transientCur = transientCap
		}
	}
	return d
}

func (b *Backoff) NextPolicy() time.Duration {
	if b.policyCur == 0 {
		b.policyCur = policyStart
	}
	d := b.policyCur
	if b.policyCur < policyCap {
		b.policyCur *= 2
		if b.policyCur > policyCap {
			b.policyCur = policyCap
		}
	}
	return d
}

// ResetTransient — called once a connection is open + first subscribe-ack
// arrives. Resets the transient curve only.
func (b *Backoff) ResetTransient() { b.transientCur = transientStart }

// ResetPolicy — called only after a data frame (not a connect or ack).
// "Connection opened" is not enough — Aster/Binance ban us by accepting
// the connection then closing on subscribe.
func (b *Backoff) ResetPolicy() { b.policyCur = policyStart }
