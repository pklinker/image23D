"""SSE stream tests -- PLAN-BUGFIX.md item 4.

Runs against the real Redis (db 15, per conftest) because the bugs were all in
the interaction with pubsub: subscribing too late, never closing the
subscription, and raising an HTTP error from inside a generator.
"""
import asyncio
import json
import uuid

import pytest

import api.app.main as main
from common.db import SessionLocal
from common.models import Job


@pytest.fixture
async def redis_client():
    from redis import asyncio as aioredis

    from common.settings import settings

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    main.app.state.redis = client
    yield client
    await client.aclose()


async def _make_job(status="running", **fields) -> uuid.UUID:
    async with SessionLocal() as session:
        job = Job(
            input_object_key="uploads/test.png",
            params={},
            status=status,
            stage_timings=[],
            **fields,
        )
        session.add(job)
        await session.commit()
        return job.id


def _payload(frame: str) -> dict:
    assert frame.startswith("data: "), frame
    return json.loads(frame[6:])


async def test_terminal_job_yields_one_frame_and_closes(redis_client):
    job_id = await _make_job(status="succeeded")
    frames = [f async for f in main._event_stream(job_id)]

    assert len(frames) == 1
    assert _payload(frames[0])["status"] == "succeeded"


async def test_subscription_is_live_before_the_first_frame(redis_client):
    """The ordering bug: the row was read before subscribing, so a notification
    landing in that window was lost. If the lost one was terminal, the stream
    never closed and the browser's EventSource hung."""
    job_id = await _make_job(status="running")
    channel = f"job:{job_id}:events"

    stream = main._event_stream(job_id)
    await stream.__anext__()  # snapshot frame

    subscribers = await redis_client.execute_command("PUBSUB", "NUMSUB", channel)
    assert int(subscribers[1]) == 1, "must already be subscribed when the snapshot is emitted"

    await stream.aclose()


async def test_terminal_notification_closes_the_stream(redis_client):
    job_id = await _make_job(status="running")
    channel = f"job:{job_id}:events"
    frames = []

    async def consume():
        async for frame in main._event_stream(job_id):
            frames.append(frame)

    task = asyncio.create_task(consume())
    while int((await redis_client.execute_command("PUBSUB", "NUMSUB", channel))[1]) == 0:
        await asyncio.sleep(0.01)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = "succeeded"
        await session.commit()
    await redis_client.publish(channel, json.dumps({"status": "succeeded"}))

    await asyncio.wait_for(task, timeout=5)

    assert _payload(frames[0])["status"] == "running"
    assert _payload(frames[-1])["status"] == "succeeded"


async def test_every_frame_is_a_full_status_object(redis_client):
    """The worker publishes partial payloads ({"status": ...}) which used to be
    forwarded verbatim, so frames had inconsistent shapes."""
    job_id = await _make_job(status="running")
    channel = f"job:{job_id}:events"
    frames = []

    async def consume():
        async for frame in main._event_stream(job_id):
            frames.append(frame)

    task = asyncio.create_task(consume())
    while int((await redis_client.execute_command("PUBSUB", "NUMSUB", channel))[1]) == 0:
        await asyncio.sleep(0.01)

    await redis_client.publish(channel, json.dumps({"status": "running", "stage": "shape_upsample"}))
    await asyncio.sleep(0.2)

    async with SessionLocal() as session:
        job = await session.get(Job, job_id)
        job.status = "failed"
        await session.commit()
    await redis_client.publish(channel, json.dumps({"status": "failed"}))
    await asyncio.wait_for(task, timeout=5)

    required = {"job_id", "status", "stage", "stage_timings", "coarse_glb_key", "total_seconds"}
    for frame in frames:
        assert required <= set(_payload(frame)), "every frame must be a complete JobStatusResponse"


async def test_heartbeat_keeps_an_idle_stream_open(redis_client, monkeypatch):
    """Idle streams get cut by proxies; a comment frame holds the connection."""
    monkeypatch.setattr(main, "SSE_HEARTBEAT_SECONDS", 0.1)
    job_id = await _make_job(status="running")

    stream = main._event_stream(job_id)
    await stream.__anext__()  # snapshot
    beat = await asyncio.wait_for(stream.__anext__(), timeout=3)

    assert beat.startswith(":"), "heartbeat must be an SSE comment, not a data frame"
    await stream.aclose()


async def test_stream_releases_its_redis_connection(redis_client):
    """`unsubscribe` alone left the pubsub connection checked out, leaking one
    per stream opened."""
    pool = redis_client.connection_pool

    for _ in range(15):
        job_id = await _make_job(status="succeeded")
        async for _frame in main._event_stream(job_id):
            pass

    assert len(pool._in_use_connections) == 0


async def test_missing_job_produces_no_frames(redis_client):
    """The 404 belongs to the route: raising HTTPException from the generator
    happens after the headers are on the wire, producing a broken stream."""
    frames = [f async for f in main._event_stream(uuid.uuid4())]
    assert frames == []


# --- the 404 belongs to the route, not the generator ----------------------


@pytest.fixture
def client():
    import httpx

    from api.app.auth import require_api_key
    from common.models import ApiKey

    main.app.dependency_overrides[require_api_key] = lambda: ApiKey(name="test", key_hash="x")
    transport = httpx.ASGITransport(app=main.app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")
    main.app.dependency_overrides.clear()


async def test_unknown_job_returns_a_real_404(client, redis_client):
    """Previously the check lived inside the generator, so the 200 headers were
    already sent and the client got a truncated stream instead of a 404."""
    async with client as http:
        response = await http.get(f"/v1/jobs/{uuid.uuid4()}/events")

    assert response.status_code == 404
    assert response.json()["detail"] == "job not found"


async def test_stream_sets_proxy_safe_headers(client, redis_client):
    job_id = await _make_job(status="succeeded")
    async with client as http:
        response = await http.get(f"/v1/jobs/{job_id}/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    # Without this nginx buffers the whole stream and delivers it at the end.
    assert response.headers["x-accel-buffering"] == "no"
