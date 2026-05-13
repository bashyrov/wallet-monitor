package config

import (
	"testing"
)

func TestGetenvBool_TruthyVariants(t *testing.T) {
	cases := []struct {
		val  string
		want bool
	}{
		{"1", true},
		{"true", true},
		{"TRUE", true},
		{"True", true},
		{"yes", true},
		{"on", true},
	}
	for _, c := range cases {
		t.Setenv("X_TEST_BOOL", c.val)
		if got := getenvBool("X_TEST_BOOL", false); got != c.want {
			t.Errorf("getenvBool(%q): want %v got %v", c.val, c.want, got)
		}
	}
}

func TestGetenvBool_FalsyVariants(t *testing.T) {
	cases := []string{"0", "false", "FALSE", "no", "off"}
	for _, v := range cases {
		t.Setenv("X_TEST_BOOL", v)
		if got := getenvBool("X_TEST_BOOL", true); got != false {
			t.Errorf("getenvBool(%q): want false got %v", v, got)
		}
	}
}

func TestGetenvBool_UnsetReturnsDefault(t *testing.T) {
	if got := getenvBool("X_UNSET_VAR_42", true); got != true {
		t.Errorf("unset with default=true: want true got %v", got)
	}
	if got := getenvBool("X_UNSET_VAR_42", false); got != false {
		t.Errorf("unset with default=false: want false got %v", got)
	}
}

func TestGetenvBool_UnrecognisedReturnsDefault(t *testing.T) {
	// Garbage value falls back to default rather than failing.
	t.Setenv("X_TEST_BOOL", "maybe")
	if got := getenvBool("X_TEST_BOOL", true); got != true {
		t.Errorf("garbage val with default=true: want true got %v", got)
	}
}

func TestLoad_RedisBookWriteDefaultsTrue(t *testing.T) {
	// Default preserves current production behaviour — the toggle is
	// opt-OUT, so a fresh deployment continues writing to Redis.
	cfg := Load()
	if !cfg.RedisBookWriteEnabled {
		t.Errorf("RedisBookWriteEnabled default: want true got false (regression — must default to true)")
	}
}

func TestLoad_RedisBookWriteRespectsEnv(t *testing.T) {
	t.Setenv("AVALANT_REDIS_BOOK_WRITE", "false")
	cfg := Load()
	if cfg.RedisBookWriteEnabled {
		t.Errorf("env=false should disable; got enabled=true")
	}
}
