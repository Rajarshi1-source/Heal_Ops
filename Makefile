# HealOps - see AGENTS.md for the full command list.
# Windows users: run these under Git Bash or WSL, or invoke the underlying commands directly.

SHELL := /bin/bash
.DEFAULT_GOAL := help

KIND_CLUSTER    ?= healops
# v1.36.0 was never published; v1.36.4 is the current 1.36 patch, pinned by digest.
KIND_NODE_IMAGE ?= kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed
KPS_CHART_VER   ?= 88.4.0
NAMESPACE       ?= healops
# Must match secrets.webhookSharedSecret in the HealOps values file the release uses.
WEBHOOK_TOKEN   ?= change-me

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- local development -------------------------------------------------------

.PHONY: install
install: ## Sync the Python environment with uv
	uv sync --all-groups

.PHONY: dev
dev: ## Bring up the local stack (engine, dashboard, postgres, valkey, s3, grafana)
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
chaos: ## Chaos suite: fix, compensation and refusal per runbook, against the live kind cluster
	uv run pytest -m chaos -v -p no:cacheprovider engine/tests/chaos

.PHONY: gates
gates: ## The three CI decision gates: decision, policy, ledger
	uv run python -m evals.decision_eval
	uv run python -m evals.policy_eval
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
	# Alertmanager mounts the webhook secret, so it must exist before the chart starts the pods.
	kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n monitoring create secret generic healops-webhook \
		--from-literal=token=$(WEBHOOK_TOKEN) --dry-run=client -o yaml | kubectl apply -f -
	helm upgrade --install kps prometheus-community/kube-prometheus-stack \
		--version $(KPS_CHART_VER) \
		--namespace monitoring --create-namespace \
		-f k8s/prometheus/kube-prometheus-stack-values.yaml \
		--wait --timeout 10m

.PHONY: images
images: ## Build the engine, dashboard and chaos images and load them into kind
	docker build -t healops-engine:0.1.0 -f engine/Dockerfile .
	docker build -t healops-dashboard:0.1.0 dashboard
	docker build -t ghcr.io/rajarshi1-source/healops-chaos:0.1.0 chaos-app
	kind load docker-image healops-engine:0.1.0 healops-dashboard:0.1.0 \
		ghcr.io/rajarshi1-source/healops-chaos:0.1.0 --name $(KIND_CLUSTER)

.PHONY: deploy
deploy: ## Install alert rules, the chaos app and HealOps into the kind cluster
	helm upgrade --install alerting-rules charts/alerting-rules --namespace monitoring --wait
	# No --wait: the refusal fixtures crash-loop by design -- that is what makes them refusal cases --
	# so they never become Ready, and a waiting install would time out on its own fixtures.
	helm upgrade --install chaos charts/chaos-app --namespace chaos --create-namespace
	helm upgrade --install healops charts/healops \
		--namespace $(NAMESPACE) --create-namespace \
		-f charts/healops/values-local.yaml --rollback-on-failure --wait --timeout 10m
	# HealOps never remediates HealOps: every layer agrees, the namespace included.
	kubectl label namespace $(NAMESPACE) healops.io/remediation=disabled --overwrite

PUBLIC_NS ?= healops-public

.PHONY: public-tier
public-tier: ## The read-only public tier in kind, seeded with this cluster's recorded incidents
	# Data only, as plain SQL: the public release runs the same migrations, so the schema is its own.
	kubectl -n $(NAMESPACE) exec deploy/healops-postgres -- \
		pg_dump -U postgres -d healops --data-only --no-owner \
		--exclude-table-data=alembic_version > public-seed.sql
	helm upgrade --install healops-public charts/healops \
		--namespace $(PUBLIC_NS) --create-namespace \
		-f charts/healops/values-public.yaml -f charts/healops/values-public-kind.yaml \
		--rollback-on-failure --wait --timeout 10m
	# A faithful copy: the public release's migrations seed rows of their own (the budget scopes),
	# so empty every table first, then load the demo's exact state -- in one session, triggers off.
	( printf '%s\n' "SET session_replication_role = replica;" \
		"SELECT 'TRUNCATE ' || string_agg(format('%I', tablename), ', ') || ' CASCADE' FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'alembic_version' \\gexec"; \
		cat public-seed.sql ) | kubectl -n $(PUBLIC_NS) exec -i deploy/healops-postgres -- \
		psql -U postgres -d healops -v ON_ERROR_STOP=1 -q
	kubectl label namespace $(PUBLIC_NS) healops.io/remediation=disabled --overwrite
	@echo "public tier: kubectl -n $(PUBLIC_NS) port-forward svc/healops-dashboard 3001:3000"

.PHONY: public-tier-down
public-tier-down: ## Remove the rehearsed public tier
	helm uninstall healops-public --namespace $(PUBLIC_NS)
	kubectl delete namespace $(PUBLIC_NS) --wait=false

.PHONY: watchdog
watchdog: ## Run one watchdog sweep locally (in-cluster it is the chart's CronJob, every minute)
	cd engine/src && uv run python -m watchdog

.PHONY: demo
demo: ## Inject the five faults in sequence
	uv run python chaos-app/injectors/run_demo.py

.PHONY: corpus
corpus: ## After `make chaos`: harvest evidence + real cases, then capture the live scenarios
	uv run python chaos-app/injectors/capture_corpus.py harvest
	uv run python chaos-app/injectors/capture_corpus.py scenarios
	# A regenerated corpus is a deliberate decision change: freeze the digests and commit both.
	uv run python -m evals.decision_eval --freeze

# --- cli ---------------------------------------------------------------------

.PHONY: healopsctl
healopsctl: ## Run the CLI, e.g. make healopsctl ARGS="audit --verify-chain"
	uv run python -m cli.healopsctl $(ARGS)
