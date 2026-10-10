import io
import json
import os
import uuid

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from app.agent import bedrock_agent
from app.config import get_settings
from app.providers import bedrock
from app.providers.base import Grounding, ToolSpec
from app.rag.retriever import BedrockKBRetriever, build_kb_retrieve_request
from app.security import CurrentUser
from tests.conftest import get_pool


@pytest.fixture
def aws(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "bedrock_model_id", "us.amazon.nova-lite-v1:0")
    bedrock.reset_clients()
    made = {}

    def make(service):
        c = boto3.client(service, region_name="us-east-1")
        st = Stubber(c)
        st.activate()
        bedrock.set_client(service, c)
        made[service] = st
        return st

    yield make
    for st in made.values():
        st.deactivate()
    bedrock.reset_clients()


def converse_response(content, stop="end_turn", usage=(100, 20)):
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": stop,
        "usage": {"inputTokens": usage[0], "outputTokens": usage[1], "totalTokens": sum(usage)},
        "metrics": {"latencyMs": 300},
    }


async def test_converse_with_guardrail_wraps_grounding_in_guard_content(aws, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "bedrock_guardrail_id", "gr-123")
    monkeypatch.setattr(s, "bedrock_guardrail_version", "1")
    st = aws("bedrock-runtime")
    expected = {
        "modelId": "us.amazon.nova-lite-v1:0",
        "system": [{"text": "SYS"}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"guardContent": {"text": {"text": "[1] Leave\n24 days", "qualifiers": ["grounding_source"]}}},
                    {"guardContent": {"text": {"text": "How many days?", "qualifiers": ["query"]}}},
                    {"text": ANY},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": 700, "temperature": 0.2, "topP": 0.9},
        "guardrailConfig": {"guardrailIdentifier": "gr-123", "guardrailVersion": "1", "trace": "enabled"},
    }
    st.add_response("converse", converse_response([{"text": "24 days [1]"}]), expected)
    r = await bedrock.BedrockLLM().generate("SYS", [{"role": "user", "content": "How many days?"}], Grounding("[1] Leave\n24 days", "How many days?"))
    assert r.text == "24 days [1]" and r.usage.input_tokens == 100 and r.guardrail is None
    st.assert_no_pending_responses()


async def test_guardrail_intervention_is_reported(aws, monkeypatch):
    monkeypatch.setattr(get_settings(), "bedrock_guardrail_id", "gr-123")
    st = aws("bedrock-runtime")
    st.add_response("converse", converse_response([{"text": "Sorry, I can't help with that."}], stop="guardrail_intervened"))
    r = await bedrock.BedrockLLM().generate("SYS", [{"role": "user", "content": "q"}], Grounding("ctx", "q"))
    assert r.guardrail == "intervened"


async def test_without_guardrail_context_is_inlined(aws, monkeypatch):
    monkeypatch.setattr(get_settings(), "bedrock_guardrail_id", "")
    req = bedrock.build_converse_request("m", "SYS", [{"role": "user", "content": "q"}], Grounding("CTX", "q"))
    assert "guardrailConfig" not in req
    assert "<policy_context>\nCTX" in req["messages"][0]["content"][0]["text"]


async def test_converse_tool_use_round_trip(aws):
    st = aws("bedrock-runtime")
    tools = [ToolSpec("get_leave_balance", "Leave", {"type": "object", "properties": {"leave_type": {"type": "string"}}})]
    history = [
        {"role": "user", "content": "search then leave"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "search_policies", "args": {"query": "x"}}, {"id": "t2", "name": "get_leave_balance", "args": {}}]},
        {"role": "tool", "tool_call_id": "t1", "name": "search_policies", "content": "{\"context\": \"\"}"},
        {"role": "tool", "tool_call_id": "t2", "name": "get_leave_balance", "content": "{\"annual_remaining\": 5}"},
    ]
    expected = {
        "modelId": "us.amazon.nova-lite-v1:0",
        "system": [{"text": "SYS"}],
        "messages": [
            {"role": "user", "content": [{"text": "search then leave"}]},
            {"role": "assistant", "content": [{"toolUse": {"toolUseId": "t1", "name": "search_policies", "input": {"query": "x"}}}, {"toolUse": {"toolUseId": "t2", "name": "get_leave_balance", "input": {}}}]},
            {
                "role": "user",
                "content": [
                    {"toolResult": {"toolUseId": "t1", "content": [{"text": "{\"context\": \"\"}"}], "status": "success"}},
                    {"toolResult": {"toolUseId": "t2", "content": [{"text": "{\"annual_remaining\": 5}"}], "status": "success"}},
                ],
            },
        ],
        "inferenceConfig": ANY,
        "toolConfig": {"tools": [{"toolSpec": {"name": "get_leave_balance", "description": "Leave", "inputSchema": {"json": tools[0].parameters}}}], "toolChoice": {"auto": {}}},
    }
    st.add_response("converse", converse_response([{"text": "Checking"}, {"toolUse": {"toolUseId": "t3", "name": "get_leave_balance", "input": {"leave_type": "annual"}}}], stop="tool_use"), expected)
    r = await bedrock.BedrockLLM().chat_with_tools("SYS", history, tools)
    assert r.tool_calls[0].id == "t3" and r.tool_calls[0].args == {"leave_type": "annual"}
    assert r.stop_reason == "tool_use"


async def test_throttling_maps_to_retryable_error(aws):
    st = aws("bedrock-runtime")
    st.add_client_error("converse", service_error_code="ThrottlingException", http_status_code=429)
    with pytest.raises(bedrock.ProviderError) as e:
        await bedrock.BedrockLLM().generate("s", [{"role": "user", "content": "q"}])
    assert e.value.retryable


async def test_titan_embedding_request(aws):
    st = aws("bedrock-runtime")
    body = json.dumps({"embedding": [0.1] * 1024, "inputTextTokenCount": 4}).encode()
    st.add_response(
        "invoke_model",
        {"body": StreamingBody(io.BytesIO(body), len(body)), "contentType": "application/json"},
        {"modelId": "amazon.titan-embed-text-v2:0", "contentType": "application/json", "accept": "application/json", "body": json.dumps({"inputText": "annual leave", "dimensions": 1024, "normalize": True})},
    )
    vecs, tokens = await bedrock.TitanEmbedder().embed(["annual leave"])
    assert len(vecs[0]) == 1024 and tokens == 4


def test_kb_retrieve_request_filters():
    r = build_kb_retrieve_request("KBTEST0001", "t-1", "q", 5)
    assert r["retrievalConfiguration"]["vectorSearchConfiguration"] == {"numberOfResults": 5, "filter": {"equals": {"key": "tenant_id", "value": "t-1"}}}
    r = build_kb_retrieve_request("KBTEST0001", "t-1", "q", 5, "HYBRID", "hr")
    vc = r["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert vc["overrideSearchType"] == "HYBRID"
    assert vc["filter"] == {"andAll": [{"equals": {"key": "tenant_id", "value": "t-1"}}, {"equals": {"key": "category", "value": "hr"}}]}


async def test_kb_retriever_drops_other_tenant_chunks(aws, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "bedrock_kb_id", "KBTEST0001")
    monkeypatch.setattr(s, "min_relevance", 0.1)
    st = aws("bedrock-agent-runtime")
    st.add_response(
        "retrieve",
        {
            "retrievalResults": [
                {"content": {"text": "24 days"}, "score": 0.8, "location": {"type": "S3", "s3Location": {"uri": "s3://b/tenants/t-1/d1/leave.md"}}, "metadata": {"tenant_id": "t-1", "doc_id": "d1", "title": "Leave"}},
                {"content": {"text": "night shift 10%"}, "score": 0.7, "location": {"type": "S3", "s3Location": {"uri": "s3://b/x"}}, "metadata": {"tenant_id": "t-2", "doc_id": "d9", "title": "Shift"}},
                {"content": {"text": "weak"}, "score": 0.05, "location": {"type": "S3", "s3Location": {"uri": "s3://b/y"}}, "metadata": {"tenant_id": "t-1", "doc_id": "d2", "title": "Weak"}},
            ]
        },
        build_kb_retrieve_request("KBTEST0001", "t-1", "leave?", 5),
    )
    hits, _ = await BedrockKBRetriever().retrieve("t-1", "leave?")
    assert [h.title for h in hits] == ["Leave"] and hits[0].source == "bedrock_kb"


def user(tenant="t-1"):
    return CurrentUser("u-1", tenant, "acme", "e@acme.example", "E", "employee")


def test_invoke_agent_request_carries_identity_and_tenant_filter(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "bedrock_agent_id", "AG1")
    monkeypatch.setattr(s, "bedrock_agent_alias_id", "AL1")
    monkeypatch.setattr(s, "bedrock_kb_id", "KBTEST0001")
    req = bedrock_agent.build_invoke_request(user(), "sess-1", text="hi")
    assert req["agentId"] == "AG1" and req["inputText"] == "hi"
    assert req["sessionState"]["sessionAttributes"] == {"tenant_id": "t-1", "user_id": "u-1", "tenant_slug": "acme"}
    kb = req["sessionState"]["knowledgeBaseConfigurations"][0]
    assert kb["retrievalConfiguration"]["vectorSearchConfiguration"]["filter"] == {"equals": {"key": "tenant_id", "value": "t-1"}}
    rc = bedrock_agent.build_invoke_request(user(), "sess-1", return_control={"invocation_id": "inv-1", "action_group": "it", "function": "create_it_ticket", "result": {"ticket_id": 7}})
    assert "inputText" not in rc
    assert rc["sessionState"]["invocationId"] == "inv-1"
    fr = rc["sessionState"]["returnControlInvocationResults"][0]["functionResult"]
    assert fr["function"] == "create_it_ticket" and json.loads(fr["responseBody"]["TEXT"]["body"]) == {"ticket_id": 7}


def test_parse_agent_events():
    events = [
        {"trace": {"trace": {"orchestrationTrace": {"invocationInput": {"invocationType": "KNOWLEDGE_BASE", "knowledgeBaseLookupInput": {"text": "q"}}}}}},
        {"trace": {"trace": {"orchestrationTrace": {"modelInvocationOutput": {"metadata": {"usage": {"inputTokens": 900, "outputTokens": 40}}}}}}},
        {"chunk": {"bytes": b"You get 24 days.", "attribution": {"citations": [{"retrievedReferences": [{"content": {"text": "24 days"}, "metadata": {"title": "Leave", "doc_id": "d1"}}]}]}}},
    ]
    p = bedrock_agent.parse_events(events)
    assert p["text"] == "You get 24 days." and p["citations"][0]["title"] == "Leave"
    assert p["trace"] == [{"step": "knowledge_base", "name": "knowledge_base"}] and p["input_tokens"] == 900


class FakeAgentRuntime:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def invoke_agent(self, **req):
        self.requests.append(req)
        return {"completion": self.responses.pop(0), "sessionId": req["sessionId"]}


async def test_bedrock_agent_return_control_ticket_flow(client, fresh_employee, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "agent_mode", "bedrock_agent")
    monkeypatch.setattr(s, "bedrock_agent_id", "AG1")
    monkeypatch.setattr(s, "bedrock_agent_alias_id", "AL1")
    monkeypatch.setattr(s, "bedrock_kb_id", "")
    fake = FakeAgentRuntime(
        [
            [
                {"returnControl": {"invocationId": "inv-9", "invocationInputs": [{"functionInvocationInput": {"actionGroup": "it_actions", "function": "create_it_ticket", "parameters": [{"name": "category", "type": "string", "value": "network"}, {"name": "summary", "type": "string", "value": "VPN drops every hour"}, {"name": "priority", "type": "string", "value": "high"}]}}]}}
            ],
            [{"chunk": {"bytes": b"Your ticket is raised."}}],
        ]
    )
    bedrock.reset_clients()
    bedrock.set_client("bedrock-agent-runtime", fake)
    try:
        r = await client.post("/api/agent/message", headers=fresh_employee["headers"], json={"text": "VPN keeps dropping", "session_id": "sess-bedrock-1"})
        d = r.json()
        assert d["type"] == "confirm" and d["action"]["args"] == {"category": "network", "priority": "high", "summary": "VPN drops every hour"}
        assert fake.requests[0]["sessionState"]["sessionAttributes"]["user_id"] == fresh_employee["user"]["id"]
        r = await client.post("/api/agent/confirm", headers=fresh_employee["headers"], json={"session_id": "sess-bedrock-1", "action_id": d["action"]["id"], "approve": True})
        assert r.json()["text"] == "Your ticket is raised."
        result = json.loads(fake.requests[1]["sessionState"]["returnControlInvocationResults"][0]["functionResult"]["responseBody"]["TEXT"]["body"])
        assert result["status"] == "open" and result["category"] == "network"
        via = await get_pool().fetchval("SELECT created_via FROM tickets WHERE id = $1", result["ticket_id"])
        assert via == "bedrock_agent"
    finally:
        bedrock.reset_clients()


async def test_kb_ingestion_writes_metadata_sidecar_and_syncs(services, aws, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "retriever", "bedrock_kb")
    monkeypatch.setattr(s, "s3_bucket", "pd-bucket")
    monkeypatch.setattr(s, "bedrock_kb_id", "KBTEST0001")
    monkeypatch.setattr(s, "bedrock_kb_data_source_id", "DSTEST0001")
    from app.services import ingest

    async def no_sleep(_):
        return None

    monkeypatch.setattr(ingest.asyncio, "sleep", no_sleep)
    tenant = await get_pool().fetchval("SELECT id FROM tenants WHERE slug = 'acme'")
    doc = await ingest.create_document(str(tenant), None, "kb-test.md", "text/markdown", f"# KB Test {uuid.uuid4()}\n\nSome policy text for the knowledge base.".encode(), None, "hr", publish=False)
    s3 = aws("s3")
    agent = aws("bedrock-agent")
    key = f"tenants/{tenant}/{doc['id']}/kb-test.md"
    s3.add_response("put_object", {}, {"Bucket": "pd-bucket", "Key": key, "Body": ANY, "ContentType": "text/markdown", "ServerSideEncryption": "AES256"})
    meta = {"metadataAttributes": {"tenant_id": str(tenant), "doc_id": doc["id"], "title": doc["title"], "category": "hr"}}
    s3.add_response("put_object", {}, {"Bucket": "pd-bucket", "Key": f"{key}.metadata.json", "Body": json.dumps(meta).encode(), "ContentType": "application/json", "ServerSideEncryption": "AES256"})
    job = {"knowledgeBaseId": "KBTEST0001", "dataSourceId": "DSTEST0001", "ingestionJobId": "job-1", "status": "STARTING", "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z"}
    agent.add_response("start_ingestion_job", {"ingestionJob": job}, {"knowledgeBaseId": "KBTEST0001", "dataSourceId": "DSTEST0001"})
    agent.add_response("get_ingestion_job", {"ingestionJob": {**job, "status": "IN_PROGRESS"}}, {"knowledgeBaseId": "KBTEST0001", "dataSourceId": "DSTEST0001", "ingestionJobId": "job-1"})
    agent.add_response("get_ingestion_job", {"ingestionJob": {**job, "status": "COMPLETE"}}, {"knowledgeBaseId": "KBTEST0001", "dataSourceId": "DSTEST0001", "ingestionJobId": "job-1"})
    assert await ingest.process_document(doc["id"]) == "ready"
    s3.assert_no_pending_responses()
    agent.assert_no_pending_responses()
    row = await get_pool().fetchrow("SELECT status, s3_key FROM documents WHERE id = $1", uuid.UUID(doc["id"]))
    assert row["status"] == "ready" and row["s3_key"] == key
