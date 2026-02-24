VERSION := 0.1.0
IMAGE := llm-gateway
REGISTRY ?= ghcr.io/aachtenberg
KUBECONFIG ?= ~/.kube/config-k3s

.PHONY: build run test docker docker-push deploy clean

# Build binary
build:
	go build -ldflags="-s -w -X main.Version=$(VERSION)" -o llm-gateway .

# Run locally
run: build
	./llm-gateway

# Run tests
test:
	go test -v ./...

# Build Docker image
docker:
	docker build -t $(IMAGE):$(VERSION) -t $(IMAGE):latest .

# Push to registry
docker-push: docker
ifdef REGISTRY
	docker tag $(IMAGE):$(VERSION) $(REGISTRY)/$(IMAGE):$(VERSION)
	docker tag $(IMAGE):latest $(REGISTRY)/$(IMAGE):latest
	docker push $(REGISTRY)/$(IMAGE):$(VERSION)
	docker push $(REGISTRY)/$(IMAGE):latest
else
	@echo "Set REGISTRY to push to remote registry"
endif

# Deploy to k8s
deploy:
	KUBECONFIG=$(KUBECONFIG) kubectl apply -f k8s/deployment.yaml
	KUBECONFIG=$(KUBECONFIG) kubectl apply -f k8s/servicemonitor.yaml

# Create secrets (requires GROQ_API_KEY and ANTHROPIC_API_KEY env vars)
secrets:
	KUBECONFIG=$(KUBECONFIG) kubectl create secret generic llm-gateway-secrets \
		--namespace=ai \
		--from-literal=groq-api-key=$${GROQ_API_KEY} \
		--from-literal=anthropic-api-key=$${ANTHROPIC_API_KEY} \
		--dry-run=client -o yaml | KUBECONFIG=$(KUBECONFIG) kubectl apply -f -

# Deploy all (including secrets - be careful!)
deploy-all: secrets deploy
	@echo "Deployed to ai namespace"

# Check status
status:
	KUBECONFIG=$(KUBECONFIG) kubectl get all -n ai
	@echo ""
	KUBECONFIG=$(KUBECONFIG) kubectl get pods -n ai -o wide

# Clean
clean:
	rm -f llm-gateway
	go clean

# Local dev with hot reload (requires air: go install github.com/air-verse/air@latest)
dev:
	air

# Show help
help:
	@echo "LLM Gateway - Makefile commands:"
	@echo "  make build       - Build binary"
	@echo "  make run         - Build and run locally"
	@echo "  make test        - Run tests"
	@echo "  make docker      - Build Docker image"
	@echo "  make docker-push - Push to ghcr.io/aachtenberg/llm-gateway"
	@echo "  make deploy      - Deploy to k3s cluster"
	@echo "  make secrets     - Create/update k8s secrets"
	@echo "  make deploy-all  - Deploy everything (secrets + manifests)"
	@echo "  make status      - Check deployment status"
	@echo "  make clean       - Clean build artifacts"
	@echo "  make deploy      - Deploy to k8s"
	@echo "  make clean       - Clean build artifacts"
