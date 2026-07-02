package okxdex

import (
	"testing"

	"github.com/bytedance/sonic"
)

func TestSignRequest(t *testing.T) {
	cases := []struct {
		name, method, path, body, want string
	}{
		{
			name:   "get no body",
			method: "GET",
			path:   "/api/v6/dex/market/supported/chain",
			want:   "v4mKwAl0/cp+GTSHdhNcEoacLcOWZZEDy/ZBlXxJMzM=",
		},
		{
			name:   "post with body",
			method: "POST",
			path:   "/api/v6/dex/market/price",
			body:   `[{"chainIndex":"1","tokenContractAddress":"0xabc"}]`,
			want:   "lR5hBItYYOc73kuEmVbVyCIUqBVKT6JpWROIF7rV84M=",
		},
	}
	for _, c := range cases {
		got := signRequest("test-secret", "2026-07-02T17:14:12.123Z", c.method, c.path, c.body)
		if got != c.want {
			t.Errorf("%s: sign = %q, want %q", c.name, got, c.want)
		}
	}
}

func TestPriceEndpointShape(t *testing.T) {
	captured := `{
		"code": "0",
		"data": [
			{
				"chainIndex": "1",
				"tokenContractAddress": "0x382bB369d343125BfB2117af9c149795C6C65C50",
				"time": "1716892020000",
				"price": "26.458143090226812"
			},
			{
				"chainIndex": "501",
				"tokenContractAddress": "So11111111111111111111111111111111111111112",
				"time": "1716892020000",
				"price": "148.02"
			}
		],
		"msg": ""
	}`
	var env envelope
	if err := sonic.Unmarshal([]byte(captured), &env); err != nil {
		t.Fatalf("envelope decode: %v", err)
	}
	if env.Code != "0" {
		t.Fatalf("code = %q, want 0", env.Code)
	}
	var rows []priceRow
	if err := sonic.Unmarshal(env.Data, &rows); err != nil {
		t.Fatalf("data decode: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("rows = %d, want 2", len(rows))
	}
	if rows[0].Price != "26.458143090226812" || rows[0].ChainIndex != "1" {
		t.Errorf("row0 = %+v", rows[0])
	}
	if got := PriceKey(rows[0].ChainIndex, rows[0].TokenContractAddress); got != "1:0x382bb369d343125bfb2117af9c149795c6c65c50" {
		t.Errorf("PriceKey = %q", got)
	}
}

func TestNotConfigured(t *testing.T) {
	c := &Client{}
	if err := c.do(t.Context(), "GET", "/api/v6/dex/market/supported/chain", nil, nil); err != ErrNotConfigured {
		t.Fatalf("err = %v, want ErrNotConfigured", err)
	}
}
