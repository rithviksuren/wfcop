from __future__ import annotations

from copy import deepcopy

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.diff import (
    apply_workflow_operations,
    diff_workflows,
    workflows_semantically_equal,
)
from copilot_api.llm import HeuristicProvider
from copilot_api.models import Workflow, WorkflowEdge, WorkflowNode
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


def base_workflow() -> Workflow:
    return Workflow(
        id="wf_diff",
        name="Email to Slack",
        owner_id="owner_1",
        version=3,
        nodes=[
            WorkflowNode(
                id="email",
                type="gmail_trigger",
                role="trigger",
                label="Receive Email",
                description="Receive unread email",
                config={"from_contains": "any sender", "search_text": ""},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                label="Notify Slack",
                description="Send a Slack message",
                config={
                    "channel_id": "general",
                    "message_template": "New email",
                },
            ),
        ],
        edges=[WorkflowEdge(from_="email", to="slack")],
    )


def target_workflow() -> Workflow:
    workflow = deepcopy(base_workflow())
    workflow.name = "Stripe Email Automation"
    workflow.version = 4
    workflow.nodes[0].config["from_contains"] = "Stripe"
    workflow.nodes[0].label = "Receive Stripe Email"
    workflow.nodes.pop()
    workflow.nodes.append(
        WorkflowNode(
            id="notion",
            type="notion_create_page",
            label="Archive in Notion",
            description="Create a Notion page",
            config={
                "data_source_id": "default",
                "title_template": "Email: {{subject}}",
                "content_template": "{{body}}",
            },
        )
    )
    workflow.edges = [WorkflowEdge(from_="email", to="notion")]
    return workflow


def build_client(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "diff.sqlite3"))
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    return TestClient(api_main.app), repository


def test_diff_operations_round_trip_complete_workflow_semantics():
    before = base_workflow()
    after = target_workflow()

    operations = diff_workflows(before, after)
    patched = apply_workflow_operations(before, operations)

    assert [operation.op for operation in operations] == [
        "update_workflow",
        "disconnect_nodes",
        "remove_node",
        "update_node",
        "add_node",
        "connect_nodes",
    ]
    update = next(
        operation for operation in operations if operation.op == "update_node"
    )
    assert update.node.label == "Receive Stripe Email"
    assert update.node.config["from_contains"] == "Stripe"
    assert workflows_semantically_equal(patched, after)
    assert before.nodes[0].config["from_contains"] == "any sender"


def test_operation_only_copilot_endpoint_persists_without_full_workflow(
    tmp_path,
    monkeypatch,
):
    client, repository = build_client(tmp_path, monkeypatch)
    service = api_main.app.dependency_overrides[api_main.get_service]()
    created = service.create(
        "When I receive an email from Stripe, send a Slack message to the finance team.",
        user_id="owner_1",
    ).workflow

    try:
        response = client.post(
            "/copilot/modify/operations",
            json={
                "workflow": created.model_dump(mode="json", by_alias=True),
                "instruction": (
                    "Also create a Notion page whenever an email arrives."
                ),
            },
            headers={"X-User-Id": "owner_1"},
        )

        assert response.status_code == 200
        body = response.json()
        assert "workflow" not in body
        assert body["workflow_id"] == created.id
        assert body["base_version"] == created.version
        assert body["target_version"] == created.version + 1
        assert body["persisted"] is True
        assert {"add_node", "connect_nodes"} <= {
            operation["op"] for operation in body["operations"]
        }
        stored = repository.get(created.id)
        assert [node.type for node in stored.nodes] == [
            "gmail_trigger",
            "slack_message",
            "notion_create_page",
        ]
    finally:
        api_main.app.dependency_overrides.clear()


def test_diff_and_apply_endpoints_support_optimistic_concurrency(
    tmp_path,
    monkeypatch,
):
    client, repository = build_client(tmp_path, monkeypatch)
    before = repository.save(base_workflow())
    after = target_workflow()

    try:
        diff_response = client.post(
            "/workflows/diff",
            json={
                "before": before.model_dump(mode="json", by_alias=True),
                "after": after.model_dump(mode="json", by_alias=True),
            },
            headers={"X-User-Id": "owner_1"},
        )

        assert diff_response.status_code == 200
        diff_body = diff_response.json()
        assert diff_body["persisted"] is False
        assert "workflow" not in diff_body

        conflict = client.patch(
            f"/workflows/{before.id}/operations",
            json={
                "expected_version": before.version - 1,
                "operations": diff_body["operations"],
            },
            headers={"X-User-Id": "owner_1"},
        )
        assert conflict.status_code == 409

        applied = client.patch(
            f"/workflows/{before.id}/operations",
            json={
                "expected_version": before.version,
                "operations": diff_body["operations"],
            },
            headers={"X-User-Id": "owner_1"},
        )
        assert applied.status_code == 200
        applied_body = applied.json()
        assert "workflow" not in applied_body
        assert applied_body["target_version"] == before.version + 1
        assert applied_body["validation"]["valid"] is True
        stored = repository.get(before.id)
        assert workflows_semantically_equal(stored, after)
    finally:
        api_main.app.dependency_overrides.clear()
