package remediate

import (
	"context"
	"fmt"
	"time"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
)

// Resolution is the section 5.2 label: after an executed action, did the
// workload reach Ready within the window, and did it stay there. Approval
// measures whether a human found the diagnosis convincing; this measures
// whether it was correct. The gap between them is the number section 4
// exists to keep small.
type Resolution struct {
	IncidentID     string        `json:"incident_id"`
	ReadyWithinSLA bool          `json:"ready_within_sla"`
	StayedReady    bool          `json:"stayed_ready"`
	Resolved       bool          `json:"resolved"`
	ReadyAfter     time.Duration `json:"ready_after"`
	Detail         string        `json:"detail"`
}

// Resolver watches a workload after a change.
type Resolver struct {
	Client kubernetes.Interface

	// ReadyWithin is how long the workload has to reach Ready, and StableFor
	// how long it must then stay Ready. Section 5.2: 10 and 30 minutes.
	ReadyWithin time.Duration
	StableFor   time.Duration
	Poll        time.Duration

	// Injectable for tests.
	Now   func() time.Time
	Sleep func(context.Context, time.Duration) error
}

// NewResolver returns a Resolver with the section 5.2 windows.
func NewResolver(client kubernetes.Interface) *Resolver {
	return &Resolver{
		Client:      client,
		ReadyWithin: 10 * time.Minute,
		StableFor:   30 * time.Minute,
		Poll:        15 * time.Second,
		Now:         time.Now,
		Sleep:       sleepCtx,
	}
}

func sleepCtx(ctx context.Context, d time.Duration) error {
	select {
	case <-ctx.Done():
		return fmt.Errorf("sleep interrupted: %w", ctx.Err())
	case <-time.After(d):
		return nil
	}
}

// Track waits for the workload to become Ready and then to stay Ready.
func (r *Resolver) Track(ctx context.Context, incidentID string, t Target) (Resolution, error) {
	res := Resolution{IncidentID: incidentID}
	start := r.Now()

	// Phase one: reach Ready within the window.
	var readyAt time.Time
	for {
		ready, detail, err := r.ready(ctx, t)
		if err != nil {
			return res, err
		}
		if ready {
			readyAt = r.Now()
			break
		}
		if r.Now().Sub(start) >= r.ReadyWithin {
			res.Detail = fmt.Sprintf("not Ready after %s: %s", r.ReadyWithin, detail)
			return res, nil
		}
		if err := r.Sleep(ctx, r.Poll); err != nil {
			return res, fmt.Errorf("tracking %s: %w", incidentID, err)
		}
	}
	res.ReadyWithinSLA = true
	res.ReadyAfter = readyAt.Sub(start)

	// Phase two: stay Ready for the stability window. Any observation that
	// is not Ready ends it; a workload that flaps has not resolved.
	for r.Now().Sub(readyAt) < r.StableFor {
		if err := r.Sleep(ctx, r.Poll); err != nil {
			return res, fmt.Errorf("tracking %s: %w", incidentID, err)
		}
		ready, detail, err := r.ready(ctx, t)
		if err != nil {
			return res, err
		}
		if !ready {
			res.Detail = fmt.Sprintf("Ready after %s, then not Ready %s later: %s", res.ReadyAfter.Truncate(time.Second), r.Now().Sub(readyAt).Truncate(time.Second), detail)
			return res, nil
		}
	}
	res.StayedReady = true
	res.Resolved = true
	res.Detail = fmt.Sprintf("Ready after %s and stayed Ready for %s", res.ReadyAfter.Truncate(time.Second), r.StableFor)
	return res, nil
}

// ready reports whether every desired replica of the workload is Ready on
// the current generation. Reading the controller's status rather than
// individual pods means a rollout that has not finished is not Ready yet.
func (r *Resolver) ready(ctx context.Context, t Target) (bool, string, error) {
	switch t.Kind {
	case "Deployment":
		d, err := r.Client.AppsV1().Deployments(t.Namespace).Get(ctx, t.Name, metav1.GetOptions{})
		if err != nil {
			return false, "", fmt.Errorf("reading Deployment %s/%s: %w", t.Namespace, t.Name, err)
		}
		want := int32(1)
		if d.Spec.Replicas != nil {
			want = *d.Spec.Replicas
		}
		ok := d.Status.ObservedGeneration >= d.Generation &&
			d.Status.ReadyReplicas >= want &&
			d.Status.UpdatedReplicas >= want &&
			d.Status.UnavailableReplicas == 0
		return ok, fmt.Sprintf("ready %d/%d updated %d generation %d/%d", d.Status.ReadyReplicas, want, d.Status.UpdatedReplicas, d.Status.ObservedGeneration, d.Generation), nil
	case "StatefulSet":
		s, err := r.Client.AppsV1().StatefulSets(t.Namespace).Get(ctx, t.Name, metav1.GetOptions{})
		if err != nil {
			return false, "", fmt.Errorf("reading StatefulSet %s/%s: %w", t.Namespace, t.Name, err)
		}
		want := int32(1)
		if s.Spec.Replicas != nil {
			want = *s.Spec.Replicas
		}
		ok := s.Status.ObservedGeneration >= s.Generation && s.Status.ReadyReplicas >= want && s.Status.UpdatedReplicas >= want
		return ok, fmt.Sprintf("ready %d/%d", s.Status.ReadyReplicas, want), nil
	case "DaemonSet":
		d, err := r.Client.AppsV1().DaemonSets(t.Namespace).Get(ctx, t.Name, metav1.GetOptions{})
		if err != nil {
			return false, "", fmt.Errorf("reading DaemonSet %s/%s: %w", t.Namespace, t.Name, err)
		}
		ok := d.Status.ObservedGeneration >= d.Generation &&
			d.Status.NumberReady >= d.Status.DesiredNumberScheduled &&
			d.Status.UpdatedNumberScheduled >= d.Status.DesiredNumberScheduled
		return ok, fmt.Sprintf("ready %d/%d", d.Status.NumberReady, d.Status.DesiredNumberScheduled), nil
	default:
		return false, "", fmt.Errorf("cannot track a %s", t.Kind)
	}
}
