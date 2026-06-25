from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.llm import HeuristicProvider
from copilot_api.models import Workflow, WorkflowEdge, WorkflowNode
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


def valid_workflow() -> Workflow:
    return Workflow(
        name="Stripe Emails to Finance Slack",
        owner_id="owner_1",
        created_by="owner_1",
        updated_by="owner_1",
        nodes=[
            WorkflowNode(
                id="gmail",
                type="gmail_trigger",
                role="trigger",
                config={"from_contains": "Stripe", "search_text": ""},
            ),
            WorkflowNode(
                id="slack",
                type="slack_message",
                config={
                    "channel_id": "finance",
                    "message_template": "New email from {{from}}: {{subject}}",
                },
            ),
        ],
        edges=[WorkflowEdge(from_="gmail", to="slack")],
    )


def test_sqlite_repository_persists_workflows_across_instances(tmp_path):
    database_path = tmp_path / "persistent.sqlite3"
    first_repository = WorkflowRepository(str(database_path))
    workflow = first_repository.save(valid_workflow())

    second_repository = WorkflowRepository(str(database_path))
    restored = second_repository.get(workflow.id)
    summaries = second_repository.list(user_id="owner_1")

    assert restored == workflow
    assert [summary.id for summary in summaries] == [workflow.id]
    assert summaries[0].name == "Stripe Emails to Finance Slack"
    assert summaries[0].node_count == 2


def test_sqlite_repository_updates_and_deletes_persistently(tmp_path):
    database_path = tmp_path / "update-delete.sqlite3"
    repository = WorkflowRepository(str(database_path))
    workflow = repository.save(valid_workflow())
    workflow.name = "Updated workflow"
    workflow.version += 1
    repository.save(workflow)

    reopened = WorkflowRepository(str(database_path))
    assert reopened.get(workflow.id).name == "Updated workflow"
    assert reopened.get(workflow.id).version == 2
    assert reopened.delete(workflow.id) is True

    final_repository = WorkflowRepository(str(database_path))
    assert final_repository.get(workflow.id) is None
    assert final_repository.list(user_id="owner_1") == []


def test_repository_initializes_integrity_pragmas_and_indexes(tmp_path):
    database_path = tmp_path / "schema.sqlite3"
    WorkflowRepository(str(database_path))

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }

    assert version == 3
    assert journal_mode.lower() == "wal"
    assert "idx_workflows_updated_at" in indexes
    assert "idx_workflow_runs_workflow_started" in indexes


def test_repository_rejects_orphaned_run_and_task_records(tmp_path):
    from copilot_api.models import WorkflowRun, WorkflowTask

    repository = WorkflowRepository(str(tmp_path / "integrity.sqlite3"))

    try:
        repository.save_run(
            WorkflowRun(workflow_id="missing", trigger_type="manual")
        )
    except ValueError as exc:
        assert "workflow missing does not exist" in str(exc)
    else:
        raise AssertionError("Orphaned workflow run should be rejected.")

    workflow = repository.save(valid_workflow())
    try:
        repository.save_task(
            WorkflowTask(
                workflow_id=workflow.id,
                run_id="missing_run",
                title="Orphaned task",
            )
        )
    except ValueError as exc:
        assert "run missing_run does not exist" in str(exc)
    else:
        raise AssertionError("Orphaned workflow task should be rejected.")


def test_workflow_crud_api_survives_repository_restart(tmp_path, monkeypatch):
    database_path = tmp_path / "workflow-api.sqlite3"
    repository = WorkflowRepository(str(database_path))
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    client = TestClient(api_main.app)
    headers = {"X-User-Id": "owner_1"}

    try:
        created_response = client.post(
            "/copilot/create",
            json={
                "instruction": (
                    "When I receive an email from Stripe, "
                    "send a Slack message to the finance team."
                )
            },
            headers=headers,
        )
        assert created_response.status_code == 201
        workflow = created_response.json()["workflow"]

        restarted_repository = WorkflowRepository(str(database_path))
        restarted_service = CopilotService(
            provider=HeuristicProvider(),
            repository=restarted_repository,
        )
        monkeypatch.setattr(api_main, "repository", restarted_repository)
        api_main.app.dependency_overrides[api_main.get_service] = (
            lambda: restarted_service
        )

        retrieved = client.get(
            f"/workflows/{workflow['id']}",
            headers=headers,
        )
        assert retrieved.status_code == 200
        assert retrieved.json()["name"] == workflow["name"]

        listed = client.get("/workflows", headers=headers)
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == [workflow["id"]]

        updated = client.patch(
            f"/workflows/{workflow['id']}",
            json={"name": "Persisted workflow update"},
            headers=headers,
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Persisted workflow update"

        after_update_restart = WorkflowRepository(str(database_path))
        assert (
            after_update_restart.get(workflow["id"]).name
            == "Persisted workflow update"
        )

        deleted = client.delete(
            f"/workflows/{workflow['id']}",
            headers=headers,
        )
        assert deleted.status_code == 204
        assert WorkflowRepository(str(database_path)).get(workflow["id"]) is None
    finally:
        api_main.app.dependency_overrides.clear()


def test_workflow_retrieval_enforces_visibility(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "visibility.sqlite3"))
    workflow = repository.save(valid_workflow())
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    client = TestClient(api_main.app)

    try:
        response = client.get(
            f"/workflows/{workflow.id}",
            headers={"X-User-Id": "stranger"},
        )
        assert response.status_code == 403
    finally:
        api_main.app.dependency_overrides.clear()


def test_generic_save_endpoint_preserves_ownership_and_blocks_overwrite(
    tmp_path,
    monkeypatch,
):
    repository = WorkflowRepository(str(tmp_path / "save-security.sqlite3"))
    workflow = repository.save(valid_workflow())
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    client = TestClient(api_main.app)

    try:
        hostile_payload = workflow.model_dump(mode="json", by_alias=True)
        hostile_payload["name"] = "Hijacked"
        hostile_payload["owner_id"] = "stranger"
        forbidden = client.post(
            "/workflows",
            json={"workflow": hostile_payload},
            headers={"X-User-Id": "stranger"},
        )
        assert forbidden.status_code == 403
        assert repository.get(workflow.id).name != "Hijacked"

        owner_payload = workflow.model_dump(mode="json", by_alias=True)
        owner_payload["name"] = "Owner update"
        owner_payload["owner_id"] = "stranger"
        saved = client.post(
            "/workflows",
            json={"workflow": owner_payload},
            headers={"X-User-Id": "owner_1"},
        )
        assert saved.status_code == 200
        assert saved.json()["name"] == "Owner update"
        assert saved.json()["owner_id"] == "owner_1"
        assert saved.json()["version"] == workflow.version + 1
    finally:
        api_main.app.dependency_overrides.clear()
