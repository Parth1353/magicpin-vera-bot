"""The HTTP contract the judge harness depends on."""

import pytest
from fastapi.testclient import TestClient

from vera_bot import app as app_module


@pytest.fixture()
def client():
    app_module.contexts.clear()
    app_module.conversations.clear()
    app_module.planner.clear()
    with TestClient(app_module.app) as test_client:
        yield test_client


def _push(client, scope, context_id, version, payload):
    return client.post("/v1/context", json={"scope": scope, "context_id": context_id,
                                            "version": version, "payload": payload,
                                            "delivered_at": "2026-04-26T10:00:00Z"})


def test_healthz_shape(client):
    body = client.get("/v1/healthz").json()
    assert body["status"] == "ok"
    assert isinstance(body["uptime_seconds"], int)
    assert set(body["contexts_loaded"]) == {"category", "merchant", "customer", "trigger"}


def test_metadata_shape(client):
    body = client.get("/v1/metadata").json()
    for field in ("team_name", "team_members", "model", "approach", "contact_email",
                  "version", "submitted_at"):
        assert body.get(field), field


def test_context_push_is_idempotent_and_versioned(client, categories):
    payload = categories["dentists"]
    first = _push(client, "category", "dentists", 1, payload)
    assert first.status_code == 200 and first.json()["accepted"] is True
    assert first.json()["ack_id"] and first.json()["stored_at"]

    replay = _push(client, "category", "dentists", 1, payload)
    assert replay.status_code == 409
    assert replay.json() == {"accepted": False, "reason": "stale_version", "current_version": 1}

    bumped = _push(client, "category", "dentists", 2, payload)
    assert bumped.status_code == 200 and bumped.json()["accepted"] is True

    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1


def test_invalid_scope_is_rejected(client):
    response = _push(client, "widget", "x", 1, {})
    assert response.status_code == 400
    assert response.json()["reason"] == "invalid_scope"


def test_tick_returns_empty_actions_when_nothing_is_loaded(client):
    response = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z",
                                             "available_triggers": ["nope"]})
    assert response.status_code == 200
    assert response.json() == {"actions": []}


def test_tick_action_carries_every_required_field(client, categories, merchants, triggers):
    _push(client, "category", "dentists", 1, categories["dentists"])
    merchant = merchants["m_001_drmeera_dentist_delhi"]
    _push(client, "merchant", merchant["merchant_id"], 1, merchant)
    trigger = triggers["trg_001_research_digest_dentists"]
    _push(client, "trigger", trigger["id"], 1, trigger)

    body = client.post("/v1/tick", json={"now": "2026-04-26T10:35:00Z",
                                         "available_triggers": [trigger["id"]]}).json()
    assert len(body["actions"]) == 1
    action = body["actions"][0]
    for field in ("conversation_id", "merchant_id", "customer_id", "send_as", "trigger_id",
                  "template_name", "template_params", "body", "cta", "suppression_key",
                  "rationale"):
        assert field in action, field
    assert action["merchant_id"] == merchant["merchant_id"]
    assert action["trigger_id"] == trigger["id"]
    assert action["suppression_key"] == trigger["suppression_key"]
    assert action["send_as"] == "vera"
    assert action["body"].strip()
    assert action["template_params"]


def test_the_same_trigger_is_not_sent_twice(client, categories, merchants, triggers):
    _push(client, "category", "dentists", 1, categories["dentists"])
    merchant = merchants["m_001_drmeera_dentist_delhi"]
    _push(client, "merchant", merchant["merchant_id"], 1, merchant)
    trigger = triggers["trg_001_research_digest_dentists"]
    _push(client, "trigger", trigger["id"], 1, trigger)

    payload = {"now": "2026-04-26T10:35:00Z", "available_triggers": [trigger["id"]]}
    first = client.post("/v1/tick", json=payload).json()["actions"]
    second = client.post("/v1/tick", json=payload).json()["actions"]
    assert len(first) == 1 and second == []


def test_reply_on_an_unknown_conversation_is_handled(client):
    body = client.post("/v1/reply", json={
        "conversation_id": "never_seen", "merchant_id": "m_unknown",
        "from_role": "merchant", "message": "Ok lets do it. Whats next?",
        "received_at": "2026-04-26T10:45:00Z", "turn_number": 2}).json()
    assert body["action"] in ("send", "wait", "end")
    assert body["rationale"]


def test_reply_actions_have_the_right_shape(client):
    send = client.post("/v1/reply", json={
        "conversation_id": "c1", "merchant_id": "m1", "from_role": "merchant",
        "message": "yes please", "turn_number": 2}).json()
    assert send["action"] == "send" and send["body"] and send["cta"]

    end = client.post("/v1/reply", json={
        "conversation_id": "c2", "merchant_id": "m2", "from_role": "merchant",
        "message": "stop messaging me", "turn_number": 2}).json()
    assert end == {"action": "end", "rationale": end["rationale"]}

    wait = client.post("/v1/reply", json={
        "conversation_id": "c3", "merchant_id": "m3", "from_role": "merchant",
        "message": "call me next week", "turn_number": 2}).json()
    assert wait["action"] == "wait" and isinstance(wait["wait_seconds"], int)


def test_malformed_body_never_produces_a_500(client):
    assert client.post("/v1/tick", json={"available_triggers": "not-a-list"}).status_code in (200, 422)
    assert client.post("/v1/context", json={"scope": "merchant"}).status_code in (400, 422)


def test_teardown_wipes_state(client, categories):
    _push(client, "category", "dentists", 1, categories["dentists"])
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 1
    client.post("/v1/teardown", json={})
    assert client.get("/v1/healthz").json()["contexts_loaded"]["category"] == 0
