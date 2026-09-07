from __future__ import annotations

import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient


LAB_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB_ROOT))

from app.chaos.proxy import create_app  # noqa: E402


class ChaosPlaceholderTests(unittest.TestCase):
    def test_health_is_available_but_proxy_route_is_not(self) -> None:
        with TestClient(create_app()) as client:
            self.assertEqual(client.get("/health").json(), {"status": "placeholder", "faults_enabled": False})
            self.assertEqual(client.post("/v1/chat/completions", json={}).status_code, 404)

    def test_m1_rejects_fault_configuration(self) -> None:
        with self.assertRaisesRegex(ValueError, "M3"):
            create_app(fault_mode="429")


if __name__ == "__main__":
    unittest.main()
