"""HxSync — Kafka / Kinesis / PubSub stream destination adapter (protocol stub).

Real connector would use aiokafka / boto3 / google-cloud-pubsub.
"""
from __future__ import annotations

from case_service.hxsync.protocol import SyncDestinationProtocol, register_destination


@register_destination("kafka")
class KafkaDestination(SyncDestinationProtocol):

    def health_check(self) -> dict:
        brokers = self.config.get("brokers")
        if not brokers:
            return {"ok": False, "message": "Missing 'brokers' in config", "latency_ms": 0}
        return {"ok": True, "message": f"Kafka brokers={brokers} configured (live connection requires aiokafka)", "latency_ms": 1}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        pass  # Kafka is schema-less; topic auto-creation handled by broker

    def push_rows(self, table: str, rows: list[dict]) -> int:
        return len(rows)


@register_destination("kinesis")
class KinesisDestination(SyncDestinationProtocol):

    def health_check(self) -> dict:
        stream = self.config.get("stream_name")
        if not stream:
            return {"ok": False, "message": "Missing 'stream_name' in config", "latency_ms": 0}
        return {"ok": True, "message": f"Kinesis stream={stream} configured (live connection requires boto3)", "latency_ms": 1}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        pass

    def push_rows(self, table: str, rows: list[dict]) -> int:
        return len(rows)


@register_destination("pubsub")
class PubSubDestination(SyncDestinationProtocol):

    def health_check(self) -> dict:
        topic = self.config.get("topic_id")
        if not topic:
            return {"ok": False, "message": "Missing 'topic_id' in config", "latency_ms": 0}
        return {"ok": True, "message": f"PubSub topic={topic} configured (live connection requires google-cloud-pubsub)", "latency_ms": 1}

    def ensure_schema(self, table: str, columns: list[dict]) -> None:
        pass

    def push_rows(self, table: str, rows: list[dict]) -> int:
        return len(rows)
