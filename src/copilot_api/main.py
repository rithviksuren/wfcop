from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .auth import GoogleOAuth, GoogleOAuthError
from .catalog import catalog_for_prompt
from .executor import WorkflowExecutionError, WorkflowExecutor
from .llm import LLMError, build_provider
from .models import (
    AuthStatus,
    AuthUser,
    BuildWorkflowRequest,
    CopilotResponse,
    CreateWorkflowRequest,
    ExplainWorkflowRequest,
    FixWorkflowRequest,
    InviteMemberRequest,
    IntegrationSummary,
    IntegrationUpdate,
    ModifyWorkflowRequest,
    RunWorkflowRequest,
    SaveWorkflowRequest,
    ShareWorkflowRequest,
    TeamMember,
    UpdateMemberRoleRequest,
    UpdateWorkflowRequest,
    ValidationResult,
    Workflow,
    WorkflowAnalysisResponse,
    WorkflowPermissionSummary,
    WorkflowRun,
    WorkflowSummary,
    WorkflowTask,
    WorkflowTaskSummary,
)
from .repository import WorkflowRepository
from .service import CopilotService
from .validation import validate_workflow

app = FastAPI(title="AI Workflow Copilot", version="0.1.0")
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
repository = WorkflowRepository()
provider = build_provider()
service = CopilotService(provider=provider, repository=repository)
google_oauth = GoogleOAuth.from_env()
SESSION_COOKIE = "flowmind_session"

INTEGRATIONS = {
    "gmail": {
        "name": "Gmail",
        "required": ("email", "app_password"),
        "secret": ("app_password",),
    },
    "slack": {
        "name": "Slack",
        "required": ("webhook_url",),
        "secret": ("webhook_url",),
    },
    "teams": {
        "name": "Microsoft Teams",
        "required": ("webhook_url",),
        "secret": ("webhook_url",),
    },
    "notion": {
        "name": "Notion",
        "required": ("api_token", "database_id"),
        "secret": ("api_token",),
    },
    "jira": {
        "name": "Jira",
        "required": ("base_url", "email", "api_token", "project_key"),
        "secret": ("api_token",),
    },
    "hubspot": {
        "name": "HubSpot",
        "required": ("private_app_token",),
        "secret": ("private_app_token",),
    },
    "google_calendar": {
        "name": "Google Calendar",
        "required": ("service_account_json", "calendar_id"),
        "secret": ("service_account_json",),
    },
    "google_drive": {
        "name": "Google Drive",
        "required": ("service_account_json", "folder_id"),
        "secret": ("service_account_json",),
    },
    "google_sheets": {
        "name": "Google Sheets",
        "required": ("service_account_json", "spreadsheet_id"),
        "secret": ("service_account_json",),
    },
    "github": {
        "name": "GitHub",
        "required": ("personal_access_token", "repository"),
        "secret": ("personal_access_token",),
    },
    "discord": {
        "name": "Discord",
        "required": ("webhook_url",),
        "secret": ("webhook_url",),
    },
    "airtable": {
        "name": "Airtable",
        "required": ("personal_access_token", "base_id", "table_name"),
        "secret": ("personal_access_token",),
    },
    "stripe": {
        "name": "Stripe",
        "required": ("secret_key",),
        "secret": ("secret_key",),
    },
    "salesforce": {
        "name": "Salesforce",
        "required": ("instance_url", "access_token"),
        "secret": ("access_token",),
    },
}


def get_service() -> CopilotService:
    return service


@app.middleware("http")
async def require_authenticated_session(request: Request, call_next):
    protected_prefixes = ("/copilot", "/workflows", "/team", "/integrations", "/tasks")
    if request.url.path.startswith(protected_prefixes):
        session_user = repository.user_for_session(request.cookies.get(SESSION_COOKIE))
        legacy_user_id = request.headers.get("X-User-Id")
        allow_test_header = bool(os.getenv("PYTEST_CURRENT_TEST")) or os.getenv("AUTH_ALLOW_DEV_HEADER") == "1"
        user_id = session_user.id if session_user else legacy_user_id if allow_test_header else None
        if not user_id:
            return JSONResponse({"detail": "Sign in is required."}, status_code=401)
        if not legacy_user_id:
            request.scope["headers"].append((b"x-user-id", user_id.encode("utf-8")))
    return await call_next(request)


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")


@app.get("/auth/me", response_model=AuthStatus)
def auth_status(request: Request) -> AuthStatus:
    member = repository.user_for_session(request.cookies.get(SESSION_COOKIE))
    setup_message = None
    if not google_oauth.config.configured:
        setup_message = (
            "Google sign-in is not configured yet. Add GOOGLE_OAUTH_CLIENT_ID and "
            "GOOGLE_OAUTH_CLIENT_SECRET to .env."
        )
    return AuthStatus(
        authenticated=member is not None,
        google_configured=google_oauth.config.configured,
        user=AuthUser(**member.model_dump()) if member else None,
        setup_message=setup_message,
        oauth_redirect_uri=_oauth_redirect_uri(request),
    )


@app.get("/auth/google/start")
def google_sign_in(request: Request) -> RedirectResponse:
    try:
        state = google_oauth.new_state()
        redirect_uri = _oauth_redirect_uri(request)
        repository.save_oauth_state(state, redirect_uri)
        return RedirectResponse(
            google_oauth.authorization_url(state, redirect_uri=redirect_uri),
            status_code=302,
        )
    except GoogleOAuthError:
        return RedirectResponse("/?auth_error=google_not_configured", status_code=302)


@app.get("/auth/google/callback")
def google_callback(
    state: str | None = Query(default=None),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    if error:
        return RedirectResponse(f"/?auth_error={error}", status_code=302)
    redirect_uri = repository.consume_oauth_state(state) if state else None
    if not state or not code or redirect_uri is None:
        return RedirectResponse("/?auth_error=invalid_state", status_code=302)
    try:
        profile = google_oauth.exchange_code(code, redirect_uri=redirect_uri)
    except GoogleOAuthError:
        return RedirectResponse("/?auth_error=google_sign_in_failed", status_code=302)

    existing_member = repository.get_member_by_email(profile["email"])
    existing_members = repository.list_members()
    role = existing_member.role if existing_member else "admin" if not existing_members else "member"
    member = repository.save_member(
        TeamMember(
            id=f"google_{profile['sub']}",
            email=profile["email"],
            name=profile["name"] or None,
            picture=profile["picture"] or None,
            role=role,
        )
    )
    token = repository.create_session(member.id)
    response = RedirectResponse("/", status_code=302)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=14 * 24 * 60 * 60,
        httponly=True,
        secure=os.getenv("APP_COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        path="/",
    )
    return response


@app.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(request: Request) -> Response:
    repository.delete_session(request.cookies.get(SESSION_COOKIE))
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": provider.name}


@app.get("/nodes")
def list_nodes() -> list[dict]:
    return catalog_for_prompt()


@app.get("/integrations", response_model=list[IntegrationSummary])
def list_integrations(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> list[IntegrationSummary]:
    _require_admin(x_user_id)
    return [_integration_summary(provider) for provider in INTEGRATIONS]


@app.put("/integrations/{provider}", response_model=IntegrationSummary)
def save_integration(
    provider: str,
    request: IntegrationUpdate,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> IntegrationSummary:
    _require_admin(x_user_id)
    definition = _integration_definition(provider)
    current = repository.get_integration(provider)
    allowed_fields = set(definition["required"]) | {"title_property"}
    for field, raw_value in request.config.items():
        if field not in allowed_fields:
            continue
        value = raw_value.strip()
        if value:
            current[field] = value
    missing = [field for field in definition["required"] if not current.get(field)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{definition['name']} requires: {', '.join(missing)}.",
        )
    if provider == "gmail":
        current["app_password"] = WorkflowExecutor.normalize_gmail_app_password(current["app_password"])
        try:
            WorkflowExecutor(repository).test_gmail_connection(
                current["email"],
                current["app_password"],
            )
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        current["verified"] = "true"
    repository.save_integration(provider, current)
    return _integration_summary(provider)


@app.delete("/integrations/{provider}", status_code=status.HTTP_204_NO_CONTENT)
def delete_integration(
    provider: str,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Response:
    _require_admin(x_user_id)
    _integration_definition(provider)
    repository.delete_integration(provider)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/copilot/create", response_model=CopilotResponse)
def create_workflow(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.create(request.instruction, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/copilot/analyze", response_model=WorkflowAnalysisResponse)
def analyze_workflow(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
) -> WorkflowAnalysisResponse:
    return copilot.analyze(request.instruction)


@app.post("/copilot/build", response_model=Workflow)
def build_analyzed_workflow(
    request: BuildWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    try:
        return copilot.build_analyzed_workflow(request.workflow, user_id=x_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/copilot/modify", response_model=CopilotResponse)
def modify_workflow(
    request: ModifyWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.modify(request.workflow, request.instruction, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/copilot/fix", response_model=CopilotResponse)
def fix_workflow(
    request: FixWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.fix(request.workflow, request.instruction, request.validation_errors, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/copilot/explain", response_model=CopilotResponse)
def explain_workflow(request: ExplainWorkflowRequest, copilot: CopilotService = Depends(get_service)) -> CopilotResponse:
    try:
        return copilot.explain(request.workflow, request.instruction, request.context)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/workflows", response_model=Workflow)
def save_workflow(
    request: SaveWorkflowRequest, x_user_id: str | None = Header(default=None, alias="X-User-Id")
) -> Workflow:
    validation = validate_workflow(request.workflow)
    if not validation.valid:
        raise HTTPException(status_code=422, detail=[error.model_dump() for error in validation.errors])
    if x_user_id and request.workflow.owner_id is None:
        request.workflow.owner_id = x_user_id
        request.workflow.created_by = x_user_id
        request.workflow.updated_by = x_user_id
    return repository.save(request.workflow)


@app.get("/workflows", response_model=list[WorkflowSummary])
def list_workflows(
    status: str | None = Query(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> list[WorkflowSummary]:
    workflows = repository.list(user_id=x_user_id)
    if status:
        workflows = [workflow for workflow in workflows if workflow.status == status]
    return workflows


@app.get("/workflows/{workflow_id}", response_model=Workflow)
def get_workflow(
    workflow_id: str,
    copilot: CopilotService = Depends(get_service),
) -> Workflow:
    workflow = repository.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return copilot.repair_legacy_workflow(workflow)


@app.patch("/workflows/{workflow_id}", response_model=Workflow)
def update_workflow(
    workflow_id: str,
    request: UpdateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    try:
        return copilot.update_workflow(workflow, request, user_id=x_user_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.delete("/workflows/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_workflow(
    workflow_id: str,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Response:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    repository.delete(workflow_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.post("/workflows/{workflow_id}/pause", response_model=Workflow)
def pause_workflow(
    workflow_id: str,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    return copilot.update_workflow(workflow, UpdateWorkflowRequest(status="paused"), user_id=x_user_id)


@app.post("/workflows/{workflow_id}/activate", response_model=Workflow)
def activate_workflow(
    workflow_id: str,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    return copilot.update_workflow(workflow, UpdateWorkflowRequest(status="active"), user_id=x_user_id)


@app.post("/workflows/{workflow_id}/share", response_model=WorkflowPermissionSummary)
def share_workflow(
    workflow_id: str,
    request: ShareWorkflowRequest,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowPermissionSummary:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    return repository.share_workflow(workflow, request.visibility, request.team_permission, request.members)


@app.get("/workflows/{workflow_id}/permissions", response_model=WorkflowPermissionSummary)
def get_workflow_permissions(workflow_id: str) -> WorkflowPermissionSummary:
    workflow = _get_workflow_or_404(workflow_id)
    return repository.permissions_for_workflow(workflow_id, workflow.visibility)


@app.post("/workflows/{workflow_id}/run", response_model=WorkflowRun)
def run_workflow(
    workflow_id: str,
    request: RunWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowRun:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "run")
    return copilot.run_workflow(workflow, request.trigger_type, request.input)


@app.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRun])
def list_workflow_runs(
    workflow_id: str,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> list[WorkflowRun]:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "run")
    return repository.list_runs(workflow_id)


@app.get("/workflows/{workflow_id}/runs/{run_id}", response_model=WorkflowRun)
def get_workflow_run(
    workflow_id: str,
    run_id: str,
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowRun:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "run")
    run = repository.get_run(workflow_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Workflow run not found.")
    return run


@app.get("/tasks", response_model=list[WorkflowTaskSummary])
def list_created_tasks(
    workflow_id: str | None = Query(default=None),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> list[WorkflowTaskSummary]:
    visible_workflow_ids = {workflow.id for workflow in repository.list(user_id=x_user_id)}
    tasks = repository.list_tasks(workflow_id)
    return [
        WorkflowTaskSummary(
            **{
                **task.model_dump(exclude={"source"}),
                "title": WorkflowExecutor.decode_email_header(task.title),
            }
        )
        for task in tasks
        if task.workflow_id in visible_workflow_ids
    ]


@app.post("/team/invites", response_model=TeamMember)
def invite_member(request: InviteMemberRequest) -> TeamMember:
    payload = {"email": request.email, "role": request.role, "name": request.name}
    if request.id:
        payload["id"] = request.id
    member = TeamMember(**payload)
    return repository.save_member(member)


@app.get("/team/members", response_model=list[TeamMember])
def list_members() -> list[TeamMember]:
    return repository.list_members()


@app.patch("/team/members/{user_id}", response_model=TeamMember)
def update_member_role(user_id: str, request: UpdateMemberRoleRequest) -> TeamMember:
    member = repository.update_member_role(user_id, request.role)
    if member is None:
        raise HTTPException(status_code=404, detail="Team member not found.")
    return member


@app.post("/workflows/validate", response_model=ValidationResult)
def validate(request: SaveWorkflowRequest) -> ValidationResult:
    return validate_workflow(request.workflow)


def _get_workflow_or_404(workflow_id: str) -> Workflow:
    workflow = repository.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return workflow


def _integration_definition(provider: str) -> dict:
    definition = INTEGRATIONS.get(provider)
    if definition is None:
        raise HTTPException(status_code=404, detail="Integration not found.")
    return definition


def _integration_summary(provider: str) -> IntegrationSummary:
    definition = _integration_definition(provider)
    config = repository.get_integration(provider)
    secret_fields = set(definition["secret"])
    public_fields = set(definition["required"]) | {"title_property"}
    values = {
        field: value
        for field, value in config.items()
        if field in public_fields and field not in secret_fields
    }
    required = definition["required"]
    connected = all(config.get(field) for field in required)
    if provider == "gmail":
        connected = connected and config.get("verified") == "true"
    return IntegrationSummary(
        provider=provider,
        name=definition["name"],
        connected=connected,
        values=values,
        configured_fields=sorted(
            field for field, value in config.items() if field in public_fields and value
        ),
        updated_at=repository.integration_updated_at(provider),
    )


def _require_admin(user_id: str | None) -> None:
    member = repository.get_member(user_id)
    if member is None or member.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access is required for integration settings.")


def _oauth_redirect_uri(request: Request) -> str:
    forwarded_host = request.headers.get("X-Forwarded-Host")
    if forwarded_host:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "http")
        return f"{forwarded_proto}://{forwarded_host}/auth/google/callback"
    return google_oauth.config.redirect_uri


def _require_workflow_permission(workflow: Workflow, user_id: str | None, required: str) -> None:
    permission = repository.permission_for_user(workflow, user_id, repository.get_member(user_id).role if user_id and repository.get_member(user_id) else None)
    if permission == "edit_run":
        return
    if required == "run" and permission == "run":
        return
    if user_id is None:
        return
    raise HTTPException(status_code=403, detail="You do not have permission for this workflow.")
