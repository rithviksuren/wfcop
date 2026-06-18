from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from .catalog import NODE_CATALOG
from .diff import diff_workflows
from .llm import LLMError, LLMProvider
from .models import (
    CopilotResponse,
    RunTriggerType,
    UpdateWorkflowRequest,
    ValidationErrorDetail,
    Workflow,
    WorkflowRun,
    WorkflowRunStep,
)
from .repository import WorkflowRepository
from .validation import validate_workflow


class CopilotService:
    def __init__(self, provider: LLMProvider, repository: WorkflowRepository, max_attempts: int = 2) -> None:
        self.provider = provider
        self.repository = repository
        self.max_attempts = max_attempts

    def create(
        self, instruction: str, context: dict[str, Any] | None = None, user_id: str | None = None
    ) -> CopilotResponse:
        result = self._generate_valid_workflow("create", {"instruction": instruction, "context": context or {}})
        self._prepare_for_canvas(result.workflow)
        if user_id:
            result.workflow.owner_id = user_id
            result.workflow.created_by = user_id
            result.workflow.updated_by = user_id
        self.repository.save(result.workflow)
        return result

    def modify(
        self,
        workflow: Workflow,
        instruction: str,
        context: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> CopilotResponse:
        before = deepcopy(workflow)
        payload = {"workflow": workflow.model_dump(by_alias=True), "instruction": instruction, "context": context or {}}
        result = self._generate_valid_workflow("modify", payload)
        result.workflow.id = workflow.id
        result.workflow.version = workflow.version + 1
        result.workflow.created_at = workflow.created_at
        result.workflow.updated_at = datetime.now(timezone.utc)
        result.workflow.owner_id = workflow.owner_id
        result.workflow.created_by = workflow.created_by
        result.workflow.updated_by = user_id or workflow.updated_by
        result.workflow.status = workflow.status
        result.workflow.visibility = workflow.visibility
        result.workflow.mode = workflow.mode
        result.workflow.trigger_schedule = workflow.trigger_schedule
        self._prepare_for_canvas(result.workflow)
        result.operations = diff_workflows(before, result.workflow)
        self.repository.save(result.workflow)
        return result

    def fix(
        self,
        workflow: Workflow,
        instruction: str,
        validation_errors: list[ValidationErrorDetail] | None = None,
        context: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> CopilotResponse:
        before = deepcopy(workflow)
        errors = validation_errors or validate_workflow(workflow).errors
        payload = {
            "workflow": workflow.model_dump(by_alias=True),
            "instruction": instruction,
            "validation_errors": [error.model_dump() for error in errors],
            "context": context or {},
        }
        result = self._generate_valid_workflow("fix", payload)
        result.workflow.id = workflow.id
        result.workflow.version = workflow.version + 1
        result.workflow.created_at = workflow.created_at
        result.workflow.updated_at = datetime.now(timezone.utc)
        result.workflow.owner_id = workflow.owner_id
        result.workflow.created_by = workflow.created_by
        result.workflow.updated_by = user_id or workflow.updated_by
        result.workflow.status = workflow.status
        result.workflow.visibility = workflow.visibility
        result.workflow.mode = workflow.mode
        result.workflow.trigger_schedule = workflow.trigger_schedule
        self._prepare_for_canvas(result.workflow)
        result.operations = diff_workflows(before, result.workflow)
        self.repository.save(result.workflow)
        return result

    def explain(self, workflow: Workflow, instruction: str, context: dict[str, Any] | None = None) -> CopilotResponse:
        payload = {
            "workflow": workflow.model_dump(by_alias=True),
            "instruction": instruction,
            "context": context or {},
        }
        raw = self.provider.generate("explain", payload)
        explanation = raw.get("explanation") or "No explanation was generated."
        validation = validate_workflow(workflow)
        return CopilotResponse(workflow=workflow, validation=validation, explanation=explanation, provider=self.provider.name)

    def update_workflow(
        self, workflow: Workflow, request: UpdateWorkflowRequest, user_id: str | None = None
    ) -> Workflow:
        update = request.model_dump(exclude_unset=True)
        for field, value in update.items():
            setattr(workflow, field, value)
        workflow.version += 1
        workflow.updated_at = datetime.now(timezone.utc)
        workflow.updated_by = user_id or workflow.updated_by
        self._prepare_for_canvas(workflow)
        validation = validate_workflow(workflow)
        if not validation.valid:
            messages = "; ".join(error.message for error in validation.errors)
            raise ValueError(messages)
        if workflow.mode == "scheduled" and not workflow.trigger_schedule:
            raise ValueError("Scheduled workflows require trigger_schedule.")
        return self.repository.save(workflow)

    def run_workflow(
        self,
        workflow: Workflow,
        trigger_type: RunTriggerType = "manual",
        input_payload: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        self._prepare_for_canvas(workflow)
        started_at = datetime.now(timezone.utc)
        run = WorkflowRun(workflow_id=workflow.id, trigger_type=trigger_type, status="running", started_at=started_at)
        step_input = input_payload or {}
        steps: list[WorkflowRunStep] = []

        for index, node in enumerate(workflow.nodes, start=1):
            step_started = datetime.now(timezone.utc)
            label = node.label or node.type.replace("_", " ").title()
            if node.config.get("simulate_failure"):
                step = WorkflowRunStep(
                    step_id=node.id,
                    label=label,
                    status="failed",
                    started_at=step_started,
                    completed_at=datetime.now(timezone.utc),
                    input=step_input,
                    error=f"{label} failed during simulated execution.",
                )
                steps.append(step)
                run.status = "failed"
                break

            output = {
                "message": f"{label} completed.",
                "node_type": node.type,
                "sequence": index,
            }
            step = WorkflowRunStep(
                step_id=node.id,
                label=label,
                status="success",
                started_at=step_started,
                completed_at=datetime.now(timezone.utc),
                input=step_input,
                output=output,
            )
            steps.append(step)
            step_input = output

        completed_at = datetime.now(timezone.utc)
        if run.status != "failed":
            run.status = "success"
        run.completed_at = completed_at
        run.duration_ms = max(1, int((completed_at - started_at).total_seconds() * 1000))
        run.steps = steps
        succeeded = sum(1 for step in steps if step.status == "success")
        run.summary = f"{succeeded} of {len(workflow.nodes)} steps completed successfully."
        self.repository.save_run(run)
        return run

    def _generate_valid_workflow(self, task: str, payload: dict[str, Any]) -> CopilotResponse:
        current_payload = payload
        last_error: LLMError | None = None

        for _ in range(self.max_attempts):
            try:
                raw = self.provider.generate(task, current_payload)
            except LLMError as exc:
                last_error = exc
                break

            workflow = Workflow.model_validate(raw["workflow"])
            validation = validate_workflow(workflow)
            if validation.valid:
                return CopilotResponse(
                    workflow=workflow,
                    validation=validation,
                    explanation=raw.get("explanation"),
                    provider=self.provider.name,
                )
            current_payload = {
                **payload,
                "workflow": workflow.model_dump(by_alias=True),
                "validation_errors": [error.model_dump() for error in validation.errors],
            }
            task = "fix"

        if last_error is not None:
            raise last_error
        fallback_workflow = Workflow.model_validate(current_payload["workflow"])
        validation = validate_workflow(fallback_workflow)
        return CopilotResponse(workflow=fallback_workflow, validation=validation, provider=self.provider.name)

    def _prepare_for_canvas(self, workflow: Workflow) -> None:
        for node in workflow.nodes:
            definition = NODE_CATALOG.get(node.type)
            if node.role == "action":
                if "trigger" in node.type or node.type == "webhook":
                    node.role = "trigger"
                elif "condition" in node.type:
                    node.role = "condition"
            if node.label is None:
                node.label = node.type.replace("_", " ").title()
            if node.description is None:
                node.description = definition.description if definition else node.type.replace("_", " ")
            node.status = "idle"
