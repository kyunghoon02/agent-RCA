ANSIBLE_DIR := automation/ansible
ANSIBLE_VENV := .venv-ansible
ANSIBLE_PYTHON ?= python3.12
ANSIBLE_BIN := ../../$(ANSIBLE_VENV)/bin
DEV_PYTHON ?= python3.12

.PHONY: bootstrap-dev bootstrap-ansible validate-phase0 test-core validate-core \
	ktcloud-readiness validate-ansible render-online-boutique terraform-fmt \
	terraform-validate

bootstrap-dev:
	$(DEV_PYTHON) -m venv .venv
	.venv/bin/python -m pip install --requirement requirements-dev.txt

bootstrap-ansible:
	$(ANSIBLE_PYTHON) -m venv $(ANSIBLE_VENV)
	$(ANSIBLE_VENV)/bin/python -m pip install --requirement $(ANSIBLE_DIR)/requirements.txt
	cd $(ANSIBLE_DIR) && $(ANSIBLE_BIN)/ansible-galaxy collection install \
		--requirements-file collections/requirements.yml \
		--collections-path .collections

validate-phase0:
	.venv/bin/python tools/validate_phase0.py

test-core:
	PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v

validate-core: validate-phase0 test-core

ktcloud-readiness:
	.venv/bin/python tools/check_ktcloud_readiness.py

validate-ansible: ktcloud-readiness
	@echo "KT Cloud self-managed Kubernetes playbooks are not implemented yet."
	@exit 2

render-online-boutique:
	kubectl kustomize platform/online-boutique

terraform-fmt: ktcloud-readiness
	@echo "Active KT Cloud Terraform root is not implemented yet."
	@exit 2

terraform-validate: ktcloud-readiness
	@echo "Active KT Cloud Terraform root is not implemented yet."
	@exit 2
