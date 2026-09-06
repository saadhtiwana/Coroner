// Package remediate turns an approved diagnosis into a concrete action, and
// executes it only when execution has been deliberately enabled.
//
// The brain proposes in prose; a human approves or edits that prose. Neither
// is executable. This package derives the one concrete change the prose
// asks for, states it precisely, and says plainly when it cannot. A plan
// that is not executable is still emitted, because "Coroner would have
// changed X to Y" is what a proposal path is for, and because saying why
// nothing will run is more useful than running something else.
//
// Every rule here is deliberately narrow. The agent patches a memory limit
// or an image on the owning workload and nothing else. It never deletes,
// never restarts blindly, and never touches a bare pod, whose resources and
// image are immutable.
package remediate

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	"k8s.io/apimachinery/pkg/api/resource"

	"github.com/saadhtiwana/coroner/agent/internal/contract"
)

// Kind is the concrete change a plan makes.
type Kind string

const (
	// KindSetMemory raises the memory limit, and the request if it would
	// otherwise exceed the limit, on one container of the owning workload.
	KindSetMemory Kind = "set-memory"
	// KindSetImage changes one container's image on the owning workload.
	KindSetImage Kind = "set-image"
	// KindManual means the approved action is not one the agent will
	// perform. The plan says why.
	KindManual Kind = "manual"
)

// Target is the object a plan patches.
type Target struct {
	Kind      string `json:"kind"`
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	Container string `json:"container"`
}

// Plan is the concrete action. It is emitted whether or not it executes.
type Plan struct {
	IncidentID  string `json:"incident_id"`
	FailureType string `json:"failure_type"`
	Decision    string `json:"decision"`
	Action      string `json:"action"`

	Kind       Kind   `json:"kind"`
	Executable bool   `json:"executable"`
	Reason     string `json:"reason"`
	Target     Target `json:"target"`

	MemoryLimit   string `json:"memory_limit,omitempty"`
	MemoryRequest string `json:"memory_request,omitempty"`
	Image         string `json:"image,omitempty"`
	Previous      string `json:"previous,omitempty"`
}

// Summary is one line for a log or a terminal.
func (p Plan) Summary() string {
	t := p.Target
	switch p.Kind {
	case KindSetMemory:
		return fmt.Sprintf("set memory limit of %s/%s container %s from %s to %s", t.Kind, t.Name, t.Container, p.Previous, p.MemoryLimit)
	case KindSetImage:
		return fmt.Sprintf("set image of %s/%s container %s from %s to %s", t.Kind, t.Name, t.Container, p.Previous, p.Image)
	default:
		return "no action will be taken: " + p.Reason
	}
}

// Incident is what the agent reads from the brain for an approved row.
type Incident struct {
	IncidentID     string `json:"incident_id"`
	FailureType    string `json:"failure_type"`
	Decision       string `json:"decision"`
	DecisionAction string `json:"decision_action"`
	ContractJSON   string `json:"contract_json"`
}

// patchable are the workload kinds whose pod template the agent will patch.
var patchable = map[string]bool{"Deployment": true, "StatefulSet": true, "DaemonSet": true}

// Build derives the plan for an approved incident.
func Build(inc Incident) (Plan, error) {
	var c contract.Contract
	if err := json.Unmarshal([]byte(inc.ContractJSON), &c); err != nil {
		return Plan{}, fmt.Errorf("incident %s carries no readable contract: %w", inc.IncidentID, err)
	}

	p := Plan{
		IncidentID:  inc.IncidentID,
		FailureType: inc.FailureType,
		Decision:    inc.Decision,
		Action:      inc.DecisionAction,
		Kind:        KindManual,
		Target:      Target{Namespace: c.Pod.Namespace, Name: c.Pod.Name, Kind: "Pod", Container: c.Container.Name},
	}

	if c.Owner == nil {
		p.Reason = "the pod has no controller; a bare pod's resources and image are immutable, so the pod must be recreated by hand"
		return p, nil
	}
	if !patchable[c.Owner.Kind] {
		p.Reason = fmt.Sprintf("the owning %s is not a workload the agent patches", c.Owner.Kind)
		return p, nil
	}
	p.Target = Target{Kind: c.Owner.Kind, Namespace: c.Pod.Namespace, Name: c.Owner.Name, Container: c.Container.Name}

	switch inc.FailureType {
	case "OOMKilled", "OOMKilledDuringInit":
		return planMemory(p, &c, inc.FailureType == "OOMKilledDuringInit"), nil
	case "ImagePullBackOff":
		return planImage(p, &c), nil
	default:
		p.Reason = "the cause is inside the application; the agent does not restart or patch a workload whose fix it cannot state"
		return p, nil
	}
}

// Floors for a raised limit when the action text names no quantity. An
// init kill needs enough for the runtime to start; a runtime kill needs
// enough headroom to tell a low limit from a leak next time.
var (
	floorInit    = resource.MustParse("64Mi")
	floorRuntime = resource.MustParse("128Mi")
)

func planMemory(p Plan, c *contract.Contract, duringInit bool) Plan {
	current, err := resource.ParseQuantity(c.Container.MemoryLimit)
	if err != nil || current.IsZero() {
		p.Reason = "the container has no memory limit to raise; the kill came from the node, not from a limit"
		return p
	}

	target := quantityFromText(p.Action)
	if target == nil || target.Cmp(current) <= 0 {
		doubled := current.DeepCopy()
		doubled.Add(current)
		if duringInit {
			doubled.Add(current)
			doubled.Add(current)
		}
		floor := floorRuntime
		if duringInit {
			floor = floorInit
		}
		if doubled.Cmp(floor) < 0 {
			doubled = floor
		}
		target = &doubled
	}

	p.Kind = KindSetMemory
	p.Executable = true
	p.Previous = current.String()
	p.MemoryLimit = target.String()
	if req, err := resource.ParseQuantity(c.Container.MemoryRequest); err == nil && !req.IsZero() {
		// A request above the limit is invalid; keep them equal when the
		// request was equal before, otherwise leave the request alone.
		if req.Cmp(current) == 0 || req.Cmp(*target) > 0 {
			p.MemoryRequest = target.String()
		}
	}
	p.Reason = fmt.Sprintf("raise the memory limit from %s to %s; the target is taken from the approved action when it names one", current.String(), target.String())
	return p
}

// quantityRE finds the first Kubernetes memory quantity in prose, such as
// 256Mi, 1Gi, 512M, or 2 GiB.
var quantityRE = regexp.MustCompile(`(?i)\b(\d+(?:\.\d+)?)\s?(Ki|Mi|Gi|Ti|K|M|G|T)i?B?\b`)

func quantityFromText(text string) *resource.Quantity {
	m := quantityRE.FindStringSubmatch(text)
	if m == nil {
		return nil
	}
	unit := m[2]
	// Normalise the loose spellings people type to the strict ones the
	// resource parser accepts: "MiB" and "mi" both mean Mi.
	switch strings.ToLower(unit) {
	case "ki", "mi", "gi", "ti":
		unit = strings.ToUpper(unit[:1]) + "i"
	default:
		unit = strings.ToUpper(unit)
	}
	q, err := resource.ParseQuantity(m[1] + unit)
	if err != nil {
		return nil
	}
	return &q
}

// imageRE finds an image reference with a registry or a tag in prose. A
// bare word is not accepted, so "redis" in a sentence cannot become an
// image; "redis:7.2" or "ghcr.io/org/app:v1" can.
var imageRE = regexp.MustCompile(`(?i)\b((?:[a-z0-9.-]+(?::\d+)?/)?[a-z0-9._/-]+(?::[a-z0-9._-]+|@sha256:[a-f0-9]{64}))`)

func planImage(p Plan, c *contract.Contract) Plan {
	current := c.Container.Image
	for _, m := range imageRE.FindAllStringSubmatch(p.Action, -1) {
		candidate := strings.Trim(m[1], "`'\".,")
		if candidate == "" || candidate == current || strings.HasSuffix(current, candidate) {
			continue
		}
		if !strings.Contains(candidate, "/") && !strings.Contains(candidate, ":") {
			continue
		}
		p.Kind = KindSetImage
		p.Executable = true
		p.Previous = current
		p.Image = candidate
		p.Reason = "replace the image with the reference named in the approved action"
		return p
	}
	p.Reason = "the approved action names no image reference other than the failing one; edit the action to name the image to use, or attach a pull secret by hand"
	return p
}
