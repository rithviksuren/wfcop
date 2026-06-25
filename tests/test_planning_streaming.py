from __future__ import annotations

import json

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.llm import HeuristicProvider
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService
from copilot_api.validation import validate_workflow


HEADERS = {"X-User-Id": "planner_1"}


def build_client(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "planning.sqlite3"))
    service = CopilotService(
        provider=HeuristicProvider(),
        repository=repository,
    )
    monkeypatch.setattr(api_main, "repository", repository)
    api_main.app.dependency_overrides[api_main.get_service] = lambda: service
    return TestClient(api_main.app), service, repository


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(
            next(line[6:] for line in lines if line.startswith("data: "))
        )
        events.append((event, data))
    return events


def test_broad_lead_capture_request_plans_and_asks_clarifying_questions(
    tmp_path,
    monkeypatch,
):
    client, _, repository = build_client(tmp_path, monkeypatch)

    try:
        response = client.post(
            "/copilot/plans",
            json={"instruction": "Build a lead capture system."},
            headers=HEADERS,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["analysis"] is None
        assert body["session"]["status"] == "awaiting_clarification"
        assert [question["id"] for question in body["session"]["questions"]] == [
            "lead_source",
            "lead_destination",
            "lead_notification",
        ]
        assert [step["status"] for step in body["session"]["steps"]] == [
            "completed",
            "in_progress",
            "pending",
            "pending",
        ]
        assert repository.get_plan_session(
            body["session"]["id"],
            "planner_1",
        ) is not None
    finally:
        api_main.app.dependency_overrides.clear()


def test_clarification_answers_generate_and_validate_the_workflow(
    tmp_path,
    monkeypatch,
):
    client, _, _ = build_client(tmp_path, monkeypatch)

    try:
        started = client.post(
            "/copilot/plans",
            json={"instruction": "Build a lead capture system."},
            headers=HEADERS,
        ).json()
        session_id = started["session"]["id"]

        continued = client.post(
            f"/copilot/plans/{session_id}/answers",
            json={
                "answers": {
                    "lead_source": "Customer form submission",
                    "lead_destination": "HubSpot CRM",
                    "lead_notification": "Slack",
                }
            },
            headers=HEADERS,
        )

        assert continued.status_code == 200
        body = continued.json()
        assert body["session"]["status"] == "completed"
        assert body["session"]["resolved_instruction"] == (
            "Whenever a customer fills a form, update HubSpot CRM, notify Slack."
        )
        assert [node["type"] for node in body["analysis"]["proposed_workflow"]["nodes"]] == [
            "form_submission_trigger",
            "crm_update",
            "slack_message",
        ]
        assert validate_workflow(
            api_main.Workflow.model_validate(
                body["analysis"]["proposed_workflow"]
            )
        ).valid
        assert all(
            step["status"] == "completed"
            for step in body["session"]["steps"]
        )
    finally:
        api_main.app.dependency_overrides.clear()


def test_specific_request_skips_clarification_and_generates_immediately(
    tmp_path,
    monkeypatch,
):
    client, _, _ = build_client(tmp_path, monkeypatch)

    try:
        response = client.post(
            "/copilot/plans",
            json={
                "instruction": (
                    "Whenever a customer fills a form, update HubSpot CRM "
                    "and notify Slack."
                )
            },
            headers=HEADERS,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["session"]["status"] == "completed"
        assert body["session"]["questions"] == []
        assert body["analysis"] is not None
    finally:
        api_main.app.dependency_overrides.clear()


def test_planning_session_is_owner_scoped_and_survives_restart(tmp_path):
    database_path = tmp_path / "planning-restart.sqlite3"
    first_repository = WorkflowRepository(str(database_path))
    first_service = CopilotService(
        provider=HeuristicProvider(),
        repository=first_repository,
    )
    started = first_service.plan(
        "Build a lead capture system.",
        user_id="owner_1",
    )

    restarted = CopilotService(
        provider=HeuristicProvider(),
        repository=WorkflowRepository(str(database_path)),
    )
    loaded = restarted.get_plan(started.session.id, "owner_1")

    assert loaded.session.id == started.session.id
    try:
        restarted.get_plan(started.session.id, "stranger")
    except LookupError:
        pass
    else:
        raise AssertionError("Planning sessions must be owner-scoped.")


def test_sse_streams_clarification_then_generation_progress(
    tmp_path,
    monkeypatch,
):
    client, _, _ = build_client(tmp_path, monkeypatch)

    try:
        with client.stream(
            "POST",
            "/copilot/plans/stream",
            json={"instruction": "Build a lead capture system."},
            headers=HEADERS,
        ) as response:
            text = response.read().decode()
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(
                "text/event-stream"
            )
        events = parse_sse(text)
        assert [event for event, _ in events] == [
            "accepted",
            "planning",
            "clarification",
            "complete",
        ]
        session_id = events[2][1]["data"]["session_id"]

        with client.stream(
            "POST",
            f"/copilot/plans/{session_id}/answers/stream",
            json={
                "answers": {
                    "lead_source": "Customer form submission",
                    "lead_destination": "HubSpot CRM",
                    "lead_notification": "Microsoft Teams",
                }
            },
            headers=HEADERS,
        ) as response:
            answer_text = response.read().decode()
            assert response.status_code == 200
        answer_events = parse_sse(answer_text)
        assert [event for event, _ in answer_events] == [
            "accepted",
            "planning",
            "analysis",
            "validation",
            "complete",
        ]
        assert answer_events[3][1]["data"]["valid"] is True
        node_types = [
            node["type"]
            for node in answer_events[2][1]["data"]["analysis"][
                "proposed_workflow"
            ]["nodes"]
        ]
        assert node_types == [
            "form_submission_trigger",
            "crm_update",
            "teams_message",
        ]
    finally:
        api_main.app.dependency_overrides.clear()
