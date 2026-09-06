#!/bin/sh
# Coroner, end to end, in one command.
#
# Creates a local cluster, breaks one workload per failure shape, and prints
# the diagnosis Coroner produces for each. The only configuration is the
# model key. No Slack, no manifests to edit, no images to publish.
#
#   GROQ_API_KEY=... make demo
#
# Everything it creates is named coroner or eval and is removed by
# `make demo-down`. See docs/DESIGN.md section 7.

set -eu

ROOT="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
CLUSTER="${CLUSTER:-coroner}"
PORT="${CORONER_PORT:-8000}"
WORK="${TMPDIR:-/tmp}/coroner-demo"
BRAIN_LOG="$WORK/brain.log"
AGENT_LOG="$WORK/agent.log"

# ImagePullBackOff is promoted out of shadow mode for the demo, so one
# incident shows the approval affordance. It is the class whose correctness
# is externally verifiable, per docs/DESIGN.md section 2.5. Every other type
# stays in shadow and offers a rating instead. Section 5.5.
PROMOTED="${CORONER_PROMOTED_TYPES:-ImagePullBackOff}"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'demo: %s\n' "$*" >&2; exit 1; }

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required and was not found in PATH"
}

cleanup() {
  if [ -n "${BRAIN_PID:-}" ] && kill -0 "$BRAIN_PID" 2>/dev/null; then
    kill "$BRAIN_PID" 2>/dev/null || true
    wait "$BRAIN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# ------------------------------------------------------------ prerequisites

need docker
need kind
need kubectl
need uv
need go
docker info >/dev/null 2>&1 || die "docker is installed but not running"
[ -n "${GROQ_API_KEY:-}" ] || [ -f "$ROOT/brain/.env" ] ||
  die "set GROQ_API_KEY (the only configuration this needs), or put it in brain/.env"

mkdir -p "$WORK"
: > "$BRAIN_LOG"

# ------------------------------------------------------------------ cluster

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  say "cluster $CLUSTER already exists, reusing it"
else
  say "creating the cluster (one control plane, two workers)"
  kind create cluster --config "$ROOT/deploy/kind-cluster.yaml" --wait 180s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

# The broken workloads run redis:alpine with imagePullPolicy Never, so the
# image has to be on the nodes. `kind load docker-image` fails on a
# containerd image store; a single-platform archive is what it accepts.
say "loading redis:alpine onto the nodes"
docker image inspect redis:alpine >/dev/null 2>&1 || docker pull redis:alpine
PLATFORM="linux/$(docker info --format '{{.Architecture}}' | sed 's/x86_64/amd64/;s/aarch64/arm64/')"
docker save --platform "$PLATFORM" redis:alpine -o "$WORK/redis-alpine.tar"
kind load image-archive "$WORK/redis-alpine.tar" --name "$CLUSTER" >/dev/null
rm -f "$WORK/redis-alpine.tar"

# -------------------------------------------------------------------- brain

say "starting the brain on port $PORT"
cd "$ROOT/brain"
uv sync --quiet
CORONER_SINK=stdout \
CORONER_PROMOTED_TYPES="$PROMOTED" \
CORONER_LEDGER_PATH="$WORK/ledger.sqlite3" \
CORONER_PUBLIC_URL="http://localhost:$PORT" \
CORONER_TRACING="${CORONER_TRACING:-off}" \
  uv run uvicorn coroner_brain.api:app --host 127.0.0.1 --port "$PORT" --log-level warning \
  >"$BRAIN_LOG" 2>&1 &
BRAIN_PID=$!
cd "$ROOT"

i=0
until curl -sf -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || { tail -20 "$BRAIN_LOG" >&2; die "the brain did not come up"; }
  kill -0 "$BRAIN_PID" 2>/dev/null || { tail -20 "$BRAIN_LOG" >&2; die "the brain exited"; }
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/healthz" | sed 's/^/   /'
if ! curl -sf "http://127.0.0.1:$PORT/healthz" | grep -q '"credentials_present":true'; then
  die "the brain has no model credential; set GROQ_API_KEY"
fi

# ---------------------------------------------------------------- workloads

say "breaking one workload per failure shape"
kubectl apply -f "$ROOT/deploy/probes/" | sed 's/^/   /'

say "waiting for all four to reach their failure states"
i=0
until [ "$(kubectl get pods -l coroner.dev/purpose=probe -o json |
  "$ROOT/scripts/count-failed.py")" -ge 4 ]; do
  i=$((i + 1))
  [ "$i" -lt 120 ] || die "the workloads did not fail in time; kubectl get pods for why"
  sleep 5
done
kubectl get pods -l coroner.dev/purpose=probe | sed 's/^/   /'

# -------------------------------------------------------------------- agent

say "collecting evidence and asking the brain"
cd "$ROOT/agent"
go build -o "$WORK/coroner-agent" ./cmd/coroner-agent
cd "$ROOT"
"$WORK/coroner-agent" --once --namespace default \
  --brain-url "http://127.0.0.1:$PORT" >/dev/null 2>"$AGENT_LOG" ||
  { cat "$AGENT_LOG" >&2; die "the agent failed"; }
grep -c 'verdict received' "$AGENT_LOG" | sed 's/^/   verdicts: /'

# ----------------------------------------------------------------- verdicts

say "diagnoses"
cat "$BRAIN_LOG"

cat <<EOF

== what you just saw
   Four workloads broken on purpose, one per failure shape Coroner handles.
   For each: Observed, the facts the agent collected, verbatim, with no model
   involvement; Inferred, the model's diagnosis with every citation checked
   against those facts in code; and Decision, which offers approval only for
   $PROMOTED, promoted here so one incident shows the affordance. Every other
   type is in shadow mode and asks for a rating instead, because none has
   cleared its accuracy prediction yet.

   The ledger is at $WORK/ledger.sqlite3.
   Tear it all down with: make demo-down
EOF
