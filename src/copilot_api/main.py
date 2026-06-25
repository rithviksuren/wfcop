from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from .auth import GoogleOAuth, GoogleOAuthError
from .catalog import catalog_for_prompt
from .executor import WorkflowExecutionError, WorkflowExecutor
from .llm import LLMError, build_provider
from .models import (
    ApplyWorkflowOperationsRequest,
    AuthStatus,
    AuthUser,
    APIErrorResponse,
    BuildWorkflowRequest,
    ContinueWorkflowPlanRequest,
    Conversation,
    ConversationDetail,
    ConversationMessageRequest,
    ConversationResponse,
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
    WorkflowDiffRequest,
    WorkflowOperationsResponse,
    WorkflowPlanEvent,
    WorkflowPlanResponse,
    WorkflowPermissionSummary,
    WorkflowRun,
    WorkflowSummary,
    WorkflowTask,
    WorkflowTaskSummary,
)
from .diff import apply_workflow_operations, diff_workflows
from .repository import WorkflowRepository
from .service import CopilotService
from .validation import validate_workflow

API_VERSION = "0.2.0"
API_FEATURES = [
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
]

app = FastAPI(
    title="AI Workflow Copilot",
    version=API_VERSION,
    description=(
        "Create, modify, repair, explain, persist, and execute automation workflows."
    ),
    openapi_tags=[
        {
            "name": "Copilot",
            "description": "Natural-language workflow design and maintenance.",
        }
    ],
)
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
        "required": ("api_token",),
        "optional": ("data_source_id", "database_id", "title_property"),
        "one_of": (("data_source_id", "database_id"),),
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
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "api_version": API_VERSION,
        "provider": provider.name,
        "features": API_FEATURES,
    }


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
    allowed_fields = set(definition["required"]) | set(definition.get("optional", ()))
    for field, raw_value in request.config.items():
        if field not in allowed_fields:
            continue
        value = raw_value.strip()
        if value:
            current[field] = value
    if provider == "notion" and current.get("data_source_id"):
        try:
            current["data_source_id"] = WorkflowExecutor.normalize_notion_id(
                current["data_source_id"],
                "data source",
            )
        except WorkflowExecutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    missing = [field for field in definition["required"] if not current.get(field)]
    missing_groups = [
        fields
        for fields in definition.get("one_of", ())
        if not any(current.get(field) for field in fields)
    ]
    if missing or missing_groups:
        requirements = list(missing)
        requirements.extend(" or ".join(fields) for fields in missing_groups)
        raise HTTPException(
            status_code=422,
            detail=f"{definition['name']} requires: {', '.join(requirements)}.",
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


COPILOT_ERROR_RESPONSES = {
    401: {"model": APIErrorResponse, "description": "Authentication required."},
    422: {"model": APIErrorResponse, "description": "Invalid request or workflow."},
    502: {"model": APIErrorResponse, "description": "AI provider unavailable."},
}
OPERATION_ERROR_RESPONSES = {
    **COPILOT_ERROR_RESPONSES,
    409: {
        "model": APIErrorResponse,
        "description": "Workflow version conflict.",
    },
}


@app.post(
    "/copilot/create",
    response_model=CopilotResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Copilot"],
    summary="Create a workflow",
    description=(
        "Generates, validates, assigns ownership to, and persists a workflow from "
        "a natural-language instruction."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def create_workflow(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.create(request.instruction, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/plans",
    response_model=WorkflowPlanResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Copilot"],
    summary="Plan a workflow request",
    description=(
        "Creates a durable multi-step plan. Broad requests return focused "
        "clarifying questions; sufficiently specific requests return analysis."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def plan_workflow_request(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowPlanResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    return copilot.plan(
        request.instruction,
        x_user_id,
        request.context,
    )


@app.post(
    "/copilot/plans/{session_id}/answers",
    response_model=WorkflowPlanResponse,
    tags=["Copilot"],
    summary="Answer workflow planning questions",
    description=(
        "Continues a durable plan with clarification answers and generates the "
        "workflow analysis once all required decisions are resolved."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def continue_workflow_plan(
    session_id: str,
    request: ContinueWorkflowPlanRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowPlanResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    try:
        return copilot.continue_plan(
            session_id,
            request.answers,
            x_user_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/copilot/plans/{session_id}",
    response_model=WorkflowPlanResponse,
    tags=["Copilot"],
    summary="Get a workflow plan",
    responses=COPILOT_ERROR_RESPONSES,
)
def get_workflow_plan(
    session_id: str,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowPlanResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    try:
        return copilot.get_plan(session_id, x_user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/copilot/plans/stream",
    response_class=StreamingResponse,
    tags=["Copilot"],
    summary="Stream workflow planning progress",
    description=(
        "Streams Server-Sent Events for acceptance, planning, clarification or "
        "analysis, validation, and completion."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def stream_workflow_plan(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> StreamingResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")

    def events() -> Iterator[str]:
        yield _sse_event(
            WorkflowPlanEvent(
                event="accepted",
                message="Workflow request accepted.",
                data={"instruction": request.instruction},
            )
        )
        yield _sse_event(
            WorkflowPlanEvent(
                event="planning",
                message="Analyzing scope and missing decisions.",
            )
        )
        try:
            result = copilot.plan(
                request.instruction,
                x_user_id,
                request.context,
            )
            if result.session.status == "awaiting_clarification":
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="clarification",
                        message="Clarification is required before generation.",
                        data={
                            "session_id": result.session.id,
                            "questions": [
                                question.model_dump(mode="json")
                                for question in result.session.questions
                            ],
                        },
                    )
                )
            else:
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="analysis",
                        message="Workflow analysis and graph generated.",
                        data={
                            "session_id": result.session.id,
                            "analysis": result.analysis.model_dump(
                                mode="json",
                                by_alias=True,
                            )
                            if result.analysis
                            else None,
                        },
                    )
                )
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="validation",
                        message="Generated workflow passed planning validation.",
                        data={
                            "valid": validate_workflow(
                                result.analysis.proposed_workflow
                            ).valid
                            if result.analysis
                            else False,
                        },
                    )
                )
            yield _sse_event(
                WorkflowPlanEvent(
                    event="complete",
                    message="Planning step completed.",
                    data={
                        "session": result.session.model_dump(mode="json")
                    },
                )
            )
        except Exception as exc:
            yield _sse_event(
                WorkflowPlanEvent(
                    event="error",
                    message=str(exc),
                )
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/copilot/plans/{session_id}/answers/stream",
    response_class=StreamingResponse,
    tags=["Copilot"],
    summary="Stream workflow generation after clarification",
    responses=COPILOT_ERROR_RESPONSES,
)
def stream_workflow_plan_answers(
    session_id: str,
    request: ContinueWorkflowPlanRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> StreamingResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")

    def events() -> Iterator[str]:
        yield _sse_event(
            WorkflowPlanEvent(
                event="accepted",
                message="Clarification answers accepted.",
                data={"session_id": session_id},
            )
        )
        yield _sse_event(
            WorkflowPlanEvent(
                event="planning",
                message="Resolving answers into a concrete workflow request.",
            )
        )
        try:
            result = copilot.continue_plan(
                session_id,
                request.answers,
                x_user_id,
            )
            if result.session.status == "awaiting_clarification":
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="clarification",
                        message="Additional required answers are missing.",
                        data={
                            "questions": [
                                question.model_dump(mode="json")
                                for question in result.session.questions
                            ]
                        },
                    )
                )
            else:
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="analysis",
                        message="Workflow graph generated from clarified requirements.",
                        data={
                            "analysis": result.analysis.model_dump(
                                mode="json",
                                by_alias=True,
                            )
                            if result.analysis
                            else None
                        },
                    )
                )
                yield _sse_event(
                    WorkflowPlanEvent(
                        event="validation",
                        message="Generated workflow validated.",
                        data={
                            "valid": validate_workflow(
                                result.analysis.proposed_workflow
                            ).valid
                            if result.analysis
                            else False
                        },
                    )
                )
            yield _sse_event(
                WorkflowPlanEvent(
                    event="complete",
                    message="Planning step completed.",
                    data={
                        "session": result.session.model_dump(mode="json")
                    },
                )
            )
        except Exception as exc:
            yield _sse_event(
                WorkflowPlanEvent(event="error", message=str(exc))
            )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/copilot/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Copilot"],
    summary="Start a workflow conversation",
    description=(
        "Creates a workflow and stores the instruction, validated workflow "
        "snapshot, and provider result as the first durable conversation turn."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def start_copilot_conversation(
    request: ConversationMessageRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> ConversationResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    try:
        return copilot.start_conversation(
            request.instruction,
            x_user_id,
            request.context,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/conversations/{conversation_id}/messages",
    response_model=ConversationResponse,
    tags=["Copilot"],
    summary="Continue a workflow conversation",
    description=(
        "Uses the conversation's latest persisted workflow and recent turns to "
        "interpret a follow-up instruction, then stores the new workflow snapshot."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def continue_copilot_conversation(
    conversation_id: str,
    request: ConversationMessageRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> ConversationResponse:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    try:
        return copilot.continue_conversation(
            conversation_id,
            request.instruction,
            x_user_id,
            request.context,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get(
    "/copilot/conversations",
    response_model=list[Conversation],
    tags=["Copilot"],
    summary="List workflow conversations",
    responses=COPILOT_ERROR_RESPONSES,
)
def list_copilot_conversations(
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> list[Conversation]:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    return copilot.list_conversations(x_user_id)


@app.get(
    "/copilot/conversations/{conversation_id}",
    response_model=ConversationDetail,
    tags=["Copilot"],
    summary="Get workflow conversation memory",
    responses=COPILOT_ERROR_RESPONSES,
)
def get_copilot_conversation(
    conversation_id: str,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> ConversationDetail:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Sign in is required.")
    try:
        return copilot.get_conversation(conversation_id, x_user_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/copilot/analyze",
    response_model=WorkflowAnalysisResponse,
    tags=["Copilot"],
    summary="Analyze a workflow request",
    responses=COPILOT_ERROR_RESPONSES,
)
def analyze_workflow(
    request: CreateWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
) -> WorkflowAnalysisResponse:
    return copilot.analyze(request.instruction)


@app.post(
    "/copilot/build",
    response_model=Workflow,
    status_code=status.HTTP_201_CREATED,
    tags=["Copilot"],
    summary="Build an approved workflow plan",
    responses=COPILOT_ERROR_RESPONSES,
)
def build_analyzed_workflow(
    request: BuildWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    try:
        return copilot.build_analyzed_workflow(
            request.workflow,
            user_id=x_user_id,
            instruction=request.instruction,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/modify",
    response_model=CopilotResponse,
    tags=["Copilot"],
    summary="Modify a workflow",
    description=(
        "Applies a natural-language change while preserving unrelated valid steps, "
        "then validates and persists the updated workflow."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def modify_workflow(
    request: ModifyWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.modify(request.workflow, request.instruction, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/modify/operations",
    response_model=WorkflowOperationsResponse,
    tags=["Copilot"],
    summary="Modify a workflow and return operations only",
    description=(
        "Applies and persists a Copilot modification while returning a compact "
        "semantic patch instead of the complete workflow payload."
    ),
    responses=OPERATION_ERROR_RESPONSES,
)
def modify_workflow_operations(
    request: ModifyWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowOperationsResponse:
    stored_workflow = _get_workflow_or_404(request.workflow.id)
    _require_workflow_permission(stored_workflow, x_user_id, "edit_run")
    if stored_workflow.version != request.workflow.version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workflow version conflict: expected {request.workflow.version}, "
                f"current version is {stored_workflow.version}."
            ),
        )
    try:
        result = copilot.modify(
            stored_workflow,
            request.instruction,
            request.context,
            user_id=x_user_id,
        )
        return WorkflowOperationsResponse(
            workflow_id=result.workflow.id,
            base_version=stored_workflow.version,
            target_version=result.workflow.version,
            operations=result.operations,
            validation=result.validation,
            persisted=True,
            provider=result.provider,
            explanation=result.explanation,
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/fix",
    response_model=CopilotResponse,
    tags=["Copilot"],
    summary="Repair an invalid workflow",
    description=(
        "Repairs validation failures using the supplied errors, workflow context, "
        "and safe catalog defaults."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def fix_workflow(
    request: FixWorkflowRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> CopilotResponse:
    try:
        return copilot.fix(request.workflow, request.instruction, request.validation_errors, request.context, user_id=x_user_id)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post(
    "/copilot/explain",
    response_model=CopilotResponse,
    tags=["Copilot"],
    summary="Explain a workflow",
    description=(
        "Returns a deterministic, graph-aware explanation without exposing secrets "
        "or integration identifiers."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
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
    existing = repository.get(request.workflow.id)
    if existing is not None:
        _require_workflow_permission(existing, x_user_id, "edit_run")
        request.workflow.owner_id = existing.owner_id
        request.workflow.created_by = existing.created_by
        request.workflow.created_at = existing.created_at
        request.workflow.version = max(
            request.workflow.version,
            existing.version + 1,
        )
    elif x_user_id:
        request.workflow.owner_id = x_user_id
        request.workflow.created_by = x_user_id
    request.workflow.updated_by = x_user_id or request.workflow.updated_by
    request.workflow.updated_at = datetime.now(timezone.utc)
    return repository.save(request.workflow)


@app.post(
    "/workflows/diff",
    response_model=WorkflowOperationsResponse,
    tags=["Workflows"],
    summary="Calculate a workflow patch",
    description=(
        "Returns deterministic operations that transform the before workflow "
        "into the after workflow without persisting either payload."
    ),
    responses=COPILOT_ERROR_RESPONSES,
)
def calculate_workflow_diff(
    request: WorkflowDiffRequest,
) -> WorkflowOperationsResponse:
    validation = validate_workflow(request.after)
    return WorkflowOperationsResponse(
        workflow_id=request.after.id,
        base_version=request.before.version,
        target_version=request.after.version,
        operations=diff_workflows(request.before, request.after),
        validation=validation,
        persisted=False,
    )


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
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> Workflow:
    workflow = repository.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    _require_workflow_permission(workflow, x_user_id, "run")
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


@app.patch(
    "/workflows/{workflow_id}/operations",
    response_model=WorkflowOperationsResponse,
    tags=["Workflows"],
    summary="Apply workflow operations",
    description=(
        "Applies a semantic patch using optimistic version checking, validates "
        "the result, and persists it without returning the full workflow."
    ),
    responses=OPERATION_ERROR_RESPONSES,
)
def apply_workflow_patch(
    workflow_id: str,
    request: ApplyWorkflowOperationsRequest,
    copilot: CopilotService = Depends(get_service),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
) -> WorkflowOperationsResponse:
    workflow = _get_workflow_or_404(workflow_id)
    _require_workflow_permission(workflow, x_user_id, "edit_run")
    if workflow.version != request.expected_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Workflow version conflict: expected {request.expected_version}, "
                f"current version is {workflow.version}."
            ),
        )
    try:
        patched = apply_workflow_operations(workflow, request.operations)
        stored = copilot.update_workflow(
            workflow,
            UpdateWorkflowRequest(
                name=patched.name,
                status=patched.status,
                visibility=patched.visibility,
                mode=patched.mode,
                trigger_schedule=patched.trigger_schedule,
                nodes=patched.nodes,
                edges=patched.edges,
            ),
            user_id=x_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WorkflowOperationsResponse(
        workflow_id=stored.id,
        base_version=request.expected_version,
        target_version=stored.version,
        operations=request.operations,
        validation=validate_workflow(stored),
        persisted=True,
    )


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
    try:
        return copilot.run_workflow(workflow, request.trigger_type, request.input)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
    public_fields = set(definition["required"]) | set(definition.get("optional", ()))
    values = {
        field: value
        for field, value in config.items()
        if field in public_fields and field not in secret_fields
    }
    required = definition["required"]
    connected = all(config.get(field) for field in required) and all(
        any(config.get(field) for field in fields)
        for fields in definition.get("one_of", ())
    )
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


def _sse_event(event: WorkflowPlanEvent) -> str:
    payload = json.dumps(
        {
            "message": event.message,
            "data": event.data,
        },
        separators=(",", ":"),
    )
    return f"event: {event.event}\ndata: {payload}\n\n"
