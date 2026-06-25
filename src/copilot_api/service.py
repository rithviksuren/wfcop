from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import re
from typing import Any

from .catalog import NODE_CATALOG
from .diff import diff_workflows
from .executor import WorkflowExecutionError, WorkflowExecutor
from .intent import enforce_workflow_intent
from .intelligence import WorkflowIntelligenceEngine
from .llm import LLMError, LLMProvider
from .models import (
    ClarifyingQuestion,
    Conversation,
    ConversationDetail,
    ConversationResponse,
    ConversationTurn,
    CopilotResponse,
    RunTriggerType,
    UpdateWorkflowRequest,
    ValidationErrorDetail,
    ValidationResult,
    Workflow,
    WorkflowAnalysisResponse,
    WorkflowEdge,
    WorkflowNode,
    WorkflowRun,
    WorkflowRunStep,
    WorkflowPlanResponse,
    WorkflowPlanSession,
    PlanningStep,
)
from .repository import WorkflowRepository
from .validation import repair_workflow, validate_workflow


class CopilotService:
    def __init__(self, provider: LLMProvider, repository: WorkflowRepository, max_attempts: int = 2) -> None:
        self.provider = provider
        self.repository = repository
        self.provider.bind_repository(repository)
        self.executor = WorkflowExecutor(repository)
        self.intelligence = WorkflowIntelligenceEngine(repository)
        self.max_attempts = max_attempts

    def analyze(self, instruction: str) -> WorkflowAnalysisResponse:
        return self.intelligence.analyze(instruction)

    def plan(
        self,
        instruction: str,
        user_id: str,
        context: dict[str, Any] | None = None,
    ) -> WorkflowPlanResponse:
        questions = self._clarifying_questions(instruction, context or {})
        now = datetime.now(timezone.utc)
        steps = self._planning_steps(
            awaiting_clarification=bool(questions)
        )
        session = WorkflowPlanSession(
            owner_id=user_id,
            instruction=instruction,
            status="awaiting_clarification" if questions else "ready",
            steps=steps,
            questions=questions,
            context=context or {},
            created_at=now,
            updated_at=now,
        )
        analysis = None
        if not questions:
            session.resolved_instruction = instruction
            analysis = self.analyze(instruction)
            session.status = "completed"
            session.steps = self._complete_planning_steps()
        self.repository.save_plan_session(session)
        return WorkflowPlanResponse(session=session, analysis=analysis)

    def continue_plan(
        self,
        session_id: str,
        answers: dict[str, str],
        user_id: str,
    ) -> WorkflowPlanResponse:
        session = self.repository.get_plan_session(session_id, user_id)
        if session is None:
            raise LookupError("Workflow plan not found.")
        known_questions = {question.id: question for question in session.questions}
        unknown = sorted(set(answers) - set(known_questions))
        if unknown:
            raise ValueError(
                "Unknown clarification answers: " + ", ".join(unknown)
            )
        session.answers.update(
            {
                key: value.strip()
                for key, value in answers.items()
                if value.strip()
            }
        )
        missing = [
            question
            for question in session.questions
            if question.required and not session.answers.get(question.id)
        ]
        session.updated_at = datetime.now(timezone.utc)
        if missing:
            session.questions = missing
            session.status = "awaiting_clarification"
            session.steps = self._planning_steps(awaiting_clarification=True)
            self.repository.save_plan_session(session)
            return WorkflowPlanResponse(session=session)

        resolved = self._resolve_plan_instruction(
            session.instruction,
            session.answers,
        )
        session.resolved_instruction = resolved
        session.status = "completed"
        session.questions = []
        session.steps = self._complete_planning_steps()
        analysis = self.analyze(resolved)
        self.repository.save_plan_session(session)
        return WorkflowPlanResponse(session=session, analysis=analysis)

    def get_plan(
        self,
        session_id: str,
        user_id: str,
    ) -> WorkflowPlanResponse:
        session = self.repository.get_plan_session(session_id, user_id)
        if session is None:
            raise LookupError("Workflow plan not found.")
        analysis = (
            self.analyze(session.resolved_instruction)
            if session.status == "completed" and session.resolved_instruction
            else None
        )
        return WorkflowPlanResponse(session=session, analysis=analysis)

    def build_analyzed_workflow(
        self,
        workflow: Workflow,
        user_id: str | None = None,
        instruction: str | None = None,
    ) -> Workflow:
        if instruction:
            analysis = self.intelligence.analyze(instruction)
            if analysis.unsupported_tasks:
                raise ValueError(
                    "FlowMind cannot build these requested actions yet: "
                    + "; ".join(analysis.unsupported_tasks)
                )
            expected_types = [
                node.type for node in analysis.proposed_workflow.nodes
            ]
            submitted_types = [node.type for node in workflow.nodes]
            if submitted_types != expected_types:
                raise ValueError(
                    "The proposed steps do not match the request. Analyze the request again "
                    "before building."
                )
        now = datetime.now(timezone.utc)
        workflow.id = Workflow().id
        workflow.version = 1
        workflow.created_at = now
        workflow.updated_at = now
        workflow.owner_id = user_id
        workflow.created_by = user_id
        workflow.updated_by = user_id
        self._prepare_for_canvas(workflow)
        validation = validate_workflow(workflow)
        if not validation.valid:
            messages = "; ".join(error.message for error in validation.errors)
            raise ValueError(messages)
        return self.repository.save(workflow)

    def repair_legacy_workflow(self, workflow: Workflow) -> Workflow:
        if self.intelligence.repair_legacy_email_workflow(workflow):
            workflow.version += 1
            workflow.updated_at = datetime.now(timezone.utc)
            self._prepare_for_canvas(workflow)
            self.repository.save(workflow)
        return workflow

    def create(
        self, instruction: str, context: dict[str, Any] | None = None, user_id: str | None = None
    ) -> CopilotResponse:
        result = self._generate_valid_workflow(
            "create",
            {
                "instruction": instruction,
                "context": context or {},
                "_tool_context": {"user_id": user_id},
            },
        )
        result.workflow = enforce_workflow_intent(result.workflow, instruction)
        result.workflow = self._ground_to_instruction(result.workflow, instruction)
        self._prepare_for_canvas(result.workflow)
        result.workflow, result.validation = self._repair_and_validate(
            result.workflow
        )
        if not result.validation.valid:
            raise ValueError(self._validation_message(result.validation))
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
        payload = {
            "workflow": workflow.model_dump(by_alias=True),
            "instruction": instruction,
            "context": context or {},
            "_tool_context": {"user_id": user_id},
        }
        result = self._generate_valid_workflow("modify", payload)
        result.workflow = self.intelligence.apply_contextual_modification(
            before,
            result.workflow,
            instruction,
        )
        result.workflow = self.intelligence.apply_additive_modification(
            before,
            result.workflow,
            instruction,
        )
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
        result.workflow, result.validation = self._repair_and_validate(
            result.workflow
        )
        if not result.validation.valid:
            raise ValueError(self._validation_message(result.validation))
        result.operations = diff_workflows(before, result.workflow)
        self.repository.save(result.workflow)
        return result

    def start_conversation(
        self,
        instruction: str,
        user_id: str,
        context: dict[str, Any] | None = None,
    ) -> ConversationResponse:
        result = self.create(
            instruction,
            context=context,
            user_id=user_id,
        )
        now = datetime.now(timezone.utc)
        conversation = Conversation(
            owner_id=user_id,
            title=self._conversation_title(instruction),
            workflow_id=result.workflow.id,
            created_at=now,
            updated_at=now,
        )
        self.repository.save_conversation(conversation)
        turn = ConversationTurn(
            conversation_id=conversation.id,
            sequence=1,
            kind="create",
            instruction=instruction,
            workflow=deepcopy(result.workflow),
            operations=result.operations,
            explanation=result.explanation,
            provider=result.provider,
        )
        self.repository.save_conversation_turn(turn)
        return ConversationResponse(
            conversation=conversation,
            turn=turn,
            validation=result.validation,
        )

    def continue_conversation(
        self,
        conversation_id: str,
        instruction: str,
        user_id: str,
        context: dict[str, Any] | None = None,
    ) -> ConversationResponse:
        conversation = self.repository.get_conversation(
            conversation_id,
            user_id,
        )
        if conversation is None:
            raise LookupError("Conversation not found.")
        latest = self.repository.latest_conversation_turn(conversation.id)
        if latest is None:
            raise ValueError("Conversation has no workflow context.")
        workflow = (
            self.repository.get(conversation.workflow_id)
            if conversation.workflow_id
            else None
        ) or deepcopy(latest.workflow)
        turns = self.repository.list_conversation_turns(conversation.id)
        memory_context = {
            **(context or {}),
            "conversation_history": [
                {
                    "sequence": turn.sequence,
                    "instruction": turn.instruction,
                    "workflow_name": turn.workflow.name,
                    "node_types": [
                        node.type for node in turn.workflow.nodes
                    ],
                }
                for turn in turns[-6:]
            ],
        }
        result = self.modify(
            workflow,
            instruction,
            context=memory_context,
            user_id=user_id,
        )
        conversation.workflow_id = result.workflow.id
        conversation.updated_at = datetime.now(timezone.utc)
        self.repository.save_conversation(conversation)
        turn = ConversationTurn(
            conversation_id=conversation.id,
            sequence=latest.sequence + 1,
            kind="modify",
            instruction=instruction,
            workflow=deepcopy(result.workflow),
            operations=result.operations,
            explanation=result.explanation,
            provider=result.provider,
        )
        self.repository.save_conversation_turn(turn)
        return ConversationResponse(
            conversation=conversation,
            turn=turn,
            validation=result.validation,
        )

    def get_conversation(
        self,
        conversation_id: str,
        user_id: str,
    ) -> ConversationDetail:
        conversation = self.repository.get_conversation(
            conversation_id,
            user_id,
        )
        if conversation is None:
            raise LookupError("Conversation not found.")
        return ConversationDetail(
            conversation=conversation,
            turns=self.repository.list_conversation_turns(conversation.id),
        )

    def list_conversations(self, user_id: str) -> list[Conversation]:
        return self.repository.list_conversations(user_id)

    def fix(
        self,
        workflow: Workflow,
        instruction: str,
        validation_errors: list[ValidationErrorDetail] | None = None,
        context: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> CopilotResponse:
        before = deepcopy(workflow)
        raw_errors = validation_errors or validate_workflow(workflow).errors
        errors = [
            error
            if isinstance(error, ValidationErrorDetail)
            else ValidationErrorDetail.model_validate(error)
            for error in raw_errors
        ]
        payload = {
            "workflow": workflow.model_dump(by_alias=True),
            "instruction": instruction,
            "validation_errors": [error.model_dump() for error in errors],
            "context": context or {},
            "_tool_context": {"user_id": user_id},
        }
        result = self._generate_valid_workflow("fix", payload)
        result.workflow = self._repair_using_context(
            before,
            result.workflow,
            errors,
            instruction,
            context or {},
        )
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
        result.workflow, result.validation = self._repair_and_validate(
            result.workflow
        )
        if not result.validation.valid:
            raise ValueError(self._validation_message(result.validation))
        result.operations = diff_workflows(before, result.workflow)
        self.repository.save(result.workflow)
        return result

    def explain(self, workflow: Workflow, instruction: str, context: dict[str, Any] | None = None) -> CopilotResponse:
        validation = validate_workflow(workflow)
        explanation = self._explain_workflow(workflow, validation.valid)
        return CopilotResponse(
            workflow=workflow,
            validation=validation,
            explanation=explanation,
            provider=self.provider.name,
        )

    def update_workflow(
        self, workflow: Workflow, request: UpdateWorkflowRequest, user_id: str | None = None
    ) -> Workflow:
        for field in request.model_fields_set:
            setattr(workflow, field, deepcopy(getattr(request, field)))
        workflow.version += 1
        workflow.updated_at = datetime.now(timezone.utc)
        workflow.updated_by = user_id or workflow.updated_by
        self._prepare_for_canvas(workflow)
        validation = validate_workflow(workflow)
        if not validation.valid:
            raise ValueError(self._validation_message(validation))
        return self.repository.save(workflow)

    def run_workflow(
        self,
        workflow: Workflow,
        trigger_type: RunTriggerType = "manual",
        input_payload: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        workflow = self.repair_legacy_workflow(workflow)
        self._prepare_for_canvas(workflow)
        validation = validate_workflow(workflow)
        if not validation.valid:
            raise ValueError(self._validation_message(validation))
        started_at = datetime.now(timezone.utc)
        run = WorkflowRun(workflow_id=workflow.id, trigger_type=trigger_type, status="running", started_at=started_at)
        self.repository.save_run(run)
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

            try:
                execution = self.executor.execute(
                    node,
                    step_input,
                    workflow_id=workflow.id,
                    run_id=run.id,
                )
                output = {
                    **execution.output,
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
                if not execution.continue_workflow:
                    break
            except WorkflowExecutionError as exc:
                steps.append(
                    WorkflowRunStep(
                        step_id=node.id,
                        label=label,
                        status="failed",
                        started_at=step_started,
                        completed_at=datetime.now(timezone.utc),
                        input=step_input,
                        error=str(exc),
                    )
                )
                run.status = "failed"
                break

        completed_at = datetime.now(timezone.utc)
        if run.status != "failed":
            run.status = "success"
        run.completed_at = completed_at
        run.duration_ms = max(1, int((completed_at - started_at).total_seconds() * 1000))
        run.steps = steps
        succeeded = sum(1 for step in steps if step.status == "success")
        if run.status == "failed":
            run.summary = f"The workflow could not finish. {steps[-1].error}"
        elif len(steps) < len(workflow.nodes):
            run.summary = f"No action was taken because {steps[-1].label} did not match."
        else:
            run.summary = f"Workflow completed successfully. {succeeded} steps ran."
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
                    provider=raw.get("provider", self.provider.name),
                )
            current_payload = {
                **payload,
                "workflow": workflow.model_dump(by_alias=True),
                "validation_errors": [error.model_dump() for error in validation.errors],
            }
            task = "fix"

        if last_error is not None:
            raise last_error
        fallback_workflow = repair_workflow(
            Workflow.model_validate(current_payload["workflow"])
        )
        validation = validate_workflow(fallback_workflow)
        return CopilotResponse(workflow=fallback_workflow, validation=validation, provider=self.provider.name)

    def _prepare_for_canvas(self, workflow: Workflow) -> None:
        for node in workflow.nodes:
            definition = NODE_CATALOG.get(node.type)
            if definition:
                node.role = definition.role
            if node.label is None:
                node.label = node.type.replace("_", " ").title()
            if node.description is None:
                node.description = definition.description if definition else node.type.replace("_", " ")
            node.status = "idle"

    def _repair_and_validate(
        self,
        workflow: Workflow,
    ) -> tuple[Workflow, ValidationResult]:
        validation = validate_workflow(workflow)
        if validation.valid:
            return workflow, validation
        repaired = repair_workflow(workflow)
        self._prepare_for_canvas(repaired)
        return repaired, validate_workflow(repaired)

    def _validation_message(self, validation: ValidationResult) -> str:
        return "; ".join(error.message for error in validation.errors)

    def _ground_to_instruction(self, workflow: Workflow, instruction: str) -> Workflow:
        analysis = self.intelligence.analyze(instruction)
        if analysis.unsupported_tasks or not analysis.extracted.tasks:
            return workflow

        expected = analysis.proposed_workflow
        if [node.type for node in workflow.nodes] != [
            node.type for node in expected.nodes
        ]:
            return expected

        for generated_node, expected_node in zip(workflow.nodes, expected.nodes):
            definition = NODE_CATALOG.get(expected_node.type)
            defaults = definition.defaults if definition else {}
            if expected_node.type in {
                "gmail_trigger",
                "filter_condition",
                "notion_create_page",
            }:
                explicit_config = expected_node.config
            else:
                explicit_config = {
                    key: value
                    for key, value in expected_node.config.items()
                    if defaults.get(key) != value
                }
            generated_node.config.update(explicit_config)
            generated_node.label = expected_node.label
            generated_node.description = expected_node.description

        workflow.name = expected.name
        if expected.mode == "scheduled":
            workflow.mode = expected.mode
            workflow.trigger_schedule = expected.trigger_schedule
            workflow.status = expected.status
        return workflow

    def _repair_using_context(
        self,
        original: Workflow,
        generated: Workflow,
        errors: list[ValidationErrorDetail],
        instruction: str,
        context: dict[str, Any],
    ) -> Workflow:
        repaired = deepcopy(original)
        generated_by_id = {node.id: node for node in generated.nodes}

        for error in errors:
            target = next(
                (
                    node
                    for node in repaired.nodes
                    if (error.node_id and node.id == error.node_id)
                    or (error.node_type and node.type == error.node_type)
                ),
                None,
            )
            if target is None or not error.field:
                continue

            generated_target = generated_by_id.get(target.id) or next(
                (
                    node
                    for node in generated.nodes
                    if node.type == target.type
                ),
                None,
            )
            value = self._infer_repair_value(
                repaired,
                target,
                error.field,
                generated_target,
                instruction,
                context,
            )
            if value not in (None, ""):
                target.config[error.field] = value

        existing_ids = {node.id for node in repaired.nodes}
        repaired.edges = [
            edge
            for edge in repaired.edges
            if edge.from_ in existing_ids
            and edge.to in existing_ids
            and edge.from_ != edge.to
        ]
        if len(repaired.nodes) > 1 and not repaired.edges:
            repaired.edges = [
                WorkflowEdge(
                    from_=repaired.nodes[index].id,
                    to=repaired.nodes[index + 1].id,
                )
                for index in range(len(repaired.nodes) - 1)
            ]
        return repaired

    def _infer_repair_value(
        self,
        workflow: Workflow,
        node: WorkflowNode,
        field: str,
        generated_node: WorkflowNode | None,
        instruction: str,
        context: dict[str, Any],
    ) -> Any:
        if node.type == "slack_message" and field == "channel_id":
            context_text = " ".join(
                [
                    workflow.name,
                    node.label or "",
                    node.description or "",
                    instruction,
                    " ".join(str(value) for value in context.values()),
                ]
            ).lower()
            channel_match = re.search(
                r"(?:#|to\s+(?:the\s+)?|notify\s+(?:the\s+)?)"
                r"([a-z0-9_-]+)\s+(?:team|channel)\b",
                context_text,
            )
            if channel_match:
                return channel_match.group(1)
            for department in (
                "finance",
                "sales",
                "support",
                "engineering",
                "operations",
                "marketing",
            ):
                if department in context_text:
                    return department

        if generated_node is not None:
            generated_value = generated_node.config.get(field)
            if generated_value not in (None, ""):
                return generated_value
        definition = NODE_CATALOG.get(node.type)
        return definition.defaults.get(field) if definition else None

    def _explain_workflow(self, workflow: Workflow, valid: bool) -> str:
        if not workflow.nodes:
            return (
                f'"{workflow.name}" has no steps yet, so it cannot perform any automation.'
            )

        incoming: dict[str, int] = {node.id: 0 for node in workflow.nodes}
        outgoing: dict[str, list[str]] = {node.id: [] for node in workflow.nodes}
        for edge in workflow.edges:
            if edge.from_ in outgoing and edge.to in incoming:
                outgoing[edge.from_].append(edge.to)
                incoming[edge.to] += 1

        triggers = [
            node
            for node in workflow.nodes
            if node.role == "trigger" or incoming[node.id] == 0
        ]
        conditions = [node for node in workflow.nodes if node.role == "condition"]
        actions = [
            node
            for node in workflow.nodes
            if node not in triggers and node not in conditions
        ]

        if workflow.mode == "scheduled":
            timing = (
                f'It runs on the schedule "{workflow.trigger_schedule}".'
                if workflow.trigger_schedule
                else "It is configured as scheduled, but no schedule is set."
            )
        else:
            timing = "It runs when started manually or by its configured trigger."

        sections = [
            f'"{workflow.name}" is a {workflow.status} workflow. {timing}',
            "Trigger: " + " ".join(
                self._explain_node(node) for node in triggers
            ),
        ]
        if conditions:
            sections.append(
                "Conditions: "
                + " ".join(self._explain_node(node) for node in conditions)
            )
        if actions:
            parallel = any(len(targets) > 1 for targets in outgoing.values())
            action_heading = "Actions run in parallel: " if parallel else "Actions: "
            sections.append(
                action_heading
                + " ".join(self._explain_node(node) for node in actions)
            )

        if not valid:
            sections.append(
                "Attention: the workflow currently has validation errors and must be fixed before it can run reliably."
            )
        return "\n\n".join(sections)

    def _explain_node(self, node: WorkflowNode) -> str:
        config = node.config
        if node.type == "gmail_trigger":
            sender = str(config.get("from_contains", "any sender"))
            search_text = str(config.get("search_text", "")).strip()
            sentence = (
                "Gmail checks for a new unread email"
                if sender.lower() in {"", "any sender"}
                else f"Gmail checks for a new unread email from {sender}"
            )
            if search_text:
                sentence += f' containing "{search_text}"'
            return sentence + "."

        if node.type == "filter_condition":
            field = str(config.get("field", "the selected field")).replace("_", " ")
            operator = str(config.get("operator", "matches")).replace("_", " ")
            value = config.get("value", "")
            return f'It continues only when {field} {operator} "{value}".'

        if node.type == "slack_message":
            channel = str(config.get("channel_id", "the selected channel"))
            template = str(config.get("message_template", "")).strip()
            detail = (
                f' using the message template "{template}"'
                if template
                else ""
            )
            return f"It sends a Slack message to #{channel}{detail}."

        if node.type == "notion_create_page":
            title = str(config.get("title_template", "New workflow event"))
            content_fields = self._template_fields(
                str(config.get("content_template", ""))
            )
            detail = (
                " and includes " + ", ".join(content_fields)
                if content_fields
                else ""
            )
            return f'It creates a Notion page titled "{title}"{detail}.'

        if node.type == "task_create":
            title = str(config.get("title_template", "New task"))
            return f'It creates a task titled "{title}".'

        if node.type == "calendar_event_trigger":
            minutes = config.get("lookahead_minutes", 60)
            return f"Google Calendar checks for events in the next {minutes} minutes."

        if node.type == "reminder_create":
            message = str(config.get("message_template", "Upcoming event"))
            return f'It creates a reminder saying "{message}".'

        if node.type == "form_submission_trigger":
            return "A form submission starts the workflow."

        if node.type == "jira_ticket_create":
            project = str(config.get("project_key", "the configured project"))
            return f"It creates a Jira ticket in project {project}."

        if node.type == "crm_update":
            provider = str(config.get("provider", "the connected CRM")).title()
            return f"It creates or updates the matching contact in {provider}."

        if node.type == "email_send":
            subject = str(config.get("subject_template", "Follow-up"))
            return f'It sends a follow-up email with subject "{subject}".'

        if node.type == "teams_message":
            channel = str(config.get("channel_id", "the selected channel"))
            return f"It sends a Microsoft Teams message to {channel}."

        if node.type == "webhook":
            return "An incoming webhook request starts the workflow."

        definition = NODE_CATALOG.get(node.type)
        return (
            f"{definition.description}."
            if definition
            else f"It runs the {node.type.replace('_', ' ')} step."
        )

    def _template_fields(self, template: str) -> list[str]:
        friendly_names = {
            "from": "sender",
            "to": "recipient",
            "date": "date",
            "subject": "subject",
            "body": "email body",
            "name": "name",
            "email": "email address",
            "message": "message",
        }
        fields: list[str] = []
        for raw_field in re.findall(r"\{\{\s*([^}]+?)\s*\}\}", template):
            field = friendly_names.get(raw_field, raw_field.replace("_", " "))
            if field not in fields:
                fields.append(field)
        return fields

    def _conversation_title(self, instruction: str) -> str:
        clean = re.sub(r"\s+", " ", instruction).strip(" .")
        return clean if len(clean) <= 80 else clean[:77].rstrip() + "..."

    def _clarifying_questions(
        self,
        instruction: str,
        context: dict[str, Any],
    ) -> list[ClarifyingQuestion]:
        text = f"{instruction} {' '.join(str(value) for value in context.values())}".lower()
        broad_lead_request = bool(
            re.search(r"\blead(?:\s+capture|\s+intake|\s+system)?\b", text)
        )
        if not broad_lead_request:
            return []

        questions: list[ClarifyingQuestion] = []
        if not re.search(r"\b(?:form|email|gmail|webhook|http)\b", text):
            questions.append(
                ClarifyingQuestion(
                    id="lead_source",
                    question="How should new leads enter the workflow?",
                    reason="The trigger determines what starts the automation.",
                    choices=[
                        "Customer form submission",
                        "Incoming email",
                        "Webhook/API request",
                    ],
                )
            )
        if not re.search(r"\b(?:hubspot|crm|jira|task list|notion)\b", text):
            questions.append(
                ClarifyingQuestion(
                    id="lead_destination",
                    question="Where should each captured lead be stored?",
                    reason="The workflow needs a system of record.",
                    choices=[
                        "HubSpot CRM",
                        "Jira ticket",
                        "Team task list",
                        "Notion page",
                    ],
                )
            )
        if not re.search(r"\b(?:slack|teams|follow-up email|send email|notify)\b", text):
            questions.append(
                ClarifyingQuestion(
                    id="lead_notification",
                    question="How should the team be notified about a new lead?",
                    reason="Notification routing is a business choice and should not be guessed.",
                    choices=[
                        "Slack",
                        "Microsoft Teams",
                        "Send a follow-up email",
                        "No notification",
                    ],
                )
            )
        return questions

    def _planning_steps(
        self,
        *,
        awaiting_clarification: bool,
    ) -> list[PlanningStep]:
        return [
            PlanningStep(
                id="understand",
                label="Understand outcome",
                description="Identify the requested business outcome and workflow scope.",
                status="completed",
            ),
            PlanningStep(
                id="clarify",
                label="Resolve missing decisions",
                description="Ask only for choices that materially change the workflow.",
                status="in_progress" if awaiting_clarification else "completed",
            ),
            PlanningStep(
                id="design",
                label="Design workflow",
                description="Select supported nodes and construct the graph.",
                status="pending" if awaiting_clarification else "in_progress",
            ),
            PlanningStep(
                id="validate",
                label="Validate workflow",
                description="Check configuration and graph integrity.",
                status="pending",
            ),
        ]

    def _complete_planning_steps(self) -> list[PlanningStep]:
        return [
            PlanningStep(
                id="understand",
                label="Understand outcome",
                description="Identify the requested business outcome and workflow scope.",
                status="completed",
            ),
            PlanningStep(
                id="clarify",
                label="Resolve missing decisions",
                description="Ask only for choices that materially change the workflow.",
                status="completed",
            ),
            PlanningStep(
                id="design",
                label="Design workflow",
                description="Select supported nodes and construct the graph.",
                status="completed",
            ),
            PlanningStep(
                id="validate",
                label="Validate workflow",
                description="Check configuration and graph integrity.",
                status="completed",
            ),
        ]

    def _resolve_plan_instruction(
        self,
        instruction: str,
        answers: dict[str, str],
    ) -> str:
        source = answers.get("lead_source", "Customer form submission")
        destination = answers.get("lead_destination", "HubSpot CRM")
        notification = answers.get("lead_notification", "No notification")

        source_clause = {
            "customer form submission": "Whenever a customer fills a form",
            "incoming email": "Whenever a lead email arrives",
            "webhook/api request": "Whenever a lead arrives through a webhook",
        }.get(source.lower(), f"Whenever {source}")
        destination_clause = {
            "hubspot crm": "update HubSpot CRM",
            "jira ticket": "create a Jira ticket",
            "team task list": "create a task in the team task list",
            "notion page": "create a Notion page",
        }.get(destination.lower(), destination)
        notification_clause = {
            "slack": "notify Slack",
            "microsoft teams": "notify Microsoft Teams",
            "send a follow-up email": "send a follow-up email",
            "no notification": "",
        }.get(notification.lower(), notification)
        actions = ", ".join(
            item for item in (destination_clause, notification_clause) if item
        )
        return f"{source_clause}, {actions}."
