package remediate

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"sync"
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"

	"github.com/saadhtiwana/coroner/agent/internal/approval"
	"github.com/saadhtiwana/coroner/agent/internal/brain"
	"github.com/saadhtiwana/coroner/agent/internal/contract"
)

var secret = []byte("shared")

type fakeBrain struct {
	rows        []brain.ApprovedIncident
	executions  map[string][]brain.ExecutionReport
	resolutions map[string][]brain.ResolutionReport
	lastExecute bool
}

func newFakeBrain(rows ...brain.ApprovedIncident) *fakeBrain {
	return &fakeBrain{rows: rows, executions: map[string][]brain.ExecutionReport{}, resolutions: map[string][]brain.ResolutionReport{}}
}

func (f *fakeBrain) Approved(_ context.Context, execute bool) ([]brain.ApprovedIncident, error) {
	f.lastExecute = execute
	return f.rows, nil
}

func (f *fakeBrain) ReportExecution(_ context.Context, id string, r brain.ExecutionReport) error {
	f.executions[id] = append(f.executions[id], r)
	return nil
}

func (f *fakeBrain) ReportResolution(_ context.Context, id string, r brain.ResolutionReport) error {
	f.resolutions[id] = append(f.resolutions[id], r)
	return nil
}

func approvedRow(t *testing.T, signWith []byte) brain.ApprovedIncident {
	t.Helper()
	c := contract.Contract{
		IncidentID: "inc-1", FailureType: "OOMKilled",
		Pod:       contract.Pod{Namespace: "shop", Name: "worker-x", UID: "u"},
		Owner:     &contract.Owner{Kind: "Deployment", Name: "worker"},
		Container: contract.Container{Name: "hog", MemoryLimit: "128Mi", MemoryRequest: "128Mi"},
	}
	raw, _ := json.Marshal(c)
	row := brain.ApprovedIncident{
		IncidentID: "inc-1", FailureType: "OOMKilled", ContextHash: "hash-1",
		Decision: "approved", DecisionAction: "Raise the limit to 512Mi", DecisionAt: "2026-09-06T15:00:00+00:00",
		ContractJSON: string(raw),
	}
	row.ApprovalToken = approval.Sign(signWith, approval.Claims{
		IncidentID: row.IncidentID, ContextHash: row.ContextHash, Decision: row.Decision,
		Action: row.DecisionAction, DecidedAt: row.DecisionAt,
	})
	return row
}

func runner(fb *fakeBrain, enabled bool) (*Runner, *fake.Clientset) {
	client := fake.NewClientset(deployment("128Mi"))
	r := &Runner{
		Brain:    fb,
		Secret:   secret,
		Executor: Executor{Client: client, Enabled: enabled},
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
		Known:    &sync.Map{},
	}
	return r, client
}

func TestProposalPathEmitsThePlanAndAppliesNothing(t *testing.T) {
	fb := newFakeBrain(approvedRow(t, secret))
	r, client := runner(fb, false)
	if err := r.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if fb.lastExecute {
		t.Error("with execution disabled the brain should be asked for unproposed rows only")
	}
	reports := fb.executions["inc-1"]
	if len(reports) != 1 || reports[0].Status != "proposed" {
		t.Fatalf("reports = %+v", reports)
	}
	plan, ok := reports[0].Plan.(Plan)
	if !ok || plan.MemoryLimit != "512Mi" || !plan.Executable {
		t.Errorf("plan = %+v", reports[0].Plan)
	}
	d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
	if got := d.Spec.Template.Spec.Containers[1].Resources.Limits.Memory().String(); got != "128Mi" {
		t.Errorf("the workload was changed on the proposal path: %s", got)
	}
}

func TestExecutionAppliesAndTracks(t *testing.T) {
	fb := newFakeBrain(approvedRow(t, secret))
	r, client := runner(fb, true)
	var tracked []string
	r.Tracking = func(_ context.Context, id string, tg Target) { tracked = append(tracked, id+":"+tg.Kind+"/"+tg.Name) }
	if err := r.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	reports := fb.executions["inc-1"]
	if len(reports) != 1 || reports[0].Status != "executed" {
		t.Fatalf("reports = %+v", reports)
	}
	d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
	if got := d.Spec.Template.Spec.Containers[1].Resources.Limits.Memory().String(); got != "512Mi" {
		t.Errorf("limit = %s, want 512Mi", got)
	}
	if len(tracked) != 1 || tracked[0] != "inc-1:Deployment/worker" {
		t.Errorf("tracking = %v", tracked)
	}
}

func TestABadTokenIsRefusedBeforeAnythingElse(t *testing.T) {
	cases := map[string]func(row *brain.ApprovedIncident){
		"wrong secret":   func(row *brain.ApprovedIncident) { *row = approvedRow(t, []byte("other")) },
		"edited action":  func(row *brain.ApprovedIncident) { row.DecisionAction = "delete everything" },
		"other evidence": func(row *brain.ApprovedIncident) { row.ContextHash = "hash-2" },
		"rejected":       func(row *brain.ApprovedIncident) { row.Decision = "rejected" },
		"no token":       func(row *brain.ApprovedIncident) { row.ApprovalToken = "" },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			row := approvedRow(t, secret)
			mutate(&row)
			fb := newFakeBrain(row)
			r, client := runner(fb, true)
			if err := r.RunOnce(context.Background()); err != nil {
				t.Fatal(err)
			}
			reports := fb.executions["inc-1"]
			if len(reports) != 1 || reports[0].Status != "refused" {
				t.Fatalf("reports = %+v", reports)
			}
			d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
			if got := d.Spec.Template.Spec.Containers[1].Resources.Limits.Memory().String(); got != "128Mi" {
				t.Errorf("the workload was changed on a refused approval: %s", got)
			}
		})
	}
}

func TestAnApprovalForEvidenceThisAgentDidNotSendIsRefused(t *testing.T) {
	row := approvedRow(t, secret)
	fb := newFakeBrain(row)
	r, _ := runner(fb, true)
	r.Remember("inc-1", "hash-this-agent-saw")
	if err := r.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := fb.executions["inc-1"][0]; got.Status != "refused" || got.Detail == "" {
		t.Errorf("report = %+v", got)
	}
}

func TestManualPlansAreReportedAsRefused(t *testing.T) {
	row := approvedRow(t, secret)
	row.FailureType = "CrashLoopBackOff"
	fb := newFakeBrain(row)
	r, _ := runner(fb, true)
	if err := r.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := fb.executions["inc-1"][0]; got.Status != "refused" {
		t.Errorf("report = %+v", got)
	}
}

func TestNoSecretMeansNothingExecutes(t *testing.T) {
	fb := newFakeBrain(approvedRow(t, secret))
	r, _ := runner(fb, true)
	r.Secret = nil
	if err := r.RunOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	if got := fb.executions["inc-1"][0]; got.Status != "refused" {
		t.Errorf("report = %+v", got)
	}
}
