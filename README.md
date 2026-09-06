# Coroner

A pod died. Coroner works out why, shows you the evidence, and asks before it touches anything.

[![ci](https://github.com/saadhtiwana/Coroner/actions/workflows/ci.yml/badge.svg)](https://github.com/saadhtiwana/Coroner/actions/workflows/ci.yml)

It handles four failure shapes: CrashLoopBackOff, ImagePullBackOff, OOMKilled, and the awkward one where the memory limit is so low the container runtime dies before your code runs.

The uncomfortable part, and the reason most of this code exists: for a lot of real incidents, the evidence available at the moment of failure simply does not contain the cause. Ask a language model to explain one of those and it will hand you a fluent, specific, wrong answer. Coroner is built to say "I don't know" out loud instead.

---

## Try it

```
git clone https://github.com/saadhtiwana/Coroner.git
cd Coroner
GROQ_API_KEY=your-key make demo
```

That creates a local kind cluster, breaks one workload per failure shape, diagnoses each, and prints what it found. One environment variable, no Slack, nothing to edit. `make demo-down` when you are done.

Needs `docker`, `kind`, `kubectl`, `go`, `uv`.

Already have a cluster?

```
kubectl create namespace coroner
kubectl create secret generic coroner-model -n coroner --from-literal=api-key=your-key
kubectl apply -f https://raw.githubusercontent.com/saadhtiwana/Coroner/main/deploy/install.yaml
kubectl logs -n coroner deploy/coroner-brain -f
```

---

## How it works

![Coroner architecture: the agent runs in the cluster and holds the only cluster credentials; the brain holds a model credential and none for the cluster. The agent posts an evidence contract over HTTP; the brain diagnoses, writes an append-only ledger, and delivers the verdict to a sink. An approval comes back as a signed token the agent verifies before it changes anything.](docs/img/architecture.svg)

Two processes, and the split is the point. The **agent** lives in your cluster and holds the only credentials that can change anything; its RBAC is read-only until you deliberately turn execution on. The **brain** does the reasoning and has no cluster access at all, so the process reachable from outside is the one that can't hurt you.

![Request flow: collect, classify, evidence gate, diagnose, validate, deliver. The gate can end a run before any model call. A failed citation check retries once and then abstains. Approval is offered only for a promoted failure type at or above the threshold.](docs/img/request-flow.svg)

Three details that came from watching real clusters rather than from reasoning:

- **Events are filtered by pod UID, never by name.** A recreated pod with the same name brings its old events along, and with them a different root cause. In a rollout loop that is the normal case, not an edge case.
- **Logs are grabbed the moment the failure is detected.** Log availability is racy. The same pod returned logs one minute and `unable to retrieve container logs` the next. Fetch them lazily and you will get nothing, exactly when it matters most.
- **"Wrote nothing" and "logs are gone" are different facts.** They are stored separately, because they deserve different amounts of trust.

---

## Why you can believe it

Start here: **the approval button is not the safety mechanism.** A tired human at 3am, handed a confident paragraph and a green button, clicks the button. Any design that treats the gate as the defence has put the defence in the wrong place. These are the actual defences, strongest first. The first four are code, and they run no matter what the model does.

**It refuses to guess.** A CrashLoopBackOff with no retrievable logs and a generic exit code never reaches the model at all. There is nothing to reason from, so nothing is asked. "Insufficient context" is a success here, not an error, and it is reported as one.

**Every claim has to cite real evidence, and code checks it.**

![The evidence validator: every cited path is resolved against the contract that was sent and its value compared, scalars exactly; every quoted span in the prose must appear verbatim in the collected text. An invented path and a fabricated log quote are both rejected in code.](docs/img/evidence-validation.svg)

The model must attach an `evidence` array to every diagnosis. Afterwards, code resolves each cited path against the exact contract that was sent and compares the value. An invented stack trace does not survive a substring check against text the agent actually collected. Fails the check, gets one retry, then abstains. It never reaches you.

**Confidence is a ceiling, not a self-report.**

![Confidence ceilings by evidence class: 0.95 for an image pull with the registry error in the event, down to 0.15 for a crash loop with no logs, which is routed to abstention. Final confidence is the lower of the model's estimate and the ceiling.](docs/img/confidence-ceiling.svg)

A model's opinion of its own confidence is worth very little, and worth least when it is wrong. The ceiling is computed from the evidence before the model is asked, and the model can only lower it. Across 31 runs on the same four incidents, its number never once fell below a ceiling.

**No button beats a greyed-out button.** Below the threshold, in shadow mode, or on an abstention, there is no approve control at all. You cannot approve by reflex if there is nothing to click.

**Facts and claims are kept apart.** Every message has two blocks: what was collected, verbatim, and what the model inferred. When the evidence is thin the first block is visibly thin, which tells you more than any confidence score.

Prompt instructions like "do not speculate" are not counted as a defence here. Nice to have, worth nothing under pressure.

---

## What it can't do

- **It cannot outrun your logging.** CrashLoopBackOff accuracy is capped by how well your application explains itself on the way down, and Coroner cannot improve that. Panic silently, log to a file, die on a signal, and the evidence contains no cause. Roughly half of real crash-loop traffic looks like that.
- **It has only been tested on failures built to fail.** Every incident measured so far was constructed in a local kind cluster, which makes the ground truth exact and the sample unrepresentative.
- **It doesn't find problems.** It sits downstream of your alerting and explains something already known to be broken. No dashboards, no metrics, no paging.
- **It doesn't act on its own.** Execution is off by default, needs a separate RBAC manifest, and needs a signed approval tied to that exact diagnosis and action text.

An accuracy measurement is in progress: 69 incidents, each built with a known cause, scored per failure type. The predictions were [committed in writing before any of it was implemented](docs/DESIGN.md#24-committed-prediction), along with the results that would falsify them. Numbers go here when the run finishes, whatever they say.

---

## Design

The design document is meant to be read. It argues with itself, records what was tried and reversed, and keeps every decision made without review.

- [Are these failures even diagnosable?](docs/DESIGN.md#2-the-three-mvp-failure-types-and-whether-they-are-actually-diagnosable)
- [The evidence contract](docs/DESIGN.md#3-the-context-contract)
- [Hallucination as the main risk](docs/DESIGN.md#4-hallucination-is-the-primary-risk)
- [How accuracy gets measured](docs/DESIGN.md#5-measuring-diagnosis-accuracy)
- [Decisions made without review](docs/DESIGN.md#6-decisions-made-without-input)
- [What is still unresolved](docs/DESIGN.md#8-open-items)

MIT licensed. See [LICENSE](LICENSE).
