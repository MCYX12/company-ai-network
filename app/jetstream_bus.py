"""JetStream-backed at-least-once delivery with the task ledger as the single authority.

JetStream carries and redelivers; the SQLite task ledger owns business state. Business
effects are applied at most once because the ledger's terminal guard is idempotent.
"""
import asyncio
import json
import logging

logger = logging.getLogger(__name__)

# A durable consumer keeps the config it was first created with: nats-py asks the
# server for the consumer and, when it exists, reuses it without sending `config`
# (nats/js/client.py:604-615). These are the fields this module requests, so they are
# the ones that can silently drift from the code/env var the operator just changed.
_DURABLE_CONFIG_FIELDS = ('max_deliver', 'ack_wait', 'max_ack_pending')


def apply_once(store, task_id, result):
    """Apply a business result exactly once; the ledger's terminal state is the guard."""
    row = store.get(task_id)
    if not row or row['state'] in ('succeeded', 'failed', 'expired', 'cancelled'):
        return False
    store.finish(task_id, result)
    return True


def pending(store, now=None):
    """Tasks the ledger still owes delivery for (reconciliation source of truth)."""
    return store.due(now)


async def connect(url, **kwargs):
    import nats
    nc = await nats.connect(url, **kwargs)
    return nc, nc.jetstream()


async def ensure(js, name='TASKS', prefix='poc', replicas=1, max_msgs=100000, max_age=86400):
    from nats.js.api import StreamConfig, DiscardPolicy
    # max_age (seconds) bounds the stream when a target has no durable consumer: the
    # Router also delivers on Core, so those messages would otherwise accumulate.
    tasks = await js.add_stream(name=name, subjects=[prefix + '.task.>'],
                                config=StreamConfig(num_replicas=replicas, max_msgs=max_msgs,
                                                    max_age=max_age, discard=DiscardPolicy.OLD))
    dlq = await js.add_stream(name=name + 'DLQ', subjects=[prefix + '.dlq.>'],
                              config=StreamConfig(num_replicas=replicas, max_age=max_age))
    return {'tasks': tasks.config.name, 'dlq': dlq.config.name}


async def publish(js, prefix, body):
    ack = await js.publish(prefix + '.task.' + body['target'], json.dumps(body).encode())
    return {'stream': ack.stream, 'seq': ack.seq}


async def _dead_letter(js, prefix, message, target_id):
    await js.publish(prefix + '.dlq.' + (target_id or 'unknown'), message.data)
    await message.term()


async def _keepalive(message, ack_wait):
    """Extend the ack deadline while a slow handler (for example a model call) runs."""
    interval = max(1.0, float(ack_wait) / 2)
    while True:
        await asyncio.sleep(interval)
        await message.in_progress()


async def _deliver(js, prefix, handler, message, body, max_deliver, ack_wait, target):
    delivered = getattr(message.metadata, 'num_delivered', 1)
    keepalive = asyncio.ensure_future(_keepalive(message, ack_wait))
    try:
        await handler(body)
    except Exception:
        # The message is executed on every delivery up to max_deliver; only the
        # final failed attempt is moved to the DLQ, so max_deliver=1 still runs once.
        if delivered >= max_deliver:
            await _dead_letter(js, prefix, message, target or body.get('target'))
        else:
            await message.nak()
    else:
        await message.ack()
    finally:
        keepalive.cancel()
        try:
            await keepalive
        except BaseException:
            pass


def _config_differences(requested, actual):
    """(field, requested, actual) for every requested field the server did not keep."""
    differences = []
    for field in _DURABLE_CONFIG_FIELDS:
        wanted = getattr(requested, field, None)
        if wanted is None:
            continue
        current = getattr(actual, field, None)
        if current != wanted:
            differences.append((field, wanted, current))
    return differences


async def _warn_on_durable_drift(subscription, durable, requested):
    """Make a durable consumer that ignores the requested config observable.

    nats-py only sends `config` while creating the consumer; when the durable already
    exists the server-side settings win silently, so changing max_deliver/ack_wait/
    max_ack_pending in code or in an environment variable would look applied while the
    old values stay live. This is read-only and best effort: if the consumer cannot be
    read (missing, permissions, transport) we return and leave the subscribe error path
    exactly as it was, and nothing is ever deleted or recreated.
    """
    if not durable:
        return
    consumer_info = getattr(subscription, 'consumer_info', None)
    if consumer_info is None:
        return
    try:
        info = await consumer_info()
        actual = getattr(info, 'config', None)
        if actual is None:
            return
        differences = _config_differences(requested, actual)
    except Exception as exc:  # a diagnostic must not become a new failure point
        logger.debug('durable consumer %s: consumer info unavailable: %s', durable, exc)
        return
    if not differences:
        return
    logger.warning(
        'durable consumer %s on stream %s already exists, so nats-py reused its stored config and '
        'ignored the requested one: %s. The requested settings only apply on first creation; to '
        'apply them, delete this durable consumer (stream %s, consumer %s) and let it be recreated.',
        getattr(info, 'name', None) or durable, getattr(info, 'stream_name', None) or '?',
        ', '.join('%s requested=%r actual=%r' % (field, wanted, current)
                  for field, wanted, current in differences),
        getattr(info, 'stream_name', None) or '?', getattr(info, 'name', None) or durable)


async def consume(js, prefix, handler, durable='worker-1', max_deliver=3, total=None, fetch_timeout=1.0,
                  new_only=False, target=None, ack_wait=30, max_ack_pending=None):
    """Pull consumer with explicit ack.

    - target sets a per-device filter_subject; without it the consumer sees
      every target, which is only appropriate for an operations tool.
    - A message is executed on every delivery up to max_deliver; it reaches
      the DLQ only after the final allowed attempt still fails.
    - A malformed body is quarantined immediately instead of terminating the loop.
    - ack_wait must cover the slowest handler; in_progress() keepalive stops a
      long model call from being redelivered while it is still running.
    - max_ack_pending bounds unacknowledged messages in flight when set; leaving it
      unset keeps the server default. All three knobs are only sent on first creation,
      so an existing durable that disagrees with them is reported as a warning.
    """
    from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy
    filter_subject = prefix + '.task.' + target if target else prefix + '.task.>'
    requested = ConsumerConfig(ack_policy=AckPolicy.EXPLICIT, max_deliver=max_deliver, ack_wait=ack_wait,
                               max_ack_pending=max_ack_pending,
                               deliver_policy=DeliverPolicy.NEW if new_only else DeliverPolicy.ALL)
    subscription = await js.pull_subscribe(filter_subject, durable=durable, config=requested)
    await _warn_on_durable_drift(subscription, durable, requested)
    processed = 0
    while total is None or processed < total:
        try:
            messages = await subscription.fetch(1, timeout=fetch_timeout)
        except Exception:
            if total is not None:
                break
            continue
        for message in messages:
            try:
                body = json.loads(message.data)
                if not isinstance(body, dict):
                    raise ValueError('Task body must be an object')
            except (ValueError, TypeError):
                await _dead_letter(js, prefix, message, None)
                continue
            await _deliver(js, prefix, handler, message, body, max_deliver, ack_wait, target)
        processed += len(messages)
    return processed
