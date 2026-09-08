from __future__ import annotations

import json
import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


LAB_ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = LAB_ROOT / "scripts" / "verify_tool_calling.py"
START_OLLAMA = LAB_ROOT / "scripts" / "start_ollama.sh"
CLOUD_VERIFIER = LAB_ROOT / "scripts" / "verify_cloud_credentials.py"


def load_validator_module():
    spec = importlib.util.spec_from_file_location("verify_tool_calling", VALIDATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cloud_verifier_module():
    spec = importlib.util.spec_from_file_location("verify_cloud_credentials", CLOUD_VERIFIER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolCallValidatorTests(unittest.TestCase):
    def run_validator(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as handle:
            json.dump(payload, handle)
            payload_path = Path(handle.name)
        try:
            return subprocess.run(
                [sys.executable, str(VALIDATOR), "--validate-file", str(payload_path)],
                check=False,
                capture_output=True,
                text=True,
            )
        finally:
            payload_path.unlink(missing_ok=True)

    def test_accepts_exact_get_campaign_call(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "get_campaign",
                                    "arguments": '{"campaign_id":"camp_001"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }

        result = self.run_validator(payload)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["valid"], True)

    def test_rejects_wrong_tool_name(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "get_report",
                                    "arguments": '{"campaign_id":"camp_001"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }

        result = self.run_validator(payload)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "wrong_tool_name")

    def test_rejects_malformed_arguments(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"name": "get_campaign", "arguments": "not-json"}}
                        ]
                    }
                }
            ]
        }

        result = self.run_validator(payload)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "arguments_not_json")

    def test_rejects_wrong_campaign(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "get_campaign",
                                    "arguments": '{"campaign_id":"camp_999"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }

        result = self.run_validator(payload)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["reason"], "wrong_campaign_id")

    def test_sampling_records_each_response_and_aggregate(self) -> None:
        module = load_validator_module()
        self.assertTrue(hasattr(module, "sample_responses"), "sampling API is missing")
        seen_bodies: list[dict[str, object]] = []

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "choices": [
                            {
                                "message": {
                                    "tool_calls": [
                                        {
                                            "function": {
                                                "name": "get_campaign",
                                                "arguments": '{"campaign_id":"camp_001"}',
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ).encode()

        def fake_opener(request, timeout):
            seen_bodies.append(json.loads(request.data))
            self.assertEqual(timeout, 120)
            return FakeResponse()

        summary = module.sample_responses(
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3:0.6b",
            sample_count=2,
            opener=fake_opener,
        )

        self.assertEqual(summary["valid_count"], 2)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["valid_rate"], 1.0)
        self.assertEqual(len(summary["samples"]), 2)
        self.assertEqual(len(seen_bodies), 2)
        self.assertEqual(seen_bodies[0]["temperature"], 0)
        self.assertIn("/no_think", seen_bodies[0]["messages"][0]["content"])
        self.assertIn("原样保留", seen_bodies[0]["messages"][0]["content"])
        self.assertIn("camp_001", seen_bodies[0]["messages"][0]["content"])

    def test_local_opener_explicitly_disables_proxies(self) -> None:
        module = load_validator_module()
        self.assertTrue(hasattr(module, "build_local_opener"), "local opener is missing")

        sentinel = object()
        with mock.patch.object(
            module.urllib.request,
            "build_opener",
            return_value=sentinel,
        ) as build_opener:
            opener = module.build_local_opener()

        self.assertIs(opener, sentinel)
        proxy_handler = build_opener.call_args.args[0]
        self.assertIsInstance(proxy_handler, module.urllib.request.ProxyHandler)
        self.assertEqual(proxy_handler.proxies, {})


class OllamaLauncherTests(unittest.TestCase):
    def test_print_config_keeps_owned_paths_inside_lab(self) -> None:
        result = subprocess.run(
            ["bash", str(START_OLLAMA), "--print-config"],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        config = json.loads(result.stdout)
        for key in ("models", "log", "pid"):
            self.assertTrue(Path(config[key]).is_relative_to(LAB_ROOT), (key, config[key]))
        self.assertEqual(config["host"], "127.0.0.1:11434")

    def test_launcher_keeps_server_in_foreground(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bin_dir = Path(temp_dir)
            fake_ollama = bin_dir / "ollama"
            fake_lsof = bin_dir / "lsof"
            fake_ollama.write_text("#!/bin/sh\n/bin/sleep 10\n", encoding="utf-8")
            fake_lsof.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            fake_ollama.chmod(0o755)
            fake_lsof.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{bin_dir}:{environment['PATH']}"

            process = subprocess.Popen(
                ["bash", str(START_OLLAMA)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                time.sleep(0.2)
                self.assertIsNone(process.poll(), "launcher exited instead of supervising Ollama")
            finally:
                process.terminate()
                process.wait(timeout=2)
                if process.stdout is not None:
                    process.stdout.close()
                if process.stderr is not None:
                    process.stderr.close()


class CloudCredentialVerifierTests(unittest.TestCase):
    def test_parses_dotenv_without_exporting_values(self) -> None:
        module = load_cloud_verifier_module()
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as handle:
            handle.write("SENTRY_DSN='https://public@example.invalid/123'\n")
            handle.write('LANGFUSE_PUBLIC_KEY="pk-test"\n')
            handle.write("LANGFUSE_SECRET_KEY=sk-test\n")
            handle.write("LANGFUSE_BASE_URL=https://cloud.langfuse.com/\n")
            env_path = Path(handle.name)
        try:
            values = module.parse_dotenv(env_path)
        finally:
            env_path.unlink(missing_ok=True)

        self.assertEqual(values["LANGFUSE_PUBLIC_KEY"], "pk-test")
        self.assertNotIn("pk-test", json.dumps(module.redacted_config(values)))
        self.assertNotIn("sk-test", json.dumps(module.redacted_config(values)))

    def test_sentry_store_request_uses_dsn_project_and_public_key(self) -> None:
        module = load_cloud_verifier_module()
        request, event_id = module.build_sentry_request(
            "https://public-key@o1.ingest.sentry.io/12345"
        )

        self.assertEqual(request.full_url, "https://o1.ingest.sentry.io/api/12345/store/")
        self.assertIn("sentry_key=public-key", request.get_header("X-sentry-auth"))
        payload = json.loads(request.data)
        self.assertEqual(payload["event_id"], event_id)
        self.assertEqual(payload["tags"]["stage"], "M0")

    def test_langfuse_request_uses_basic_auth_and_projects_endpoint(self) -> None:
        module = load_cloud_verifier_module()
        request = module.build_langfuse_request(
            "https://cloud.langfuse.com/", "pk-test", "sk-test"
        )

        self.assertEqual(request.full_url, "https://cloud.langfuse.com/api/public/projects")
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))

    def test_langfuse_request_normalizes_known_cloud_host_without_scheme(self) -> None:
        module = load_cloud_verifier_module()
        request = module.build_langfuse_request(
            "cloud.langfuse.com", "pk-test", "sk-test"
        )

        self.assertEqual(request.full_url, "https://cloud.langfuse.com/api/public/projects")


if __name__ == "__main__":
    unittest.main()
