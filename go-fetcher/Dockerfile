# Multi-stage build. Final image is distroless/static — ~15MB vs ~400MB
# for the Python container. No shell, no package manager — minimal attack
# surface for a process that talks to 16 exchange WS endpoints.

FROM golang:1.22-alpine AS build
WORKDIR /src

# Cache layer for deps. CI rebuilds the binary in <30s when deps unchanged.
COPY go.mod go.sum* ./
RUN go mod download

COPY . .

# CGO disabled → fully static binary, no glibc dependency, runs on any
# distroless image.
RUN CGO_ENABLED=0 go build \
    -trimpath \
    -ldflags="-s -w" \
    -o /out/fetcher \
    ./cmd/fetcher

FROM gcr.io/distroless/static-debian12
WORKDIR /
COPY --from=build /out/fetcher /fetcher

# Default cache dir matches Python — bind-mount /tmp/avalant_cache from host.
ENV AVALANT_FETCHER_CACHE_DIR=/tmp/avalant_cache

# Run as root so we can write to the docker-managed volume that was
# created with root ownership. The container has no listening ports
# and no shell — the attack surface is just outbound TCP to the
# 17 exchanges we whitelisted upstream.
ENTRYPOINT ["/fetcher"]
