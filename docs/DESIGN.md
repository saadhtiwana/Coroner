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

Section 7 adds a second constraint of the same weight: the system must be
evaluable by a stranger in under five minutes.

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
| OOMKilled during init (StartError, exit 128), runtime message or a pod event names memory | 0.80 |
| OOMKilled during init, nothing in the evidence names memory | 0.15, routed to abstention |
| CrashLoopBackOff, previous logs contain a fatal line | 0.80 |
| CrashLoopBackOff, previous logs present, no clear error | 0.40 |
| CrashLoopBackOff, logs unavailable | 0.15, routed to abstention |

The two init rows were added after Phase 3 (section 6.9). Before them the
StartError shape fell through to the running OOM row and a diagnosis resting on
one runtime string and no logs was capped at the same 0.90 as a kernel verdict.

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
| `shadow_rating` | in shadow mode, whether the human would have approved |

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

**The bootstrap problem.** As stated, that rule cannot be satisfied. Promotion
requires 20 labelled incidents, the label in section 5.2 is the approve or
reject decision, and shadow mode renders no approve button. A failure type in
shadow mode would therefore accumulate zero labels and remain in shadow mode
permanently, which is not a conservative default but a deadlock.

**Resolution: rating and approval are separate acts.** In shadow mode the
message carries a rating control asking "would you have approved this?", with
answers `would_approve`, `would_reject`, and `unsure`, written to
`shadow_rating`. It authorises nothing. Nothing executes. It is a judgement
about the diagnosis, recorded at the moment the human has the incident in front
of them and can cheaply say whether Coroner was right.

That yields the label without granting the action, so promotion is reachable
while the safety property, that no mutation occurs without a real approval keyed
to a diagnosis, is untouched.

Two consequences worth stating:

**Rating and approval are stored in different fields and never merged.** A
rating is cheap and hypothetical; an approval carries consequence and is made
under different incentives. A human who says they would have approved has not
borne the risk of being wrong, so ratings are expected to run optimistic
relative to real approvals. Collapsing them into one column would hide exactly
that bias. After promotion, the gap between a class's shadow rating rate and its
subsequent approval rate is itself a useful measurement, and it is only
available because the two were kept apart.

**Abstentions are rated too.** In shadow mode an `INSUFFICIENT_CONTEXT` outcome
still asks whether abstaining was the right call, which is the same signal
described in 5.3 and feeds the same metric.

---

## 6. Decisions made without input

Choices made unilaterally that a reviewer might reasonably have wanted to make
themselves, recorded here so they are visible rather than buried in commit
history. All entries below are ratified and implemented.

### 6.1 The three MVP failure types

**Decision:** CrashLoopBackOff, ImagePullBackOff, and OOMKilled.

Chosen without being specified. They were picked to span the recoverability
range rather than to be the three most common: one class where the cause is
fully present in the structured context, one where the fact is certain but the
cause is not recoverable from a single snapshot, and one bounded entirely by
application log quality. A set chosen purely by frequency would likely have
included Pending or unschedulable and would have made section 2's honest
assessment less informative, because the classes would not have differed in the
dimension that matters.

**Ratified as implemented.**

### 6.2 Keeping the 64Mi StartError capture as a fourth fixture

**Decision:** `fixtures/oom-startError/` is retained as a distinct incident,
making four fixtures for three failure types.

It was produced by accident while trying to record a canonical OOMKilled: at a
64Mi limit the container was killed during runtime init rather than during
execution, yielding `StartError` with exit code 128 and surfacing as
`CrashLoopBackOff`, instead of `OOMKilled` with 137. The obvious move was to
discard it as a bad capture. It was kept because it is the only concrete proof
that a classifier keyed on exit code 137 misses a real and reachable OOM case,
and because it demonstrates that the surface `waiting.reason` does not identify
the failure type. It is now the evidence behind that claim in section 2.2.

**Ratified as implemented.**

### 6.3 SQLite for the accuracy ledger

**Decision:** the section 5 ledger is SQLite, brain-side, schema-versioned and
append-only.

No storage was specified. SQLite was chosen because the ledger is small,
single-writer, and read almost exclusively by one process, and because a file
that can be copied and inspected with standard tools keeps the ledger honest in
a way a hosted service does not. It is also consistent with section 1's refusal
to become an observability platform: reaching for Postgres or a time-series
store would invite the ledger to grow into general queryability, which is
explicitly not its job.

**Ratified as implemented.**

### 6.4 Implementation order

**Decision:** ImagePullBackOff first, then CrashLoopBackOff, then OOMKilled.

The first choice was made unilaterally; the ordering of the remaining two was
raised in review and confirmed. The rationale, including the risk that a
pipeline validated only on a near-deterministic class hardens invisible
assumptions, is in section 2.5.

**Ratified as implemented.**

### 6.5 Read-only RBAC until an execution path exists

**Decision:** `deploy/manifests/rbac.yaml` grants only get, list, and watch. No
write verbs exist anywhere in the deployed configuration.

The design requires approval before mutation, and it would have been reasonable
to define the full permission set now and rely on the approval gate to police
it. Granting nothing instead makes an unapproved mutation impossible rather than
merely disallowed, which is a stronger guarantee than any code path can offer,
and it means a defect in the approval logic during development cannot damage a
cluster. Write verbs are added in the phase that implements approval-gated
execution, not before.

**Ratified as implemented.**

### 6.6 Resolving the Phase 0 and Phase 1 instruction conflict

**Decision:** Phase 1's scaffold instruction was treated as superseding Phase
0's standing prohibitions, so the project directory was created and `git init`
was run.

Phase 0 ended with an explicit instruction not to create the project directory
and not to run `git init`. Phase 1 then required a Go module, a Python package,
fixtures, and per-commit history, none of which are possible without both. The
prohibitions were read as scoped to Phase 0, where the task was diagnostics
only, rather than as standing constraints.

A related conflict was resolved the other way. Phase 1 asked for the kind config
at `deploy/kind-cluster.yaml` while the same message repeated the prohibition on
creating the project directory. At that point nothing else required the
directory, so the narrower reading was available and was taken: the file was
staged outside the repository and the prohibition was honoured until Phase 1
proper. The general rule applied in both cases is that an explicit instruction
to produce something overrides an earlier prohibition only when the instruction
cannot otherwise be satisfied.

One structural consequence: the root commit, `bf67cb9`, which installs the
commit-msg hook, is on `main` rather than on `chore/scaffold`. A branch requires
a merge base and empty commits are prohibited, so the first commit could not
itself sit on the phase branch.

**Ratified as implemented.**
### 6.7 Groq for the reasoning model, reached through an OpenAI-compatible client

**Decision:** the brain calls `openai/gpt-oss-120b` on Groq, through the
OpenAI-compatible client rather than a bespoke one.

This entry supersedes an earlier one recorded in the same section. When a Gemini
key was offered, the decision was Anthropic, on three grounds: the evidence
validator in section 4.2 depends on reliable structured output and is not where
an unexercised SDK belongs; an aggressively rate-limited free tier would truncate
the 20-incident promotion evaluation in section 5.5 and corrupt the ledger; and
`model_id` is recorded per diagnosis so a later switch stays cheap **provided it
is logged rather than defaulted into**. That decision was then reversed by
direction in favour of Groq. It is recorded here rather than edited away, because
a decision log that quietly replaces its own entries is not a log.

Two of the three original reasons survive the reversal and are not resolved by
it. Groq's free tier is rate limited, so section 5.5's evaluation must be run in
a way that tolerates interruption, and a truncated evaluation must be discarded
rather than averaged. Structured output was the other concern, and it turned out
to be well founded in a specific way: Groq's strict mode rejects the schema
Pydantic generates by default, requiring `additionalProperties: false` on every
nested object including those under `$defs`, and every property listed in
`required`. That is handled once in `schema.py` rather than discovered on a live
incident.

The third reason is what makes the reversal acceptable. `model_id` and
`prompt_version` are columns on every ledger row, so diagnoses produced by either
provider remain attributable and comparable, and a future switch back costs
nothing in history.

**Model choice.** Of the models Groq serves, most are not candidates at all:
Whisper is speech recognition, Orpheus is text to speech, Prompt-Guard and
gpt-oss-safeguard are safety classifiers, and Allam is Arabic-specific. That
leaves `gpt-oss-120b`, `gpt-oss-20b`, `qwen3.8-27b`, and `groq/compound`.
`groq/compound` is an agentic system with built-in tool use, which is the wrong
shape for a deterministic single-shot structured call and introduces
nondeterminism the validator would have to fight. `gpt-oss-120b` is the largest
of the remainder and has the most headroom for a six-field schema containing a
nested citation array.

**Reliability is not assumed.** The requirement was a model whose JSON is
parseable under load, and that has not yet been measured across enough calls to
claim. The pipeline is therefore built so it does not depend on the claim:
unparseable output is handled on exactly the same path as a failed citation
check, one retry then `INSUFFICIENT_CONTEXT`. A model that returns malformed JSON
degrades to abstention, never to a wrong diagnosis reaching a human. Measured
parse-failure rates belong in the section 5 ledger once there are enough
incidents to compute them.

The key lives in `brain/.env`, which is gitignored, with `brain/.env.example`
carrying the variable names and no values.

**Ratified as implemented.**

### 6.8 Rebuilding the Phase 2 branch after a failed isolation check

**Decision:** the Phase 2 branch was reset with `git reset --mixed main` and
recommitted, rather than repaired with follow-up commits.

The per-commit isolation check found two defects. `go.mod` and `go.sum` were
never staged, so the commit introducing the informer imports could not build on
its own. The Go and Python halves of the contract changed in different commits,
and because a test compares the two declarations directly, every commit between
them failed.

Nothing had been pushed. The standing rule against rewriting history protects
published history, and a local reset before any push is not what it guards
against. The alternative, leaving permanently broken commits and stacking
fix-ups on top, would have made the isolation guarantee false forever. All six
rebuilt commits verify in isolation.

The isolation check runs per commit from here on, before any merge.

**Ratified as implemented.**

### 6.9 An explicit ceiling for the init OOM shape

**Decision:** `OOMKilledDuringInit` has its own evidence classes and ceilings,
0.80 when the runtime message or a pod event names memory and 0.15 otherwise,
instead of sharing the running OOM's 0.90.

Found by re-running the recorded 2Mi incident: it scored 0.90 with seven valid
citations on a contract whose logs were retrieved and empty. Every citation
checked out and the diagnosis was correct. The ceiling was still wrong, because
it was not earned by the evidence class; there was no row for StartError with
exit 128, so `classify_evidence` filed it with `OOMKilled` and the running OOM
ceiling applied by default. A ceiling that is reached by fall-through is not a
ceiling.

The value is 0.80 rather than 0.90 for a reason that is specific to the
evidence, not to the diagnosis. A running OOM carries the kernel's verdict in
a structured field, `lastState.terminated.reason = OOMKilled`, and usually a
log that stops mid-stream. An init kill carries neither. Its entire causal
signal is one free-text runtime message, and section 6.10 records that the
runtime does not word that message consistently. A diagnosis resting on a
single unstable string sits below one resting on a kernel verdict, and level
with one resting on an application's own fatal log line. It is not lower than
0.80 because when the string is present it is verbatim and unambiguous, in the
same way a registry response is. Re-run under the new row: model 0.95, final
0.80, seven citations, approvable. The section 5 calibration data is what
decides whether 0.80 is right; this is the first value with a stated reason.

The second row exists for the case where the agent classified an init kill but
nothing in the contract names memory. That can only happen if the agent and the
brain disagree about what counts as a marker, which they must not, so it is a
guard rather than a path. It abstains before any model call.

**Ratified as implemented.**

### 6.10 The agent corroborates an init kill from the pod's own events

**Decision:** when the latest termination is an init failure (`StartError` or
`ContainerCannotRun`) and any event for the same pod UID names memory, the agent
classifies `OOMKilledDuringInit` under the rule `event-message-names-oom`. The
refinement is narrow: a container that ran and exited is never reclassified by
an old memory event.

Recorded 2026-09-06 on the same 2Mi container across consecutive restarts. The
runtime reported `container init was OOM-killed (memory limit too low?)` for
some kills and `error during container init: procReady not received` for
others, with no change to the workload. A classifier reading only the latest
termination message filed the second wording as an ordinary CrashLoopBackOff
with exit 128 and empty logs, and the brain's gate abstained on it. The pod's
event history, filtered by UID so it belongs to this incarnation and no other,
retained the earlier wording.

This is also the first organic abstention. The recording is kept as
`brain/tests/contracts/starterror-procready.json` and the gate is required to
abstain on it without a model call. Section 8 records why one is not enough.

Two consequences. The incident id is now derived after events are collected,
because it includes the failure type. Watch-mode deduplication still keys on
the classification made before collection, so the seen-set and the contract
can name different types for the same occurrence; this is recorded in section
8 rather than fixed, because fixing it means collecting logs on every informer
update.

**Ratified as implemented.**

### 6.11 The prompt says what confidence means

**Decision:** the prompt defines `confidence` as confidence that `root_cause`
is correct as stated, and says that an open question downstream of a proven
fact belongs in `competing_hypothesis`, not in a lowered score. Prompt version
2.

The second Phase 3 question was whether ImagePullBackOff at 0.6 against a 0.95
ceiling meant the model was underconfident or the section 2.4 prediction was
wrong. Re-run twice on the recorded contract: 0.7, then 0.9 under the new
prompt, and 0.95 on a fresh live capture. The diagnosis text was correct each
time, named the 403, and presented both branches. The prediction was not wrong;
the model was scoring the branch question it had been told it could not
answer, because nothing had told it which question the number was for. The fix
is the prompt, which is what section 2.4 said it would be, and the falsification
criterion is about accuracy over incidents, which the section 5 harness measures,
not about a self-reported score on one.

Section 4.3 still applies. The prompt makes the model's number more useful; the
ceiling is what makes it safe.

**Ratified as implemented.**

### 6.12 The validator compares citations as the model saw them

**Decision:** cited values are compared against the JSON rendering of the
contract field, and a citation path split across `source` and `field` is
joined before resolution. The value check is unchanged.

Two rejections recorded live on correct image pull diagnoses. A bare pod's
contract said `"owner": null`; the model cited `owner` with the value `null`;
Python rendered `None` as an empty string and rejected it, twice, and a correct
diagnosis went to `INSUFFICIENT_CONTEXT`. Separately the model wrote
`source: events[2], field: message`, which is the schema's own path with the
prefix in the wrong box, and was rejected for a field that did not exist. Both
are the validator rejecting packaging rather than fabrication. Neither change
accepts a value the contract does not hold: the null owner is compared against
`null`, and the joined path still has to resolve and still has to match.

**Ratified as implemented.**

### 6.13 The brain owns the sinks and resumes by state, not by checkpoint

**Decision:** output sinks, the approval endpoints, the in-flight store, and
the expiry sweeper live in the brain. The agent posts a contract, logs the
verdict, and renders nothing. Resumption after a decision is done by loading
the parked state from the store and running a second graph, not by a
LangGraph checkpointer.

Section 7.2 says the approval gate is a property of the agent, which holds
the credentials, and not of the transport. That is preserved: the agent will
not execute an action whose token it cannot verify, and the token is minted
only by the decision path. What the agent does not do is talk to Slack or
listen for webhooks. It holds cluster credentials, and the process that holds
cluster credentials should not also be the process reachable from the
internet. The brain holds no cluster credentials and is the natural place for
an inbound webhook.

Resumption by state rather than checkpoint: the parked state is a few hundred
bytes of JSON that the ledger already understands, and a checkpoint would be
an opaque blob beside it. The Redis checkpointer for LangGraph also depends
on Redis modules that `redis:alpine` does not ship, and the demo runs on
`redis:alpine`. The second graph has three nodes, load, check, record, and
every refusal, unknown incident, already decided, window expired, missing
reason, missing edit, is a state the graph reaches rather than an exception
thrown around it.

Without a Redis URL the in-flight store is process memory. That is enough to
run on a laptop with one environment variable, which section 7 requires, and
it is logged at startup as the limitation it is.

**Ratified as implemented.**

### 6.14 Every failure type starts in shadow mode

**Decision:** `CORONER_PROMOTED_TYPES` is empty by default, so no verdict
offers approval until a type is promoted by hand. In shadow mode the message
asks "would you have approved this" and offers nothing to approve.

Section 5.5 says a type stays in shadow until it clears its section 2.4
prediction over 20 incidents, and no type has any incidents yet. A default
that offered approval before the first measurement would make the promotion
rule decorative. The cost is that a stranger running the demo sees ratings
rather than approve buttons unless they promote a type, and the demo will
promote ImagePullBackOff explicitly and say so, since that is the class whose
correctness is externally verifiable per section 2.5.

**Ratified as implemented.**

### 6.15 The ledger row carries the contract

**Decision:** schema 3 stores the evidence contract, as diagnosed, on every
ledger row.

Section 5.3 describes the ledger as a corpus of real outcomes paired with the
exact evidence available at the time, and says that corpus is the only honest
way to score a proposed change to the context contract offline. Until schema
3 the row held the verdict and its citations but not the evidence, so the
corpus described in 5.3 did not exist. It does now. The cost is a few
kilobytes per row, mostly log tail, in a file that section 5.4 already says
is small and single-writer.

The same column is what lets the Slack message be redrawn after a decision
from the row alone, with observed facts intact and buttons removed, without
the sink keeping any state of its own.

**Ratified as implemented.**

### 6.16 confidence_model is not a measurement; calibration is against the ceiling

**Decision:** the calibration metric in section 5.3 is computed against the
deterministic ceiling, which is the evidence class, and not against the
model's self-reported confidence. `confidence_model` stays in the ledger as
a recorded observation and is not used to gate anything, to bucket anything,
or to report anything as a measurement. The README must say so.

The question was whether a recorded spread of 0.6, 0.7, and 0.9 on one
contract meant the number was noise. It was measured on 2026-09-06 by running
each of the four recorded contracts ten times at temperature 0 with the
prompt unchanged (version 2). Nine of the forty calls were refused by the
provider's rate limit and are excluded below; section 8 records them.

| Contract | Runs | Mean | Standard deviation | Range | Ceiling | Final equal to ceiling |
| --- | --- | --- | --- | --- | --- | --- |
| CrashLoopBackOff, fatal log line | 8 | 0.90 | 0.000 | 0.90 to 0.90 | 0.80 | 8 of 8 |
| ImagePullBackOff, 403 in event | 8 | 0.95 | 0.003 | 0.95 to 0.96 | 0.95 | 8 of 8 |
| OOMKilled, 128Mi, log tail | 7 | 0.92 | 0.020 | 0.90 to 0.95 | 0.90 | 7 of 7 |
| OOMKilled during init, 2Mi | 8 | 0.94 | 0.023 | 0.90 to 0.96 | 0.80 | 8 of 8 |

The 0.6, 0.7, 0.9 sequence was not identical input under one prompt. The
0.6 and 0.7 were prompt version 1 on two different days; the 0.9 was prompt
version 2 after a validation retry, whose prompt tells the model to lower its
confidence. Under one prompt the run-to-run spread is at most 0.06 and the
standard deviation at most 0.023. The larger movement in the number comes from
the retry path, not from sampling: on the OOMKilled contract five of seven
successful runs needed a second attempt, and the second attempt reported 0.90
to 0.92 where the first reported 0.93 to 0.96.

What the number actually is: a value between 0.90 and 0.96 that the model
reports for every one of the four classes, at or above every ceiling. Across
31 successful runs `final` equalled the ceiling 31 times. The model's number
never lowered a final confidence, so the ceiling is not the cap on the
measurement; it is the measurement. That is the first of the two cases the
question posed, and the answer is that `confidence_model` is decorative in
practice.

The diagnosis itself was stable. For every contract the root cause named the
same thing in every run: the refused Postgres connection, the registry's 403
on an anonymous token, the 128Mi limit exceeded, the 2Mi limit too low for
init. Wording varied; the claim did not. Confidence variance and diagnosis
variance are different problems, and the second one did not occur.

Not done: taking the median of N samples. It would cost N model calls per
incident against a provider that allows about one call a minute at this
prompt size, to stabilise a number the ceiling overrides. Dropping the
model's number entirely was also not done, because it costs nothing to
record and section 5 data may one day show it carrying information below
the ceiling; if it ever does, that will be visible in the ledger.

**Ratified as implemented.**

### 6.17 What the agent will and will not execute

**Decision:** an approved action is executed only when it reduces to one of
two patches on the owning workload's pod template: raise a memory limit, or
set an image. Everything else is emitted as a manual plan that says why it
will not run. A bare pod is never touched. Execution is off by default and
needs `--execute` plus `deploy/manifests/rbac-write.yaml`, which grants
`patch` on deployments, statefulsets, and daemonsets and nothing else.

The brain proposes in prose and the human approves prose. Turning prose into
a mutation is where a remediation system does damage, so the mapping is
deliberately narrow and deterministic: a memory quantity in the approved
text becomes the new limit, otherwise the limit doubles with a floor; an
image reference in the text that differs from the failing one becomes the
new image, otherwise there is nothing to set and the plan says so. A
CrashLoopBackOff is always manual, because the cause is inside the
application and the agent will not restart or patch a workload whose fix it
cannot state. A bare pod is always manual, because its resources and image
are immutable and the only remediation is recreation, which is a decision the
agent does not make.

The token is verified before anything else is read. Its context hash is the
brain's hash of the evidence, and the agent cannot recompute it from its own
serialisation, so when this process sent the contract it holds the approval
to the hash it received with the verdict, and after a restart it holds it to
the token alone, which the brain's decision path minted with the shared
secret over that same hash. The plan is built from the contract stored on
the ledger row, so the agent needs no memory of its own to act correctly,
only the secret.

Resolution is the controller's status, not a pod's: every desired replica
Ready on the current generation within ten minutes, and every observation
Ready for thirty more. A workload that flaps has not resolved. This is the
section 5.2 label, and it is only recorded after an executed action, because
a workload that recovered on its own says nothing about the action.

**Ratified as implemented.**

### 6.18 Cost is recorded per row, prices are configuration, and the evaluation spans daily windows

**Decision:** every ledger row carries prompt tokens, completion tokens, a
cost in US dollars, and total latency. The cost is tokens multiplied by
`CORONER_PRICE_INPUT_PER_M` and `CORONER_PRICE_OUTPUT_PER_M`, which default
to the published on-demand price of `openai/gpt-oss-120b` on Groq on
2026-09-06, 0.15 and 0.60 dollars per million. They are configuration and
are stated as such wherever the cost is reported: change the model and the
numbers must change with it, or the column is fiction.

Abstention cost is reported separately from diagnosis cost, and cost per
correct diagnosis beside cost per diagnosis. An abstention at the evidence
gate made no model call and records zero tokens. Whether that makes the
safety mechanism also the cheapest path is a measurement the section 5
harness makes, not a claim this section makes.

**The token budget.** The provider's on-demand tier allows 200000 tokens a
day on this model. The section 6.16 stability run spent most of one day's
budget, and the first live Step 3 pass on 2026-09-06 saw every diagnosis
DISCARDED with "try again in 24m". A 69-incident evaluation at about 5000
tokens each, with retries, does not fit in one window on any pacing. Three
choices were available: wait and run across windows, run on a second model
with its own quota, or change tier. The tier is a billing decision and not
mine. A second model would make the evaluation measure a model section 6.7
did not choose. So the evaluation keeps the chosen model and runs across
daily windows: contracts are captured once, at detection time, and the
diagnose phase is resumable, retrying a rate-limited row after the wait the
provider names and leaving every other discard as it is. The contract is
also now sent as compact JSON, prompt version 3, which removes about a fifth
of the characters; what that is worth in tokens is read from the ledger, not
assumed.

**Ratified as implemented.**

### 6.19 Tracing explains a decision, and works with no backend

**Decision:** OpenTelemetry spans cover the whole path, agent collection, the
HTTP call, each graph node, the model call, validation, the sink, and an
approval. The console exporter is the default in both services, so tracing
works with nothing installed; OTLP is one environment variable. Spans go to
stderr, so stdout still carries only contracts and rendered verdicts.

Timing alone would not have been worth the dependency. What makes it worth it
is that the spans carry the facts that decided the outcome: the evidence
class and the ceiling it implies, whether the gate abstained before any model
call, the token counts, whether validation retried and which citation failed,
the final confidence, and whether an approve affordance was offered. Reading
one incident's trace answers "why did Coroner say that" without opening the
ledger, which is the question this project exists to make answerable. The
tests assert those attributes rather than the shape of the trace, because a
trace that claims to explain a decision is worth testing.

Two consequences worth stating. The agent injects the trace context on the
call to the brain, so one trace covers collection in the cluster through to
delivery in a sink rather than two disconnected halves. And the brain's
tracing module keeps its own provider reference as well as installing the
global one, because the OpenTelemetry global refuses replacement once set and
a test needs an in-memory exporter.

**Ratified as implemented.**

---

## 7. Evaluability

**Coroner must be evaluable in under five minutes by someone who has never seen
it.** This is a scope requirement with the same standing as the safety
invariant in section 1, not a packaging nicety deferred to the end.

### 7.1 The rationale

The failure mode for a project like this is not that the code is bad. It is that
nobody gets far enough to see it run.

An incident-response agent is unusually exposed to that failure. It cannot be
demonstrated with a screenshot or a code snippet, because the interesting
behaviour only appears when a real workload breaks in a real cluster. Every
additional step between a stranger and that moment, a cluster to provision, an
image to build, a manifest to edit, a Slack app to register, multiplies the
number of people who conclude it is not worth the effort and never form a view
of whether the reasoning is any good.

That cost is asymmetric. A reviewer who abandons setup does not report a neutral
result; they report nothing, and the project reads as unfinished. The work in
sections 2 through 5 is only worth doing if someone reaches it.

### 7.2 Requirements, all due before Phase 7

**`make demo` is the whole product.** One command creates a kind cluster,
deploys Coroner, breaks one pod per failure type, and prints the diagnoses. The
only required configuration is a single API key environment variable. No Slack,
no manifests to edit, no image build.

**Output sinks are pluggable, and stdout is the default.** Slack is opt-in
through configuration and is never a prerequisite for seeing the system work.
This is the single most important item on the list: registering a Slack app,
scoping a bot token, and wiring interactivity takes longer than everything else
here combined, and putting it in the critical path would mean nobody sees a
diagnosis without first doing the least interesting work in the project.

The architectural consequence is that Slack is one implementation of an output
interface rather than the output path. The approval gate is a property of the
agent, which holds the credentials, not of the transport that carries the
question, so a stdout sink can present the same gate without weakening the
invariant in section 1.

**Images are published to ghcr.io by CI.** Nobody builds anything to try it. The
local side-load path in the Makefile stays, because it is how development
iterates without a registry round-trip, but it is not on the path a stranger
takes.

**A single-file install manifest, applied by URL.** For someone who wants
Coroner in a cluster they already have, without cloning the repository.

### 7.3 What this rules out

Configuration that must be edited before first run. A required secret beyond the
one model key. A build step. A demo that assumes an existing cluster, or that
leaves one behind without a documented teardown. Any path where the first
observable output is a stack trace from missing configuration rather than a
diagnosis.

---

## 8. Open items

Things known to be unresolved. Each names what would resolve it.

**One organic abstention is not evidence the gate works.** Across the four
recorded incidents every `INSUFFICIENT_CONTEXT` was constructed by stripping
logs from a contract that had them. The first organic case arrived on
2026-09-06 (section 6.10), and it is one case. The gate has not been exercised
on a CrashLoopBackOff whose logs are present but name nothing, which is the
middle row of the section 2.3 table and the largest hallucination bucket.
Resolved by the section 5 harness, which must include that row.

**The running OOM ceiling collapses "what" and "why".** The table in 4.2 gives
`OOMKilled` two numbers, 0.90 for the fact and 0.50 for the cause, and the code
applies one, 0.90, to a diagnosis that carries a proposed action. The action
rests on the cause. Recorded live: a 128Mi runtime OOM was diagnosed at 0.90,
approvable, with a proposed action to raise the limit and a competing
hypothesis saying the evidence could not rule out a leak. That is the shape
section 2.2 said Coroner should decline to choose in. Not changed yet because
the alternative, capping at 0.50, lands every running OOM exactly on the
approval threshold and makes the outcome depend on a comparison operator.
Resolved by section 5 calibration data: if approved OOM actions resolve at a
rate closer to 0.50 than 0.90, the ceiling moves to 0.50 and the threshold
comparison is settled deliberately.

**Watch-mode deduplication keys on the pre-collection classification.** After
section 6.10 the contract's failure type can differ from the type the seen-set
recorded for the same occurrence. Harmless today, since the id only gates
re-emission, but it means an incident id in the agent's log can differ from the
one in the contract. Resolved by deduplicating on the emitted contract, at the
cost of collecting on every informer update, or by a cheaper pre-collection
event lookup.

**Model latency was not bounded; now it is, and the limit is known.** One
image pull call on 2026-09-06 took 999 seconds of wall clock including client
retries, against a 90 second per-request timeout. The pipeline now carries a
180 second deadline over the whole diagnose-and-validate loop and records a
call that misses it as `DISCARDED`, a third terminal outcome that is neither a
diagnosis nor an abstention, excluded from accuracy and counted. The cause is
also now measured: the provider's on-demand tier allows 8000 tokens per
minute, and one contract prompt is about 5000 tokens, so the sustainable rate
is one diagnosis a minute. In the section 6.16 run nine of forty calls were
refused with 429 once the ten-run loop outpaced that. Retry-after is honoured
inside the deadline. Resolved for the section 5 harness by pacing at the
provider's rate and re-running discarded incidents after the window; a run
that is cut short is discarded, not averaged.

**Validation retries are not recorded in detail.** Five of seven successful
OOMKilled runs in 6.16 needed a second attempt, and the ledger keeps only the
retry count and the final failure list, which is empty on success. Why the
first attempt failed is not known. Resolved by keeping the first attempt's
failures in the ledger row; the harness needs them for the parse-failure rate
in section 6.7 anyway.

**Temperature 0 is not reproducible across runs, and the number it produces
is not a measurement.** Measured in section 6.16: standard deviation at most
0.023 under one prompt, and the model's number never fell below a ceiling in
31 runs. Section 4.2 control 7 already says temperature does not reduce
hallucination; it also does not deliver determinism on this provider. The
ledger records `model_id` and `prompt_version` per row, so the variance stays
measurable. Resolved by 6.16: calibration is measured against the ceiling.
