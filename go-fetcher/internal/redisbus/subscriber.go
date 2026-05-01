// Package redisbus is the Redis pub/sub bridge between Python web roles
// and the Go fetcher. Python publishes "<venue>:<symbol>" payloads on
// `book:subscribe` and `book:unsubscribe` channels; we subscribe and
// route them into the SymbolManager.
//
// Wire contract (matches Python's _SUB_CHANNEL / _UNSUB_CHANNEL):
//
//	PUBLISH book:subscribe   "binance:BTC"
//	PUBLISH book:unsubscribe "binance:BTC"
//
// Reconnect / dropped-message handling: Redis pub/sub doesn't durable —
// if we miss a message during reconnect, the worst case is a 5-second
// extra wait until web's next periodic re-publish (Python's user-touch
// loop re-emits every active /arb pair every 30s).
package redisbus

import (
	"context"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"

	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/log"
	"github.com/bashyrov/wallet-monitor/go-fetcher/internal/symbols"
)

const (
	subscribeChannel   = "book:subscribe"
	unsubscribeChannel = "book:unsubscribe"
)

// Subscriber listens on the two pub/sub channels and routes to a
// SymbolManager. Run() blocks until ctx is cancelled, reconnecting
// with exponential backoff on transient errors.
type Subscriber struct {
	client *redis.Client
	mgr    *symbols.Manager
}

func NewSubscriber(redisURL string, mgr *symbols.Manager) (*Subscriber, error) {
	if redisURL == "" {
		return nil, nil // Redis disabled — caller skips Run()
	}
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	return &Subscriber{client: redis.NewClient(opts), mgr: mgr}, nil
}

func (s *Subscriber) Run(ctx context.Context) {
	if s == nil {
		return
	}
	backoff := 500 * time.Millisecond
	const maxBackoff = 30 * time.Second

	for {
		if ctx.Err() != nil {
			return
		}
		err := s.session(ctx)
		if err == nil || err == context.Canceled {
			return
		}
		log.L().Debug().Err(err).Dur("backoff", backoff).Msg("redis sub session ended")
		select {
		case <-ctx.Done():
			return
		case <-time.After(backoff):
		}
		backoff *= 2
		if backoff > maxBackoff {
			backoff = maxBackoff
		}
	}
}

func (s *Subscriber) session(ctx context.Context) error {
	pubsub := s.client.Subscribe(ctx, subscribeChannel, unsubscribeChannel)
	defer pubsub.Close()

	// Synchronous receipt of subscribe ack — surfaces auth/network errors
	// up front instead of silently failing on the first PUBLISH.
	if _, err := pubsub.Receive(ctx); err != nil {
		return err
	}
	log.L().Info().Msg("redis pub/sub bridge online (book:subscribe / book:unsubscribe)")

	ch := pubsub.Channel()
	for {
		select {
		case <-ctx.Done():
			return context.Canceled
		case msg, ok := <-ch:
			if !ok {
				return nil
			}
			venue, symbol := splitPair(msg.Payload)
			if venue == "" || symbol == "" {
				continue
			}
			switch msg.Channel {
			case subscribeChannel:
				s.mgr.Touch(venue, symbol)
			case unsubscribeChannel:
				s.mgr.Untouch(venue, symbol)
			}
		}
	}
}

// splitPair parses "venue:symbol" — same shape as Python's _normalize_pair.
func splitPair(payload string) (string, string) {
	idx := strings.IndexByte(payload, ':')
	if idx <= 0 || idx >= len(payload)-1 {
		return "", ""
	}
	venue := strings.TrimSpace(strings.ToLower(payload[:idx]))
	symbol := strings.TrimSpace(strings.ToUpper(payload[idx+1:]))
	if venue == "" || symbol == "" {
		return "", ""
	}
	// Defensive: enforce simple ASCII alnum + underscore (matches
	// Python's _normalize_pair regex).
	for _, c := range venue {
		if !alnum(c) && c != '_' {
			return "", ""
		}
	}
	for _, c := range symbol {
		if !alnum(c) && c != '_' {
			return "", ""
		}
	}
	return venue, symbol
}

func alnum(c rune) bool {
	return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z')
}

// Close releases the underlying Redis client connection pool.
func (s *Subscriber) Close() error {
	if s == nil {
		return nil
	}
	return s.client.Close()
}
