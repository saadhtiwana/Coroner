# Coroner: design

Status: draft, written before implementation.
Evidence base: fixtures recorded from a live kind cluster (Kubernetes v1.37.0)
on 2026-09-05, checked into `fixtures/`. Every claim about what Kubernetes does
or does not report is taken from those recordings, not from memory.

---

## 1. Scope

### What Coroner does

Coroner performs an autopsy on a workload that something else has already
declared dead, and proposes a single remediation for a human to approve or
reject in Slack.

The pipeline is fixed and narrow:

1. Observe a pod entering a terminal-ish failure state.
2. Collect the context a competent on-call engineer would collect by hand.
3. Reason over that context to produce a root-cause hypothesis.
4. Propose one concrete remediation.
5. Post hypothesis, evidence, and proposal to Slack behind an approval gate.
6. Execute only on explicit human approval. Record the outcome.

### What Coroner is not

Coroner sits **downstream of alerting**. It does not decide that something is
wrong; it explains something already known to be wrong. Concretely, it is not:

- **An observability platform.** It stores no time series, runs no scrapers,
  serves no dashboards, and answers no historical queries. Prometheus, Grafana,
  and a log store remain the system of record. Coroner reads the Kubernetes API
  and container logs at incident time only.
- **An alerting system.** It defines no alert rules, has no notion of severity
  or escalation, and pages nobody.
- **An anomaly detector.** It is not looking for problems. It is handed one.
- **An autoscaler or remediation controller.** It never acts on a timer, never
  acts on a threshold, and never acts without a human clicking approve.
- **A tracing or profiling tool.** It does not instrument application code.

The boundary matters because it bounds the context problem. Coroner sees one
pod, at one moment, through the API server. Section 2 is an honest accounting of
what that is and is not sufficient to explain.

### Non-negotiable invariant

No mutation of cluster state occurs without a recorded human approval keyed to a
specific diagnosis. This is enforced in the agent, which is the only component
holding write credentials, and which will not execute an action whose approval
token it cannot verify. The brain has no cluster credentials at all.

---

## 2. The three MVP failure types, and whether they are actually diagnosable

This section commits a prediction before implementation begins, so that it can
be falsified rather than rationalised afterwards.

The distinction that matters throughout is between **what happened** (reported
by Kubernetes, reliable, structured) and **why it happened** (usually only in
application logs, unreliable, unstructured, sometimes absent entirely).

### 2.1 ImagePullBackOff: expected to be diagnosed well

Recorded evidence (`fixtures/imagepullbackoff/`). The event message contains the
complete causal chain verbatim:

```
Failed to pull image "ghcr.io/saadhtiwana/coroner-does-not-exist:v0.0.0":
failed to pull and unpack image "...": failed to resolve reference "...":
failed to authorize: failed to fetch anonymous token: unexpected status from
GET request to https://ghcr.io/token?scope=repository%3A...%3Apull&service=ghcr.io:
403 Forbidden
```

The root cause is present in the structured context. Kubernetes itself performed
the failing operation and reported why it failed. No application cooperation is
required, and there are no logs to be missing: `restartCount` is 0 and the
container never started, so `logs` and `logs --previous` are both empty and
their absence carries no information.

The remaining difficulty is discrimination, not detection: `403 Forbidden`
against a nonexistent repository is indistinguishable from `403` against a
private repository with missing credentials, because registries deliberately
conflate the two to avoid leaking repository existence. Coroner must therefore
present both branches rather than guess, and the fix differs (correct the tag
versus attach an `imagePullSecret`).

**Prediction: 90 percent correct root cause.** This class is close to
deterministic and arguably does not need an LLM at all; the model's real
contribution is phrasing and the tag-versus-credentials disambiguation.

### 2.2 OOMKilled: the "what" is certain, the "why" is not recoverable

Recorded evidence (`fixtures/oomkilled/`):

```
lastState.terminated.reason   = OOMKilled
lastState.terminated.exitCode = 137
resources.limits.memory       = 128Mi
```

Detection is unambiguous and effectively free. Attribution is where this class
degrades, and it degrades hard.

From a single snapshot Coroner cannot distinguish:

- a memory limit set lower than the workload's honest steady-state requirement,
- a genuine leak that would exhaust any limit given time,
- a legitimate transient spike (a large tenant, an unusually big batch).

These have different, partly opposing fixes. Raising the limit is correct for
the first, delays the second, and masks the third. Distinguishing them requires
memory usage **over time**, which is precisely what section 1 says Coroner does
not have. Coroner has one data point.

Two recorded findings sharpen this:

**The logs are gone.** `kubectl logs --previous` on the OOMKilled container
returned `unable to retrieve container logs`. This is expected: the kernel OOM
killer delivers SIGKILL, the process gets no opportunity to write a final line,
and the log that exists simply stops mid-stream. There is no fatal message to
find, so the single richest source for the other two failure classes is
structurally unavailable here.

**The same underlying cause produces different surface shapes.** An earlier
recording (`fixtures/oom-startError/`) captured the same workload with a 64Mi
limit, where the container was killed during runtime init rather than during
execution:

```
lastState.terminated.reason   = StartError
lastState.terminated.exitCode = 128
state.waiting.reason          = RunContainerError
message: "container init was OOM-killed (memory limit too low?)"
```

Note that `exitCode` is 128 and `reason` is `StartError`, not 137/`OOMKilled`.
A classifier keyed on exit code 137 alone would miss this entirely and would
likely misfile it as a generic crash. The pod's surface `waiting.reason` was
`CrashLoopBackOff`, which is the wrong answer at the wrong altitude.

**Prediction: 95 percent correct on "this was killed for memory", 50 percent
correct on why.** Coroner should usually decline to choose between "limit too
low" and "leak" and should say so explicitly rather than defaulting to the
comfortable recommendation of raising the limit.

### 2.3 CrashLoopBackOff: expected to be the weakest, and the most valuable

This is the class that motivated the project, and it is the one where the
context contract is most likely to be insufficient.

Recorded evidence (`fixtures/crashloopbackoff/`). The entire structured context
for a pod dying because Postgres was unreachable:

```
state.waiting.reason          = CrashLoopBackOff
lastState.terminated.reason   = Error
lastState.terminated.exitCode = 1
restartCount                  = 4
```

Nothing there names Postgres. Nothing there names a network failure. Exit code 1
means "the process returned nonzero", which is the least informative signal a
Unix process can emit. The full `describe` output, roughly fifty lines, does not
contain the word "postgres" anywhere. The cause existed in exactly one place:

```
[error]   dial tcp 10.96.31.14:5432: connect: connection refused
[fatal]   could not initialise connection pool after 1 attempt
```

That is `kubectl logs --previous`, and it is application-authored text. Which
gives the governing constraint for this failure class:

> **Diagnosis quality for CrashLoopBackOff is bounded above by application log
> quality, which Coroner does not control and cannot improve.**

When the application logs a clear fatal line before exiting, this class should
diagnose well. When it panics silently, logs to a file instead of stdout, dies
on a signal, or emits a stack trace with no root frame, the collected context
contains no cause at all, and any confident answer is fabricated.

**A recorded operational hazard.** During fixture capture, `logs --previous`
succeeded at one moment and failed minutes later for an identically specified
pod, returning `unable to retrieve container logs`. The container runtime
reclaims dead containers; log availability is best-effort and racy. This is a
hard requirement on the agent, not a caveat: **logs must be captured at
detection time and persisted into the incident record immediately.** Collecting
them lazily when the brain asks will intermittently produce an empty evidence
set for the single most important evidence source.

**Prediction, split by a condition Coroner can detect before diagnosing:**

| Sub-case | Predicted correct | Expected share |
| --- | --- | --- |
| Previous logs present and contain a fatal or error line | 85 percent | maybe half |
| Previous logs present but with no clear error | 30 percent | maybe a quarter |
| Previous logs empty or unretrievable | under 10 percent | maybe a quarter |
| **Blended** | **around 60 percent** | |

The middle and bottom rows are where hallucination risk concentrates, and they
are roughly half of real traffic. Coroner can determine which row it is in
before it reasons, which is what makes principled abstention possible rather
than aspirational.

### 2.4 Committed prediction

| Failure type | Detect | Root cause | Confidence in this prediction |
| --- | --- | --- | --- |
| ImagePullBackOff | ~100 percent | 90 percent | high |
| OOMKilled | ~95 percent | 50 percent | medium |
| CrashLoopBackOff | ~100 percent | ~60 percent blended | low, widest error bar |

Falsification criteria, to be evaluated against the section 5 ledger:

- If ImagePullBackOff lands below 75 percent, the prompt or the parser is wrong,
  because the answer is demonstrably in the context.
- If CrashLoopBackOff lands below 40 percent over 20 or more incidents, the
  **context contract is wrong**, not the prompt. The response is to collect more
  or different context, not to write a better prompt.
- If CrashLoopBackOff exceeds 80 percent, the sample is almost certainly biased
  toward well-instrumented applications and should not be generalised.

### 2.5 Implementation order

**ImagePullBackOff is implemented first, because it is the only class where
correctness is externally verifiable.** The registry either has the image or it
does not, and the event message states which. That makes it possible to tell
whether the pipeline itself works, independently of whether the reasoning is any
good: if Coroner gets this class wrong, the defect is in collection, parsing,
transport, or rendering, not in judgement. No other class offers that
separation, and building the pipeline against a class where every failure is
ambiguous would mean debugging two unknowns at once.

**The second class implemented is CrashLoopBackOff, not OOMKilled.** This is
deliberate and it is the less comfortable choice.

A pipeline validated only against a near-deterministic class accumulates
assumptions that its author cannot see, precisely because nothing in that class
violates them. Concretely, ImagePullBackOff never exercises: an empty or
unretrievable log body, a diagnosis with no single determined answer, a
confidence ceiling below 0.9, the abstention path, or a case where the evidence
supports two hypotheses equally. Every one of those is routine in
CrashLoopBackOff, and several are the mechanisms section 4 depends on. A design
whose abstention path has never run is a design whose abstention path does not
work.

Taking CrashLoopBackOff second forces those paths open while the architecture is
still cheap to change. Taking it last would mean discovering, after both easier
classes have hardened their assumptions into the code, that the hardest and most
valuable class does not fit the shape that was built for them.

OOMKilled is third. It is the class most likely to be honestly answered with
"insufficient context to distinguish a leak from a low limit", which is a
conclusion worth reaching with the abstention machinery already proven by
CrashLoopBackOff rather than being built for the first time to serve it.

---

## 3. The context contract

The context contract is the exact, versioned set of facts the agent collects and
ships to the brain. It is a schema, not a convention, and it is versioned
(`contract_version`) so that stored diagnoses remain interpretable when it
changes.

### 3.1 What is collected

**Pod identity and placement**: namespace, name, **uid**, node name, phase,
creation timestamp, owner reference chain resolved to the controlling Deployment
or StatefulSet, including its current revision and image.

The owner chain is collected because the remediation almost always targets the
controller, not the pod. Patching a pod that a ReplicaSet will immediately
replace is a non-fix.

**Per-container status**: name, image, imageID, ready, restartCount,
`state.waiting.{reason,message}`, and the full
`lastState.terminated.{exitCode,reason,signal,startedAt,finishedAt,message}`.

`lastState` is the causally interesting half and `state` is the symptom. The
recorded fixtures make this concrete: `state.waiting.reason` was
`CrashLoopBackOff` for both a database connection failure and a memory limit set
below what container init requires. The surface reason does not identify the
failure type.

**Per-container spec**: image, command, args, resource requests and limits,
liveness and readiness probes, env var **names only**.

Env values are never collected. They routinely contain credentials, and the
brain is a separate process that talks to a third-party model API. Names are
usually sufficient to reason about misconfiguration.

**Events**, structured, filtered by `involvedObject.uid`.

**Logs**: previous container first, current as fallback, tail-bounded, captured
at detection time.

**Node conditions** for the scheduling node: Ready, MemoryPressure, DiskPressure,
PIDPressure. Cheap, and occasionally reframes a pod-level problem as a node-level
one.

### 3.2 Why structured fields rather than `describe` text

1. **`describe` has no stability contract.** It is a human-facing renderer whose
   layout changes between kubectl releases. Parsing it is parsing a UI.
2. **The agent uses client-go, not kubectl.** `describe` is not available as a
   library call without vendoring kubectl's printers. Typed access to the same
   underlying objects is the natural path and is compile-time checked.
3. **Signal density.** The recorded `describe` output is around 50 lines, of
   which roughly 6 carry causal signal. The rest is service account name,
   tolerations, and volume projections, constant across every pod in the
   cluster. Shipping it spends context budget and dilutes attention across
   boilerplate that is identical in every incident.
4. **Determinism.** A schema can be validated. Free text cannot.

### 3.3 The tradeoff: deduplicated event counts

`describe` renders events in an information-dense aggregated form:

```
Normal   Pulled   73s (x5 over 2m42s)   Container image "redis:alpine" already present
Warning  BackOff  7s  (x5 over 2m36s)   Back-off restarting failed container
```

The `x5 over 2m42s` aggregation is the difference between a pod that failed once
and a pod that has been flapping for minutes. It is genuinely valuable, and
naively selecting a few jsonpath fields would discard it.

**Decision: the signal is preserved, because it is not actually a rendering
artifact.** kubectl computes that string from fields that exist on the Event
object: `count`, `firstTimestamp`, `lastTimestamp` (and `series.count` /
`series.lastObservedTime` under the newer events API, which the collector must
also handle). The contract carries all of them.

Two deliberate additions:

**The aggregated phrasing is reconstructed for the prompt.** Having preserved the
fields, the brain renders them back into `x5 over 2m42s` form. That phrasing is
compact and heavily represented in training data, which makes it more legible to
the model than three ISO timestamps.

**Rate is computed in the agent, not by the model.** The contract carries a
derived `crashes_per_minute` and `age_seconds`. Asking a language model to
subtract timestamps and divide is a well-known failure mode, and this particular
quantity is the flap-versus-one-off discriminator, so it must not be left to
arithmetic the model performs unreliably.

### 3.4 A recorded correctness trap: events must be filtered by UID

`kubectl get events --field-selector involvedObject.name=<pod>` matches on
**name**, not identity. Verified against the recordings: the crashloop fixture
contains events from 2 distinct pod UIDs and the OOM fixture from 3, because a
pod of the same name was recreated during capture.

A collector that filters by name will silently attribute a previous
incarnation's failures to the current pod, inflating counts and, worse, mixing
in a different root cause entirely. In a Deployment rollout loop this is not an
edge case; it is the normal situation.

**Requirement: filter events by `involvedObject.uid`.** The pod UID is in the
contract for exactly this reason.

### 3.5 Log handling

- Bounded: last 200 lines or 16 KiB, whichever is smaller, tail-anchored,
  because the fatal line is at the end.
- Both streams; container stdout and stderr are already interleaved by the
  runtime.
- Captured at detection time and persisted, per the racy-availability finding
  in 2.3.
- `logs_available` is an explicit boolean in the contract, distinct from
  `logs_empty`. "The container wrote nothing" and "the runtime discarded the
  container" are different facts and drive different confidence ceilings.

### 3.6 Redaction

Applied in the agent, before anything leaves the cluster: env values dropped
entirely; log lines scanned for common secret shapes (bearer tokens, AWS keys,
`postgres://user:pass@`, PEM blocks) and replaced with a typed placeholder.
Redaction is recorded in the contract as a count so the brain knows evidence was
withheld rather than absent.

---

## 4. Hallucination is the primary risk

### 4.1 The actual failure mode

The dangerous failure is not Coroner saying nothing. It is Coroner saying
something specific, plausible, and wrong, a human approving it because it reads
like competence, and the resulting action making a live incident worse.

An approval gate is not by itself a defence. A human approving under time
pressure at 3am, given a confident paragraph and a button, will approve. The
gate only protects if the human is given a real basis on which to reject. Design
that assumes the gate is the safety mechanism has misplaced the safety
mechanism.

Section 2 establishes that this risk is not hypothetical. For roughly a quarter
of CrashLoopBackOff incidents the collected context provably contains no causal
signal, and a language model asked to explain a failure will still produce a
fluent explanation. Exit code 1 with empty logs invites confabulation, because
the honest answer is unsatisfying and the model is not optimised for honesty
about its own ignorance.

### 4.2 Countermeasures

Ordered from strongest to weakest. The first three are deterministic code that
runs after the model returns; they do not depend on the model's cooperation.

**1. Insufficient context is a first-class terminal output.**

The LangGraph state machine has a terminal node `INSUFFICIENT_CONTEXT`
alongside `DIAGNOSED`. It is a success, not an error path, and it is reported as
such in the metrics rather than hidden. Its Slack message lists what was
collected, states plainly that the cause is not determinable from it, and names
the specific missing evidence ("previous container logs were unretrievable").
That message is more useful to an on-call engineer than a guess, because it
tells them where to look next.

Entry into this node is partly deterministic: a CrashLoopBackOff with
`logs_available=false` and a generic exit code routes here **before** the model
is asked to diagnose. There is no reason to spend a model call on a context we
already know is empty, and no reason to give it the opportunity to fill the void.

**2. Every claim must cite a collected field, checked mechanically.**

Structured output requires an `evidence` array of `{source, field, value}`. After
generation, a validator confirms that every cited field path exists in the
contract that was actually sent, and that every quoted string appears verbatim
as a substring of the collected text. A diagnosis with an empty evidence array,
a path not in the contract, or a quoted log line not present in the logs is
**rejected in code and never reaches Slack**. It is retried once, then routed to
`INSUFFICIENT_CONTEXT`.

This is the highest-value control because it targets the specific mechanism of
the failure. Invented stack traces and misremembered error strings do not
survive a substring check.

**3. Confidence is capped by evidence class, not self-reported.**

A model's own confidence estimate is not trustworthy, particularly when it is
wrong. Confidence is therefore a deterministic ceiling computed from the
evidence, with the model's estimate only permitted to lower it:

```
final = min(model_confidence, ceiling(failure_type, evidence))
```

Indicative ceilings, to be recalibrated against section 5 data:

| Situation | Ceiling |
| --- | --- |
| ImagePullBackOff with registry error in event | 0.95 |
| OOMKilled, reason and limits present | 0.90 for "what", 0.50 for "why" |
| CrashLoopBackOff, previous logs contain a fatal line | 0.80 |
| CrashLoopBackOff, previous logs present, no clear error | 0.40 |
| CrashLoopBackOff, logs unavailable | 0.15, routed to abstention |

These ceilings encode section 2's predictions directly, which means section 5's
measurements test the ceilings and the predictions together.

**4. Abstention threshold gates the action, not just the wording.**

Below 0.5, the Slack message renders observations and candidate hypotheses with
**no approve button at all**. Not a greyed-out button, not a warning: the
affordance is absent. A low-confidence diagnosis cannot be approved by reflex
because there is nothing to click.

**5. Observed and inferred are visually separated.**

The Slack message has two blocks: **Observed**, verbatim collected facts with no
model involvement, and **Inferred**, model output. The human can always evaluate
the proposal against raw facts without trusting the narrative. In the empty-logs
case the Observed block is short and obviously thin, which communicates the
weakness of the diagnosis better than any confidence number.

**6. Adversarial second pass.**

A separate node receives the same evidence and the proposed diagnosis and is
asked to argue it is wrong and to name a competing hypothesis of equal support.
If it finds one, confidence is reduced and both are shown. This catches the
plausible-but-underdetermined middle case, which is the largest hallucination
bucket per section 2.

**7. Temperature 0 for diagnosis and validation.**

Determinism aids reproducibility and debugging. It does not reduce
hallucination, and is listed last so it is not mistaken for a control.

### 4.3 What is deliberately not relied upon

Prompt instructions such as "do not speculate" are not a control. They are worth
including and they are worth nothing under adversarial conditions. Every
mechanism above except 6 and 7 is enforced outside the model.

---

## 5. Measuring diagnosis accuracy

Accuracy measurement is a design requirement, not a later milestone. A system
that proposes root causes without measuring whether they are correct is not
trustworthy at any scale, and the instrumentation must exist before the first
diagnosis is served or the early incidents, which are the most informative, are
lost.

### 5.1 The ledger

Every diagnosis is persisted **before** the Slack post, never after, so that a
delivery failure cannot lose the record:

| Field | Purpose |
| --- | --- |
| `incident_id` | join key |
| `failure_type` | classified type, per 2.4 |
| `contract_version`, `context_hash` | what evidence existed |
| `evidence_class` | which confidence ceiling applied |
| `model_id`, `prompt_version` | what produced it |
| `diagnosis`, `evidence[]` | the claim and its citations |
| `confidence_model`, `confidence_final` | before and after the ceiling |
| `proposed_action` | the fix offered |
| `abstained` | whether it reached `INSUFFICIENT_CONTEXT` |
| `actual_cause` | the true cause, recorded by whoever resolved the incident |

### 5.2 Ground truth

The human's Slack decision is the label, written back to the same record:
`approved`, `rejected`, `edited`, or `expired`.

`edited` and `expired` are tracked separately rather than folded into the binary.
An edit means the diagnosis was directionally right but wrong in detail, which is
a different signal from a rejection. An expiry usually means the message was not
useful enough to act on, which is its own kind of failure.

Rejection prompts for a one-line reason. This is the highest-value and most
perishable signal in the system; it is worth the small friction at the moment
the human already has the context loaded.

**A second, stronger label.** Approval measures whether a human found the
diagnosis convincing, which is not the same as correct, and a confident wrong
answer is precisely the thing that gets approved. Where an approved action is
executed, Coroner records whether the workload reached Ready within 10 minutes
and stayed there for 30. Divergence between approval rate and resolution rate is
the direct measurement of how often Coroner is persuasive but wrong, which is the
number section 4 exists to keep small.

### 5.3 Reported metrics

Reported **per failure type, never as a single aggregate**. An aggregate would
let ImagePullBackOff's near-deterministic accuracy carry the mean while
CrashLoopBackOff is broken, which is exactly the failure this section exists to
detect.

- approval rate
- resolution rate (the stronger label)
- abstention rate, and the share of abstentions that were correct to abstain
- calibration: predicted confidence bucketed against observed approval, since a
  ceiling that never matches outcomes is miscalibrated and must move
- contradiction rate from the adversarial pass

**How an abstention gets labelled.** "The share of abstentions that were correct
to abstain" is not measurable from Coroner's own output: an abstention produces
no claim to be right or wrong about. The label has to come from outside.

When Coroner abstains, the Slack message asks the human who resolves the
incident to record the actual cause in one line, and that text is written to the
record's `actual_cause` field. An abstention is then scored correct if the
recorded cause is not derivable from the evidence Coroner held, and incorrect if
it is. The second case is the one worth finding: it means the cause *was* in the
context and Coroner failed to see it, which is a reasoning or ceiling defect
rather than a genuine evidence gap, and it is invisible without this label.

The same one-line prompt is asked on rejection, where it explains what the
diagnosis got wrong.

**This produces a second asset.** Accumulated `actual_cause` entries are a small
corpus of real root causes paired with the exact evidence available at the time.
That corpus is the only honest way to evaluate a proposed change to the context
contract: a candidate contract can be scored by asking how many previously
undiagnosable incidents it would have made diagnosable, offline, without waiting
for new incidents or re-running a model against production. Section 2.4's
falsification criterion for CrashLoopBackOff, that a sub-40 percent hit rate
indicts the contract rather than the prompt, is only actionable because this
corpus exists to test the replacement against.

### 5.4 Storage

SQLite, brain-side, schema-versioned, append-only. No record is deleted or
mutated after its label is written. Consistent with section 1: this is an
incident ledger, not a metrics platform, and it exists to answer one question,
whether Coroner is right, not to be queried generally.

### 5.5 Promotion rule

A failure type stays in shadow mode, where Coroner posts diagnoses but offers no
approve button, until it clears its section 2.4 prediction over at least 20
incidents. This makes section 2's committed predictions load-bearing rather than
decorative: they are the gate on shipping.

---

## 6. Decisions made without input

No entries yet. This section records choices made unilaterally during
implementation that a reviewer might reasonably have wanted to make themselves,
so they are visible rather than buried in commit history.
