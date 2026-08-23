DEV_PYTHON ?= python3.12
ANSIBLE_CONFIG_PATH ?= automation/ansible/ansible.cfg
ANSIBLE_INVENTORY ?= automation/ansible/inventories/dev.yml
ANSIBLE_EXAMPLE_INVENTORY ?= automation/ansible/inventories/dev.example.yml

.PHONY: bootstrap-dev validate-phase0 test-core validate-core smoke-agent-rca \
	sync-knowledge-vectors evaluate-knowledge-retrieval gcp-readiness \
	render-online-boutique terraform-fmt terraform-validate \
	bootstrap-ansible ansible-syntax ansible-ping bootstrap-kubernetes \
	verify-kubernetes render-observability deploy-observability \
	verify-observability deploy-online-boutique verify-online-boutique

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

sync-knowledge-vectors:
	PYTHONPATH=src .venv/bin/python tools/sync_knowledge_vectors.py

evaluate-knowledge-retrieval:
	PYTHONPATH=src .venv/bin/python tools/evaluate_knowledge_retrieval.py

gcp-readiness:
	.venv/bin/python tools/check_gcp_readiness.py

render-online-boutique:
	kubectl kustomize platform/online-boutique

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
