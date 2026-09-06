package brain

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/saadhtiwana/coroner/agent/internal/contract"
)

func sample() *contract.Contract {
	return &contract.Contract{
		ContractVersion: contract.Version,
		IncidentID:      "inc-abc",
		CollectedAt:     time.Date(2026, 9, 6, 13, 0, 0, 0, time.UTC),
		FailureType:     "ImagePullBackOff",
		Pod:             contract.Pod{Namespace: "default", Name: "probe-imagepull", UID: "u1"},
		Container:       contract.Container{Name: "puller", Image: "ghcr.io/x/y:v0", WaitingReason: "ImagePullBackOff"},
		Logs:            contract.Logs{Empty: true},
	}
}

func TestDiagnoseSendsTheContractAndReturnsTheVerdict(t *testing.T) {
	var received contract.Contract
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/diagnose" || r.Method != http.MethodPost {
			t.Errorf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("Content-Type = %q", ct)
		}
		if err := json.NewDecoder(r.Body).Decode(&received); err != nil {
			t.Errorf("decoding posted contract: %v", err)
		}
		final := 0.95
		_ = json.NewEncoder(w).Encode(map[string]any{
			"incident_id":         "inc-abc",
			"failure_type":        "ImagePullBackOff",
			"outcome":             "DIAGNOSED",
			"evidence_class":      "image_pull_with_registry_error",
			"root_cause":          "The registry returned 403.",
			"proposed_action":     "Fix the tag or attach a pull secret.",
			"confidence_final":    final,
			"approvable":          true,
			"a_field_added_later": "ignored",
		})
	}))
	defer srv.Close()

	c, err := New(srv.URL, 0)
	if err != nil {
		t.Fatal(err)
	}
	v, err := c.Diagnose(context.Background(), sample())
	if err != nil {
		t.Fatalf("Diagnose() error: %v", err)
	}
	if received.IncidentID != "inc-abc" || received.Container.Image != "ghcr.io/x/y:v0" {
		t.Errorf("brain received a different contract: %+v", received)
	}
	if v.Outcome != "DIAGNOSED" || !v.Approvable || v.ConfidenceFinal == nil || *v.ConfidenceFinal != 0.95 {
		t.Errorf("verdict = %+v", v)
	}
}

func TestDiagnoseSurfacesTheBrainsErrorBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = w.Write([]byte(`{"detail":"no model credentials configured"}`))
	}))
	defer srv.Close()

	c, _ := New(srv.URL, 0)
	_, err := c.Diagnose(context.Background(), sample())
	if err == nil {
		t.Fatal("expected an error for a 503")
	}
	if !strings.Contains(err.Error(), "503") || !strings.Contains(err.Error(), "no model credentials") {
		t.Errorf("error should carry status and body, got: %v", err)
	}
}

func TestDiagnoseRejectsAnAnswerForAnotherIncident(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"incident_id":"inc-other","outcome":"DIAGNOSED"}`))
	}))
	defer srv.Close()

	c, _ := New(srv.URL, 0)
	if _, err := c.Diagnose(context.Background(), sample()); err == nil {
		t.Fatal("a verdict for a different incident must not be accepted")
	}
}

func TestDiagnoseRejectsMalformedJSON(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`not json`))
	}))
	defer srv.Close()

	c, _ := New(srv.URL, 0)
	if _, err := c.Diagnose(context.Background(), sample()); err == nil {
		t.Fatal("expected a decode error")
	}
}

func TestNewRejectsBadURLs(t *testing.T) {
	for _, bad := range []string{"", "localhost:8000", "ftp://x", "://"} {
		if _, err := New(bad, 0); err == nil {
			t.Errorf("New(%q) should fail", bad)
		}
	}
	if _, err := New("http://localhost:8000/", 0); err != nil {
		t.Errorf("New(valid) failed: %v", err)
	}
}

func TestHealth(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/healthz" {
			t.Errorf("path = %s", r.URL.Path)
		}
		_, _ = w.Write([]byte(`{"status":"ok","version":"0.1.0","contract_version":"1","model":"m","credentials_present":true}`))
	}))
	defer srv.Close()

	c, _ := New(srv.URL, 0)
	h, err := c.Health(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !h.CredentialsPresent || h.ContractVersion != "1" {
		t.Errorf("health = %+v", h)
	}
}

func TestApprovedAndReports(t *testing.T) {
	var seen []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = append(seen, r.Method+" "+r.URL.RequestURI())
		switch {
		case r.URL.Path == "/incidents/approved":
			_, _ = w.Write([]byte(`[{"incident_id":"inc-1","failure_type":"OOMKilled","context_hash":"h","decision":"approved","decision_action":"raise","decision_at":"t","approval_token":"v1.x","contract_json":"{}"}]`))
		case strings.HasSuffix(r.URL.Path, "/execution"), strings.HasSuffix(r.URL.Path, "/resolution"):
			var body map[string]any
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("bad body: %v", err)
			}
			if body["detail"] == nil {
				t.Errorf("report without detail: %v", body)
			}
			w.WriteHeader(http.StatusOK)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	c, _ := New(srv.URL, 0)
	rows, err := c.Approved(context.Background(), true)
	if err != nil || len(rows) != 1 || rows[0].ApprovalToken != "v1.x" {
		t.Fatalf("Approved() = %+v, %v", rows, err)
	}
	if err := c.ReportExecution(context.Background(), "inc-1", ExecutionReport{Status: "executed", Detail: "patched"}); err != nil {
		t.Fatal(err)
	}
	if err := c.ReportResolution(context.Background(), "inc-1", ResolutionReport{Resolved: true, Detail: "ready"}); err != nil {
		t.Fatal(err)
	}
	if seen[0] != "GET /incidents/approved?execute=true" {
		t.Errorf("first request = %s", seen[0])
	}
	if _, err := c.Approved(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if seen[len(seen)-1] != "GET /incidents/approved" {
		t.Errorf("last request = %s", seen[len(seen)-1])
	}
}
