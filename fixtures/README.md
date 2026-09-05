# Fixtures

Recorded output from a live kind cluster, used for offline development so the
collector and the reasoning graph can be exercised without a cluster running.

Provenance: captured 2026-09-05 from the `coroner` kind cluster, Kubernetes
v1.37.0, three nodes. Each directory holds one incident:

| Directory | Failure | Key signals |
| --- | --- | --- |
| `crashloopbackoff/` | Application exits nonzero, cannot reach Postgres | `waiting.reason=CrashLoopBackOff`, `lastState.reason=Error`, `exitCode=1` |
| `imagepullbackoff/` | Image does not exist or is not readable | `waiting.reason=ImagePullBackOff`, `restartCount=0`, no logs |
| `oomkilled/` | Container exceeded its memory limit while running | `lastState.reason=OOMKilled`, `exitCode=137` |
| `oom-startError/` | Memory limit too low for container init to start | `lastState.reason=StartError`, `exitCode=128` |

Each directory contains:

- `pod.json` -- `kubectl get pod -o json`
- `events.json` -- `kubectl get events --field-selector involvedObject.name=<pod> -o json`
- `describe.txt` -- `kubectl describe pod`
- `logs-previous.txt` -- `kubectl logs --previous`
- `logs-current.txt` -- `kubectl logs`

## Properties worth preserving

These recordings are deliberately not cleaned up. Several carry the awkward
cases the design has to handle, and normalising them away would remove the only
regression tests for that handling:

- **`events.json` contains events from more than one pod UID.** The capture used
  `involvedObject.name`, which matches by name rather than identity, so a
  recreated pod of the same name contributed its events too. `crashloopbackoff`
  holds 2 distinct UIDs and `oomkilled` holds 3. A collector that filters by UID
  will produce the right answer against these files; one that filters by name
  will not. That is the point.

- **`oomkilled/logs-previous.txt` contains a retrieval failure, not logs.** The
  kernel OOM killer sends SIGKILL, so the process writes no final line, and the
  runtime had already reclaimed the container by capture time. This is the
  normal case for OOM, not a bad capture.

- **`oom-startError/` is the same root cause as `oomkilled/` in a different
  shape.** Exit code 128 with `reason=StartError` rather than 137 with
  `reason=OOMKilled`, surfacing as `CrashLoopBackOff`. A classifier keyed on exit
  code 137 alone misses it.

- **`crashloopbackoff/logs-previous.txt` is the only file in the whole set that
  names the actual cause.** Nothing in `pod.json`, `events.json`, or
  `describe.txt` mentions Postgres or a connection failure. This is the evidence
  behind docs/DESIGN.md section 2.3.
