DEV_PYTHON ?= python3.12

.PHONY: bootstrap-dev validate-phase0 test-core validate-core smoke-agent-rca gcp-readiness \
	render-online-boutique terraform-fmt \
	terraform-validate

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

gcp-readiness:
	.venv/bin/python tools/check_gcp_readiness.py

render-online-boutique:
	kubectl kustomize platform/online-boutique

terraform-fmt:
	@echo "Active GCP Terraform root is not implemented yet."
	@exit 2

terraform-validate:
	@echo "Active GCP Terraform root is not implemented yet."
	@exit 2
