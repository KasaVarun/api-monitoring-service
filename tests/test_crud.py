import pytest


@pytest.mark.asyncio
async def test_create_list_update_delete(client):
    created = await client.post(
        "/api/endpoints",
        json={"name": "Example", "url": "https://example.com/health"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "Example"
    assert body["availability"] == "UNKNOWN"
    assert body["interval_seconds"] == 60
    assert body["last_status_code"] is None
    assert body["total_failures"] == 0
    endpoint_id = body["id"]

    listed = await client.get("/api/endpoints")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    updated = await client.patch(
        f"/api/endpoints/{endpoint_id}",
        json={"name": "Example Health", "interval_seconds": 120, "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Example Health"
    assert updated.json()["interval_seconds"] == 120
    assert updated.json()["enabled"] is False

    missing = await client.get("/api/endpoints/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"

    deleted = await client.delete(f"/api/endpoints/{endpoint_id}")
    assert deleted.status_code == 204
    listed = await client.get("/api/endpoints")
    assert listed.json() == []


@pytest.mark.asyncio
async def test_rejects_invalid_payloads(client):
    bad_scheme = await client.post(
        "/api/endpoints",
        json={"name": "Nope", "url": "ftp://example.com/x"},
    )
    assert bad_scheme.status_code == 422

    private = await client.post(
        "/api/endpoints",
        json={"name": "Local", "url": "http://127.0.0.1/secret"},
    )
    assert private.status_code == 422
    assert "not allowed" in private.json()["message"].lower() or "not allowed" in str(
        private.json()
    ).lower()

    interval = await client.post(
        "/api/endpoints",
        json={"name": "Fast", "url": "https://example.com/x", "interval_seconds": 1},
    )
    assert interval.status_code == 422

    empty = await client.post("/api/endpoints", json={"name": "  ", "url": "https://example.com"})
    assert empty.status_code == 422


@pytest.mark.asyncio
async def test_health_and_docs(client):
    health = await client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "database": "ok"}

    docs = await client.get("/docs")
    assert docs.status_code == 200
    openapi = await client.get("/openapi.json")
    assert openapi.status_code == 200
    assert "/api/endpoints" in openapi.json()["paths"]
