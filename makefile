VERSION := 1.0.0
REGISTRY := registry.ahurix.com
PROJECT := tools
IMAGE_NAME = $(REGISTRY)/$(PROJECT)/price_publisher

.PHONY: build push test clean

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