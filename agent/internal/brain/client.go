// Package brain is the agent's HTTP client for the reasoning service.
//
// The agent sends an evidence contract and receives a verdict. It does not
// send anything else and it does not receive cluster credentials in return:
// the brain never holds any. This is the only network path between the two
// services in the collection direction.
package brain

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/trace"

	"github.com/saadhtiwana/coroner/agent/internal/contract"
)

// DefaultTimeout bounds one diagnosis round trip. The brain's own model call
// is bounded at 90 seconds with client retries on top, so this is generous
// rather than tight; a contract that cannot be diagnosed in this time is
// logged and dropped rather than blocking the watch loop.
const DefaultTimeout = 5 * time.Minute

// Verdict is the brain's response to a contract. It mirrors the fields of the
// brain's DiagnoseResponse that the agent acts on or logs; the brain may send
// more, and unknown fields are ignored so the two services can evolve
// separately as long as these names hold.
type Verdict struct {
	IncidentID    string `json:"incident_id"`
	FailureType   string `json:"failure_type"`
	Outcome       string `json:"outcome"`
	EvidenceClass string `json:"evidence_class"`
	ContextHash   string `json:"context_hash"`

	RootCause           string `json:"root_cause"`
	Explanation         string `json:"explanation"`
	ProposedAction      string `json:"proposed_action"`
	CompetingHypothesis string `json:"competing_hypothesis"`

	ConfidenceModel   *float64 `json:"confidence_model"`
	ConfidenceFinal   *float64 `json:"confidence_final"`
	ConfidenceCeiling *float64 `json:"confidence_ceiling"`

	Abstained     bool   `json:"abstained"`
	AbstainReason string `json:"abstain_reason"`
	Approvable    bool   `json:"approvable"`
}

// Client posts contracts to the brain.
type Client struct {
	baseURL string
	http    *http.Client
}

// New returns a client for the brain at baseURL. A zero timeout uses
// DefaultTimeout.
func New(baseURL string, timeout time.Duration) (*Client, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Scheme == "" || u.Host == "" {
		return nil, fmt.Errorf("brain url %q is not an absolute http(s) url", baseURL)
	}
	if u.Scheme != "http" && u.Scheme != "https" {
		return nil, fmt.Errorf("brain url %q must use http or https", baseURL)
	}
	if timeout <= 0 {
		timeout = DefaultTimeout
	}
	return &Client{
		baseURL: strings.TrimRight(baseURL, "/"),
		http:    &http.Client{Timeout: timeout},
	}, nil
}

// Diagnose sends one contract and returns the verdict.
//
// A non-2xx status is an error carrying the brain's body, because the body is
// where a validation failure or a missing credential is explained. The
// contract is sent exactly as it would be written to stdout, so what the brain
// receives is what the agent would have shown.
func (c *Client) Diagnose(ctx context.Context, con *contract.Contract) (*Verdict, error) {
	// The client span carries the trace context to the brain, so the brain's
	// spans hang under this one and a single trace runs from collection to
	// sink. The verdict's facts are added when it arrives.
	ctx, span := otel.Tracer("coroner-agent").Start(ctx, "brain.diagnose",
		trace.WithSpanKind(trace.SpanKindClient),
		trace.WithAttributes(
			attribute.String("incident_id", con.IncidentID),
			attribute.String("failure_type", con.FailureType),
			attribute.Int("contract_bytes", 0),
		),
	)
	defer span.End()

	v, err := c.diagnose(ctx, con, span)
	if err != nil {
		span.RecordError(err)
		return nil, err
	}
	span.SetAttributes(
		attribute.String("outcome", v.Outcome),
		attribute.String("evidence_class", v.EvidenceClass),
		attribute.Bool("approvable", v.Approvable),
	)
	if v.ConfidenceFinal != nil {
		span.SetAttributes(attribute.Float64("confidence_final", *v.ConfidenceFinal))
	}
	return v, nil
}

func (c *Client) diagnose(ctx context.Context, con *contract.Contract, span trace.Span) (*Verdict, error) {
	body, err := json.Marshal(con)
	if err != nil {
		return nil, fmt.Errorf("encoding contract %s: %w", con.IncidentID, err)
	}
	span.SetAttributes(attribute.Int("contract_bytes", len(body)))

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/diagnose", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("building diagnose request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("posting contract %s to brain: %w", con.IncidentID, err)
	}
	defer func() { _ = resp.Body.Close() }()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("reading brain response for %s: %w", con.IncidentID, err)
	}
	span.SetAttributes(attribute.Int("http.status_code", resp.StatusCode))
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return nil, fmt.Errorf("brain returned %d for %s: %s", resp.StatusCode, con.IncidentID, truncate(string(raw), 500))
	}

	var v Verdict
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil, fmt.Errorf("decoding brain response for %s: %w", con.IncidentID, err)
	}
	if v.IncidentID != con.IncidentID {
		return nil, fmt.Errorf("brain answered for incident %q, sent %q", v.IncidentID, con.IncidentID)
	}
	return &v, nil
}

// Health reports whether the brain answers and whether it holds a model
// credential, so a misconfiguration is visible at startup rather than on the
// first incident.
func (c *Client) Health(ctx context.Context) (Health, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, c.baseURL+"/healthz", nil)
	if err != nil {
		return Health{}, fmt.Errorf("building health request: %w", err)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return Health{}, fmt.Errorf("reaching brain at %s: %w", c.baseURL, err)
	}
	defer func() { _ = resp.Body.Close() }()
	if resp.StatusCode != http.StatusOK {
		return Health{}, fmt.Errorf("brain health returned %d", resp.StatusCode)
	}
	var h Health
	if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<16)).Decode(&h); err != nil {
		return Health{}, fmt.Errorf("decoding brain health: %w", err)
	}
	return h, nil
}

// Health mirrors the brain's /healthz body.
type Health struct {
	Status             string `json:"status"`
	Version            string `json:"version"`
	ContractVersion    string `json:"contract_version"`
	Model              string `json:"model"`
	CredentialsPresent bool   `json:"credentials_present"`
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}

// ApprovedIncident is a ledger row whose decision authorises an action and
// which the agent has not yet acted on. The fields are the ones the agent
// verifies the token against and plans from.
type ApprovedIncident struct {
	IncidentID     string `json:"incident_id"`
	FailureType    string `json:"failure_type"`
	ContextHash    string `json:"context_hash"`
	Decision       string `json:"decision"`
	DecisionAction string `json:"decision_action"`
	DecisionAt     string `json:"decision_at"`
	ApprovalToken  string `json:"approval_token"`
	ContractJSON   string `json:"contract_json"`
}

// Approved lists incidents awaiting execution. With execute false the brain
// also withholds rows already recorded as proposed, so a proposal is emitted
// once rather than on every poll.
func (c *Client) Approved(ctx context.Context, execute bool) ([]ApprovedIncident, error) {
	u := c.baseURL + "/incidents/approved"
	if execute {
		u += "?execute=true"
	}
	var out []ApprovedIncident
	if err := c.getJSON(ctx, u, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// ExecutionReport is what the agent tells the brain it did with an approval.
// status is one of proposed, refused, executed, failed.
type ExecutionReport struct {
	Status string `json:"status"`
	Detail string `json:"detail"`
	Plan   any    `json:"plan,omitempty"`
}

// ReportExecution records the execution outcome on the ledger row.
func (c *Client) ReportExecution(ctx context.Context, incidentID string, report ExecutionReport) error {
	return c.postJSON(ctx, c.baseURL+"/incidents/"+incidentID+"/execution", report)
}

// ResolutionReport is the section 5.2 label after an executed action.
type ResolutionReport struct {
	ReadyWithinSLA bool   `json:"ready_within_sla"`
	StayedReady    bool   `json:"stayed_ready"`
	Resolved       bool   `json:"resolved"`
	Detail         string `json:"detail"`
}

// ReportResolution records whether the workload recovered and stayed up.
func (c *Client) ReportResolution(ctx context.Context, incidentID string, report ResolutionReport) error {
	return c.postJSON(ctx, c.baseURL+"/incidents/"+incidentID+"/resolution", report)
}

func (c *Client) getJSON(ctx context.Context, u string, into any) error {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u, nil)
	if err != nil {
		return fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Accept", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("reaching brain: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return fmt.Errorf("reading brain response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("brain returned %d: %s", resp.StatusCode, truncate(string(raw), 300))
	}
	if err := json.Unmarshal(raw, into); err != nil {
		return fmt.Errorf("decoding brain response: %w", err)
	}
	return nil
}

func (c *Client) postJSON(ctx context.Context, u string, body any) error {
	b, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("encoding request: %w", err)
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, u, bytes.NewReader(b))
	if err != nil {
		return fmt.Errorf("building request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("reaching brain: %w", err)
	}
	defer func() { _ = resp.Body.Close() }()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<16))
	if err != nil {
		return fmt.Errorf("reading brain response: %w", err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return fmt.Errorf("brain returned %d: %s", resp.StatusCode, truncate(string(raw), 300))
	}
	return nil
}
