SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

RUNNING_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
SETUP_ENV ?= $(abspath $(RUNNING_DIR)/../setup/seedemu-k3s.env.sh)
-include $(SETUP_ENV)

OUTPUT_DIR = ../emulate/output
OUTPUT_DIR := $(abspath $(OUTPUT_DIR))
IMAGE_REGISTRY_PREFIX = seedemu
REGISTRY_PREFIX = $(if $(SEED_REGISTRY_HOST),$(SEED_REGISTRY_HOST):$(if $(SEED_REGISTRY_PORT),$(SEED_REGISTRY_PORT),5000),192.168.122.110:5000)
KUBECONFIG = $(if $(SEED_OUTPUT_KUBECONFIG),$(SEED_OUTPUT_KUBECONFIG),/home/lxl/k8s/output/kubeconfigs/seedemu-k3s.yaml)
ROLLOUT_TIMEOUT_SECONDS ?= 1800
SSH_USER ?= ubuntu
SSH_KEY ?= /home/lxl/.ssh/id_ed25519

IMAGES_YAML ?= $(OUTPUT_DIR)/images.yaml
MANIFEST ?= $(OUTPUT_DIR)/k8s.yaml
KUSTOMIZATION ?= $(OUTPUT_DIR)/kustomization.yaml
HELPER ?= $(RUNNING_DIR)/k8s_make_helper.py
PREFLIGHT ?= $(RUNNING_DIR)/k8s_preflight.sh
NAMESPACE ?= $(shell python3 "$(HELPER)" namespace --manifest "$(MANIFEST)" 2>/dev/null)

.PHONY: preflight build up render-kustomization wait clean check-output

check-output:
	@test -f "$(MANIFEST)" || { echo "Missing $(MANIFEST). Run compile first."; exit 1; }
	@test -f "$(IMAGES_YAML)" || { echo "Missing $(IMAGES_YAML). Run compile first."; exit 1; }
	@python3 "$(HELPER)" validate-manifest --manifest "$(MANIFEST)"

preflight: check-output
	@OUTPUT_DIR="$(OUTPUT_DIR)" \
	  KUBECONFIG="$(KUBECONFIG)" \
	  REGISTRY_PREFIX="$(REGISTRY_PREFIX)" \
	  IMAGE_REGISTRY_PREFIX="$(IMAGE_REGISTRY_PREFIX)" \
	  SSH_USER="$(SSH_USER)" \
	  SSH_KEY="$(SSH_KEY)" \
	  HELPER="$(HELPER)" \
	  MANIFEST="$(MANIFEST)" \
	  IMAGES_YAML="$(IMAGES_YAML)" \
	  bash "$(PREFLIGHT)"

build: check-output
	@registry_prefix="$(REGISTRY_PREFIX)"; \
	registry_host="$${registry_prefix%%/*}"; \
	registry_host="$${registry_host%%:*}"; \
	remote_dir="/tmp/seedemu-native-build-$$(date +%Y%m%d_%H%M%S)-$$$$"; \
	ssh_target="$(SSH_USER)@$${registry_host}"; \
	ssh_args=(-i "$(SSH_KEY)" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR); \
	echo "[k8s_build] uploading compile output to $${ssh_target}:$${remote_dir}/output"; \
	ssh "$${ssh_args[@]}" "$${ssh_target}" "rm -rf '$${remote_dir}' && mkdir -p '$${remote_dir}/output' '$${remote_dir}/running'"; \
	tar -C "$(OUTPUT_DIR)" -czf - . | ssh "$${ssh_args[@]}" "$${ssh_target}" "tar -C '$${remote_dir}/output' -xzf -"; \
	tar -C "$(RUNNING_DIR)" -czf - . | ssh "$${ssh_args[@]}" "$${ssh_target}" "tar -C '$${remote_dir}/running' -xzf -"; \
	echo "[k8s_build] running build on $${ssh_target}"; \
	ssh "$${ssh_args[@]}" "$${ssh_target}" "cd '$${remote_dir}/running' && sudo -n env OUTPUT_DIR='$${remote_dir}/output' REGISTRY_PREFIX='$(REGISTRY_PREFIX)' IMAGE_REGISTRY_PREFIX='$(IMAGE_REGISTRY_PREFIX)' bash ./k8s_local_build.sh"

render-kustomization: check-output
	@python3 "$(HELPER)" kustomization --images-yaml "$(IMAGES_YAML)" --image-registry-prefix "$(IMAGE_REGISTRY_PREFIX)" --registry-prefix "$(REGISTRY_PREFIX)" --output "$(KUSTOMIZATION)"
	@echo "[k8s_up] wrote $(KUSTOMIZATION)"

up: render-kustomization
	@echo "=== native-k8s deploy ==="
	@echo "output_dir=$(OUTPUT_DIR)"
	@echo "manifest=$(MANIFEST)"
	@echo "kustomization=$(KUSTOMIZATION)"
	@echo "kubeconfig=$(KUBECONFIG)"
	@echo "namespace=$(NAMESPACE)"
	@kubectl --kubeconfig "$(KUBECONFIG)" apply -k "$(OUTPUT_DIR)"
	@$(MAKE) wait OUTPUT_DIR="$(OUTPUT_DIR)" KUBECONFIG="$(KUBECONFIG)" REGISTRY_PREFIX="$(REGISTRY_PREFIX)" IMAGE_REGISTRY_PREFIX="$(IMAGE_REGISTRY_PREFIX)"

wait: check-output
	@while read -r name; do \
	  echo "[rollout] deployment/$${name}"; \
	  kubectl --kubeconfig "$(KUBECONFIG)" -n "$(NAMESPACE)" rollout status "deployment/$${name}" --timeout="$(ROLLOUT_TIMEOUT_SECONDS)s"; \
	done < <(python3 "$(HELPER)" deployment-names --manifest "$(MANIFEST)")
	@kubectl --kubeconfig "$(KUBECONFIG)" -n "$(NAMESPACE)" wait --for=condition=Ready pod -l seedemu.io/workload=seedemu --timeout="$(ROLLOUT_TIMEOUT_SECONDS)s"
	@echo "Deploy completed successfully."

clean: check-output
	@if kubectl --kubeconfig "$(KUBECONFIG)" get namespace "$(NAMESPACE)" >/dev/null 2>&1; then \
	  echo "[clean] deleting namespace $(NAMESPACE)"; \
	  kubectl --kubeconfig "$(KUBECONFIG)" delete namespace "$(NAMESPACE)" --wait=false; \
	else \
	  echo "[clean] namespace $(NAMESPACE) does not exist"; \
	fi
