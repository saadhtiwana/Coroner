// Package tracing configures OpenTelemetry for the agent.
//
// A trace should explain a decision, not just time it. The agent's spans
// carry what it observed and decided: the classification rule that fired,
// whether logs were available, the incident id, and, from the brain's
// answer, the evidence class and final confidence. The trace context is
// propagated on the HTTP call so the brain's spans hang under the agent's
// and one trace covers collection to sink.
//
// The console exporter is the default so tracing works with no backend;
// spans go to stderr so stdout still carries only contracts. Set
// CORONER_TRACING=otlp and OTEL_EXPORTER_OTLP_ENDPOINT to ship them, or
// CORONER_TRACING=off to disable.
package tracing

import (
	"context"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/exporters/stdout/stdouttrace"
	"go.opentelemetry.io/otel/propagation"
	sdkresource "go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
	"go.opentelemetry.io/otel/trace"
)

// Service is the service.name every span carries.
const Service = "coroner-agent"

// Setup installs the tracer provider for the given mode and returns a
// shutdown function. mode is console, otlp, or off; empty reads
// CORONER_TRACING and defaults to console.
func Setup(ctx context.Context, mode, version string, out io.Writer) (func(context.Context) error, string, error) {
	if mode == "" {
		mode = os.Getenv("CORONER_TRACING")
	}
	mode = strings.ToLower(strings.TrimSpace(mode))
	if mode == "" {
		mode = "console"
	}
	if mode == "off" {
		otel.SetTextMapPropagator(propagation.TraceContext{})
		return func(context.Context) error { return nil }, mode, nil
	}

	res, err := sdkresource.New(ctx,
		sdkresource.WithAttributes(semconv.ServiceName(Service), semconv.ServiceVersion(version)),
	)
	if err != nil {
		return nil, mode, fmt.Errorf("building trace resource: %w", err)
	}

	var exporter sdktrace.SpanExporter
	switch mode {
	case "otlp":
		exporter, err = otlptracehttp.New(ctx)
		if err != nil {
			return nil, mode, fmt.Errorf("configuring otlp exporter: %w", err)
		}
	case "console":
		if out == nil {
			out = os.Stderr
		}
		exporter, err = stdouttrace.New(stdouttrace.WithWriter(out), stdouttrace.WithoutTimestamps())
		if err != nil {
			return nil, mode, fmt.Errorf("configuring console exporter: %w", err)
		}
	default:
		return nil, mode, fmt.Errorf("unknown CORONER_TRACING mode %q; expected console, otlp, or off", mode)
	}

	provider := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(exporter, sdktrace.WithBatchTimeout(time.Second)),
		sdktrace.WithResource(res),
	)
	otel.SetTracerProvider(provider)
	otel.SetTextMapPropagator(propagation.TraceContext{})
	return provider.Shutdown, mode, nil
}

// Tracer returns the agent's tracer.
func Tracer() trace.Tracer {
	return otel.Tracer(Service)
}

// Start opens a span with attributes.
func Start(ctx context.Context, name string, attrs ...attribute.KeyValue) (context.Context, trace.Span) {
	return Tracer().Start(ctx, name, trace.WithAttributes(attrs...))
}

// Fail records an error on the span and marks it failed.
func Fail(span trace.Span, err error) {
	if err == nil {
		return
	}
	span.RecordError(err)
	span.SetStatus(codesError, err.Error())
}

// codesError is the OpenTelemetry error status, aliased so callers need not
// import the codes package for one constant.
var codesError = codes.Error
