SHELL := /usr/bin/env bash
.SHELLFLAGS := -euo pipefail -c

RUNNING_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
RUNNING_CONFIG ?= $(RUNNING_DIR)/configRunning.yaml
HELPER ?= $(RUNNING_DIR)/manageK8sManifest.py
PREFLIGHT ?= $(RUNNING_DIR)/validateClusterPreflight.sh

OUTPUT_DIR := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key outputDir)
MANIFEST := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key manifest)
IMAGES_YAML := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key imagesYaml)
KUSTOMIZATION := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key kustomization)
IMAGE_REGISTRY_PREFIX := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key imageRegistryPrefix)
REGISTRY_PREFIX := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key registryPrefix)
KUBECONFIG := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key kubeconfig)
SSH_USER := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key sshUser)
SSH_KEY := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key sshKey)
ROLLOUT_TIMEOUT_SECONDS := $(shell python3 "$(HELPER)" config-value --config "$(RUNNING_CONFIG)" --key rolloutTimeoutSeconds)
NAMESPACE := $(shell python3 "$(HELPER)" namespace --manifest "$(MANIFEST)" 2>/dev/null)

.PHONY: preflight build up render-kustomization wait clean check-output

check-output:
	@test -f "$(MANIFEST)" || { echo "Missing $(MANIFEST). Run compile first."; exit 1; }
	@test -f "$(IMAGES_YAML)" || { echo "Missing $(IMAGES_YAML). Run compile first."; exit 1; }
	@python3 "$(HELPER)" validate-manifest --manifest "$(MANIFEST)"

preflight: check-output
	@bash "$(PREFLIGHT)" --config "$(RUNNING_CONFIG)"

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
	ssh "$${ssh_args[@]}" "$${ssh_target}" "cd '$${remote_dir}/running' && sudo -n bash ./buildRegistryImages.sh --output-dir '$${remote_dir}/output' --registry-prefix '$(REGISTRY_PREFIX)' --image-registry-prefix '$(IMAGE_REGISTRY_PREFIX)'"

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
	@$(MAKE) wait RUNNING_CONFIG="$(RUNNING_CONFIG)"

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
