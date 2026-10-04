# HealOps - see AGENTS.md for the full command list.
# Windows users: run these under Git Bash or WSL, or invoke the underlying commands directly.

SHELL := /bin/bash
.DEFAULT_GOAL := help

KIND_CLUSTER    ?= healops
KIND_NODE_IMAGE ?= kindest/node:v1.36.0
KPS_CHART_VER   ?= 88.4.0
NAMESPACE       ?= healops

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- local development -------------------------------------------------------

.PHONY: install
install: ## Sync the Python environment with uv
	uv sync --all-groups

.PHONY: dev
dev: ## Bring up the local stack (engine, dashboard, postgres, valkey, minio, grafana)
	docker compose up --build -d
	@echo "engine    http://localhost:8000/docs"
	@echo "dashboard http://localhost:3000"
	@echo "grafana   http://localhost:3001"

.PHONY: down
down: ## Tear down the local stack
	docker compose down

.PHONY: logs
logs: ## Follow engine logs
	docker compose logs -f engine

# --- quality gates -----------------------------------------------------------

.PHONY: lint
lint: ## Ruff + mypy
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy engine/src

.PHONY: test
test: ## Unit suite - hermetic, no cluster required
	uv run pytest -m "not chaos" --cov=engine/src --cov-fail-under=80

.PHONY: chaos
chaos: ## Chaos suite - requires a live kind cluster
	uv run pytest -m chaos -v

.PHONY: gates
gates: ## The three CI decision gates
	uv run python -m evals.policy_eval
	uv run python -m evals.decision_eval
	uv run python -m evals.ledger_verify

# --- cluster -----------------------------------------------------------------

.PHONY: cluster
cluster: ## Create the local kind cluster (Kubernetes 1.36)
	kind create cluster --name $(KIND_CLUSTER) --config k8s/kind/cluster.yaml --image $(KIND_NODE_IMAGE)

.PHONY: cluster-down
cluster-down: ## Delete the local kind cluster
	kind delete cluster --name $(KIND_CLUSTER)

.PHONY: observability
observability: ## Install kube-prometheus-stack at the pinned chart version
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
	helm repo update
	helm upgrade --install kps prometheus-community/kube-prometheus-stack \
		--version $(KPS_CHART_VER) \
		--namespace monitoring --create-namespace \
		-f k8s/prometheus/kube-prometheus-stack-values.yaml \
		--wait --timeout 10m

.PHONY: deploy
deploy: ## Install HealOps and the chaos app into the kind cluster
	helm upgrade --install healops charts/healops \
		--namespace $(NAMESPACE) --create-namespace \
		-f charts/healops/values-local.yaml --wait --atomic --timeout 5m
	helm upgrade --install alerting-rules charts/alerting-rules --namespace monitoring
	helm upgrade --install chaos charts/chaos-app --namespace chaos --create-namespace --wait

.PHONY: watchdog
watchdog: ## Run one watchdog sweep locally (in-cluster it is a CronJob, every minute)
	cd engine/src && uv run python -m watchdog

.PHONY: watchdog-deploy
watchdog-deploy: ## Apply the watchdog CronJob to the kind cluster
	kubectl apply -n $(NAMESPACE) -f k8s/healops/watchdog-cronjob.yaml

.PHONY: demo
demo: ## Inject the five faults in sequence
	uv run python chaos-app/injectors/run_demo.py

.PHONY: corpus
corpus: ## Regenerate the labelled incident corpus from the chaos harness
	uv run python chaos-app/injectors/capture_corpus.py --out corpus/

# --- cli ---------------------------------------------------------------------

.PHONY: healopsctl
healopsctl: ## Run the CLI, e.g. make healopsctl ARGS="audit --verify-chain"
	uv run python -m cli.healopsctl $(ARGS)
