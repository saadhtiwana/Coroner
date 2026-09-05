// Package collect assembles the evidence contract for a failing pod.
//
// Everything the brain will ever see about an incident is produced here, so
// two properties matter more than convenience. Events are filtered by pod UID,
// never by name, because name matching silently attributes a previous pod
// incarnation's failures to the current one. Logs are captured at detection
// time, because container log availability is racy and retrieving them later
// intermittently returns nothing.
package collect

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"sort"
	"strings"
	"time"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"github.com/saadhtiwana/coroner/agent/internal/classify"
	"github.com/saadhtiwana/coroner/agent/internal/contract"
	"github.com/saadhtiwana/coroner/agent/internal/redact"
)

// Defaults for log bounding. The fatal line is at the end of the log, so both
// limits are applied tail-anchored.
const (
	DefaultTailLines   int64 = 200
	DefaultMaxLogBytes int   = 16 * 1024
)

// Collector assembles contracts.
type Collector struct {
	Client kubernetes.Interface
	Logs   LogFetcher

	// Now is injectable so age and rate calculations are deterministic in
	// tests. Defaults to time.Now.
	Now func() time.Time

	TailLines   int64
	MaxLogBytes int
}

// New returns a Collector with defaults applied.
func New(client kubernetes.Interface, logs LogFetcher) *Collector {
	return &Collector{
		Client:      client,
		Logs:        logs,
		Now:         time.Now,
		TailLines:   DefaultTailLines,
		MaxLogBytes: DefaultMaxLogBytes,
	}
}

func (c *Collector) now() time.Time {
	if c.Now == nil {
		return time.Now()
	}
	return c.Now()
}

// Collect assembles the full contract for one failing container.
func (c *Collector) Collect(ctx context.Context, pod *corev1.Pod, containerName string, result classify.Result) (*contract.Contract, error) {
	if pod == nil {
		return nil, fmt.Errorf("collect: pod is nil")
	}

	status := findContainerStatus(pod, containerName)
	if status == nil {
		return nil, fmt.Errorf("collect: no status for container %q in pod %s/%s", containerName, pod.Namespace, pod.Name)
	}
	spec := findContainerSpec(pod, containerName)

	now := c.now()
	age := now.Sub(pod.CreationTimestamp.Time)
	if age < 0 {
		age = 0
	}

	var red redact.Result

	out := &contract.Contract{
		ContractVersion: contract.Version,
		IncidentID:      IncidentID(string(pod.UID), containerName, status.RestartCount, string(result.Type)),
		CollectedAt:     now,
		FailureType:     string(result.Type),
		Pod: contract.Pod{
			Namespace: pod.Namespace,
			Name:      pod.Name,
			UID:       string(pod.UID),
			NodeName:  pod.Spec.NodeName,
			Phase:     string(pod.Status.Phase),
			CreatedAt: pod.CreationTimestamp.Time,
			AgeSecs:   age.Seconds(),
		},
		Container: buildContainer(status, spec, age),
	}

	out.Owner = c.resolveOwner(ctx, pod, containerName)
	out.Events = c.collectEvents(ctx, pod, &red)
	out.Node = c.collectNode(ctx, pod)
	out.Logs = c.collectLogs(ctx, pod, containerName, &red)

	// The waiting message can echo runtime detail; redact it like any other
	// free text rather than trusting its provenance.
	if out.Container.WaitingMessage != "" {
		r := redact.Text(out.Container.WaitingMessage)
		out.Container.WaitingMessage = r.Text
		red.Merge(r)
	}
	if out.Container.LastTerminated != nil && out.Container.LastTerminated.Message != "" {
		r := redact.Text(out.Container.LastTerminated.Message)
		out.Container.LastTerminated.Message = r.Text
		red.Merge(r)
	}

	out.RedactedCount = red.Count
	out.RedactedKinds = red.Kinds
	return out, nil
}

func buildContainer(status *corev1.ContainerStatus, spec *corev1.Container, age time.Duration) contract.Container {
	c := contract.Container{
		Name:             status.Name,
		Image:            status.Image,
		ImageID:          status.ImageID,
		Ready:            status.Ready,
		RestartCount:     status.RestartCount,
		CrashesPerMinute: contract.CrashesPerMinute(status.RestartCount, age),
	}

	if w := status.State.Waiting; w != nil {
		c.WaitingReason = w.Reason
		c.WaitingMessage = w.Message
	}

	t := status.LastTerminationState.Terminated
	if t == nil {
		t = status.State.Terminated
	}
	if t != nil {
		c.LastTerminated = &contract.Terminated{
			ExitCode:   t.ExitCode,
			Reason:     t.Reason,
			Signal:     t.Signal,
			Message:    t.Message,
			StartedAt:  contract.TimeOrNil(t.StartedAt.Time),
			FinishedAt: contract.TimeOrNil(t.FinishedAt.Time),
		}
	}

	if spec != nil {
		c.Command = spec.Command
		c.Args = spec.Args
		c.HasLivenessProbe = spec.LivenessProbe != nil
		c.HasReadinessProbe = spec.ReadinessProbe != nil

		if v, ok := spec.Resources.Limits[corev1.ResourceMemory]; ok {
			c.MemoryLimit = v.String()
		}
		if v, ok := spec.Resources.Requests[corev1.ResourceMemory]; ok {
			c.MemoryRequest = v.String()
		}
		if v, ok := spec.Resources.Limits[corev1.ResourceCPU]; ok {
			c.CPULimit = v.String()
		}
		if v, ok := spec.Resources.Requests[corev1.ResourceCPU]; ok {
			c.CPURequest = v.String()
		}

		// Names only. Values are never read, so a credential in an env var
		// cannot leave the cluster through this path at all, rather than
		// depending on the redactor to catch it.
		for _, e := range spec.Env {
			c.EnvNames = append(c.EnvNames, e.Name)
		}
		sort.Strings(c.EnvNames)
	}

	return c
}

// collectEvents lists events for the pod and filters them by UID.
//
// The field selector asks the server to filter, but the result is filtered
// again locally. Field selector support for involvedObject.uid is not
// guaranteed across API server versions, and a selector that is silently
// ignored returns every event in the namespace. The local filter is what makes
// the guarantee, not the selector.
func (c *Collector) collectEvents(ctx context.Context, pod *corev1.Pod, red *redact.Result) []contract.Event {
	list, err := c.Client.CoreV1().Events(pod.Namespace).List(ctx, metav1.ListOptions{
		FieldSelector: "involvedObject.uid=" + string(pod.UID),
	})
	if err != nil {
		return nil
	}

	out := make([]contract.Event, 0, len(list.Items))
	for i := range list.Items {
		e := &list.Items[i]
		if string(e.InvolvedObject.UID) != string(pod.UID) {
			continue
		}
		ce := normalizeEvent(e)
		r := redact.Text(ce.Message)
		ce.Message = r.Text
		red.Merge(r)
		out = append(out, ce)
	}

	sort.SliceStable(out, func(i, j int) bool { return out[i].LastSeen.Before(out[j].LastSeen) })
	return out
}

// normalizeEvent preserves both the legacy count/timestamp fields and the
// newer series fields, then derives a single occurrence count and window from
// whichever is populated.
func normalizeEvent(e *corev1.Event) contract.Event {
	ce := contract.Event{
		Type:           e.Type,
		Reason:         e.Reason,
		Message:        e.Message,
		Count:          e.Count,
		FirstTimestamp: e.FirstTimestamp.Time,
		LastTimestamp:  e.LastTimestamp.Time,
	}
	if e.Series != nil {
		ce.SeriesCount = e.Series.Count
		ce.SeriesLastObserved = contract.TimeOrNil(e.Series.LastObservedTime.Time)
	}

	ce.Occurrences = ce.Count
	if ce.SeriesCount > ce.Occurrences {
		ce.Occurrences = ce.SeriesCount
	}
	if ce.Occurrences == 0 {
		ce.Occurrences = 1
	}

	ce.FirstSeen = ce.FirstTimestamp
	if ce.FirstSeen.IsZero() {
		ce.FirstSeen = e.EventTime.Time
	}
	if ce.SeriesLastObserved != nil {
		ce.LastSeen = *ce.SeriesLastObserved
	}
	if ce.LastSeen.IsZero() {
		ce.LastSeen = ce.LastTimestamp
	}
	if ce.LastSeen.IsZero() {
		ce.LastSeen = e.EventTime.Time
	}

	ce.Aggregated = contract.FormatAggregation(ce.Occurrences, ce.FirstSeen, ce.LastSeen)
	return ce
}

func (c *Collector) collectNode(ctx context.Context, pod *corev1.Pod) contract.NodeSummary {
	summary := contract.NodeSummary{Name: pod.Spec.NodeName}
	if pod.Spec.NodeName == "" {
		return summary
	}
	node, err := c.Client.CoreV1().Nodes().Get(ctx, pod.Spec.NodeName, metav1.GetOptions{})
	if err != nil {
		return summary
	}
	for _, cond := range node.Status.Conditions {
		isTrue := cond.Status == corev1.ConditionTrue
		switch cond.Type {
		case corev1.NodeReady:
			summary.Ready = isTrue
		case corev1.NodeMemoryPressure:
			summary.MemoryPressure = isTrue
		case corev1.NodeDiskPressure:
			summary.DiskPressure = isTrue
		case corev1.NodePIDPressure:
			summary.PIDPressure = isTrue
		}
	}
	return summary
}

// collectLogs takes the previous container's output first and the current
// container's as fallback.
//
// available and empty are set independently. A container that wrote nothing
// and a container the runtime has reclaimed are different facts, and section 4
// of the design gives them different confidence ceilings.
func (c *Collector) collectLogs(ctx context.Context, pod *corev1.Pod, container string, red *redact.Result) contract.Logs {
	tail := c.TailLines
	if tail <= 0 {
		tail = DefaultTailLines
	}
	maxBytes := c.MaxLogBytes
	if maxBytes <= 0 {
		maxBytes = DefaultMaxLogBytes
	}

	out := contract.Logs{}

	// A body that is really a retrieval failure counts as unavailable, not as
	// a log, so the fallback to the current container still gets a chance.
	body, err := c.Logs.Fetch(ctx, pod.Namespace, pod.Name, container, true, tail)
	if err == nil && !isRuntimeFailureBody(body) {
		out.Available = true
		out.FromPrev = true
	} else {
		body, err = c.Logs.Fetch(ctx, pod.Namespace, pod.Name, container, false, tail)
		if err == nil && !isRuntimeFailureBody(body) {
			out.Available = true
			out.FromPrev = false
		}
	}

	if !out.Available {
		out.Empty = true
		return out
	}

	body, truncated := tailBytes(body, maxBytes)
	out.Truncated = truncated
	out.Empty = strings.TrimSpace(body) == ""

	r := redact.Text(body)
	out.Content = r.Text
	red.Merge(r)
	return out
}

// resolveOwner walks the controller chain to the workload a fix would target.
//
// Patching a pod that a ReplicaSet will immediately recreate is a non-fix, so
// the ReplicaSet is followed one more hop to its Deployment.
func (c *Collector) resolveOwner(ctx context.Context, pod *corev1.Pod, containerName string) *contract.Owner {
	ref := controllerRef(pod.OwnerReferences)
	if ref == nil {
		return nil
	}

	switch ref.Kind {
	case "ReplicaSet":
		rs, err := c.Client.AppsV1().ReplicaSets(pod.Namespace).Get(ctx, ref.Name, metav1.GetOptions{})
		if err != nil {
			return &contract.Owner{Kind: ref.Kind, Name: ref.Name}
		}
		if dep := controllerRef(rs.OwnerReferences); dep != nil && dep.Kind == "Deployment" {
			owner := &contract.Owner{
				Kind:     "Deployment",
				Name:     dep.Name,
				Revision: rs.Annotations["deployment.kubernetes.io/revision"],
			}
			if d, err := c.Client.AppsV1().Deployments(pod.Namespace).Get(ctx, dep.Name, metav1.GetOptions{}); err == nil {
				owner.Image = imageFromTemplate(&d.Spec.Template, containerName)
			}
			if owner.Image == "" {
				owner.Image = imageFromTemplate(&rs.Spec.Template, containerName)
			}
			return owner
		}
		return &contract.Owner{
			Kind:     "ReplicaSet",
			Name:     rs.Name,
			Image:    imageFromTemplate(&rs.Spec.Template, containerName),
			Revision: rs.Annotations["deployment.kubernetes.io/revision"],
		}

	case "StatefulSet":
		owner := &contract.Owner{Kind: ref.Kind, Name: ref.Name}
		if s, err := c.Client.AppsV1().StatefulSets(pod.Namespace).Get(ctx, ref.Name, metav1.GetOptions{}); err == nil {
			owner.Image = imageFromTemplate(&s.Spec.Template, containerName)
			owner.Revision = s.Status.UpdateRevision
		}
		return owner

	case "DaemonSet":
		owner := &contract.Owner{Kind: ref.Kind, Name: ref.Name}
		if d, err := c.Client.AppsV1().DaemonSets(pod.Namespace).Get(ctx, ref.Name, metav1.GetOptions{}); err == nil {
			owner.Image = imageFromTemplate(&d.Spec.Template, containerName)
		}
		return owner

	default:
		return &contract.Owner{Kind: ref.Kind, Name: ref.Name}
	}
}

func imageFromTemplate(tmpl *corev1.PodTemplateSpec, containerName string) string {
	if tmpl == nil {
		return ""
	}
	for i := range tmpl.Spec.Containers {
		if tmpl.Spec.Containers[i].Name == containerName {
			return tmpl.Spec.Containers[i].Image
		}
	}
	if len(tmpl.Spec.Containers) > 0 {
		return tmpl.Spec.Containers[0].Image
	}
	return ""
}

func controllerRef(refs []metav1.OwnerReference) *metav1.OwnerReference {
	for i := range refs {
		if refs[i].Controller != nil && *refs[i].Controller {
			return &refs[i]
		}
	}
	return nil
}

func findContainerStatus(pod *corev1.Pod, name string) *corev1.ContainerStatus {
	for i := range pod.Status.ContainerStatuses {
		if pod.Status.ContainerStatuses[i].Name == name {
			return &pod.Status.ContainerStatuses[i]
		}
	}
	for i := range pod.Status.InitContainerStatuses {
		if pod.Status.InitContainerStatuses[i].Name == name {
			return &pod.Status.InitContainerStatuses[i]
		}
	}
	return nil
}

func findContainerSpec(pod *corev1.Pod, name string) *corev1.Container {
	for i := range pod.Spec.Containers {
		if pod.Spec.Containers[i].Name == name {
			return &pod.Spec.Containers[i]
		}
	}
	for i := range pod.Spec.InitContainers {
		if pod.Spec.InitContainers[i].Name == name {
			return &pod.Spec.InitContainers[i]
		}
	}
	return nil
}

// IncidentID is derived rather than random so the same failure occurrence
// produces the same identifier, which makes the emitted contract diffable, the
// tests deterministic, and watch-mode deduplication possible without state.
func IncidentID(uid, container string, restarts int32, failureType string) string {
	sum := sha256.Sum256([]byte(fmt.Sprintf("%s|%s|%d|%s", uid, container, restarts, failureType)))
	return "inc-" + hex.EncodeToString(sum[:6])
}
