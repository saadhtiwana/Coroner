package tracing

import (
	"bytes"
	"context"
	"strings"
	"testing"

	"go.opentelemetry.io/otel/attribute"
)

func TestConsoleExporterWritesTheSpanAndItsAttributes(t *testing.T) {
	var buf bytes.Buffer
	shutdown, mode, err := Setup(context.Background(), "console", "test", &buf)
	if err != nil {
		t.Fatalf("Setup() error: %v", err)
	}
	if mode != "console" {
		t.Errorf("mode = %q, want console", mode)
	}

	ctx, span := Start(context.Background(), "agent.incident",
		attribute.String("incident_id", "inc-abc"),
		attribute.String("classify.rule", "terminated-reason-is-oomkilled"),
	)
	span.SetAttributes(attribute.Bool("logs_available", true))
	_, child := Start(ctx, "agent.collect")
	child.End()
	span.End()

	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("shutdown: %v", err)
	}
	out := buf.String()
	for _, want := range []string{"agent.incident", "agent.collect", "inc-abc", "terminated-reason-is-oomkilled", "logs_available", Service} {
		if !strings.Contains(out, want) {
			t.Errorf("exported spans do not mention %q\ngot:\n%s", want, out)
		}
	}
}

func TestOffProducesNothing(t *testing.T) {
	var buf bytes.Buffer
	shutdown, mode, err := Setup(context.Background(), "off", "test", &buf)
	if err != nil {
		t.Fatal(err)
	}
	if mode != "off" {
		t.Errorf("mode = %q, want off", mode)
	}
	_, span := Start(context.Background(), "agent.incident")
	span.End()
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if buf.Len() != 0 {
		t.Errorf("off wrote %d bytes", buf.Len())
	}
}

func TestUnknownModeIsRefused(t *testing.T) {
	if _, _, err := Setup(context.Background(), "jaeger", "test", nil); err == nil {
		t.Fatal("an unknown mode must be refused rather than silently ignored")
	}
}

func TestFailMarksTheSpan(t *testing.T) {
	var buf bytes.Buffer
	shutdown, _, err := Setup(context.Background(), "console", "test", &buf)
	if err != nil {
		t.Fatal(err)
	}
	_, span := Start(context.Background(), "agent.collect")
	Fail(span, context.DeadlineExceeded)
	Fail(span, nil)
	span.End()
	if err := shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "deadline exceeded") {
		t.Errorf("the error should be recorded on the span, got:\n%s", buf.String())
	}
}
