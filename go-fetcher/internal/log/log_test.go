package log

import (
	"testing"

	"github.com/rs/zerolog"
)

func TestInit_ValidLevel(t *testing.T) {
	Init("debug")
	if zerolog.GlobalLevel() != zerolog.DebugLevel {
		t.Errorf("debug level: want %v got %v", zerolog.DebugLevel, zerolog.GlobalLevel())
	}
}

func TestInit_InvalidLevelDefaultsToInfo(t *testing.T) {
	Init("not-a-real-level")
	if zerolog.GlobalLevel() != zerolog.InfoLevel {
		t.Errorf("invalid level should default to info, got %v", zerolog.GlobalLevel())
	}
}

func TestInit_EmptyLevelMapsToNoLevel(t *testing.T) {
	// zerolog.ParseLevel("") returns NoLevel without error — our Init
	// only falls back to Info when err != nil. So an unset LOG_LEVEL
	// env var results in NoLevel, which zerolog treats as "log
	// everything". Documenting the observed behavior.
	Init("")
	if zerolog.GlobalLevel() != zerolog.NoLevel {
		t.Errorf("empty level maps to NoLevel via ParseLevel; got %v", zerolog.GlobalLevel())
	}
}

func TestInit_AllStandardLevels(t *testing.T) {
	cases := map[string]zerolog.Level{
		"trace": zerolog.TraceLevel,
		"debug": zerolog.DebugLevel,
		"info":  zerolog.InfoLevel,
		"warn":  zerolog.WarnLevel,
		"error": zerolog.ErrorLevel,
	}
	for s, want := range cases {
		Init(s)
		if zerolog.GlobalLevel() != want {
			t.Errorf("Init(%q): want %v got %v", s, want, zerolog.GlobalLevel())
		}
	}
}

func TestL_ReturnsNonNilLogger(t *testing.T) {
	Init("info")
	l := L()
	if l == nil {
		t.Errorf("L() returned nil")
	}
}

func TestL_StableAcrossCalls(t *testing.T) {
	Init("info")
	l1 := L()
	l2 := L()
	if l1 != l2 {
		t.Errorf("L() should return the same global instance")
	}
}

func TestInit_PreservesTimestampFormat(t *testing.T) {
	Init("info")
	if zerolog.TimeFieldFormat == "" {
		t.Errorf("TimeFieldFormat should be set after Init")
	}
}
