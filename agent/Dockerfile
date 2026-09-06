# The agent image. Static, no shell, no package manager: it holds the only
# cluster credentials in the system, so there is nothing in it to borrow.
#
# The build stage stays on the builder's own architecture and cross-compiles,
# rather than being emulated for the target. Emulated, the arm64 build took
# over two minutes of QEMU; cross-compiled it takes seconds, and Go needs
# nothing but GOOS and GOARCH to do it.
FROM --platform=$BUILDPLATFORM golang:1.27 AS build
WORKDIR /src

# Dependencies first, so a code change does not re-download the module cache.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY . .
ARG TARGETOS
ARG TARGETARCH
ARG VERSION=dev
RUN --mount=type=cache,target=/go/pkg/mod \
    --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=${TARGETOS} GOARCH=${TARGETARCH} go build -trimpath \
      -ldflags="-s -w -X main.version=${VERSION}" \
      -o /out/coroner-agent ./cmd/coroner-agent

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/coroner-agent /coroner-agent
USER nonroot:nonroot
ENTRYPOINT ["/coroner-agent"]
