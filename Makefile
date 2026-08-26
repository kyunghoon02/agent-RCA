DEV_PYTHON ?= python3.12
GCLOUD_BIN ?= gcloud
ANSIBLE_CONFIG_PATH ?= automation/ansible/ansible.cfg
ANSIBLE_INVENTORY ?= automation/ansible/inventories/dev.yml
ANSIBLE_EXAMPLE_INVENTORY ?= automation/ansible/inventories/dev.example.yml
RCA_GROUND_TRUTH ?=
RCA_PREDICTION ?=

.PHONY: bootstrap-dev validate-phase0 test-core validate-core smoke-agent-rca \
	smoke-live-krca smoke-live-stategraph \
	sync-knowledge-vectors evaluate-knowledge-retrieval evaluate-rca gcp-readiness \
	render-online-boutique build-online-boutique-otel-images terraform-fmt terraform-validate \
	render-incident-platform build-incident-platform-image \
	bootstrap-ansible ansible-syntax ansible-ping bootstrap-kubernetes \
	verify-kubernetes render-observability deploy-observability \
	verify-observability deploy-online-boutique verify-online-boutique \
	render-stategraph deploy-stategraph verify-stategraph \
	deploy-incident-platform verify-incident-platform

bootstrap-dev:
	$(DEV_PYTHON) -m venv .venv
	.venv/bin/python -m pip install --requirement requirements-dev.txt

validate-phase0:
	.venv/bin/python tools/validate_phase0.py

test-core:
	PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v

validate-core: validate-phase0 test-core

smoke-agent-rca:
	PYTHONPATH=src:. .venv/bin/python tools/smoke_agent_rca.py

smoke-live-krca:
	PYTHONPATH=src .venv/bin/python tools/smoke_live_krca.py

smoke-live-stategraph:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/smoke-live-stategraph.yml

sync-knowledge-vectors:
	PYTHONPATH=src .venv/bin/python tools/sync_knowledge_vectors.py

evaluate-knowledge-retrieval:
	PYTHONPATH=src .venv/bin/python tools/evaluate_knowledge_retrieval.py

evaluate-rca:
	PYTHONPATH=src .venv/bin/python tools/evaluate_rca.py \
		--ground-truth "$(RCA_GROUND_TRUTH)" \
		--prediction "$(RCA_PREDICTION)"

gcp-readiness:
	.venv/bin/python tools/check_gcp_readiness.py

render-online-boutique:
	kubectl kustomize platform/online-boutique

render-stategraph:
	kubectl kustomize platform/stategraph

render-incident-platform:
	kubectl kustomize platform/incident-platform

build-online-boutique-otel-images:
	GCLOUD_BIN=$(GCLOUD_BIN) tools/build_online_boutique_otel_images.sh

build-incident-platform-image:
	GCLOUD_BIN=$(GCLOUD_BIN) tools/build_incident_platform_image.sh

render-observability:
	helm template local-path-provisioner \
		oci://ghcr.io/rancher/local-path-provisioner/charts/local-path-provisioner \
		--version 0.0.36 --namespace local-path-storage \
		--values platform/observability/local-path-values.yaml >/dev/null
	helm template monitoring \
		oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack \
		--version 88.5.3 --namespace observability \
		--values platform/observability/kube-prometheus-stack-values.yaml >/dev/null
	helm template loki \
		oci://ghcr.io/grafana-community/helm-charts/loki \
		--version 18.11.0 --namespace observability \
		--values platform/observability/loki-values.yaml >/dev/null
	helm repo add grafana https://grafana.github.io/helm-charts --force-update >/dev/null
	helm repo update grafana >/dev/null
	kubectl kustomize platform/observability/tempo >/dev/null
	helm template alloy grafana/alloy \
		--version 1.11.1 --namespace observability \
		--values platform/observability/alloy-values.yaml >/dev/null

terraform-fmt:
	terraform fmt -check -recursive infra/terraform

terraform-validate:
	terraform -chdir=infra/terraform/environments/dev init -backend=false -input=false
	terraform -chdir=infra/terraform/environments/dev validate

bootstrap-ansible:
	$(DEV_PYTHON) -m venv .venv-ansible
	.venv-ansible/bin/python -m pip install --requirement automation/ansible/requirements.txt

ansible-syntax:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/bootstrap.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/verify.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/deploy-observability.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/verify-observability.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/deploy-online-boutique.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/verify-online-boutique.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/deploy-stategraph.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/verify-stategraph.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/smoke-live-stategraph.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/deploy-incident-platform.yml
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_EXAMPLE_INVENTORY) --syntax-check \
		automation/ansible/playbooks/verify-incident-platform.yml

ansible-ping:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible \
		-i $(ANSIBLE_INVENTORY) kubernetes_nodes -m ansible.builtin.ping

bootstrap-kubernetes:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) automation/ansible/playbooks/bootstrap.yml

verify-kubernetes:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) automation/ansible/playbooks/verify.yml

deploy-observability:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/deploy-observability.yml

verify-observability:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/verify-observability.yml

deploy-online-boutique:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/deploy-online-boutique.yml

verify-online-boutique:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/verify-online-boutique.yml

deploy-stategraph:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/deploy-stategraph.yml

verify-stategraph:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/verify-stategraph.yml

deploy-incident-platform:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/deploy-incident-platform.yml

verify-incident-platform:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) \
		automation/ansible/playbooks/verify-incident-platform.yml
