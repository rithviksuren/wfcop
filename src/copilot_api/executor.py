from __future__ import annotations

import email
import base64
import datetime as dt
import imaplib
import json
import os
import re
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.header import decode_header, make_header
from email.message import Message
from email.message import EmailMessage
from typing import Any

from .models import WorkflowNode, WorkflowTask
from .repository import WorkflowRepository


class WorkflowExecutionError(RuntimeError):
    pass


@dataclass
class NodeExecution:
    output: dict[str, Any]
    continue_workflow: bool = True


class WorkflowExecutor:
    NOTION_API_VERSION = "2025-09-03"

    def __init__(self, repository: WorkflowRepository) -> None:
        self.repository = repository

    def execute(
        self,
        node: WorkflowNode,
        payload: dict[str, Any],
        *,
        workflow_id: str,
        run_id: str,
    ) -> NodeExecution:
        handler = getattr(self, f"_execute_{node.type}", None)
        if handler is None:
            raise WorkflowExecutionError(f"No executor is configured for node type '{node.type}'.")
        return handler(node, payload, workflow_id=workflow_id, run_id=run_id)

    @staticmethod
    def normalize_gmail_app_password(password: str) -> str:
        return re.sub(r"\s+", "", password)

    def test_gmail_connection(self, username: str, password: str) -> None:
        normalized_password = self.normalize_gmail_app_password(password)
        if len(normalized_password) != 16:
            raise WorkflowExecutionError(
                "Gmail does not accept your normal Google Account password here. "
                "Turn on 2-Step Verification, create a 16-character Google app password, "
                "and paste that app password into FlowMind."
            )
        try:
            with imaplib.IMAP4_SSL("imap.gmail.com", timeout=15) as mailbox:
                mailbox.login(username, normalized_password)
                status, _ = mailbox.select("INBOX", readonly=True)
                if status != "OK":
                    raise WorkflowExecutionError("Gmail connected, but FlowMind could not open the inbox.")
        except imaplib.IMAP4.error as exc:
            raise WorkflowExecutionError(self._gmail_authentication_error(exc)) from exc
        except OSError as exc:
            raise WorkflowExecutionError(f"FlowMind could not reach Gmail: {exc}") from exc

    def _execute_gmail_trigger(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        message = self._email_from_payload(payload)
        source = "manual input"
        if message is None:
            message = self._fetch_gmail_message(
                node.config.get("from_contains", "any sender"),
                node.config.get("search_text", ""),
            )
            source = "Gmail"
        if message is None:
            return NodeExecution(
                output={
                    "triggered": False,
                    "source": source,
                    "reason": "No unread email matched the trigger.",
                },
                continue_workflow=False,
            )

        expected_sender = str(node.config.get("from_contains", "any sender")).strip()
        sender = str(message.get("from", ""))
        matched = expected_sender.lower() in {"", "any sender"} or expected_sender.lower() in sender.lower()
        output = {**message, "triggered": matched, "source": source}
        if not matched:
            output["reason"] = f"Email sender did not contain '{expected_sender}'."
        return NodeExecution(output=output, continue_workflow=matched)

    def _execute_calendar_event_trigger(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
        if not any(event.get(field) for field in ("title", "summary", "start", "event_id")):
            raise WorkflowExecutionError(
                "Calendar execution needs an event in the run input. Live Google Calendar credentials are not configured."
            )
        return NodeExecution(output={**event, "triggered": True, "source": "manual input"})

    def _execute_calendar_event_create(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("google_calendar")
        service_account_json = integration.get("service_account_json") or os.getenv(
            "GOOGLE_CALENDAR_SERVICE_ACCOUNT_JSON"
        )
        if not service_account_json:
            raise WorkflowExecutionError(
                "Google Calendar is not connected. Open Integrations and add a service account JSON."
            )
        calendar_id = (
            node.config.get("calendar_id")
            if node.config.get("calendar_id") not in (None, "", "primary", "default")
            else integration.get("calendar_id")
            or os.getenv("GOOGLE_CALENDAR_ID")
            or "primary"
        )
        summary = self._render_template(
            str(node.config.get("summary_template", "New event")), payload
        )
        description = self._render_template(
            str(node.config.get("description_template", "")), payload
        )
        timezone_name = str(node.config.get("timezone", "UTC") or "UTC")
        start_text = self._render_template(
            str(node.config.get("start_datetime_template", "")), payload
        )
        end_text = self._render_template(
            str(node.config.get("end_datetime_template", "")), payload
        )
        start_at, end_at = self._calendar_event_window(start_text, end_text)
        token = self._google_service_account_access_token(
            service_account_json,
            "https://www.googleapis.com/auth/calendar.events",
        )
        event_payload = {
            "summary": summary or "New workflow event",
            "description": description,
            "start": {"dateTime": start_at.isoformat(), "timeZone": timezone_name},
            "end": {"dateTime": end_at.isoformat(), "timeZone": timezone_name},
        }
        result = self._post_json(
            "https://www.googleapis.com/calendar/v3/calendars/"
            f"{urllib.parse.quote(str(calendar_id), safe='')}/events",
            event_payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        return NodeExecution(
            output={
                **payload,
                "created_calendar_event": {
                    "id": result.get("id"),
                    "html_link": result.get("htmlLink"),
                    "summary": event_payload["summary"],
                    "start": event_payload["start"]["dateTime"],
                    "end": event_payload["end"]["dateTime"],
                },
            }
        )

    def _execute_webhook(self, node: WorkflowNode, payload: dict[str, Any], **_: Any) -> NodeExecution:
        if not payload:
            raise WorkflowExecutionError("Webhook execution needs a request payload.")
        return NodeExecution(output={**payload, "triggered": True, "path": node.config.get("path")})

    def _execute_form_submission_trigger(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        submission = payload.get("form") or payload.get("submission") or payload
        meaningful_fields = (
            {key: value for key, value in submission.items() if key != "source"}
            if isinstance(submission, dict)
            else {}
        )
        if not meaningful_fields:
            raise WorkflowExecutionError("This workflow needs a form submission payload to start.")
        return NodeExecution(
            output={
                **meaningful_fields,
                "triggered": True,
                "form_id": node.config.get("form_id", "any"),
                "source": "form submission",
            }
        )

    def _execute_filter_condition(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        field = str(node.config.get("field", "")).strip()
        operator = str(node.config.get("operator", "equals")).strip().lower()
        expected = node.config.get("value")
        actual = self._lookup(payload, field)
        matched = self._compare(actual, operator, expected)
        return NodeExecution(
            output={
                **payload,
                "condition": {
                    "field": field,
                    "operator": operator,
                    "expected": expected,
                    "actual": actual,
                    "matched": matched,
                },
            },
            continue_workflow=matched,
        )

    def _execute_task_create(
        self,
        node: WorkflowNode,
        payload: dict[str, Any],
        *,
        workflow_id: str,
        run_id: str,
    ) -> NodeExecution:
        title = self._render_template(str(node.config.get("title_template", "New workflow task")), payload)
        if not title or title.rstrip(": ").lower() in {"email follow-up", "follow up"}:
            title = (
                f"Email follow-up: {payload.get('subject')}"
                if payload.get("subject")
                else "Workflow follow-up task"
            )
        task = self.repository.save_task(
            WorkflowTask(
                workflow_id=workflow_id,
                run_id=run_id,
                list_id=str(node.config.get("list_id", "default")),
                title=title,
                source=payload,
            )
        )
        return NodeExecution(
            output={
                **payload,
                "created_task": {
                    "id": task.id,
                    "title": task.title,
                    "list_id": task.list_id,
                    "status": task.status,
                },
            }
        )

    def _execute_reminder_create(
        self,
        node: WorkflowNode,
        payload: dict[str, Any],
        *,
        workflow_id: str,
        run_id: str,
    ) -> NodeExecution:
        title = self._render_template(str(node.config.get("message_template", "Workflow reminder")), payload)
        task = self.repository.save_task(
            WorkflowTask(
                workflow_id=workflow_id,
                run_id=run_id,
                list_id=f"reminders:{node.config.get('channel', 'in_app')}",
                title=title,
                source=payload,
            )
        )
        return NodeExecution(output={**payload, "created_reminder": {"id": task.id, "title": task.title}})

    def _execute_slack_message(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("slack")
        webhook_url = (
            node.config.get("webhook_url")
            or integration.get("webhook_url")
            or os.getenv("SLACK_WEBHOOK_URL")
        )
        if not webhook_url:
            raise WorkflowExecutionError("Slack is not configured. Open Integrations and add a webhook URL.")
        text = self._render_template(str(node.config.get("message_template", "Workflow event")), payload)
        self._post_json(str(webhook_url), {"text": text})
        return NodeExecution(output={**payload, "sent_message": {"provider": "slack", "text": text}})

    def _execute_teams_message(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("teams")
        webhook_url = (
            node.config.get("webhook_url")
            or integration.get("webhook_url")
            or os.getenv("TEAMS_WEBHOOK_URL")
        )
        if not webhook_url:
            raise WorkflowExecutionError(
                "Microsoft Teams is not configured. Open Integrations and add a webhook URL."
            )
        text = self._render_template(str(node.config.get("message_template", "Workflow event")), payload)
        self._post_json(str(webhook_url), {"text": text})
        return NodeExecution(output={**payload, "sent_message": {"provider": "teams", "text": text}})

    def _execute_notion_create_page(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("notion")
        token = integration.get("api_token") or os.getenv("NOTION_API_TOKEN")
        if not token:
            raise WorkflowExecutionError(
                "Notion is not configured. Open Integrations and add an integration token."
            )
        data_source_id = self._resolve_notion_data_source_id(
            node,
            integration,
            str(token),
        )
        title = self._render_template(str(node.config.get("title_template", "Workflow event")), payload)
        content = self._render_template(str(node.config.get("content_template", "")), payload)
        title_property = integration.get("title_property", "Name")
        request_payload: dict[str, Any] = {
            "parent": {
                "type": "data_source_id",
                "data_source_id": data_source_id,
            },
            "properties": {title_property: {"title": [{"text": {"content": title}}]}},
        }
        children = self._notion_content_blocks(content)
        if children:
            request_payload["children"] = children
        try:
            response = self._post_json(
                "https://api.notion.com/v1/pages",
                request_payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Notion-Version": self.NOTION_API_VERSION,
                },
            )
        except WorkflowExecutionError as exc:
            raise WorkflowExecutionError(self._notion_error_message(exc)) from exc
        return NodeExecution(
            output={
                **payload,
                "created_page": {
                    "id": response.get("id"),
                    "title": title,
                    "content": content,
                },
            }
        )

    def _execute_jira_ticket_create(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("jira")
        missing = [
            field
            for field in ("base_url", "email", "api_token", "project_key")
            if not integration.get(field)
        ]
        if missing:
            raise WorkflowExecutionError(
                "Jira is not connected. Open Integrations and add the Jira API details."
            )
        credentials = base64.b64encode(
            f"{integration['email']}:{integration['api_token']}".encode("utf-8")
        ).decode("ascii")
        summary = self._render_template(
            str(node.config.get("summary_template", "New workflow item")), payload
        )
        description = self._render_template(
            str(node.config.get("description_template", "")), payload
        )
        result = self._post_json(
            f"{integration['base_url'].rstrip('/')}/rest/api/3/issue",
            {
                "fields": {
                    "project": {
                        "key": node.config.get("project_key")
                        or integration.get("project_key")
                    },
                    "summary": summary,
                    "description": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": description or summary}],
                            }
                        ],
                    },
                    "issuetype": {"name": "Task"},
                }
            },
            headers={"Authorization": f"Basic {credentials}", "Accept": "application/json"},
        )
        return NodeExecution(
            output={
                **payload,
                "created_jira_ticket": {
                    "key": result.get("key"),
                    "summary": summary,
                },
            }
        )

    def _execute_crm_update(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("hubspot")
        token = integration.get("private_app_token")
        if not token:
            raise WorkflowExecutionError(
                "HubSpot is not connected. Open Integrations and add a private app token."
            )
        email_field = str(node.config.get("email_field", "email"))
        contact_email = self._lookup(payload, email_field)
        if not contact_email:
            raise WorkflowExecutionError("The form submission does not include a customer email.")
        properties = {
            "email": str(contact_email),
            "firstname": str(payload.get("name") or payload.get("first_name") or ""),
            "company": str(payload.get("company") or ""),
        }
        result = self._post_json(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            {"properties": {key: value for key, value in properties.items() if value}},
            headers={"Authorization": f"Bearer {token}"},
        )
        return NodeExecution(
            output={
                **payload,
                "updated_crm": {
                    "provider": "hubspot",
                    "contact_id": result.get("id"),
                    "email": contact_email,
                },
            }
        )

    def _execute_email_send(
        self, node: WorkflowNode, payload: dict[str, Any], **_: Any
    ) -> NodeExecution:
        integration = self.repository.get_integration("gmail")
        username = integration.get("email") or os.getenv("GMAIL_EMAIL")
        password = integration.get("app_password") or os.getenv("GMAIL_APP_PASSWORD")
        if not username or not password:
            raise WorkflowExecutionError(
                "Gmail is not connected. Open Integrations and add a Gmail app password."
            )
        recipient = self._render_template(
            str(node.config.get("to_template", "{{email}}")), payload
        )
        if not recipient:
            raise WorkflowExecutionError("The workflow could not find a recipient email address.")
        message = EmailMessage()
        message["From"] = username
        message["To"] = recipient
        message["Subject"] = self._render_template(
            str(node.config.get("subject_template", "Workflow follow-up")), payload
        )
        message.set_content(
            self._render_template(str(node.config.get("body_template", "")), payload)
        )
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
                smtp.login(username, self.normalize_gmail_app_password(password))
                smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise WorkflowExecutionError(f"Follow-up email could not be sent: {exc}") from exc
        return NodeExecution(
            output={
                **payload,
                "sent_email": {
                    "to": recipient,
                    "subject": message["Subject"],
                },
            }
        )

    def _fetch_gmail_message(self, expected_sender: str, search_text: str = "") -> dict[str, Any] | None:
        integration = self.repository.get_integration("gmail")
        username = integration.get("email") or os.getenv("GMAIL_EMAIL")
        password = integration.get("app_password") or os.getenv("GMAIL_APP_PASSWORD")
        if not username or not password:
            raise WorkflowExecutionError(
                "Gmail is not configured. Open Integrations and add your Gmail address and app password, "
                "or provide an email object in the run input."
            )
        password = self.normalize_gmail_app_password(password)
        if len(password) != 16:
            raise WorkflowExecutionError(
                "The saved Gmail credential is not a valid Google app password. "
                "Open Integrations, replace it with a 16-character app password, and save again."
            )

        try:
            with imaplib.IMAP4_SSL("imap.gmail.com", timeout=15) as mailbox:
                mailbox.login(username, password)
                mailbox.select("INBOX")
                criteria_parts = ["UNSEEN"]
                if expected_sender.lower() not in {"", "any sender"}:
                    criteria_parts.extend(["FROM", f'"{self._escape_imap_search(expected_sender)}"'])
                if search_text:
                    criteria_parts.extend(["TEXT", f'"{self._escape_imap_search(search_text)}"'])
                criteria = f"({' '.join(criteria_parts)})"
                status, data = mailbox.search(None, criteria)
                if status != "OK" or not data or not data[0]:
                    return None
                message_id = data[0].split()[-1]
                status, message_data = mailbox.fetch(message_id, "(RFC822 X-GM-LABELS)")
                if status != "OK" or not message_data or not isinstance(message_data[0], tuple):
                    return None
                parsed = email.message_from_bytes(message_data[0][1])
                result = self._normalize_email(parsed)
                metadata = message_data[0][0].decode("utf-8", errors="replace")
                labels_match = re.search(r"X-GM-LABELS \((.*?)\)", metadata)
                labels = re.findall(r'"([^"]+)"|([^ ]+)', labels_match.group(1)) if labels_match else []
                result["labels"] = [quoted or bare for quoted, bare in labels]
                result["tag"] = self._infer_tag(result)
                return result
        except imaplib.IMAP4.error as exc:
            raise WorkflowExecutionError(self._gmail_authentication_error(exc)) from exc
        except OSError as exc:
            raise WorkflowExecutionError(f"FlowMind could not reach Gmail: {exc}") from exc

    def _gmail_authentication_error(self, error: Exception) -> str:
        detail = str(error).lower()
        if "application-specific password" in detail or "invalid credentials" in detail:
            return (
                "Google rejected the Gmail credential. Your normal Google Account password cannot be used. "
                "Open Integrations and replace it with a 16-character Google app password."
            )
        return f"Gmail authentication failed: {error}"

    def _email_from_payload(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        candidate = payload.get("email") if isinstance(payload.get("email"), dict) else payload
        if not any(candidate.get(field) for field in ("from", "sender", "subject", "body")):
            return None
        normalized = dict(candidate)
        if "from" not in normalized and "sender" in normalized:
            normalized["from"] = normalized["sender"]
        for field in ("from", "to", "subject"):
            if normalized.get(field):
                normalized[field] = self.decode_email_header(str(normalized[field]))
        normalized.setdefault("tag", self._infer_tag(normalized))
        return normalized

    def _normalize_email(self, message: Message) -> dict[str, Any]:
        body = ""
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                    body = self._decode_part(part)
                    break
        else:
            body = self._decode_part(message)
        return {
            "from": self.decode_email_header(str(message.get("From", ""))),
            "to": self.decode_email_header(str(message.get("To", ""))),
            "subject": self.decode_email_header(str(message.get("Subject", ""))),
            "date": str(message.get("Date", "")),
            "message_id": str(message.get("Message-ID", "")),
            "body": body[:10000],
        }

    def _decode_part(self, part: Message) -> str:
        raw = part.get_payload(decode=True)
        if raw is None:
            return str(part.get_payload())
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace")

    @staticmethod
    def decode_email_header(value: str) -> str:
        try:
            return str(make_header(decode_header(value))).replace("\r", "").replace("\n", " ").strip()
        except (LookupError, UnicodeDecodeError, ValueError):
            return re.sub(r"\s+", " ", value).strip()

    def _infer_tag(self, message: dict[str, Any]) -> str:
        labels = " ".join(str(value) for value in message.get("labels", []))
        text = f"{labels} {message.get('subject', '')} {message.get('body', '')}".lower()
        return "urgent" if "urgent" in text or "important" in text else ""

    def _lookup(self, payload: dict[str, Any], field: str) -> Any:
        if field in {"email_text", "content", "subject_or_body"}:
            return " ".join(
                str(payload.get(part, ""))
                for part in ("subject", "body")
                if payload.get(part)
            ).strip()
        current: Any = payload
        for part in field.split("."):
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    def _escape_imap_search(self, value: str) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    def _compare(self, actual: Any, operator: str, expected: Any) -> bool:
        if operator in {"equals", "eq", "=="}:
            return str(actual).lower() == str(expected).lower()
        if operator in {"not_equals", "neq", "!="}:
            return str(actual).lower() != str(expected).lower()
        if operator == "contains":
            if isinstance(actual, list):
                return any(str(expected).lower() in str(item).lower() for item in actual)
            return str(expected).lower() in str(actual).lower()
        if operator == "exists":
            return actual not in (None, "", [], {})
        if operator in {"greater_than", "gt", ">"}:
            return float(actual) > float(expected)
        if operator in {"less_than", "lt", "<"}:
            return float(actual) < float(expected)
        raise WorkflowExecutionError(f"Unsupported filter operator '{operator}'.")

    def _render_template(self, template: str, payload: dict[str, Any]) -> str:
        def replace(match: re.Match[str]) -> str:
            value = self._lookup(payload, match.group(1).strip())
            return "" if value is None else str(value)

        return re.sub(r"\{\{\s*([^}]+)\s*\}\}", replace, template).strip()

    def _notion_content_blocks(self, content: str) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for paragraph in re.split(r"\n\s*\n", content.strip()):
            clean = paragraph.strip()
            if not clean:
                continue
            for start in range(0, len(clean), 2000):
                chunk = clean[start : start + 2000]
                blocks.append(
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [
                                {
                                    "type": "text",
                                    "text": {"content": chunk},
                                }
                            ]
                        },
                    }
                )
        return blocks[:100]

    def _resolve_notion_data_source_id(
        self,
        node: WorkflowNode,
        integration: dict[str, str],
        token: str,
    ) -> str:
        direct_value = (
            node.config.get("data_source_id")
            if node.config.get("data_source_id") not in (None, "", "default")
            else integration.get("data_source_id")
            or os.getenv("NOTION_DATA_SOURCE_ID")
        )
        if direct_value:
            data_source_id = self.normalize_notion_id(str(direct_value), "data source")
            try:
                self._get_json(
                    f"https://api.notion.com/v1/data_sources/{data_source_id}",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Notion-Version": self.NOTION_API_VERSION,
                    },
                )
            except WorkflowExecutionError as exc:
                raise WorkflowExecutionError(self._notion_error_message(exc)) from exc
            return data_source_id

        legacy_value = (
            node.config.get("database_id")
            if node.config.get("database_id") not in (None, "", "default")
            else integration.get("database_id")
            or os.getenv("NOTION_DATABASE_ID")
        )
        if not legacy_value:
            raise WorkflowExecutionError(
                "Notion is not configured. Open Integrations and add the target data source ID."
            )

        database_id = self.normalize_notion_id(str(legacy_value), "database")
        headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": self.NOTION_API_VERSION,
        }
        try:
            database = self._get_json(
                f"https://api.notion.com/v1/databases/{database_id}",
                headers=headers,
            )
        except WorkflowExecutionError as database_error:
            try:
                self._get_json(
                    f"https://api.notion.com/v1/data_sources/{database_id}",
                    headers=headers,
                )
                return database_id
            except WorkflowExecutionError as data_source_error:
                raise WorkflowExecutionError(
                    self._notion_error_message(data_source_error)
                ) from database_error

        data_sources = database.get("data_sources")
        if not isinstance(data_sources, list) or not data_sources:
            raise WorkflowExecutionError(
                "The Notion database has no accessible data source. Share the database with "
                "your integration and copy its data source ID."
            )
        if len(data_sources) > 1:
            raise WorkflowExecutionError(
                "This Notion database has multiple data sources. Open Manage data sources, "
                "copy the exact data source ID you want, and save it in Integrations."
            )
        data_source_id = self.normalize_notion_id(
            str(data_sources[0].get("id", "")),
            "data source",
        )
        if integration.get("database_id") == legacy_value:
            migrated = {
                **integration,
                "data_source_id": data_source_id,
            }
            self.repository.save_integration("notion", migrated)
        return data_source_id

    @staticmethod
    def _notion_error_message(error: Exception) -> str:
        detail = str(error)
        lowered = detail.lower()
        if "http 404" in lowered or "object_not_found" in lowered:
            return (
                "Notion cannot access the selected data source. Open the database in Notion, "
                "choose ••• → Connections, and add the same Notion integration whose token is "
                "saved in FlowMind. If the database is in another workspace, use an integration "
                "token from that workspace. Then verify the Data source ID in FlowMind → "
                "Integrations → Notion and run the workflow again."
            )
        if "http 401" in lowered or "unauthorized" in lowered:
            return (
                "The saved Notion integration token is invalid or expired. Create or copy the "
                "internal integration secret from Notion, save it again in FlowMind → "
                "Integrations → Notion, and retry."
            )
        if "http 403" in lowered or "restricted_resource" in lowered:
            return (
                "The Notion integration does not have permission to edit this data source. "
                "Share the database with the integration and ensure it has content insertion "
                "capabilities, then retry."
            )
        if "validation_error" in lowered:
            return (
                "Notion rejected the page configuration. Check that the configured title "
                "property exactly matches the database's title column, then retry."
            )
        return detail

    @staticmethod
    def normalize_notion_id(value: str, label: str = "identifier") -> str:
        clean = value.strip()
        matches = re.findall(
            r"(?<![0-9a-fA-F])"
            r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
            r"(?![0-9a-fA-F])",
            clean,
        )
        if not matches:
            raise WorkflowExecutionError(
                f"The Notion {label} is invalid. It must be a Notion ID or URL, not an "
                "email address or account name."
            )
        raw = matches[-1].replace("-", "").lower()
        return (
            f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-"
            f"{raw[16:20]}-{raw[20:]}"
        )

    def _calendar_event_window(
        self,
        start_text: str,
        end_text: str,
    ) -> tuple[dt.datetime, dt.datetime]:
        start = self._parse_datetime(start_text) if start_text else None
        end = self._parse_datetime(end_text) if end_text else None
        if start is None:
            start = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        if end is None or end <= start:
            end = start + dt.timedelta(hours=1)
        return start, end

    def _parse_datetime(self, value: str) -> dt.datetime | None:
        clean = value.strip()
        if not clean:
            return None
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        try:
            parsed = dt.datetime.fromisoformat(clean)
        except ValueError as exc:
            raise WorkflowExecutionError(
                "Google Calendar event times must be ISO datetimes, for example "
                "2026-06-25T15:00:00+05:30."
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed

    def _google_service_account_access_token(
        self,
        service_account_json: str,
        scope: str,
    ) -> str:
        try:
            credentials = json.loads(service_account_json)
        except json.JSONDecodeError as exc:
            raise WorkflowExecutionError(
                "The saved Google Calendar service account JSON is not valid JSON."
            ) from exc
        client_email = credentials.get("client_email")
        private_key = credentials.get("private_key")
        token_uri = credentials.get("token_uri") or "https://oauth2.googleapis.com/token"
        if not client_email or not private_key:
            raise WorkflowExecutionError(
                "The Google Calendar service account JSON must include client_email and private_key."
            )
        now = int(time.time())
        assertion = self._sign_google_jwt(
            {
                "alg": "RS256",
                "typ": "JWT",
            },
            {
                "iss": client_email,
                "scope": scope,
                "aud": token_uri,
                "iat": now,
                "exp": now + 3600,
            },
            str(private_key),
        )
        request = urllib.request.Request(
            str(token_uri),
            data=urllib.parse.urlencode(
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkflowExecutionError(
                f"Google Calendar authentication failed with HTTP {exc.code}: {detail[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise WorkflowExecutionError(f"Google Calendar authentication failed: {exc}") from exc
        token = body.get("access_token")
        if not token:
            raise WorkflowExecutionError("Google Calendar did not return an access token.")
        return str(token)

    def _sign_google_jwt(
        self,
        header: dict[str, Any],
        claims: dict[str, Any],
        private_key: str,
    ) -> str:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import padding
        except ImportError as exc:
            raise WorkflowExecutionError(
                "Google Calendar event creation requires the cryptography package. "
                "Install project dependencies, then run the workflow again."
            ) from exc

        def encode(part: dict[str, Any] | bytes) -> str:
            raw = part if isinstance(part, bytes) else json.dumps(
                part,
                separators=(",", ":"),
            ).encode("utf-8")
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

        signing_input = f"{encode(header)}.{encode(claims)}".encode("ascii")
        try:
            key = serialization.load_pem_private_key(
                private_key.encode("utf-8"),
                password=None,
            )
            signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        except (TypeError, ValueError) as exc:
            raise WorkflowExecutionError(
                "The Google Calendar service account private_key could not be loaded."
            ) from exc
        return f"{signing_input.decode('ascii')}.{encode(signature)}"

    def _post_json(
        self, url: str, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkflowExecutionError(f"Integration request failed with HTTP {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise WorkflowExecutionError(f"Integration request failed: {exc}") from exc

    def _get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers=headers or {},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise WorkflowExecutionError(
                f"Integration request failed with HTTP {exc.code}: {detail[:300]}"
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise WorkflowExecutionError(f"Integration request failed: {exc}") from exc
