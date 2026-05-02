package trade

import "fmt"

// ErrorKind mirrors Python's `TradeError(kind=...)` so the HTTP layer
// can map errors to status codes consistently across web and Go paths.
type ErrorKind string

const (
	// User-facing input or business-rule violation. Surface verbatim.
	KindUser ErrorKind = "user"
	// Exchange said no — surface friendly_msg, log full detail.
	KindExchange ErrorKind = "exchange"
	// We made the request wrong (signing, shape, schema). Internal bug.
	KindInternal ErrorKind = "internal"
	// Exchange rate-limited us — caller may retry with backoff.
	KindRateLimit ErrorKind = "rate_limit"
	// Network blip — caller may retry.
	KindTransient ErrorKind = "transient"
)

// Error — every adapter returns these (or wraps a stdlib error). The
// HTTP layer / Python proxy upcasts to the matching kind.
type Error struct {
	Kind    ErrorKind
	Code    string // exchange-specific error code, optional
	Message string
	Cause   error
}

func (e *Error) Error() string {
	if e == nil {
		return "<nil trade error>"
	}
	if e.Code != "" {
		return fmt.Sprintf("trade.%s[%s]: %s", e.Kind, e.Code, e.Message)
	}
	return fmt.Sprintf("trade.%s: %s", e.Kind, e.Message)
}

func (e *Error) Unwrap() error { return e.Cause }

func errUser(msg string, args ...any) *Error {
	return &Error{Kind: KindUser, Message: fmt.Sprintf(msg, args...)}
}

func errExchange(code, msg string, args ...any) *Error {
	return &Error{Kind: KindExchange, Code: code, Message: fmt.Sprintf(msg, args...)}
}

func errInternal(msg string, cause error) *Error {
	return &Error{Kind: KindInternal, Message: msg, Cause: cause}
}

func errRateLimit(msg string, cause error) *Error {
	return &Error{Kind: KindRateLimit, Message: msg, Cause: cause}
}

func errTransient(msg string, cause error) *Error {
	return &Error{Kind: KindTransient, Message: msg, Cause: cause}
}

// IsUser / IsExchange / IsTransient — convenience for callers that
// want to decide whether to retry without a type assertion.
func IsUser(e error) bool      { return kindOf(e) == KindUser }
func IsExchange(e error) bool  { return kindOf(e) == KindExchange }
func IsTransient(e error) bool { return kindOf(e) == KindTransient }
func IsRateLimit(e error) bool { return kindOf(e) == KindRateLimit }

func kindOf(e error) ErrorKind {
	if te, ok := e.(*Error); ok && te != nil {
		return te.Kind
	}
	return ""
}
