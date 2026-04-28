import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app


class BatchApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_batch_endpoint_requires_file_or_text(self) -> None:
        response = self.client.post(
            "/api/normalize/batch",
            data={"provider": "deepseek"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "file 和 text 至少提供一个")

    def test_batch_endpoint_streams_ndjson_for_text_input(self) -> None:
        fake_result = {
            "formatted_text": "Room 1203\nTower A\nSHENZHEN",
            "scores": {"total_score": 91.5},
            "processing_time_ms": 12,
        }
        calls = []

        async def fake_normalize_address(raw_address, use_online_verify, provider):
            calls.append((raw_address, use_online_verify, provider))
            return fake_result

        with patch("main.normalize_address", new=fake_normalize_address):
            with self.client.stream(
                "POST",
                "/api/normalize/batch",
                data={
                    "text": "深圳市南山区科技园 1 号\n福田区中心一路 8 号",
                    "provider": "deepseek",
                    "use_online_verify": "false",
                },
            ) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.headers["content-type"],
                    "application/x-ndjson; charset=utf-8",
                )
                lines = [
                    json.loads(line)
                    for line in response.iter_lines()
                    if line.strip()
                ]

        self.assertEqual(len(calls), 2)
        self.assertEqual(lines[0]["type"], "item")
        self.assertEqual(lines[0]["index"], 0)
        self.assertEqual(lines[0]["status"], "success")
        self.assertEqual(lines[1]["index"], 1)
        self.assertEqual(lines[2]["type"], "done")
        self.assertEqual(lines[2]["success_count"], 2)
        self.assertEqual(lines[2]["failed_count"], 0)


if __name__ == "__main__":
    unittest.main()
