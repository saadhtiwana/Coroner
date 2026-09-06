# Coroner development tasks.
#
# Requires: go, uv, kind, kubectl, docker, golangci-lint. No Homebrew, no sudo.

CLUSTER   ?= coroner
IMAGE     ?= coroner-agent:dev
PLATFORM  ?= linux/arm64
TARBALL   ?= /tmp/$(subst :,-,$(IMAGE)).tar
KIND_CFG  := deploy/kind-cluster.yaml

GO        := go
UV        := uv
AGENT_DIR := agent
BRAIN_DIR := brain

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

# ---------------------------------------------------------------- verification

.PHONY: verify
verify: agent-verify brain-verify eval-check ## Run every check both services and the harness must pass

.PHONY: agent-verify
agent-verify: agent-build agent-vet agent-lint agent-test ## Build, vet, lint and test the agent

.PHONY: agent-build
agent-build: ## Compile the agent
	cd $(AGENT_DIR) && $(GO) build ./...

.PHONY: agent-vet
agent-vet: ## Run go vet
	cd $(AGENT_DIR) && $(GO) vet ./...

.PHONY: agent-lint
agent-lint: ## Run golangci-lint
	cd $(AGENT_DIR) && golangci-lint run

.PHONY: agent-test
agent-test: ## Run Go tests
	cd $(AGENT_DIR) && $(GO) test ./...

.PHONY: brain-verify
brain-verify: brain-lint brain-typecheck brain-test ## Lint, type-check and test the brain

.PHONY: brain-sync
brain-sync: ## Install Python dependencies
	cd $(BRAIN_DIR) && $(UV) sync --group dev

.PHONY: brain-lint
brain-lint: ## Run ruff check and format check
	cd $(BRAIN_DIR) && $(UV) run --group dev ruff check .
	cd $(BRAIN_DIR) && $(UV) run --group dev ruff format --check .

.PHONY: brain-format
brain-format: ## Apply ruff formatting
	cd $(BRAIN_DIR) && $(UV) run --group dev ruff format .

.PHONY: brain-typecheck
brain-typecheck: ## Run mypy in strict mode
	cd $(BRAIN_DIR) && $(UV) run --group dev mypy

.PHONY: brain-test
brain-test: ## Run pytest
	cd $(BRAIN_DIR) && $(UV) run --group dev pytest -q

.PHONY: brain-run
brain-run: ## Serve the brain on :8000 with the stdout sink
	cd $(BRAIN_DIR) && $(UV) run uvicorn coroner_brain.api:app --host 127.0.0.1 --port 8000

# --------------------------------------------------------------------- demo

.PHONY: demo
demo: ## Create a cluster, break one workload per failure type, print the diagnoses
	./scripts/demo.sh

.PHONY: demo-down
demo-down: ## Delete everything the demo created
	kind delete cluster --name $(CLUSTER)

# ------------------------------------------------------------------ evaluation

EVAL_OUT ?= eval/results

.PHONY: eval-check
eval-check: ## Lint, type-check and test the accuracy harness
	cd $(BRAIN_DIR) && $(UV) run --group dev ruff check ../eval
	cd $(BRAIN_DIR) && $(UV) run --group dev ruff format --check ../eval
	cd $(BRAIN_DIR) && $(UV) run --group dev mypy ../eval
	cd $(BRAIN_DIR) && $(UV) run --group dev pytest -q ../eval

.PHONY: agent-bin
agent-bin: ## Build the agent binary the harness runs
	cd $(AGENT_DIR) && $(GO) build -o bin/coroner-agent ./cmd/coroner-agent

.PHONY: eval-collect
eval-collect: agent-bin ## Create every catalogue incident and capture its contract
	cd $(BRAIN_DIR) && $(UV) run python ../eval/run.py collect --out ../$(EVAL_OUT)

.PHONY: eval-diagnose
eval-diagnose: ## Diagnose every captured contract through a running brain, resumably
	cd $(BRAIN_DIR) && $(UV) run python ../eval/run.py diagnose --out ../$(EVAL_OUT) --ledger ../$(EVAL_OUT)/ledger.sqlite3

.PHONY: eval-score
eval-score: ## Score the ledger against the catalogue and write the report
	cd $(BRAIN_DIR) && $(UV) run python ../eval/run.py score --out ../$(EVAL_OUT) --ledger ../$(EVAL_OUT)/ledger.sqlite3

# --------------------------------------------------------------------- cluster

.PHONY: cluster-up
cluster-up: ## Create the local kind cluster
	kind create cluster --config $(KIND_CFG) --wait 180s

.PHONY: cluster-down
cluster-down: ## Delete the local kind cluster
	kind delete cluster --name $(CLUSTER)

.PHONY: cluster-status
cluster-status: ## Show nodes and system pods
	kubectl get nodes -o wide
	kubectl get pods -A

.PHONY: deploy-rbac
deploy-rbac: ## Apply the namespace and read-only RBAC
	kubectl apply -f deploy/manifests/namespace.yaml
	kubectl apply -f deploy/manifests/rbac.yaml

# ------------------------------------------------------------------ image load

.PHONY: image-build
image-build: ## Build the agent container image
	docker build --platform $(PLATFORM) -t $(IMAGE) $(AGENT_DIR)

# image-load deliberately does NOT use `kind load docker-image`.
#
# On this machine that command fails with:
#     ERROR: failed to load image: ... ctr: content digest sha256:...: not found
#
# Docker Desktop 29.6.1 uses the containerd image store and keeps only the
# native platform's content, while kind imports with --all-platforms and
# therefore demands blobs for every platform in the manifest list. A plain
# `docker save` fails the same way, because the archive it writes still
# references the missing platform manifests.
#
# `docker save --platform` writes a self-consistent single-platform archive,
# which `kind load image-archive` accepts. Verified working 2026-09-05.
.PHONY: image-load
image-load: ## Side-load the agent image into the kind nodes without a registry
	docker save --platform $(PLATFORM) $(IMAGE) -o $(TARBALL)
	kind load image-archive $(TARBALL) --name $(CLUSTER)
	rm -f $(TARBALL)

.PHONY: image-push-local
image-push-local: image-build image-load ## Build then side-load in one step
