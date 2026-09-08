from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path


LAB_ROOT = Path(__file__).resolve().parents[1]


class M1StructureTests(unittest.TestCase):
    def test_manifest_keeps_m1_runtime_dependencies(self) -> None:
        manifest = tomllib.loads((LAB_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dependencies = "\n".join(manifest["project"]["dependencies"]).lower()

        for required in (
            "fastapi",
            "sqlalchemy",
            "psycopg",
            "alembic",
            "redis",
            "rq",
            "openai",
            "pydantic-settings",
            "sse-starlette",
            "uvicorn",
        ):
            self.assertIn(required, dependencies)

    def test_compose_keeps_persistence_and_secrets_inside_lab(self) -> None:
        compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")

        self.assertIn("./data/postgres:/var/lib/postgresql/data", compose)
        self.assertIn("./data/redis:/data", compose)
        self.assertNotRegex(compose, r"(?m)^volumes:\s*$")
        self.assertEqual(compose.count("env_file: ./secrets/.env"), 5)
        self.assertIn("${HARNESS_COMPOSE_LLM_BASE_URL:-http://ollama:11434/v1}", compose)

    def test_compose_declares_services_health_and_memory_limits(self) -> None:
        compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")

        for service in (
            "ollama",
            "postgres",
            "redis",
            "migrate",
            "api",
            "dispatcher",
            "worker",
            "sweeper",
        ):
            self.assertRegex(compose, rf"(?m)^  {service}:$")
        for limit in ("768m", "256m", "1g"):
            self.assertIn(f"mem_limit: {limit}", compose)
        self.assertGreaterEqual(compose.count("healthcheck:"), 2)
        worker_block = re.search(r"(?ms)^  worker:\n(.*?)(?=^  \w|\Z)", compose)
        self.assertIsNotNone(worker_block)
        self.assertIn("restart: unless-stopped", worker_block.group(1))
        self.assertIn(
            "HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS: ${HARNESS_TEST_PAUSE_AFTER_TOOL_SECONDS:-0}",
            worker_block.group(1),
        )
        for service in ("ollama", "postgres", "redis", "lgtm", "api", "dispatcher", "worker", "sweeper"):
            block = re.search(rf"(?ms)^  {service}:\n(.*?)(?=^  \w|\Z)", compose)
            self.assertIsNotNone(block)
            self.assertIn("restart: unless-stopped", block.group(1))

    def test_api_defaults_to_four_uvicorn_workers_for_concurrent_creation(self) -> None:
        compose = (LAB_ROOT / "compose.yaml").read_text(encoding="utf-8")
        api_block = re.search(r"(?ms)^  api:\n(.*?)(?=^  \w|\Z)", compose)

        self.assertIsNotNone(api_block)
        self.assertIn(
            "uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --workers $${HARNESS_API_WORKERS}",
            api_block.group(1),
        )
        self.assertIn("HARNESS_API_WORKERS: ${HARNESS_API_WORKERS:-4}", api_block.group(1))
        self.assertIn("mem_limit: 768m", api_block.group(1))

    def test_dockerfile_uses_lab_project(self) -> None:
        dockerfile = (LAB_ROOT / "Dockerfile").read_text(encoding="utf-8")

        self.assertIn("WORKDIR /app", dockerfile)
        self.assertIn("pyproject.toml", dockerfile)
        self.assertIn("uv.lock", dockerfile)
        self.assertNotIn("../", dockerfile)


if __name__ == "__main__":
    unittest.main()
