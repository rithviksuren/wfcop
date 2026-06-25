from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.llm import HeuristicProvider, LLMProvider
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


def build_service(database_path, provider=None):
    repository = WorkflowRepository(str(database_path))
    service = CopilotService(
        provider=provider or HeuristicProvider(),
        repository=repository,
    )
    return service, repository


def test_follow_up_uses_prior_workflow_and_replaces_slack_with_teams(tmp_path):
    service, repository = build_service(tmp_path / "memory.sqlite3")

    started = service.start_conversation(
        "Build an email notification workflow.",
        user_id="owner_1",
    )
    continued = service.continue_conversation(
        started.conversation.id,
        "Actually send notifications to Teams instead.",
        user_id="owner_1",
    )

    assert [node.type for node in started.turn.workflow.nodes] == [
        "gmail_trigger",
        "slack_message",
    ]
    assert [node.type for node in continued.turn.workflow.nodes] == [
        "gmail_trigger",
        "teams_message",
    ]
    assert "slack_message" not in {
        node.type for node in continued.turn.workflow.nodes
    }
    teams = continued.turn.workflow.nodes[1]
    assert teams.config["channel_id"] == "general"
    assert teams.config["message_template"] == (
        "New email from {{from}}: {{subject}}"
    )
    assert any(
        operation.op == "update_node"
        for operation in continued.turn.operations
    )
    assert repository.get(continued.turn.workflow.id).nodes[1].type == (
        "teams_message"
    )


def test_conversation_memory_survives_service_restart(tmp_path):
    database_path = tmp_path / "restart-memory.sqlite3"
    first_service, _ = build_service(database_path)
    started = first_service.start_conversation(
        "Build an email notification workflow.",
        user_id="owner_1",
    )

    restarted_service, _ = build_service(database_path)
    continued = restarted_service.continue_conversation(
        started.conversation.id,
        "Actually send notifications to Teams instead.",
        user_id="owner_1",
    )
    detail = restarted_service.get_conversation(
        started.conversation.id,
        user_id="owner_1",
    )

    assert [turn.sequence for turn in detail.turns] == [1, 2]
    assert detail.turns[0].instruction == (
        "Build an email notification workflow."
    )
    assert detail.turns[1].instruction == (
        "Actually send notifications to Teams instead."
    )
    assert continued.conversation.workflow_id == continued.turn.workflow.id


def test_conversation_memory_is_owner_scoped(tmp_path):
    service, _ = build_service(tmp_path / "isolation.sqlite3")
    started = service.start_conversation(
        "Build an email notification workflow.",
        user_id="owner_1",
    )

    assert service.list_conversations("stranger") == []
    try:
        service.get_conversation(started.conversation.id, "stranger")
    except LookupError:
        pass
    else:
        raise AssertionError("Conversation memory must be owner-scoped.")


def test_recent_conversation_history_is_passed_to_the_llm(tmp_path):
    class CapturingProvider(LLMProvider):
        name = "capturing"

        def __init__(self) -> None:
            self.delegate = HeuristicProvider()
            self.calls: list[tuple[str, dict[str, Any]]] = []

        def generate(self, task: str, payload: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((task, payload))
            return self.delegate.generate(task, payload)

    provider = CapturingProvider()
    service, _ = build_service(
        tmp_path / "capturing.sqlite3",
        provider=provider,
    )
    started = service.start_conversation(
        "Build an email notification workflow.",
        user_id="owner_1",
    )
    service.continue_conversation(
        started.conversation.id,
        "Actually send notifications to Teams instead.",
        user_id="owner_1",
    )

    modify_payload = next(
        payload for task, payload in provider.calls if task == "modify"
    )
    history = modify_payload["context"]["conversation_history"]
    assert history == [
        {
            "sequence": 1,
            "instruction": "Build an email notification workflow.",
            "workflow_name": started.turn.workflow.name,
            "node_types": ["gmail_trigger", "slack_message"],
        }
    ]


def test_conversation_api_continues_without_resending_workflow(
    tmp_path,
    monkeypatch,
):
    service, repository = build_service(tmp_path / "conversation-api.sqlite3")
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    client = TestClient(api_main.app)
    headers = {"X-User-Id": "owner_1"}

    try:
        started = client.post(
            "/copilot/conversations",
            json={"instruction": "Build an email notification workflow."},
            headers=headers,
        )
        assert started.status_code == 201
        conversation_id = started.json()["conversation"]["id"]

        continued = client.post(
            f"/copilot/conversations/{conversation_id}/messages",
            json={
                "instruction": (
                    "Actually send notifications to Teams instead."
                )
            },
            headers=headers,
        )
        assert continued.status_code == 200
        assert [
            node["type"]
            for node in continued.json()["turn"]["workflow"]["nodes"]
        ] == ["gmail_trigger", "teams_message"]

        detail = client.get(
            f"/copilot/conversations/{conversation_id}",
            headers=headers,
        )
        assert detail.status_code == 200
        assert len(detail.json()["turns"]) == 2

        listed = client.get(
            "/copilot/conversations",
            headers=headers,
        )
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [conversation_id]
    finally:
        api_main.app.dependency_overrides.clear()
