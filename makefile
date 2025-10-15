VERSION := 1.0.0
REGISTRY := registry.ahurix.com
PROJECT := tools
IMAGE_NAME = $(REGISTRY)/$(PROJECT)/price_publisher

.PHONY: build push test clean deploy-dev deploy-prod k8s-validate

build: ## Build the Docker image with multiple tags
	docker build \
		-t $(IMAGE_NAME):$(VERSION) \
		-t $(IMAGE_NAME):latest \
		.

push: ## Push all tags to registry
	docker push $(IMAGE_NAME):$(VERSION)
	docker push $(IMAGE_NAME):latest

clean: ## Clean up images
	docker rmi $(IMAGE_NAME):$(VERSION) $(IMAGE_NAME):latest || true

k8s-validate: ## Validate Kubernetes manifests
	kubectl apply --dry-run=client -k k8s/overlays/dev
	kubectl apply --dry-run=client -k k8s/overlays/prod

deploy-dev: ## Deploy to development environment
	kubectl apply -k k8s/overlays/dev
	kubectl rollout status deployment/price-publisher -n price-publisher --timeout=300s
	kubectl get pods -n price-publisher -o wide

deploy-prod: ## Deploy to production environment
	kubectl apply -k k8s/overlays/prod
	kubectl rollout status deployment/price-publisher -n price-publisher --timeout=300s
	kubectl get pods -n price-publisher -o wide

k8s-logs: ## Get application logs
	kubectl logs -f deployment/price-publisher -n price-publisher

k8s-status: ## Check deployment status
	kubectl get all -n price-publisher
	kubectl describe deployment price-publisher -n price-publisher

k8s-delete: ## Delete deployment (use with caution)
	kubectl delete -k k8s/overlays/dev || true
	kubectl delete -k k8s/overlays/prod || true