DEV_PYTHON ?= python3.12
ANSIBLE_CONFIG_PATH ?= automation/ansible/ansible.cfg
ANSIBLE_INVENTORY ?= automation/ansible/inventories/dev.yml
ANSIBLE_EXAMPLE_INVENTORY ?= automation/ansible/inventories/dev.example.yml

.PHONY: bootstrap-dev validate-phase0 test-core validate-core smoke-agent-rca \
	sync-knowledge-vectors evaluate-knowledge-retrieval gcp-readiness \
	render-online-boutique terraform-fmt terraform-validate \
	bootstrap-ansible ansible-syntax ansible-ping bootstrap-kubernetes \
	verify-kubernetes

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

ansible-ping:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible \
		-i $(ANSIBLE_INVENTORY) kubernetes_nodes -m ansible.builtin.ping

bootstrap-kubernetes:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) automation/ansible/playbooks/bootstrap.yml

verify-kubernetes:
	ANSIBLE_CONFIG=$(ANSIBLE_CONFIG_PATH) .venv-ansible/bin/ansible-playbook \
		-i $(ANSIBLE_INVENTORY) automation/ansible/playbooks/verify.yml
