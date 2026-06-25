from __future__ import annotations

import json
import urllib.error
from typing import Any

from copilot_api.catalog import NODE_CATALOG
from copilot_api.llm import (
    LLMAuthenticationError,
    LLMRateLimitError,
    LLMResponseError,
    OpenAIProvider,
)
from copilot_api.models import Workflow, WorkflowEdge, WorkflowNode
from copilot_api.repository import WorkflowRepository


def schema_config(**values: Any) -> dict[str, Any]:
    keys = {
        key
        for definition in NODE_CATALOG.values()
        for key in (*definition.required_config, *definition.defaults.keys())
    }
    return {key: values.get(key) for key in sorted(keys)}


def structured_result() -> dict[str, Any]:
    return {
        "workflow": {
            "name": "Stripe Emails to Finance Slack",
            "status": "draft",
            "visibility": "private",
            "mode": "manual",
            "trigger_schedule": None,
            "nodes": [
                {
                    "id": "node_1",
                    "type": "gmail_trigger",
                    "role": "trigger",
                    "label": "Receive Stripe Email",
                    "description": "Find a new unread email from Stripe",
                    "config": schema_config(
                        from_contains="Stripe",
                        search_text="",
                    ),
                },
                {
                    "id": "node_2",
                    "type": "slack_message",
                    "role": "action",
                    "label": "Notify Finance in Slack",
                    "description": "Send the email to the finance channel",
                    "config": schema_config(
                        channel_id="finance",
                        message_template="New email from {{from}}: {{subject}}",
                    ),
                },
            ],
            "edges": [{"from": "node_1", "to": "node_2"}],
        },
        "explanation": "Stripe emails are forwarded to the finance Slack channel.",
    }


def completed_response(
    result: dict[str, Any] | None = None,
    *,
    response_id: str = "resp_final",
) -> dict[str, Any]:
    return {
        "id": response_id,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(result or structured_result()),
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "total_tokens": 150,
        },
    }


def test_openai_uses_responses_api_structured_outputs_and_tools():
    requests = []

    def transport(payload):
        requests.append(payload)
        return completed_response(), {"x-request-id": "request_123"}

    provider = OpenAIProvider(
        api_key="test-key",
        transport=transport,
    )

    result = provider.generate(
        "create",
        {
            "instruction": (
                "When I receive an email from Stripe, "
                "send a Slack message to the finance team."
            ),
            "context": {},
        },
    )

    request = requests[0]
    assert provider.API_URL.endswith("/v1/responses")
    assert request["model"] == "gpt-5.5"
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["reasoning"]["effort"] == "low"
    assert request["store"] is False
    assert {tool["name"] for tool in request["tools"]} == {
        "search_nodes",
        "validate_workflow",
        "get_workflow",
    }
    assert all(tool["strict"] is True for tool in request["tools"])
    assert "Never invent extra actions" in request["instructions"]
    assert result["workflow"]["nodes"][0]["config"] == {
        "from_contains": "Stripe",
        "search_text": "",
    }
    assert result["provider"] == "openai"
    assert provider.last_request_id == "request_123"
    assert provider.last_response_id == "resp_final"
    assert provider.last_usage["total_tokens"] == 150


def test_openai_executes_tool_calls_and_continues_response():
    requests = []

    def transport(payload):
        requests.append(payload)
        if len(requests) == 1:
            return (
                {
                    "id": "resp_tool",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "search_nodes",
                            "arguments": json.dumps(
                                {
                                    "query": "email trigger slack message",
                                    "roles": [],
                                    "limit": 5,
                                }
                            ),
                        }
                    ],
                },
                {},
            )
        return completed_response(), {}

    provider = OpenAIProvider(
        api_key="test-key",
        transport=transport,
    )

    result = provider.generate(
        "create",
        {"instruction": "Email from Stripe to finance Slack.", "context": {}},
    )

    assert result["workflow"]["name"] == "Stripe Emails to Finance Slack"
    assert len(requests) == 2
    assert requests[1]["previous_response_id"] == "resp_tool"
    tool_output = requests[1]["input"][0]
    assert tool_output["type"] == "function_call_output"
    assert tool_output["call_id"] == "call_1"
    returned_catalog = json.loads(tool_output["output"])["nodes"]
    assert {"gmail_trigger", "slack_message"} <= {
        item["type"] for item in returned_catalog
    }
    assert provider.last_tool_calls == ["search_nodes"]


def test_openai_validation_tool_returns_structured_feedback():
    provider = OpenAIProvider(
        api_key="test-key",
        transport=lambda _: (completed_response(), {}),
    )
    candidate = structured_result()["workflow"]
    candidate["nodes"][1]["config"]["channel_id"] = None

    feedback = provider._execute_tool(
        "validate_workflow",
        {"workflow": candidate},
    )

    assert feedback["valid"] is False
    assert any(
        error["node_type"] == "slack_message"
        and error["field"] == "channel_id"
        for error in feedback["errors"]
    )


def test_openai_get_workflow_is_owner_scoped_and_sanitized(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "tools.sqlite3"))
    workflow = repository.save(
        Workflow(
            owner_id="owner_1",
            nodes=[
                WorkflowNode(
                    id="email",
                    type="gmail_trigger",
                    role="trigger",
                    config={"from_contains": "Stripe", "search_text": ""},
                ),
                WorkflowNode(
                    id="slack",
                    type="slack_message",
                    config={
                        "channel_id": "finance",
                        "message_template": "New email",
                        "webhook_url": "https://secret.example/webhook",
                    },
                ),
            ],
            edges=[WorkflowEdge(from_="email", to="slack")],
        )
    )
    provider = OpenAIProvider(
        api_key="test-key",
        repository=repository,
        transport=lambda _: (completed_response(), {}),
    )

    allowed = provider._execute_tool(
        "get_workflow",
        {"workflow_id": workflow.id},
        tool_context={"user_id": "owner_1"},
    )
    denied = provider._execute_tool(
        "get_workflow",
        {"workflow_id": workflow.id},
        tool_context={"user_id": "stranger"},
    )

    assert allowed["found"] is True
    slack_config = allowed["workflow"]["nodes"][1]["config"]
    assert slack_config["channel_id"] == "finance"
    assert "webhook_url" not in slack_config
    assert denied == {"found": False}


def test_openai_can_choose_and_chain_multiple_tools(tmp_path):
    repository = WorkflowRepository(str(tmp_path / "chain.sqlite3"))
    workflow = repository.save(
        Workflow(
            owner_id="owner_1",
            nodes=[
                WorkflowNode(
                    id="email",
                    type="gmail_trigger",
                    role="trigger",
                    config={"from_contains": "Stripe", "search_text": ""},
                ),
                WorkflowNode(
                    id="slack",
                    type="slack_message",
                    config={
                        "channel_id": "finance",
                        "message_template": "New email",
                    },
                ),
            ],
            edges=[WorkflowEdge(from_="email", to="slack")],
        )
    )
    requests = []

    def transport(payload):
        requests.append(payload)
        if len(requests) == 1:
            return (
                {
                    "id": "resp_tools",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "search_1",
                            "name": "search_nodes",
                            "arguments": json.dumps(
                                {
                                    "query": "Microsoft Teams notification",
                                    "roles": ["action"],
                                    "limit": 3,
                                }
                            ),
                        },
                        {
                            "type": "function_call",
                            "call_id": "get_1",
                            "name": "get_workflow",
                            "arguments": json.dumps(
                                {"workflow_id": workflow.id}
                            ),
                        },
                        {
                            "type": "function_call",
                            "call_id": "validate_1",
                            "name": "validate_workflow",
                            "arguments": json.dumps(
                                {"workflow": structured_result()["workflow"]}
                            ),
                        },
                    ],
                },
                {},
            )
        return completed_response(), {}

    provider = OpenAIProvider(
        api_key="test-key",
        repository=repository,
        transport=transport,
    )
    result = provider.generate(
        "modify",
        {
            "instruction": "Send notifications to Teams instead.",
            "workflow": workflow.model_dump(mode="json", by_alias=True),
            "context": {},
            "_tool_context": {"user_id": "owner_1"},
        },
    )

    assert result["provider"] == "openai"
    assert provider.last_tool_calls == [
        "search_nodes",
        "get_workflow",
        "validate_workflow",
    ]
    assert len(requests[1]["input"]) == 3
    outputs = {
        item["call_id"]: json.loads(item["output"])
        for item in requests[1]["input"]
    }
    assert outputs["get_1"]["found"] is True
    assert outputs["validate_1"]["valid"] is True
    assert outputs["search_1"]["nodes"][0]["type"] == "teams_message"
    assert "_tool_context" not in requests[0]["input"]


def test_openai_retries_transient_rate_limits():
    calls = 0
    sleeps = []

    def transport(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                OpenAIProvider.API_URL,
                429,
                "rate limited",
                {"retry-after": "0"},
                None,
            )
        return completed_response(), {}

    provider = OpenAIProvider(
        api_key="test-key",
        transport=transport,
        sleeper=sleeps.append,
    )

    result = provider.generate(
        "create",
        {"instruction": "Email from Stripe to Slack.", "context": {}},
    )

    assert result["provider"] == "openai"
    assert calls == 2
    assert sleeps == [0.0]


def test_openai_raises_typed_errors_for_auth_rate_limit_and_bad_output():
    auth_provider = OpenAIProvider(
        api_key="bad-key",
        max_retries=0,
        transport=lambda _: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                OpenAIProvider.API_URL,
                401,
                "unauthorized",
                {},
                None,
            )
        ),
    )
    try:
        auth_provider.generate("create", {"instruction": "Create.", "context": {}})
    except LLMAuthenticationError:
        pass
    else:
        raise AssertionError("Authentication failure should be typed.")

    limited_provider = OpenAIProvider(
        api_key="test-key",
        max_retries=0,
        transport=lambda _: (_ for _ in ()).throw(
            urllib.error.HTTPError(
                OpenAIProvider.API_URL,
                429,
                "rate limited",
                {},
                None,
            )
        ),
    )
    try:
        limited_provider.generate("create", {"instruction": "Create.", "context": {}})
    except LLMRateLimitError:
        pass
    else:
        raise AssertionError("Rate-limit failure should be typed.")

    bad_output_provider = OpenAIProvider(
        api_key="test-key",
        transport=lambda _: (
            {
                "id": "resp_bad",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": '{"unexpected":true}'}
                        ],
                    }
                ],
            },
            {},
        ),
    )
    try:
        bad_output_provider.generate(
            "create",
            {"instruction": "Create.", "context": {}},
        )
    except LLMResponseError:
        pass
    else:
        raise AssertionError("Malformed structured output should be rejected.")
