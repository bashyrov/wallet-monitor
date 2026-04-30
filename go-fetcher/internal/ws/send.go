package ws

import (
	"github.com/gorilla/websocket"
)

// SendText writes the bytes as a TEXT frame.
//
// This is the *only* sanctioned way for adapters to send subscribe / heartbeat
// payloads. Bug #1 (orjson→TEXT regression in Python) burned us for weeks
// because some venues (Bitget V2 books channel, MEXC spot) silently drop
// BINARY frames. Centralising the call here means every send goes through
// a single place that can never be downgraded by accident.
//
// Returning an error means the connection is dead — the runner will close
// and reconnect.
func SendText(c *websocket.Conn, payload []byte) error {
	return c.WriteMessage(websocket.TextMessage, payload)
}
