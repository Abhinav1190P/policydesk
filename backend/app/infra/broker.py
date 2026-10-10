import json
import logging
import uuid

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from app.config import get_settings

log = logging.getLogger("policydesk.broker")

EXCHANGE = "policydesk"
DLX = "policydesk.dlx"
INGEST = "ingest"
USAGE = "usage"


def retry_queue(name: str, attempt: int) -> str:
    return f"{name}.retry.{attempt}"


async def declare_topology(channel: AbstractChannel) -> dict:
    settings = get_settings()
    ex = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    dlx = await channel.declare_exchange(DLX, aio_pika.ExchangeType.DIRECT, durable=True)
    queues = {}
    for name in (INGEST, USAGE):
        q = await channel.declare_queue(
            name,
            durable=True,
            arguments={"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": f"{name}.dead"},
        )
        await q.bind(ex, routing_key=name)
        dlq = await channel.declare_queue(f"{name}.dlq", durable=True)
        await dlq.bind(dlx, routing_key=f"{name}.dead")
        for attempt in range(1, settings.ingest_max_attempts):
            rq = await channel.declare_queue(
                retry_queue(name, attempt),
                durable=True,
                arguments={
                    "x-message-ttl": settings.ingest_retry_delay_ms * (2 ** (attempt - 1)),
                    "x-dead-letter-exchange": EXCHANGE,
                    "x-dead-letter-routing-key": name,
                },
            )
            await rq.bind(ex, routing_key=retry_queue(name, attempt))
        queues[name] = q
    return {"exchange": ex, "dlx": dlx, "queues": queues}


class Broker:
    def __init__(self) -> None:
        self.connection: AbstractRobustConnection | None = None
        self.channel: AbstractChannel | None = None
        self.exchange = None

    async def connect(self, url: str | None = None) -> None:
        self.connection = await aio_pika.connect_robust(url or get_settings().rabbitmq_url, timeout=10)
        self.channel = await self.connection.channel(publisher_confirms=True)
        topo = await declare_topology(self.channel)
        self.exchange = topo["exchange"]

    @property
    def ready(self) -> bool:
        return self.exchange is not None and self.connection is not None and not self.connection.is_closed

    async def publish(self, routing_key: str, payload: dict, attempt: int = 0, message_id: str | None = None) -> None:
        if not self.ready:
            raise RuntimeError("broker not connected")
        msg = aio_pika.Message(
            body=json.dumps(payload, default=str).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=message_id or str(uuid.uuid4()),
            headers={"x-attempt": attempt},
        )
        await self.exchange.publish(msg, routing_key=routing_key)

    async def queue_depths(self) -> dict:
        if not self.ready:
            return {}
        out = {}
        ch = await self.connection.channel()
        try:
            for name in (INGEST, f"{INGEST}.dlq", USAGE, f"{USAGE}.dlq"):
                q = await ch.declare_queue(name, passive=True)
                out[name] = q.declaration_result.message_count
        finally:
            await ch.close()
        return out

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.close()
        self.connection = None
        self.channel = None
        self.exchange = None


broker = Broker()
