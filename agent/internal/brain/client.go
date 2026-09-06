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
	body, err := json.Marshal(con)
	if err != nil {
		return nil, fmt.Errorf("encoding contract %s: %w", con.IncidentID, err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, c.baseURL+"/diagnose", bytes.NewReader(body))
	if err != nil {
		return nil, fmt.Errorf("building diagnose request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")

	resp, err := c.http.Do(req)
	if err != nil {
		return nil, fmt.Errorf("posting contract %s to brain: %w", con.IncidentID, err)
	}
	defer func() { _ = resp.Body.Close() }()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, fmt.Errorf("reading brain response for %s: %w", con.IncidentID, err)
	}
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
