# The agent image. Static, no shell, no package manager: it holds the only
# cluster credentials in the system, so there is nothing in it to borrow.
FROM golang:1.27 AS build
WORKDIR /src

# Dependencies first, so a code change does not re-download the module cache.
COPY go.mod go.sum ./
RUN go mod download

COPY . .
ARG VERSION=dev
RUN CGO_ENABLED=0 go build -trimpath \
      -ldflags="-s -w -X main.version=${VERSION}" \
      -o /out/coroner-agent ./cmd/coroner-agent

FROM gcr.io/distroless/static-debian12:nonroot
COPY --from=build /out/coroner-agent /coroner-agent
USER nonroot:nonroot
ENTRYPOINT ["/coroner-agent"]
