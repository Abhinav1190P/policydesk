import asyncio
import json
import logging
import signal

from aio_pika.abc import AbstractIncomingMessage

from app.config import get_settings
from app.db.pool import close_pool, create_pool, get_pool
from app.infra.broker import INGEST, USAGE, Broker, broker, declare_topology, retry_queue
from app.infra.redis_client import close_redis, create_redis
from app.providers.base import ProviderError
from app.services import ingest, usage

log = logging.getLogger("policydesk.worker")


async def _retry_or_dead(msg: AbstractIncomingMessage, queue: str, payload: dict, err: Exception, on_dead=None) -> None:
    s = get_settings()
    attempt = int((msg.headers or {}).get("x-attempt", 0)) + 1
    if attempt < s.ingest_max_attempts:
        await broker.publish(retry_queue(queue, attempt), payload, attempt=attempt, message_id=msg.message_id)
        await msg.ack()
        log.warning("%s message %s failed (attempt %d), retrying: %s", queue, msg.message_id, attempt, err)
        return
    if on_dead:
        await on_dead(str(err))
    log.error("%s message %s dead-lettered after %d attempts: %s", queue, msg.message_id, attempt, err)
    await msg.reject(requeue=False)


async def handle_ingest(msg: AbstractIncomingMessage) -> None:
    payload = json.loads(msg.body)
    op = payload.get("op", "ingest")
    doc_id = payload.get("document_id")
    try:
        if op == "sync":
            status = await ingest.sync_knowledge_base()
            if status != "COMPLETE":
                raise ProviderError(f"sync ended with {status}", retryable=True)
        else:
            result = await ingest.process_document(doc_id)
            log.info("document %s -> %s", doc_id, result)
        await msg.ack()
    except (ingest.PermanentError, ProviderError) as e:
        if isinstance(e, ProviderError) and e.retryable:
            if doc_id:
                await ingest.mark_queued(doc_id, str(e))

            async def dead_retryable(reason: str):
                if doc_id:
                    await ingest.mark_failed(doc_id, reason)

            await _retry_or_dead(msg, INGEST, payload, e, dead_retryable)
            return
        if doc_id:
            await ingest.mark_failed(doc_id, str(e))
        log.error("document %s failed permanently: %s", doc_id, e)
        await msg.reject(requeue=False)
    except Exception as e:
        if doc_id:
            await ingest.mark_queued(doc_id, str(e))

        async def dead(reason: str):
            if doc_id:
                await ingest.mark_failed(doc_id, reason)

        await _retry_or_dead(msg, INGEST, payload, e, dead)


async def handle_usage(msg: AbstractIncomingMessage) -> None:
    payload = json.loads(msg.body)
    try:
        await usage.insert_usage(payload)
        await msg.ack()
    except Exception as e:
        await _retry_or_dead(msg, USAGE, payload, e)


async def maintain_partitions() -> None:
    await get_pool().execute("SELECT ensure_usage_partitions(date_trunc('month', now())::date, 3)")


class Consumers:
    def __init__(self) -> None:
        self.channel = None
        self.tags: list[tuple] = []

    async def start(self, b: Broker) -> None:
        s = get_settings()
        self.channel = await b.connection.channel()
        await self.channel.set_qos(prefetch_count=s.worker_prefetch)
        topo = await declare_topology(self.channel)
        q_ingest = topo["queues"][INGEST]
        q_usage = topo["queues"][USAGE]
        self.tags.append((q_ingest, await q_ingest.consume(handle_ingest)))
        self.tags.append((q_usage, await q_usage.consume(handle_usage)))
        await maintain_partitions()
        log.info("consumers started (prefetch=%d)", s.worker_prefetch)

    async def stop(self) -> None:
        for q, tag in self.tags:
            try:
                await q.cancel(tag)
            except Exception:
                pass
        self.tags.clear()
        if self.channel is not None and not self.channel.is_closed:
            await self.channel.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    await create_pool()
    await create_redis()
    await broker.connect()
    consumers = Consumers()
    await consumers.start(broker)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    async def daily():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=86400)
            except asyncio.TimeoutError:
                await maintain_partitions()

    task = asyncio.create_task(daily())
    await stop.wait()
    task.cancel()
    await consumers.stop()
    await broker.close()
    await close_redis()
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
