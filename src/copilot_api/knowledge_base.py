from __future__ import annotations

import re
from dataclasses import dataclass

from .models import ExtractedWorkflowRequest, WorkflowIntentProfile, WorkflowRecommendation


@dataclass(frozen=True)
class ProvenWorkflow:
    id: str
    name: str
    description: str
    industries: tuple[str, ...]
    workflow_types: tuple[str, ...]
    apps: tuple[str, ...]
    trigger: str
    steps: tuple[str, ...]
    guidance: tuple[str, ...]


PROVEN_WORKFLOWS: tuple[ProvenWorkflow, ...] = (
    ProvenWorkflow(
        id="sales-lead-intake",
        name="Sales lead intake and rapid follow-up",
        description="Capture a form lead, create ownership work, notify sales, update CRM, and acknowledge the customer.",
        industries=("sales", "marketing", "general"),
        workflow_types=("lead_management", "form_automation"),
        apps=("Jira", "Slack", "HubSpot", "Gmail"),
        trigger="Customer form submission",
        steps=(
            "Create Jira ticket",
            "Send Slack notification",
            "Update HubSpot contact",
            "Send follow-up email",
        ),
        guidance=(
            "Preserve the submitted email as the CRM identity.",
            "Notify the sales channel before sending the customer acknowledgement.",
            "Use the customer name in ticket and email templates when available.",
        ),
    ),
    ProvenWorkflow(
        id="support-ticket-triage",
        name="Customer support intake and triage",
        description="Turn support requests into tracked tickets, notify responders, and acknowledge the customer.",
        industries=("support", "saas", "general"),
        workflow_types=("support_triage", "ticket_management"),
        apps=("Jira", "Slack", "Gmail"),
        trigger="Customer support form submission",
        steps=("Create Jira ticket", "Send Slack notification", "Send acknowledgement email"),
        guidance=(
            "Include the customer message in the Jira description.",
            "Escalate high-priority language to the urgent support channel.",
        ),
    ),
    ProvenWorkflow(
        id="email-follow-up-tasks",
        name="Email-to-task follow-up",
        description="Find relevant unread emails, validate a condition, and create actionable follow-up tasks.",
        industries=("general", "sales", "operations"),
        workflow_types=("email_management", "task_automation"),
        apps=("Gmail",),
        trigger="Matching unread email",
        steps=("Check email condition", "Create follow-up task"),
        guidance=(
            "Search Gmail before evaluating the condition.",
            "Use the email subject in the task title.",
        ),
    ),
    ProvenWorkflow(
        id="meeting-action-capture",
        name="Meeting action-item capture",
        description="Review an upcoming event, create reminders, and publish preparation notes.",
        industries=("general", "consulting", "operations"),
        workflow_types=("meeting_management",),
        apps=("Google Calendar", "Notion"),
        trigger="Upcoming calendar event",
        steps=("Create reminder", "Create Notion preparation page"),
        guidance=("Keep the calendar event title as the shared context across steps.",),
    ),
    ProvenWorkflow(
        id="employee-onboarding",
        name="Employee onboarding coordination",
        description="Coordinate onboarding work, announcements, documentation, and welcome communication.",
        industries=("hr",),
        workflow_types=("employee_onboarding",),
        apps=("Jira", "Slack", "Notion", "Gmail"),
        trigger="New employee form submission",
        steps=("Create onboarding ticket", "Notify Slack", "Create Notion page", "Send welcome email"),
        guidance=("Do not send the welcome email until the onboarding record is created.",),
    ),
)


class WorkflowKnowledgeBase:
    name = "flowmind-proven-workflows"

    def retrieve(
        self,
        extracted: ExtractedWorkflowRequest,
        intent: WorkflowIntentProfile,
        limit: int = 3,
    ) -> list[tuple[ProvenWorkflow, float, str]]:
        query_tokens = self._tokens(
            " ".join(
                [
                    extracted.trigger,
                    extracted.goal,
                    *extracted.tasks,
                    intent.industry,
                    intent.workflow_type,
                    *intent.apps,
                ]
            )
        )
        ranked: list[tuple[ProvenWorkflow, float, str]] = []
        for template in PROVEN_WORKFLOWS:
            template_tokens = self._tokens(
                " ".join(
                    [
                        template.name,
                        template.description,
                        template.trigger,
                        *template.steps,
                        *template.apps,
                        *template.industries,
                        *template.workflow_types,
                    ]
                )
            )
            overlap = len(query_tokens & template_tokens) / max(1, len(query_tokens))
            type_bonus = 0.3 if intent.workflow_type in template.workflow_types else 0
            industry_bonus = (
                0.2
                if intent.industry != "general" and intent.industry in template.industries
                else 0.05
                if intent.industry == "general" and "general" in template.industries
                else 0
            )
            requested_apps = {app.lower() for app in intent.apps}
            template_apps = {app.lower() for app in template.apps}
            app_recall_bonus = 0.2 * (
                len(requested_apps & template_apps) / max(1, len(requested_apps))
            )
            app_precision_bonus = 0.15 * (
                len(requested_apps & template_apps) / max(1, len(template_apps))
            )
            if requested_apps == template_apps:
                app_precision_bonus += 0.05
            task_bonus = 0.2 if self._task_match(query_tokens, template_tokens) else 0
            trigger_bonus = 0.15 if self._trigger_match(query_tokens, template) else 0
            score = min(
                1.0,
                overlap
                + type_bonus
                + industry_bonus
                + app_recall_bonus
                + app_precision_bonus
                + task_bonus
                + trigger_bonus,
            )
            reason_parts = []
            if type_bonus:
                reason_parts.append("same workflow type")
            if industry_bonus:
                reason_parts.append("proven in this industry")
            if requested_apps & template_apps:
                reason_parts.append("uses the requested apps")
            reason = ", ".join(reason_parts) or "similar trigger and task sequence"
            ranked.append((template, round(score, 2), reason))
        return sorted(ranked, key=lambda item: item[1], reverse=True)[:limit]

    def recommendations(
        self,
        extracted: ExtractedWorkflowRequest,
        intent: WorkflowIntentProfile,
        limit: int = 3,
    ) -> list[WorkflowRecommendation]:
        return [
            WorkflowRecommendation(
                id=template.id,
                name=template.name,
                description=template.description,
                match_score=score,
                reason=reason,
                apps=list(template.apps),
                steps=list(template.steps),
            )
            for template, score, reason in self.retrieve(extracted, intent, limit)
        ]

    def template(self, template_id: str | None) -> ProvenWorkflow | None:
        return next((item for item in PROVEN_WORKFLOWS if item.id == template_id), None)

    def _tokens(self, text: str) -> set[str]:
        stop_words = {"a", "an", "and", "the", "to", "in", "of", "for", "with", "when"}
        return {
            token
            for token in re.findall(r"[a-z0-9]+", text.lower())
            if len(token) > 1 and token not in stop_words
        }

    def _task_match(self, query_tokens: set[str], template_tokens: set[str]) -> bool:
        task_terms = {"task", "tasks", "ticket", "tickets", "reminder", "page"}
        return bool(query_tokens & template_tokens & task_terms)

    def _trigger_match(self, query_tokens: set[str], template: ProvenWorkflow) -> bool:
        trigger_groups = (
            {"email", "emails", "mail", "mails", "inbox", "gmail"},
            {"form", "submission", "lead", "customer"},
            {"calendar", "event", "meeting"},
        )
        template_tokens = self._tokens(template.trigger)
        return any(query_tokens & group and template_tokens & group for group in trigger_groups)
