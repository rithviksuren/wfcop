from __future__ import annotations

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.llm import HeuristicProvider
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


USER_HEADERS = {"X-User-Id": "api_owner"}


def build_api_client(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "copilot-api.sqlite3"))
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    return TestClient(api_main.app), repository


def test_copilot_api_supports_create_modify_fix_and_explain(tmp_path, monkeypatch):
    client, repository = build_api_client(tmp_path, monkeypatch)

    created_response = client.post(
        "/copilot/create",
        json={
            "instruction": (
                "When I receive an email from Stripe, "
                "send a Slack message to the finance team."
            )
        },
        headers=USER_HEADERS,
    )

    assert created_response.status_code == 201
    created = created_response.json()
    assert created["validation"]["valid"] is True
    assert [node["type"] for node in created["workflow"]["nodes"]] == [
        "gmail_trigger",
        "slack_message",
    ]
    assert created["workflow"]["owner_id"] == "api_owner"
    assert repository.get(created["workflow"]["id"]) is not None

    modified_response = client.post(
        "/copilot/modify",
        json={
            "workflow": created["workflow"],
            "instruction": (
                "Also create a Notion page whenever an email arrives."
            ),
        },
        headers=USER_HEADERS,
    )

    assert modified_response.status_code == 200
    modified = modified_response.json()
    assert modified["validation"]["valid"] is True
    assert [node["type"] for node in modified["workflow"]["nodes"]] == [
        "gmail_trigger",
        "slack_message",
        "notion_create_page",
    ]
    assert any(
        operation["op"] == "add_node"
        for operation in modified["operations"]
    )

    invalid_workflow = modified["workflow"]
    slack = next(
        node
        for node in invalid_workflow["nodes"]
        if node["type"] == "slack_message"
    )
    del slack["config"]["channel_id"]

    fixed_response = client.post(
        "/copilot/fix",
        json={
            "workflow": invalid_workflow,
            "instruction": "Fix the workflow.",
            "validation_errors": [
                {
                    "node": "slack_message",
                    "error": "channel_id missing",
                }
            ],
        },
        headers=USER_HEADERS,
    )

    assert fixed_response.status_code == 200
    fixed = fixed_response.json()
    assert fixed["validation"]["valid"] is True
    repaired_slack = next(
        node
        for node in fixed["workflow"]["nodes"]
        if node["type"] == "slack_message"
    )
    assert repaired_slack["config"]["channel_id"] == "finance"

    explain_response = client.post(
        "/copilot/explain",
        json={
            "workflow": fixed["workflow"],
            "instruction": "Explain this workflow.",
        },
        headers=USER_HEADERS,
    )

    assert explain_response.status_code == 200
    explained = explain_response.json()
    assert "Gmail checks for a new unread email from Stripe." in explained["explanation"]
    assert "It sends a Slack message to #finance" in explained["explanation"]
    assert "It creates a Notion page" in explained["explanation"]

    api_main.app.dependency_overrides.clear()


def test_copilot_api_requires_authentication(tmp_path, monkeypatch):
    client, _ = build_api_client(tmp_path, monkeypatch)

    response = client.post(
        "/copilot/create",
        json={"instruction": "When an email arrives, create a task."},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Sign in is required."}
    api_main.app.dependency_overrides.clear()


def test_copilot_api_rejects_blank_instructions(tmp_path, monkeypatch):
    client, _ = build_api_client(tmp_path, monkeypatch)

    response = client.post(
        "/copilot/create",
        json={"instruction": "   "},
        headers=USER_HEADERS,
    )

    assert response.status_code == 422
    api_main.app.dependency_overrides.clear()


def test_openapi_documents_all_copilot_capabilities():
    schema = api_main.app.openapi()

    assert schema["info"]["version"] == "0.2.0"

    expected_status = {
        "/copilot/create": "201",
        "/copilot/modify": "200",
        "/copilot/fix": "200",
        "/copilot/explain": "200",
    }
    for path, success_status in expected_status.items():
        operation = schema["paths"][path]["post"]
        assert operation["tags"] == ["Copilot"]
        assert success_status in operation["responses"]
        assert "401" in operation["responses"]
        assert "422" in operation["responses"]
        assert "502" in operation["responses"]
        assert operation["requestBody"]["required"] is True

    operation_only = schema["paths"]["/copilot/modify/operations"]["post"]
    assert "200" in operation_only["responses"]
    assert "409" in operation_only["responses"]
    assert (
        operation_only["summary"]
        == "Modify a workflow and return operations only"
    )
    assert "/copilot/conversations" in schema["paths"]
    assert (
        "/copilot/conversations/{conversation_id}/messages"
        in schema["paths"]
    )
    assert "/copilot/plans" in schema["paths"]
    assert "/copilot/plans/stream" in schema["paths"]
    assert "/copilot/plans/{session_id}/answers" in schema["paths"]
    assert (
        "/copilot/plans/{session_id}/answers/stream"
        in schema["paths"]
    )


def test_health_identifies_loaded_copilot_feature_set():
    response = TestClient(api_main.app).get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["api_version"] == "0.2.0"
    assert payload["provider"]
    assert {
        "workflow_create",
        "workflow_modify",
        "workflow_fix",
        "workflow_explain",
        "workflow_validation",
        "workflow_persistence",
        "workflow_diffing",
        "conversation_memory",
        "tool_calling",
        "multi_step_planning",
        "streaming_responses",
    } <= set(payload["features"])
