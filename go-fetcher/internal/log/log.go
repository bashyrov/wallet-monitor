// Package log centralises zerolog setup. JSON output by default — matches
// Python's AVALANT_LOG_FORMAT=json so existing log shippers don't care which
// runtime emitted the line.
package log

import (
	"os"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

// Init wires up the global zerolog instance. Called once from main().
//
// Format mirrors Python's JsonFormatter: timestamp + level + message + the
// usual structured fields (exchange, symbol, ws.frames, ...).
func Init(level string) {
	zerolog.TimeFieldFormat = time.RFC3339Nano
	lvl, err := zerolog.ParseLevel(level)
	if err != nil {
		lvl = zerolog.InfoLevel
	}
	zerolog.SetGlobalLevel(lvl)
	log.Logger = zerolog.New(os.Stdout).With().Timestamp().Logger()
}

// L returns the global logger. Always use this — never construct your own.
func L() *zerolog.Logger { return &log.Logger }
