from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]


class WorkerImageBoundaryTests(unittest.TestCase):
    def test_collection_cli_matches_cilium_and_uses_an_immutable_binary_source(self):
        versions = yaml.safe_load((ROOT / "platform/versions.yaml").read_text())
        hubble = versions["hubble"]
        expected_version = "v" + versions["cilium"]["chart_version"]
        self.assertEqual(hubble["worker_cli_version"], expected_version)
        self.assertRegex(hubble["worker_cli_source_image"],
                         rf"^quay.io/cilium/cilium:{expected_version}@sha256:[0-9a-f]{{64}}$")
        dockerfile = (ROOT / "platform/incident-platform/Dockerfile").read_text()
        self.assertIn("FROM " + hubble["worker_cli_source_image"] + " AS hubble-cli", dockerfile)
        self.assertIn("COPY --from=hubble-cli /usr/bin/hubble /usr/local/bin/hubble", dockerfile)
        self.assertIn("hubble version | grep -F 'hubble " + expected_version + "@'", dockerfile)
        self.assertNotIn("releases/download/", dockerfile)

    def test_collection_rollout_does_not_accept_partial_hubble_probe(self):
        tasks = yaml.safe_load((ROOT / "automation/ansible/playbooks/deploy-incident-worker.yml").read_text())[0]["tasks"]
        probe = next(task for task in tasks if task["name"] ==
                     "Verify v2 Hubble collection and projection without creating an Incident")
        code = probe["ansible.builtin.command"]["argv"][-1]
        compile(code, "hubble-live-probe", "exec")
        self.assertIn("assert batch.status == 'SUCCEEDED'", code)
        self.assertIn("assert not draft.facts['observation_gaps']", code)
        self.assertIn("assert not draft.facts['truncated']", code)
        self.assertIn("max_raw_flows=500", code)
        self.assertIn("max_raw_flows=500", (ROOT / "tools/run_incident_worker.py").read_text())
        self.assertIn("incident_persisted': False", code)
        version_check = next(task for task in tasks if task["name"] ==
                             "Require the collection CLI version pinned for the Relay")
        self.assertIn("hubble.worker_cli_version", str(version_check))

    def test_runtime_verifiers_use_each_components_pin(self):
        tasks = yaml.safe_load((ROOT / "automation/ansible/roles/incident_platform_verify/tasks/main.yml").read_text())
        worker_check = next(task for task in tasks if task["name"] == "Require the digest-pinned read-only Incident worker")
        self.assertIn("incident_platform.worker.image_digest", str(worker_check))
        self.assertNotIn("incident_platform.reconciler.image_digest", str(worker_check))
        tasks = yaml.safe_load((ROOT / "automation/ansible/roles/evaluation_runtime_verify/tasks/main.yml").read_text())
        pin_check = next(task for task in tasks if "evaluation_runtime_expected_digest" in task.get("vars", {}))
        self.assertEqual(pin_check["vars"]["evaluation_runtime_expected_digest"],
            "{{ incident_platform.worker.image_digest "
            "if evaluation_runtime_deployment_name == incident_platform_worker_deployment "
            "else incident_platform.viewer_api.image_digest "
            "if evaluation_runtime_deployment_name == incident_platform_viewer_deployment "
            "else incident_platform.reconciler.image_digest }}")
        self.assertIn("evaluation_runtime_expected_digest in", str(pin_check["ansible.builtin.assert"]["that"]))

    def test_collection_worker_pin_does_not_replace_the_agent_pin(self):
        tasks = yaml.safe_load((ROOT / "automation/ansible/roles/incident_platform_stack/tasks/main.yml").read_text())
        overlay = next(task for task in tasks if task["name"] == "Render a project-local, digest-pinned Incident Platform image overlay")
        content = next(task for task in overlay["block"] if task["name"] == "Write the digest-pinned Incident Platform image override")["ansible.builtin.copy"]["content"]
        # Render just the image mapping; unrelated runtime/secret inputs are
        # deliberately not needed to verify this deployment boundary.
        images_template = "images:" + content.split("images:", 1)[1].split("patches:", 1)[0]
        replacements = {
            "execution_target.location": "fixture-region",
            "incident_platform_gcp_project.content": "fixture-project",
            "incident_platform.artifact_repository_id": "fixture-images",
            "incident_platform.reconciler.image_digest": "sha256:" + "1" * 64,
            "incident_platform.worker.image_digest": "sha256:" + "2" * 64,
            "incident_platform.viewer_api.image_digest": "sha256:" + "3" * 64,
        }
        for key, value in replacements.items():
            images_template = images_template.replace("{{ " + key + " }}", value)
        rendered = yaml.safe_load(images_template)
        images = {image["name"]: image["digest"] for image in rendered["images"]}
        self.assertEqual(images, {
            "agent-rca-runtime": "sha256:" + "1" * 64,
            "agent-rca-collection-runtime": "sha256:" + "2" * 64,
            "agent-rca-viewer-api": "sha256:" + "3" * 64,
        })
        for filename, expected in (
            ("incident-worker.yaml", "agent-rca-collection-runtime"),
            ("agent-worker.yaml", "agent-rca-runtime"),
            ("incident-webhook.yaml", "agent-rca-runtime"),
        ):
            documents = yaml.safe_load_all((ROOT / "platform/incident-platform" / filename).read_text())
            deployment = next(doc for doc in documents if doc["kind"] == "Deployment")
            image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
            self.assertEqual(image, expected + "@sha256:" + "0" * 64)
