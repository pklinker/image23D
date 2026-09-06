"""Job creation API tests -- PLAN-BUGFIX.md item 7.

Any `object_key` string used to be accepted and queued, so a typo cost a GPU
slot and ~60s to discover. And `params` was an untyped dict: stored, never read,
never validated.
"""
import uuid

import httpx
import pytest

import api.app.main as main
from api.app.auth import require_api_key
from common.db import SessionLocal
from common.models import ApiKey, Job
from common.schemas import DEFAULT_FACE_COUNT

pytestmark = pytest.mark.usefixtures("clean_tables")


class _StubQueue:
    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, name, *args):
        self.enqueued.append((name, args))


@pytest.fixture
def api(monkeypatch):
    """The app with auth, object storage and the queue stubbed out."""
    queue = _StubQueue()
    main.app.state.redis_pool = queue
    main.app.dependency_overrides[require_api_key] = lambda: ApiKey(name="test", key_hash="x")
    # Rate limiting needs Redis; the routes' own dependency chain resolves the
    # key through it, so bypass it here too.
    for dep in (main.require_job_creation_rate_limit, main.require_upload_rate_limit):
        main.app.dependency_overrides[dep] = lambda: ApiKey(name="test", key_hash="x")

    existing = {"uploads/real.png"}
    monkeypatch.setattr(main, "object_exists", lambda key: key in existing)

    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
    client.queue = queue
    client.existing = existing
    yield client
    main.app.dependency_overrides.clear()


async def _post_job(api, **body):
    async with api as http:
        return await http.post("/v1/jobs", json={"object_key": "uploads/real.png", **body})


# --- object_key validation ------------------------------------------------


async def test_valid_key_is_accepted_and_queued(api):
    response = await _post_job(api)

    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert api.queue.enqueued == [("run_pipeline_job", (job_id,))]


async def test_key_outside_the_upload_prefix_is_rejected(api):
    response = await _post_job(api, object_key="artifacts/somebody-elses/final.glb")

    assert response.status_code == 400
    assert "uploads/" in response.json()["detail"]
    assert api.queue.enqueued == []


async def test_missing_object_is_rejected_before_burning_a_gpu_slot(api):
    """This is the whole point of item 7: a typo should cost milliseconds, not
    a queue slot and a minute of GPU time."""
    response = await _post_job(api, object_key="uploads/typo.png")

    assert response.status_code == 404
    assert api.queue.enqueued == []


# --- params ---------------------------------------------------------------


async def test_params_may_be_omitted_entirely(api):
    """The viewer sends none, and must keep working."""
    response = await _post_job(api)
    assert response.status_code == 202

    async with SessionLocal() as session:
        job = await session.get(Job, uuid.UUID(response.json()["job_id"]))
    assert job.params["bbox"] is None
    assert job.params["seed"] is None


async def test_effective_params_are_recorded(api):
    """Defaults are filled in, so the row says what actually ran rather than
    what the caller happened to mention."""
    response = await _post_job(api, params={"seed": 99})
    assert response.status_code == 202

    async with SessionLocal() as session:
        job = await session.get(Job, uuid.UUID(response.json()["job_id"]))
    assert job.params == {"seed": 99, "target_face_count": DEFAULT_FACE_COUNT, "bbox": None}


async def test_bbox_is_accepted_when_supplied(api):
    response = await _post_job(api, params={"bbox": [0.2, 0.1, 0.6, 0.95]})
    assert response.status_code == 202

    async with SessionLocal() as session:
        job = await session.get(Job, uuid.UUID(response.json()["job_id"]))
    assert job.params["bbox"] == [0.2, 0.1, 0.6, 0.95]


async def test_malformed_bbox_is_rejected_not_ignored(api):
    """Silently dropping a bad bbox would hand back a plausible-looking model
    built from the wrong region, with no signal the integration is broken."""
    response = await _post_job(api, params={"bbox": [0.9, 0.1, 0.2, 0.5]})

    assert response.status_code == 422
    assert api.queue.enqueued == []


async def test_out_of_range_bbox_is_rejected(api):
    response = await _post_job(api, params={"bbox": [0, 0, 2.0, 1.0]})
    assert response.status_code == 422


async def test_unknown_param_is_rejected(api):
    response = await _post_job(api, params={"target_face_counts": 100_000})
    assert response.status_code == 422


async def test_out_of_range_face_count_is_rejected(api):
    response = await _post_job(api, params={"target_face_count": 5_000_000})
    assert response.status_code == 422


# --- uploads --------------------------------------------------------------


async def test_upload_rejects_a_non_image_content_type(api):
    async with api as http:
        response = await http.post(
            "/v1/uploads", json={"filename": "x.exe", "content_type": "application/x-msdownload"}
        )
    assert response.status_code == 422


# --- API key scopes (PLAN-BUGFIX.md item 10) ------------------------------


@pytest.fixture
def api_as():
    """Client authenticated as a key with a given scope."""
    import contextlib

    @contextlib.asynccontextmanager
    async def _make(scope, key_id=None):
        actor = ApiKey(id=key_id or uuid.uuid4(), name=f"{scope}-key", scope=scope, key_hash="x")
        main.app.dependency_overrides[require_api_key] = lambda: actor
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        try:
            async with client as http:
                yield http, actor
        finally:
            main.app.dependency_overrides.clear()

    return _make


async def test_service_key_cannot_mint_keys(api_as):
    """A key handed to an integration must not be able to issue itself more."""
    async with api_as("service") as (http, _):
        response = await http.post("/v1/api-keys", json={"name": "sneaky"})
    assert response.status_code == 403


async def test_service_key_cannot_list_or_revoke(api_as):
    async with api_as("service") as (http, _):
        assert (await http.get("/v1/api-keys")).status_code == 403
        assert (await http.post(f"/v1/api-keys/{uuid.uuid4()}/revoke")).status_code == 403


async def test_admin_key_mints_service_keys_by_default(api_as):
    """Least privilege: admin has to be asked for explicitly."""
    async with api_as("admin") as (http, _):
        response = await http.post("/v1/api-keys", json={"name": "integration"})

    assert response.status_code == 200
    assert response.json()["scope"] == "service"
    assert response.json()["key"].startswith("i23d_")


async def test_admin_can_mint_another_admin_explicitly(api_as):
    async with api_as("admin") as (http, _):
        response = await http.post("/v1/api-keys", json={"name": "ops", "scope": "admin"})
    assert response.json()["scope"] == "admin"


async def test_unknown_scope_is_rejected(api_as):
    async with api_as("admin") as (http, _):
        response = await http.post("/v1/api-keys", json={"name": "x", "scope": "superuser"})
    assert response.status_code == 422


async def test_admin_cannot_revoke_its_own_key(api_as):
    """Minting the first key means writing directly to Postgres, so locking
    yourself out has no in-band way back."""
    actor_id = uuid.uuid4()
    async with api_as("admin", key_id=actor_id) as (http, _):
        response = await http.post(f"/v1/api-keys/{actor_id}/revoke")

    assert response.status_code == 400
    assert "cannot revoke" in response.json()["detail"]


async def test_admin_can_revoke_another_key(api_as):
    async with api_as("admin") as (http, _):
        created = await http.post("/v1/api-keys", json={"name": "doomed"})
        response = await http.post(f"/v1/api-keys/{created.json()['id']}/revoke")

    assert response.status_code == 200
    assert response.json() == {"revoked": True}
