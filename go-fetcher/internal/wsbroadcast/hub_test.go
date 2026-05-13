package wsbroadcast

import (
	"testing"
)

func TestNewHub_InitialState(t *testing.T) {
	h := NewHub("test")
	if h.Count() != 0 {
		t.Errorf("new hub count: want 0 got %d", h.Count())
	}
}

func TestHub_RegisterIncrementsCount(t *testing.T) {
	h := NewHub("test")
	c1 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	c2 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	h.register(c1)
	if h.Count() != 1 {
		t.Errorf("after 1 register: want 1 got %d", h.Count())
	}
	h.register(c2)
	if h.Count() != 2 {
		t.Errorf("after 2 registers: want 2 got %d", h.Count())
	}
}

func TestHub_RegisterAssignsMonotonicIDs(t *testing.T) {
	h := NewHub("test")
	c1 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	c2 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	c3 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	h.register(c1)
	h.register(c2)
	h.register(c3)
	if c1.id == c2.id || c2.id == c3.id {
		t.Errorf("ids should be unique: %d %d %d", c1.id, c2.id, c3.id)
	}
	if c2.id != c1.id+1 || c3.id != c2.id+1 {
		t.Errorf("ids should be monotonic: %d %d %d", c1.id, c2.id, c3.id)
	}
}

func TestHub_BroadcastDeliversToAllClients(t *testing.T) {
	h := NewHub("test")
	c1 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	c2 := &client{outbox: make(chan []byte, 8), done: make(chan struct{})}
	h.register(c1)
	h.register(c2)

	msg := []byte(`{"hello":"world"}`)
	h.Broadcast(msg)

	// Both clients should have received the message in their outbox.
	select {
	case got := <-c1.outbox:
		if string(got) != string(msg) {
			t.Errorf("c1 got: %s", got)
		}
	default:
		t.Errorf("c1 outbox empty after broadcast")
	}
	select {
	case got := <-c2.outbox:
		if string(got) != string(msg) {
			t.Errorf("c2 got: %s", got)
		}
	default:
		t.Errorf("c2 outbox empty after broadcast")
	}
}

func TestHub_BroadcastEmptyIsNoOp(t *testing.T) {
	h := NewHub("test")
	// No clients registered — must not panic
	h.Broadcast([]byte(`{}`))
	if h.Count() != 0 {
		t.Errorf("count changed unexpectedly: %d", h.Count())
	}
}

func TestHub_NameAccessible(t *testing.T) {
	h := NewHub("long-short")
	if h.name != "long-short" {
		t.Errorf("name: want long-short got %q", h.name)
	}
}
