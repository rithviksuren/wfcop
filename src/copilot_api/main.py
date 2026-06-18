from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .catalog import catalog_for_prompt
from .llm import LLMError, build_provider
from .models import (
    CopilotResponse,
    CreateWorkflowRequest,
    ExplainWorkflowRequest,
    FixWorkflowRequest,
    InviteMemberRequest,
    ModifyWorkflowRequest,
    RunWorkflowRequest,
    SaveWorkflowRequest,
    ShareWorkflowRequest,
    TeamMember,
    UpdateMemberRoleRequest,
    UpdateWorkflowRequest,
    ValidationResult,
    Workflow,
    WorkflowPermissionSummary,
    WorkflowRun,
    WorkflowSummary,
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


def get_service() -> CopilotService:
    return service


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "provider": provider.name}


@app.get("/nodes")
def list_nodes() -> list[dict]:
    return catalog_for_prompt()


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
def get_workflow(workflow_id: str) -> Workflow:
    workflow = repository.get(workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return workflow


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


def _require_workflow_permission(workflow: Workflow, user_id: str | None, required: str) -> None:
    permission = repository.permission_for_user(workflow, user_id, repository.get_member(user_id).role if user_id and repository.get_member(user_id) else None)
    if permission == "edit_run":
        return
    if required == "run" and permission == "run":
        return
    if user_id is None:
        return
    raise HTTPException(status_code=403, detail="You do not have permission for this workflow.")
