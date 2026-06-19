from __future__ import annotations

from fastapi.testclient import TestClient

from copilot_api import main as api_main
from copilot_api.auth import GoogleOAuthConfig
from copilot_api.llm import HeuristicProvider
from copilot_api.models import (
    ShareWorkflowRequest,
    TeamMember,
    UpdateWorkflowRequest,
    WorkflowPermissionGrant,
    WorkflowTask,
)
from copilot_api.repository import WorkflowRepository
from copilot_api.service import CopilotService


def build_service(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "flowmind.sqlite3"))
    return CopilotService(provider=HeuristicProvider(), repository=repository), repository


def test_create_flowmind_workflow_has_canvas_metadata(tmp_path):
    service, repository = build_service(tmp_path)

    response = service.create(
        "Every morning, check my inbox for emails tagged urgent and create a task in our task list.",
        user_id="user_owner",
    )

    assert response.validation.valid
    assert response.workflow.owner_id == "user_owner"
    assert response.workflow.status == "active"
    assert response.workflow.mode == "scheduled"
    assert response.workflow.trigger_schedule == "daily at 09:00"
    assert [node.role for node in response.workflow.nodes] == ["trigger", "condition", "action"]
    assert all(node.label for node in response.workflow.nodes)
    assert all(node.description for node in response.workflow.nodes)

    summaries = repository.list(user_id="user_owner")
    assert summaries[0].permission == "edit_run"
    assert summaries[0].status == "active"


def test_scheduled_workflow_requires_schedule_and_can_be_activated(tmp_path):
    service, _ = build_service(tmp_path)
    created = service.create("When an email arrives from Stripe, send a Slack message.", user_id="admin_1")

    try:
        service.update_workflow(created.workflow, UpdateWorkflowRequest(mode="scheduled"), user_id="admin_1")
    except ValueError as exc:
        assert "trigger_schedule" in str(exc)
    else:
        raise AssertionError("Scheduled workflow without trigger_schedule should fail.")

    updated = service.update_workflow(
        created.workflow,
        UpdateWorkflowRequest(mode="scheduled", trigger_schedule="daily at 09:00", status="active"),
        user_id="admin_1",
    )

    assert updated.mode == "scheduled"
    assert updated.trigger_schedule == "daily at 09:00"
    assert updated.status == "active"


def test_share_workflow_controls_dashboard_visibility(tmp_path):
    service, repository = build_service(tmp_path)
    owner = repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    member = repository.save_member(TeamMember(id="member_1", email="member@example.com", role="member"))
    created = service.create("When an email arrives from Stripe, send a Slack message.", user_id=owner.id)

    assert repository.list(user_id=member.id) == []

    repository.share_workflow(
        created.workflow,
        visibility="restricted",
        team_permission="run",
        members=[WorkflowPermissionGrant(user_id=member.id, permission="run")],
    )

    member_summaries = repository.list(user_id=member.id)
    assert len(member_summaries) == 1
    assert member_summaries[0].permission == "run"
    assert member_summaries[0].visibility == "restricted"


def test_run_workflow_persists_step_level_history(tmp_path):
    service, repository = build_service(tmp_path)
    created = service.create(
        "Every morning, check my inbox for emails tagged urgent and create a task in our task list.",
        user_id="user_owner",
    )

    run = service.run_workflow(
        created.workflow,
        input_payload={
            "email": {
                "from": "alerts@example.com",
                "subject": "Urgent customer follow-up",
                "body": "Please handle this urgent request.",
                "tag": "urgent",
            }
        },
    )

    assert run.status == "success"
    assert run.duration_ms is not None
    assert len(run.steps) == len(created.workflow.nodes)
    assert run.steps[0].input["email"]["tag"] == "urgent"
    assert run.steps[-1].output["sequence"] == len(created.workflow.nodes)
    assert run.steps[-1].output["created_task"]["title"] == "Email follow-up: Urgent customer follow-up"
    assert repository.list_tasks(created.workflow.id)[0].run_id == run.id

    runs = repository.list_runs(created.workflow.id)
    assert runs[0].id == run.id
    assert runs[0].steps[-1].label == created.workflow.nodes[-1].label


def test_run_stops_without_creating_task_when_condition_does_not_match(tmp_path):
    service, repository = build_service(tmp_path)
    created = service.create(
        "Check my inbox for emails tagged urgent and create a task.",
        user_id="user_owner",
    )

    run = service.run_workflow(
        created.workflow,
        input_payload={
            "email": {
                "from": "news@example.com",
                "subject": "Weekly update",
                "body": "Nothing urgent here.",
                "tag": "normal",
            }
        },
    )

    assert run.status == "success"
    assert len(run.steps) == 2
    assert run.steps[-1].output["condition"]["matched"] is False
    assert repository.list_tasks(created.workflow.id) == []
    assert "no action was taken" in run.summary.lower()


def test_gmail_run_fails_honestly_when_no_event_or_credentials_exist(tmp_path, monkeypatch):
    service, _ = build_service(tmp_path)
    created = service.create("When an email arrives, create a task.", user_id="user_owner")
    monkeypatch.delenv("GMAIL_EMAIL", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    run = service.run_workflow(created.workflow)

    assert run.status == "failed"
    assert "Gmail is not configured" in run.steps[0].error


def test_integrations_are_masked_and_preserve_saved_secrets(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "integrations.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    monkeypatch.setattr(api_main, "repository", repository)
    checked_credentials = []
    monkeypatch.setattr(
        "copilot_api.main.WorkflowExecutor.test_gmail_connection",
        lambda self, username, password: checked_credentials.append((username, password)),
    )

    client = TestClient(api_main.app)
    saved = client.put(
        "/integrations/gmail",
        json={"config": {"email": "owner@example.com", "app_password": "abcd efgh ijkl mnop"}},
        headers={"X-User-Id": "admin_1"},
    )

    assert saved.status_code == 200
    assert saved.json()["connected"] is True
    assert saved.json()["values"] == {"email": "owner@example.com"}
    assert "abcdefghijklmnop" not in saved.text
    assert checked_credentials[0] == ("owner@example.com", "abcdefghijklmnop")
    assert repository.get_integration("gmail")["verified"] == "true"

    updated = client.put(
        "/integrations/gmail",
        json={"config": {"email": "new@example.com", "app_password": ""}},
        headers={"X-User-Id": "admin_1"},
    )

    assert updated.status_code == 200
    assert repository.get_integration("gmail")["app_password"] == "abcdefghijklmnop"
    assert updated.json()["values"]["email"] == "new@example.com"


def test_executor_uses_saved_gmail_integration(tmp_path, monkeypatch):
    service, repository = build_service(tmp_path)
    repository.save_integration(
        "gmail",
        {"email": "owner@example.com", "app_password": "abcdefghijklmnop"},
    )
    created = service.create("When an email arrives, create a task.", user_id="user_owner")
    captured = {}

    class FakeMailbox:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def login(self, username, password):
            captured["login"] = (username, password)

        def select(self, _):
            return "OK", []

        def search(self, *_):
            return "OK", [b""]

    monkeypatch.setattr("copilot_api.executor.imaplib.IMAP4_SSL", lambda *_, **__: FakeMailbox())

    run = service.run_workflow(created.workflow)

    assert captured["login"] == ("owner@example.com", "abcdefghijklmnop")
    assert run.status == "success"
    assert "no action was taken" in run.summary.lower()


def test_notion_page_receives_email_title_and_content(tmp_path, monkeypatch):
    service, repository = build_service(tmp_path)
    data_source_id = "bc1211ca-e3f1-4939-ae34-5260b16f627c"
    repository.save_integration(
        "notion",
        {"api_token": "secret", "data_source_id": data_source_id},
    )
    analysis = service.analyze(
        "Check email for newsletters and add its details to a page in my Notion."
    )
    workflow = service.build_analyzed_workflow(
        analysis.proposed_workflow,
        user_id="user_owner",
        instruction=analysis.instruction,
    )
    captured = {}

    def fake_post(_url, payload, headers=None):
        captured["payload"] = payload
        captured["headers"] = headers
        return {"id": "page_123"}

    monkeypatch.setattr(service.executor, "_post_json", fake_post)

    run = service.run_workflow(
        workflow,
        input_payload={
            "email": {
                "from": "news@example.com",
                "to": "owner@example.com",
                "subject": "Weekly newsletter",
                "date": "2026-06-19",
                "body": "The complete newsletter content.",
            }
        },
    )

    assert run.status == "success"
    assert run.steps[-1].output["created_page"]["title"] == "Email: Weekly newsletter"
    assert captured["payload"]["parent"] == {
        "type": "data_source_id",
        "data_source_id": data_source_id,
    }
    assert captured["headers"]["Notion-Version"] == "2025-09-03"
    children = captured["payload"]["children"]
    copied_content = "\n".join(
        block["paragraph"]["rich_text"][0]["text"]["content"]
        for block in children
    )
    assert "news@example.com" in copied_content
    assert "Weekly newsletter" in copied_content
    assert "The complete newsletter content." in copied_content


def test_legacy_notion_database_id_is_resolved_and_migrated(tmp_path, monkeypatch):
    service, repository = build_service(tmp_path)
    database_id = "6ee911d9-189c-4844-93e8-260c1438b6e4"
    data_source_id = "bc1211ca-e3f1-4939-ae34-5260b16f627c"
    repository.save_integration(
        "notion",
        {"api_token": "secret", "database_id": database_id},
    )
    analysis = service.analyze(
        "When an email arrives, add its details to a page in my Notion."
    )
    workflow = service.build_analyzed_workflow(
        analysis.proposed_workflow,
        user_id="user_owner",
        instruction=analysis.instruction,
    )
    captured = {}

    def fake_get(url, headers=None):
        captured["get_url"] = url
        captured["get_headers"] = headers
        return {"data_sources": [{"id": data_source_id, "name": "Newsletter archive"}]}

    def fake_post(_url, payload, headers=None):
        captured["payload"] = payload
        return {"id": "page_123"}

    monkeypatch.setattr(service.executor, "_get_json", fake_get)
    monkeypatch.setattr(service.executor, "_post_json", fake_post)

    run = service.run_workflow(
        workflow,
        input_payload={
            "email": {
                "from": "news@example.com",
                "subject": "Newsletter",
                "body": "Content",
            }
        },
    )

    assert run.status == "success"
    assert captured["get_url"].endswith(f"/v1/databases/{database_id}")
    assert captured["get_headers"]["Notion-Version"] == "2025-09-03"
    assert captured["payload"]["parent"]["data_source_id"] == data_source_id
    assert repository.get_integration("notion")["data_source_id"] == data_source_id


def test_notion_integration_rejects_email_as_data_source_id(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "notion-invalid.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).put(
        "/integrations/notion",
        json={
            "config": {
                "api_token": "secret",
                "data_source_id": "rithvikkumar35@gmail.com",
            }
        },
        headers={"X-User-Id": "admin_1"},
    )

    assert response.status_code == 422
    assert "not an email address" in response.json()["detail"]
    assert repository.get_integration("notion") == {}


def test_gmail_integration_rejects_normal_account_password(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "gmail-password.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).put(
        "/integrations/gmail",
        json={"config": {"email": "owner@example.com", "app_password": "my-normal-password"}},
        headers={"X-User-Id": "admin_1"},
    )

    assert response.status_code == 422
    assert "normal Google Account password" in response.json()["detail"]
    assert repository.get_integration("gmail") == {}


def test_old_unverified_gmail_credentials_are_not_reported_as_connected(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "old-gmail.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    repository.save_integration(
        "gmail",
        {"email": "owner@example.com", "app_password": "old-account-password"},
    )
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).get(
        "/integrations",
        headers={"X-User-Id": "admin_1"},
    )

    gmail = next(item for item in response.json() if item["provider"] == "gmail")
    assert gmail["connected"] is False
    assert "verified" not in gmail["values"]
    assert "verified" not in gmail["configured_fields"]


def test_google_calendar_integration_is_listed_and_masks_service_account(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "google-calendar.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    monkeypatch.setattr(api_main, "repository", repository)
    client = TestClient(api_main.app)

    integrations = client.get("/integrations", headers={"X-User-Id": "admin_1"})
    calendar = next(item for item in integrations.json() if item["provider"] == "google_calendar")

    assert calendar["name"] == "Google Calendar"
    assert calendar["connected"] is False

    saved = client.put(
        "/integrations/google_calendar",
        json={
            "config": {
                "service_account_json": '{"client_email":"calendar@example.iam.gserviceaccount.com"}',
                "calendar_id": "team@example.com",
            }
        },
        headers={"X-User-Id": "admin_1"},
    )

    assert saved.status_code == 200
    assert saved.json()["connected"] is True
    assert saved.json()["values"] == {"calendar_id": "team@example.com"}
    assert "client_email" not in saved.text
    assert repository.get_integration("google_calendar")["service_account_json"].startswith("{")


def test_delete_workflow_removes_related_data(tmp_path):
    service, repository = build_service(tmp_path)
    owner = repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    member = repository.save_member(TeamMember(id="member_1", email="member@example.com", role="member"))
    created = service.create("When an email arrives, send a Slack message.", user_id=owner.id)
    repository.share_workflow(
        created.workflow,
        visibility="restricted",
        team_permission="run",
        members=[WorkflowPermissionGrant(user_id=member.id, permission="run")],
    )
    service.run_workflow(created.workflow)

    assert repository.delete(created.workflow.id)
    assert repository.get(created.workflow.id) is None
    assert repository.list_runs(created.workflow.id) == []
    assert repository.permissions_for_workflow(created.workflow.id).members == []
    assert not repository.delete(created.workflow.id)


def test_delete_workflow_endpoint_returns_no_content(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "delete-endpoint.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    workflow = repository.save(
        build_service(tmp_path)[0].create("When an email arrives, send a Slack message.", user_id="admin_1").workflow
    )
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).delete(
        f"/workflows/{workflow.id}",
        headers={"X-User-Id": "admin_1"},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert repository.get(workflow.id) is None


def test_auth_status_reports_missing_google_configuration(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "auth-status.sqlite3"))
    monkeypatch.setattr(api_main, "repository", repository)
    monkeypatch.setattr(
        api_main.google_oauth,
        "config",
        GoogleOAuthConfig(client_id="", client_secret="", redirect_uri="http://localhost/callback"),
    )

    response = TestClient(api_main.app).get("/auth/me")

    assert response.status_code == 200
    assert response.json()["authenticated"] is False
    assert response.json()["google_configured"] is False
    assert "GOOGLE_OAUTH_CLIENT_ID" in response.json()["setup_message"]
    assert response.json()["oauth_redirect_uri"] == "http://localhost/callback"


def test_google_start_uses_forwarded_browser_origin(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "oauth-origin.sqlite3"))
    monkeypatch.setattr(api_main, "repository", repository)
    captured = {}
    monkeypatch.setattr(
        api_main.google_oauth,
        "authorization_url",
        lambda state, redirect_uri=None: captured.setdefault(
            "url", f"https://accounts.example/auth?redirect_uri={redirect_uri}"
        ),
    )

    response = TestClient(api_main.app).get(
        "/auth/google/start",
        headers={
            "X-Forwarded-Host": "localhost:3000",
            "X-Forwarded-Proto": "http",
        },
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert "http://localhost:3000/auth/google/callback" in captured["url"]


def test_google_callback_creates_session_and_authenticates_user(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "google-auth.sqlite3"))
    monkeypatch.setattr(api_main, "repository", repository)
    monkeypatch.setattr(
        api_main.google_oauth,
        "exchange_code",
        lambda code, redirect_uri=None: {
            "sub": "123456",
            "email": "owner@example.com",
            "name": "Flow Owner",
            "picture": "https://example.com/avatar.png",
        },
    )
    state = "valid-state"
    repository.save_oauth_state(state, "http://127.0.0.1:3000/auth/google/callback")
    client = TestClient(api_main.app)

    callback = client.get(
        f"/auth/google/callback?state={state}&code=auth-code",
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert api_main.SESSION_COOKIE in callback.cookies

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["authenticated"] is True
    assert me.json()["user"]["id"] == "google_123456"
    assert me.json()["user"]["role"] == "admin"
    assert me.json()["user"]["picture"] == "https://example.com/avatar.png"

    logout = client.post("/auth/logout")
    assert logout.status_code == 204
    assert client.get("/auth/me").json()["authenticated"] is False


def test_protected_api_requires_session(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "protected.sqlite3"))
    monkeypatch.setattr(api_main, "repository", repository)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("AUTH_ALLOW_DEV_HEADER", raising=False)

    response = TestClient(api_main.app).get("/workflows")

    assert response.status_code == 401
    assert response.json()["detail"] == "Sign in is required."


def test_tasks_endpoint_returns_visible_tasks_without_source_payload(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "tasks-page.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    workflow = build_service(tmp_path)[0].create(
        "When an email arrives, create a task.",
        user_id="admin_1",
    ).workflow
    repository.save(workflow)
    repository.save_task(
        WorkflowTask(
            workflow_id=workflow.id,
            run_id="run_1",
            title="Email follow-up: New job alert",
            source={"body": "private email contents"},
        )
    )
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).get(
        "/tasks",
        headers={"X-User-Id": "admin_1"},
    )

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Email follow-up: New job alert"
    assert "source" not in response.json()[0]


def test_tasks_endpoint_decodes_existing_mime_encoded_titles(tmp_path, monkeypatch):
    repository = WorkflowRepository(str(tmp_path / "encoded-task.sqlite3"))
    repository.save_member(TeamMember(id="admin_1", email="admin@example.com", role="admin"))
    workflow = build_service(tmp_path)[0].create(
        "When an email arrives, create a task.",
        user_id="admin_1",
    ).workflow
    repository.save(workflow)
    repository.save_task(
        WorkflowTask(
            workflow_id=workflow.id,
            run_id="run_1",
            title="Email follow-up: =?UTF-8?Q?=F0=9F=8C=9F_New_Jobs_Today?=",
        )
    )
    monkeypatch.setattr(api_main, "repository", repository)

    response = TestClient(api_main.app).get(
        "/tasks",
        headers={"X-User-Id": "admin_1"},
    )

    assert response.status_code == 200
    assert response.json()[0]["title"] == "Email follow-up: 🌟 New Jobs Today"
