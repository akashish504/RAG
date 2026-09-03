"""Thin SQS producer used by the polling trigger (scripts/run_poller.py)."""

from __future__ import annotations

import json
from typing import Any

from botocore.client import BaseClient


class SqsProducer:
    """Sends JSON payloads to one SQS queue."""

    def __init__(self, *, client: BaseClient, queue_url: str) -> None:
        self._client = client
        self._queue_url = queue_url

    def send(self, payload: dict[str, Any]) -> str:
        """Send ``payload`` as a JSON message body. Returns the SQS MessageId."""
        response = self._client.send_message(
            QueueUrl=self._queue_url,
            MessageBody=json.dumps(payload, ensure_ascii=False),
        )
        return response["MessageId"]
