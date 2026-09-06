package remediate

import (
	"context"
	"errors"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes/fake"
)

func deployment(limit string) *appsv1.Deployment {
	one := int32(1)
	return &appsv1.Deployment{
		ObjectMeta: metav1.ObjectMeta{Name: "worker", Namespace: "shop", Generation: 1},
		Spec: appsv1.DeploymentSpec{
			Replicas: &one,
			Template: corev1.PodTemplateSpec{Spec: corev1.PodSpec{Containers: []corev1.Container{
				{Name: "sidecar", Image: "envoy:1.30", Resources: corev1.ResourceRequirements{
					Limits: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse("64Mi")},
				}},
				{Name: "hog", Image: "redis:alpine", Resources: corev1.ResourceRequirements{
					Limits:   corev1.ResourceList{corev1.ResourceMemory: resource.MustParse(limit)},
					Requests: corev1.ResourceList{corev1.ResourceMemory: resource.MustParse(limit)},
				}},
			}}},
		},
	}
}

func memoryPlan() Plan {
	return Plan{
		IncidentID: "inc-1", Kind: KindSetMemory, Executable: true,
		Target:      Target{Kind: "Deployment", Namespace: "shop", Name: "worker", Container: "hog"},
		MemoryLimit: "512Mi", MemoryRequest: "512Mi", Previous: "128Mi",
	}
}

func TestExecutionIsDisabledByDefault(t *testing.T) {
	client := fake.NewClientset(deployment("128Mi"))
	res, err := Executor{Client: client}.Execute(context.Background(), memoryPlan())
	if !errors.Is(err, ErrExecutionDisabled) {
		t.Fatalf("err = %v, want ErrExecutionDisabled", err)
	}
	if res.Applied || res.Patch == "" {
		t.Errorf("the patch should be emitted and not applied: %+v", res)
	}
	d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
	if got := d.Spec.Template.Spec.Containers[1].Resources.Limits.Memory().String(); got != "128Mi" {
		t.Errorf("limit changed to %s with execution disabled", got)
	}
}

func TestMemoryPatchTouchesOnlyTheNamedContainer(t *testing.T) {
	client := fake.NewClientset(deployment("128Mi"))
	res, err := Executor{Client: client, Enabled: true}.Execute(context.Background(), memoryPlan())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Applied {
		t.Fatal("not applied")
	}
	d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
	hog := d.Spec.Template.Spec.Containers[1]
	if hog.Resources.Limits.Memory().String() != "512Mi" || hog.Resources.Requests.Memory().String() != "512Mi" {
		t.Errorf("hog resources = %v", hog.Resources)
	}
	if hog.Image != "redis:alpine" {
		t.Errorf("image changed to %s", hog.Image)
	}
	sidecar := d.Spec.Template.Spec.Containers[0]
	if sidecar.Resources.Limits.Memory().String() != "64Mi" || sidecar.Image != "envoy:1.30" {
		t.Errorf("the sidecar was touched: %v", sidecar)
	}
}

func TestImagePatch(t *testing.T) {
	client := fake.NewClientset(deployment("128Mi"))
	p := Plan{
		IncidentID: "inc-2", Kind: KindSetImage, Executable: true,
		Target: Target{Kind: "Deployment", Namespace: "shop", Name: "worker", Container: "hog"},
		Image:  "ghcr.io/acme/worker:v2",
	}
	if _, err := (Executor{Client: client, Enabled: true}).Execute(context.Background(), p); err != nil {
		t.Fatal(err)
	}
	d, _ := client.AppsV1().Deployments("shop").Get(context.Background(), "worker", metav1.GetOptions{})
	if d.Spec.Template.Spec.Containers[1].Image != "ghcr.io/acme/worker:v2" {
		t.Errorf("image = %s", d.Spec.Template.Spec.Containers[1].Image)
	}
}

func TestManualPlansNeverExecute(t *testing.T) {
	client := fake.NewClientset(deployment("128Mi"))
	p := Plan{IncidentID: "inc-3", Kind: KindManual, Reason: "bare pod"}
	if _, err := (Executor{Client: client, Enabled: true}).Execute(context.Background(), p); !errors.Is(err, ErrNotExecutable) {
		t.Fatalf("err = %v", err)
	}
}

func TestMissingWorkloadIsAnError(t *testing.T) {
	client := fake.NewClientset()
	if _, err := (Executor{Client: client, Enabled: true}).Execute(context.Background(), memoryPlan()); err == nil {
		t.Fatal("expected an error for a missing Deployment")
	}
}

// ------------------------------------------------------------------ resolve

type script struct {
	client *fake.Clientset
	now    time.Time
	steps  []func(d *appsv1.Deployment)
	i      int
}

// tick advances the clock and applies the next scripted status change, so
// a whole 40 minute observation runs in milliseconds.
func (s *script) tick(ctx context.Context, d time.Duration) error {
	s.now = s.now.Add(d)
	if s.i < len(s.steps) {
		dep, _ := s.client.AppsV1().Deployments("shop").Get(ctx, "worker", metav1.GetOptions{})
		s.steps[s.i](dep)
		_, _ = s.client.AppsV1().Deployments("shop").UpdateStatus(ctx, dep, metav1.UpdateOptions{})
		s.i++
	}
	return nil
}

func resolver(t *testing.T, steps ...func(d *appsv1.Deployment)) (*Resolver, *script) {
	t.Helper()
	client := fake.NewClientset(deployment("512Mi"))
	s := &script{client: client, now: time.Date(2026, 9, 6, 15, 0, 0, 0, time.UTC), steps: steps}
	r := NewResolver(client)
	r.Poll = time.Minute
	r.Now = func() time.Time { return s.now }
	r.Sleep = s.tick
	return r, s
}

func becomeReady(d *appsv1.Deployment) {
	d.Status.ObservedGeneration = d.Generation
	d.Status.ReadyReplicas = 1
	d.Status.UpdatedReplicas = 1
	d.Status.UnavailableReplicas = 0
}

func becomeUnready(d *appsv1.Deployment) {
	d.Status.ReadyReplicas = 0
	d.Status.UnavailableReplicas = 1
}

func TestResolvedWhenReadyInTimeAndStable(t *testing.T) {
	r, _ := resolver(t, func(*appsv1.Deployment) {}, becomeReady)
	res, err := r.Track(context.Background(), "inc-1", Target{Kind: "Deployment", Namespace: "shop", Name: "worker"})
	if err != nil {
		t.Fatal(err)
	}
	if !res.ReadyWithinSLA || !res.StayedReady || !res.Resolved {
		t.Errorf("resolution = %+v", res)
	}
	if res.ReadyAfter != 2*time.Minute {
		t.Errorf("ReadyAfter = %s", res.ReadyAfter)
	}
}

func TestNotReadyInTimeIsNotResolved(t *testing.T) {
	r, _ := resolver(t)
	res, err := r.Track(context.Background(), "inc-1", Target{Kind: "Deployment", Namespace: "shop", Name: "worker"})
	if err != nil {
		t.Fatal(err)
	}
	if res.ReadyWithinSLA || res.Resolved {
		t.Errorf("resolution = %+v", res)
	}
	if res.Detail == "" {
		t.Error("detail should say what was observed")
	}
}

func TestFlappingAfterReadyIsNotResolved(t *testing.T) {
	steps := []func(*appsv1.Deployment){becomeReady}
	for i := 0; i < 5; i++ {
		steps = append(steps, func(*appsv1.Deployment) {})
	}
	steps = append(steps, becomeUnready)
	r, _ := resolver(t, steps...)
	res, err := r.Track(context.Background(), "inc-1", Target{Kind: "Deployment", Namespace: "shop", Name: "worker"})
	if err != nil {
		t.Fatal(err)
	}
	if !res.ReadyWithinSLA {
		t.Error("it did become Ready")
	}
	if res.StayedReady || res.Resolved {
		t.Errorf("a flap must not count as resolved: %+v", res)
	}
}

func TestTrackingStopsWithTheContext(t *testing.T) {
	r, _ := resolver(t)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r.Sleep = sleepCtx
	if _, err := r.Track(ctx, "inc-1", Target{Kind: "Deployment", Namespace: "shop", Name: "worker"}); err == nil {
		t.Fatal("expected a context error")
	}
}
