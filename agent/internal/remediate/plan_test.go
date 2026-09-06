package remediate

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/saadhtiwana/coroner/agent/internal/contract"
)

func incident(t *testing.T, failure, decision, action string, mutate func(c *contract.Contract)) Incident {
	t.Helper()
	c := contract.Contract{
		ContractVersion: contract.Version,
		IncidentID:      "inc-1",
		FailureType:     failure,
		Pod:             contract.Pod{Namespace: "shop", Name: "worker-7d9-abc", UID: "u1"},
		Owner:           &contract.Owner{Kind: "Deployment", Name: "worker", Image: "redis:alpine"},
		Container:       contract.Container{Name: "hog", Image: "docker.io/library/redis:alpine", MemoryLimit: "128Mi", MemoryRequest: "128Mi"},
	}
	if mutate != nil {
		mutate(&c)
	}
	raw, err := json.Marshal(c)
	if err != nil {
		t.Fatal(err)
	}
	return Incident{IncidentID: "inc-1", FailureType: failure, Decision: decision, DecisionAction: action, ContractJSON: string(raw)}
}

func TestMemoryPlanTakesTheQuantityFromTheAction(t *testing.T) {
	p, err := Build(incident(t, "OOMKilled", "edited", "Raise the memory limit of the hog container to 512Mi and redeploy.", nil))
	if err != nil {
		t.Fatal(err)
	}
	if p.Kind != KindSetMemory || !p.Executable {
		t.Fatalf("plan = %+v", p)
	}
	if p.MemoryLimit != "512Mi" || p.MemoryRequest != "512Mi" || p.Previous != "128Mi" {
		t.Errorf("limit %s request %s previous %s", p.MemoryLimit, p.MemoryRequest, p.Previous)
	}
	if p.Target != (Target{Kind: "Deployment", Namespace: "shop", Name: "worker", Container: "hog"}) {
		t.Errorf("target = %+v", p.Target)
	}
	if !strings.Contains(p.Summary(), "128Mi to 512Mi") {
		t.Errorf("summary = %s", p.Summary())
	}
}

func TestMemoryPlanDoublesWhenTheActionNamesNothingUsable(t *testing.T) {
	cases := []struct {
		name, action, failure, want string
	}{
		{"no quantity", "Increase the memory limit and investigate the index build.", "OOMKilled", "256Mi"},
		{"quantity not above current", "Set the limit to 64Mi.", "OOMKilled", "256Mi"},
		{"init kill gets four times with a floor", "Raise the limit so init can start.", "OOMKilledDuringInit", "64Mi"},
		{"loose spelling", "bump it to 1 GiB", "OOMKilled", "1Gi"},
		{"prose with a percentage", "raise by 50% to around 2 gi", "OOMKilled", "2Gi"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			mutate := func(c *contract.Contract) {}
			if tc.failure == "OOMKilledDuringInit" {
				mutate = func(c *contract.Contract) { c.Container.MemoryLimit = "2Mi"; c.Container.MemoryRequest = "2Mi" }
			}
			p, err := Build(incident(t, tc.failure, "approved", tc.action, mutate))
			if err != nil {
				t.Fatal(err)
			}
			if p.MemoryLimit != tc.want {
				t.Errorf("limit = %s, want %s (plan %+v)", p.MemoryLimit, tc.want, p)
			}
		})
	}
}

func TestMemoryPlanLeavesALowerRequestAlone(t *testing.T) {
	p, _ := Build(incident(t, "OOMKilled", "approved", "raise to 512Mi", func(c *contract.Contract) { c.Container.MemoryRequest = "64Mi" }))
	if p.MemoryRequest != "" {
		t.Errorf("a request below the new limit should not be touched, got %s", p.MemoryRequest)
	}
}

func TestBarePodAndUnknownOwnersAreManual(t *testing.T) {
	bare, _ := Build(incident(t, "OOMKilled", "approved", "raise to 512Mi", func(c *contract.Contract) { c.Owner = nil }))
	if bare.Kind != KindManual || bare.Executable {
		t.Errorf("bare pod plan = %+v", bare)
	}
	if !strings.Contains(bare.Reason, "immutable") {
		t.Errorf("reason should explain immutability: %s", bare.Reason)
	}
	job, _ := Build(incident(t, "OOMKilled", "approved", "raise to 512Mi", func(c *contract.Contract) { c.Owner.Kind = "Job" }))
	if job.Kind != KindManual || job.Executable {
		t.Errorf("job plan = %+v", job)
	}
}

func TestImagePlanNeedsANamedReference(t *testing.T) {
	vague, _ := Build(incident(t, "ImagePullBackOff", "approved", "Correct the image tag or attach an imagePullSecret so the node can authenticate.", func(c *contract.Contract) {
		c.Container.Image = "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0"
	}))
	if vague.Kind != KindManual || vague.Executable {
		t.Errorf("vague action must not execute: %+v", vague)
	}
	if !strings.Contains(vague.Reason, "names no image reference") {
		t.Errorf("reason = %s", vague.Reason)
	}

	named, _ := Build(incident(t, "ImagePullBackOff", "edited", "Use ghcr.io/saadhtiwana/coroner-agent:v0.1.0 instead.", func(c *contract.Contract) {
		c.Container.Image = "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0"
	}))
	if named.Kind != KindSetImage || !named.Executable || named.Image != "ghcr.io/saadhtiwana/coroner-agent:v0.1.0" {
		t.Errorf("named plan = %+v", named)
	}

	same, _ := Build(incident(t, "ImagePullBackOff", "approved", "Check that ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0 exists.", func(c *contract.Contract) {
		c.Container.Image = "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0"
	}))
	if same.Executable {
		t.Errorf("naming the failing image again is not a new image: %+v", same)
	}
	bareWord, _ := Build(incident(t, "ImagePullBackOff", "approved", "Try redis instead.", nil))
	if bareWord.Executable {
		t.Errorf("a bare word is not an image reference: %+v", bareWord)
	}
}

func TestCrashLoopIsAlwaysManual(t *testing.T) {
	p, _ := Build(incident(t, "CrashLoopBackOff", "approved", "Fix DATABASE_URL and redeploy the orders-api Deployment.", nil))
	if p.Kind != KindManual || p.Executable {
		t.Errorf("crashloop plan = %+v", p)
	}
}

func TestUnreadableContractIsAnError(t *testing.T) {
	if _, err := Build(Incident{IncidentID: "inc-x", ContractJSON: "not json"}); err == nil {
		t.Fatal("expected an error")
	}
}
