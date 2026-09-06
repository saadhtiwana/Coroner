package remediate

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/kubernetes"
)

// ErrNotExecutable is returned when a manual plan is handed to Execute.
var ErrNotExecutable = errors.New("plan is not executable")

// ErrExecutionDisabled is returned when execution has not been enabled.
// It is a distinct error so the reason nothing ran is never ambiguous.
var ErrExecutionDisabled = errors.New("execution is disabled; the plan was emitted and not applied")

// Executor applies plans. Enabled defaults to false and must be set on
// purpose: docs/DESIGN.md section 6.5 keeps write verbs out of RBAC until
// execution is deliberately turned on, and this flag is the code half of
// that guarantee.
type Executor struct {
	Client  kubernetes.Interface
	Enabled bool
}

// Result records what was applied.
type Result struct {
	IncidentID string `json:"incident_id"`
	Kind       Kind   `json:"kind"`
	Target     Target `json:"target"`
	Patch      string `json:"patch"`
	Applied    bool   `json:"applied"`
}

// Execute applies a plan with a strategic merge patch on the owning
// workload's pod template. Only the one container and the one field the
// plan names are touched; the controller rolls the change out.
func (e Executor) Execute(ctx context.Context, p Plan) (Result, error) {
	res := Result{IncidentID: p.IncidentID, Kind: p.Kind, Target: p.Target}
	if !p.Executable {
		return res, fmt.Errorf("%w: %s", ErrNotExecutable, p.Reason)
	}
	patch, err := buildPatch(p)
	if err != nil {
		return res, err
	}
	res.Patch = string(patch)
	if !e.Enabled {
		return res, ErrExecutionDisabled
	}
	if err := e.apply(ctx, p.Target, patch); err != nil {
		return res, err
	}
	res.Applied = true
	return res, nil
}

func buildPatch(p Plan) ([]byte, error) {
	container := map[string]any{"name": p.Target.Container}
	switch p.Kind {
	case KindSetMemory:
		limits := map[string]any{"memory": p.MemoryLimit}
		resources := map[string]any{"limits": limits}
		if p.MemoryRequest != "" {
			resources["requests"] = map[string]any{"memory": p.MemoryRequest}
		}
		container["resources"] = resources
	case KindSetImage:
		container["image"] = p.Image
	default:
		return nil, fmt.Errorf("%w: kind %q", ErrNotExecutable, p.Kind)
	}
	patch := map[string]any{
		"spec": map[string]any{
			"template": map[string]any{
				"spec": map[string]any{
					"containers": []any{container},
				},
			},
		},
	}
	b, err := json.Marshal(patch)
	if err != nil {
		return nil, fmt.Errorf("encoding patch: %w", err)
	}
	return b, nil
}

func (e Executor) apply(ctx context.Context, t Target, patch []byte) error {
	var err error
	switch t.Kind {
	case "Deployment":
		_, err = e.Client.AppsV1().Deployments(t.Namespace).Patch(ctx, t.Name, types.StrategicMergePatchType, patch, metav1.PatchOptions{FieldManager: "coroner-agent"})
	case "StatefulSet":
		_, err = e.Client.AppsV1().StatefulSets(t.Namespace).Patch(ctx, t.Name, types.StrategicMergePatchType, patch, metav1.PatchOptions{FieldManager: "coroner-agent"})
	case "DaemonSet":
		_, err = e.Client.AppsV1().DaemonSets(t.Namespace).Patch(ctx, t.Name, types.StrategicMergePatchType, patch, metav1.PatchOptions{FieldManager: "coroner-agent"})
	default:
		return fmt.Errorf("%w: cannot patch a %s", ErrNotExecutable, t.Kind)
	}
	if err != nil {
		return fmt.Errorf("patching %s %s/%s: %w", t.Kind, t.Namespace, t.Name, err)
	}
	return nil
}
