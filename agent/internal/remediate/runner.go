package remediate

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"sync"
	"time"

	"github.com/saadhtiwana/coroner/agent/internal/approval"
	"github.com/saadhtiwana/coroner/agent/internal/brain"
)

// Brain is the slice of the brain client the runner needs.
type Brain interface {
	Approved(ctx context.Context, execute bool) ([]brain.ApprovedIncident, error)
	ReportExecution(ctx context.Context, incidentID string, report brain.ExecutionReport) error
	ReportResolution(ctx context.Context, incidentID string, report brain.ResolutionReport) error
}

// Runner polls the brain for approved incidents and acts on each exactly
// once: verify the token, build the plan, emit it, and, only when execution
// is enabled, apply it and track the result.
type Runner struct {
	Brain    Brain
	Secret   []byte
	Executor Executor
	Resolver *Resolver
	Logger   *slog.Logger

	// Known maps incident id to the context hash the agent received with
	// the verdict, when this process was the one that sent the contract.
	// When present it must match the token's claim. When absent, after a
	// restart, the token alone is verified: it was minted by the brain's
	// decision path with the shared secret and binds the same hash.
	Known *sync.Map

	// Tracking runs the resolver for an executed action. In production it is
	// a goroutine; tests run it inline.
	Tracking func(ctx context.Context, incidentID string, t Target)
}

// Remember records the context hash the brain returned for a contract this
// process sent.
func (r *Runner) Remember(incidentID, contextHash string) {
	if r.Known == nil {
		r.Known = &sync.Map{}
	}
	r.Known.Store(incidentID, contextHash)
}

// RunOnce polls once and acts on every approved incident.
func (r *Runner) RunOnce(ctx context.Context) error {
	rows, err := r.Brain.Approved(ctx, r.Executor.Enabled)
	if err != nil {
		return fmt.Errorf("listing approved incidents: %w", err)
	}
	for i := range rows {
		r.handle(ctx, rows[i])
	}
	return nil
}

// Run polls until the context ends.
func (r *Runner) Run(ctx context.Context, every time.Duration) {
	ticker := time.NewTicker(every)
	defer ticker.Stop()
	for {
		if err := r.RunOnce(ctx); err != nil && ctx.Err() == nil {
			r.Logger.Warn("remediation poll failed", "error", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}

func (r *Runner) handle(ctx context.Context, inc brain.ApprovedIncident) {
	log := r.Logger.With("incident_id", inc.IncidentID, "decision", inc.Decision)

	// The token first. Nothing else is looked at until it verifies.
	claims := approval.Claims{
		IncidentID:  inc.IncidentID,
		ContextHash: inc.ContextHash,
		Decision:    inc.Decision,
		Action:      inc.DecisionAction,
		DecidedAt:   inc.DecisionAt,
	}
	if err := approval.Verify(r.Secret, inc.ApprovalToken, claims); err != nil {
		r.refuse(ctx, log, inc.IncidentID, "approval token did not verify: "+err.Error())
		return
	}
	if r.Known != nil {
		if seen, ok := r.Known.Load(inc.IncidentID); ok && seen != inc.ContextHash {
			r.refuse(ctx, log, inc.IncidentID, "the approval is for different evidence than this agent sent")
			return
		}
	}

	plan, err := Build(Incident{
		IncidentID:     inc.IncidentID,
		FailureType:    inc.FailureType,
		Decision:       inc.Decision,
		DecisionAction: inc.DecisionAction,
		ContractJSON:   inc.ContractJSON,
	})
	if err != nil {
		r.refuse(ctx, log, inc.IncidentID, err.Error())
		return
	}

	log.Info("remediation plan", "kind", plan.Kind, "executable", plan.Executable, "plan", plan.Summary())

	if !plan.Executable {
		r.report(ctx, log, inc.IncidentID, brain.ExecutionReport{Status: "refused", Detail: plan.Reason, Plan: plan})
		return
	}

	result, err := r.Executor.Execute(ctx, plan)
	switch {
	case errors.Is(err, ErrExecutionDisabled):
		r.report(ctx, log, inc.IncidentID, brain.ExecutionReport{
			Status: "proposed",
			Detail: "execution is disabled; the plan was emitted and not applied: " + plan.Summary(),
			Plan:   plan,
		})
		return
	case err != nil:
		log.Error("remediation failed", "error", err)
		r.report(ctx, log, inc.IncidentID, brain.ExecutionReport{Status: "failed", Detail: err.Error(), Plan: plan})
		return
	}

	log.Info("remediation applied", "target", fmt.Sprintf("%s/%s", result.Target.Kind, result.Target.Name))
	r.report(ctx, log, inc.IncidentID, brain.ExecutionReport{Status: "executed", Detail: plan.Summary(), Plan: plan})
	if r.Tracking != nil {
		r.Tracking(ctx, inc.IncidentID, plan.Target)
	}
}

// Track runs the resolver and reports the section 5.2 label.
func (r *Runner) Track(ctx context.Context, incidentID string, t Target) {
	if r.Resolver == nil {
		return
	}
	res, err := r.Resolver.Track(ctx, incidentID, t)
	if err != nil {
		r.Logger.Warn("resolution tracking ended early", "incident_id", incidentID, "error", err)
		return
	}
	r.Logger.Info("resolution", "incident_id", incidentID, "resolved", res.Resolved, "detail", res.Detail)
	if err := r.Brain.ReportResolution(ctx, incidentID, brain.ResolutionReport{
		ReadyWithinSLA: res.ReadyWithinSLA,
		StayedReady:    res.StayedReady,
		Resolved:       res.Resolved,
		Detail:         res.Detail,
	}); err != nil {
		r.Logger.Warn("could not report resolution", "incident_id", incidentID, "error", err)
	}
}

func (r *Runner) refuse(ctx context.Context, log *slog.Logger, incidentID, reason string) {
	log.Warn("approval refused", "reason", reason)
	r.report(ctx, log, incidentID, brain.ExecutionReport{Status: "refused", Detail: reason})
}

func (r *Runner) report(ctx context.Context, log *slog.Logger, incidentID string, rep brain.ExecutionReport) {
	if err := r.Brain.ReportExecution(ctx, incidentID, rep); err != nil {
		log.Warn("could not report execution outcome", "status", rep.Status, "error", err)
	}
}
