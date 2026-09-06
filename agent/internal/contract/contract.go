// Package contract defines the versioned set of facts the agent collects about
// a failing pod and ships to the brain.
//
// The schema is deliberately explicit rather than a marshalled Kubernetes
// object: the brain must receive a stable, redacted, minimal view, and stored
// diagnoses must stay interpretable when the shape changes. See docs/DESIGN.md
// section 3.
package contract

import "time"

// Version identifies the schema. Bump on any change to the shape below; it is
// persisted alongside every diagnosis so old records remain readable.
const Version = "1"

// Contract is the complete evidence set for a single incident.
type Contract struct {
	ContractVersion string    `json:"contract_version"`
	IncidentID      string    `json:"incident_id"`
	CollectedAt     time.Time `json:"collected_at"`

	// FailureType is the classified shape, not the pod's surface waiting
	// reason. The two differ: the recorded fixtures show CrashLoopBackOff on
	// the surface for both a database connection failure and a memory limit
	// too low for container init.
	FailureType string `json:"failure_type"`

	Pod       Pod         `json:"pod"`
	Owner     *Owner      `json:"owner,omitempty"`
	Container Container   `json:"container"`
	Events    []Event     `json:"events"`
	Logs      Logs        `json:"logs"`
	Node      NodeSummary `json:"node"`

	// RedactedCount reports how many values were withheld by the redactor, so
	// the brain can distinguish withheld evidence from absent evidence.
	// RedactedKinds names the categories withheld, without revealing values.
	RedactedCount int      `json:"redacted_count"`
	RedactedKinds []string `json:"redacted_kinds,omitempty"`
}

// Pod identifies the failing pod. UID is load-bearing: events must be filtered
// by UID rather than name, or a previous incarnation of the same pod name
// contributes its failures to this one (docs/DESIGN.md section 3.4).
type Pod struct {
	Namespace string    `json:"namespace"`
	Name      string    `json:"name"`
	UID       string    `json:"uid"`
	NodeName  string    `json:"node_name"`
	Phase     string    `json:"phase"`
	CreatedAt time.Time `json:"created_at"`
	AgeSecs   float64   `json:"age_seconds"`
}

// Owner is the controlling workload. Remediation almost always targets this
// rather than the pod, which a controller would immediately replace.
type Owner struct {
	Kind     string `json:"kind"`
	Name     string `json:"name"`
	Image    string `json:"image"`
	Revision string `json:"revision,omitempty"`
}

// Container carries both the symptom (State) and the cause (LastState).
type Container struct {
	Name         string   `json:"name"`
	Image        string   `json:"image"`
	ImageID      string   `json:"image_id"`
	Ready        bool     `json:"ready"`
	RestartCount int32    `json:"restart_count"`
	Command      []string `json:"command,omitempty"`
	Args         []string `json:"args,omitempty"`

	WaitingReason  string `json:"waiting_reason,omitempty"`
	WaitingMessage string `json:"waiting_message,omitempty"`

	LastTerminated *Terminated `json:"last_terminated,omitempty"`

	MemoryLimit   string `json:"memory_limit,omitempty"`
	MemoryRequest string `json:"memory_request,omitempty"`
	CPULimit      string `json:"cpu_limit,omitempty"`
	CPURequest    string `json:"cpu_request,omitempty"`

	// EnvNames carries names only. Values are never collected; they routinely
	// hold credentials and the brain talks to a third-party model API.
	EnvNames []string `json:"env_names,omitempty"`

	HasLivenessProbe  bool `json:"has_liveness_probe"`
	HasReadinessProbe bool `json:"has_readiness_probe"`

	// CrashesPerMinute is computed here rather than by the model. It is the
	// flap-versus-one-off discriminator and depends on arithmetic over
	// timestamps that language models perform unreliably.
	CrashesPerMinute float64 `json:"crashes_per_minute"`
}

// Terminated is the previous container's exit. Recorded exit codes seen in
// fixtures: 1 (generic application failure), 137 (OOMKilled), 128 (StartError,
// where container init itself was OOM-killed).
type Terminated struct {
	ExitCode int32  `json:"exit_code"`
	Reason   string `json:"reason"`
	Signal   int32  `json:"signal,omitempty"`
	Message  string `json:"message,omitempty"`

	// StartedAt is nil when the container never started. Kubernetes reports a
	// zero timestamp there, which serialises as 1970 and reads as a real time
	// the container was up. Null says what actually happened.
	StartedAt  *time.Time `json:"started_at"`
	FinishedAt *time.Time `json:"finished_at"`
}

// Event preserves the fields kubectl uses to render "x5 over 2m42s". Keeping
// Count, First and Last retains the flap signal that a naive field selection
// would discard (docs/DESIGN.md section 3.3).
type Event struct {
	Type    string `json:"type"`
	Reason  string `json:"reason"`
	Message string `json:"message"`

	// Raw fields, preserved as reported. Either representation may be the
	// populated one depending on which events API wrote the object, so both
	// are carried rather than collapsed at collection time.
	Count              int32      `json:"count"`
	FirstTimestamp     time.Time  `json:"first_timestamp"`
	LastTimestamp      time.Time  `json:"last_timestamp"`
	SeriesCount        int32      `json:"series_count,omitempty"`
	SeriesLastObserved *time.Time `json:"series_last_observed,omitempty"`

	// Normalized across both representations, and the reconstructed kubectl
	// phrasing, which is what the prompt actually consumes.
	Occurrences int32     `json:"occurrences"`
	FirstSeen   time.Time `json:"first_seen"`
	LastSeen    time.Time `json:"last_seen"`
	Aggregated  string    `json:"aggregated,omitempty"`
}

// Logs carries the previous container's output where available.
//
// Available and Empty are distinct: "the container wrote nothing" and "the
// runtime discarded the container" are different facts and drive different
// confidence ceilings. Log retrieval is racy, so collection happens at
// detection time (docs/DESIGN.md section 2.3).
type Logs struct {
	Available bool   `json:"available"`
	Empty     bool   `json:"empty"`
	FromPrev  bool   `json:"from_previous"`
	Truncated bool   `json:"truncated"`
	Content   string `json:"content"`
}

// NodeSummary can reframe a pod-level problem as a node-level one.
type NodeSummary struct {
	Name           string `json:"name"`
	Ready          bool   `json:"ready"`
	MemoryPressure bool   `json:"memory_pressure"`
	DiskPressure   bool   `json:"disk_pressure"`
	PIDPressure    bool   `json:"pid_pressure"`
}

// TimeOrNil returns a pointer to t, or nil when t carries no information.
// Optional timestamps are pointers because encoding/json cannot omit a zero
// struct, and a zero time serialises as a date in 1970 that reads as real.
//
// Two zero values exist. Go's zero time.Time is year 1, and the Unix epoch is
// 1970. Kubernetes reports the second for a container that never started:
// recorded live, lastState.terminated.startedAt was "1970-01-01T00:00:00Z" on
// a container whose init was killed. Both mean "never", and both become null.
func TimeOrNil(t time.Time) *time.Time {
	if t.IsZero() || t.Unix() == 0 {
		return nil
	}
	return &t
}
