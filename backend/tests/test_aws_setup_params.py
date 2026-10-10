import importlib.util
from pathlib import Path

import boto3
import pytest
from botocore.validate import ParamValidator

SPEC = importlib.util.spec_from_file_location("policydesk_aws", Path(__file__).resolve().parents[2] / "aws" / "policydesk_aws.py")
aws = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aws)

N = aws.Names("policydesk", "123456789012", "us-east-1")
ARN = "arn:aws:iam::123456789012:role/x"
KB = "KBTEST0001"
AGENT = "AGTEST0001"


def validate(service: str, operation: str, params: dict) -> None:
    client = boto3.client(service, region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    shape = client.meta.service_model.operation_model(operation).input_shape
    report = ParamValidator().validate(params, shape)
    assert not report.has_errors(), report.generate_report()


CASES = [
    ("s3vectors", "CreateIndex", aws.vector_index_params(N)),
    ("bedrock-agent", "CreateKnowledgeBase", aws.knowledge_base_params(N, ARN, "arn:aws:s3vectors:us-east-1:123456789012:bucket/b", "arn:aws:s3vectors:us-east-1:123456789012:bucket/b/index/policies")),
    ("bedrock-agent", "CreateDataSource", aws.data_source_params(N, KB, "fixed")),
    ("bedrock-agent", "CreateDataSource", aws.data_source_params(N, KB, "hierarchical")),
    ("bedrock-agent", "CreateDataSource", aws.data_source_params(N, KB, "semantic")),
    ("bedrock", "CreateGuardrail", aws.guardrail_params(N)),
    ("lambda", "CreateFunction", aws.lambda_params(N, ARN, "https://policydesk.example", "s3cr3t")),
    ("bedrock-agent", "CreateAgent", aws.agent_params(N, ARN, "us.amazon.nova-lite-v1:0", "gr123", "1")),
    ("bedrock-agent", "CreateAgentActionGroup", aws.leave_action_group(AGENT, "arn:aws:lambda:us-east-1:123456789012:function:f")),
    ("bedrock-agent", "CreateAgentActionGroup", aws.ticket_action_group(AGENT)),
    ("bedrock", "PutModelInvocationLoggingConfiguration", aws.logging_config(N, ARN)),
    ("budgets", "CreateBudget", aws.budget_params(N, "5", "me@example.com")),
]


@pytest.mark.parametrize("service,operation,params", CASES, ids=[f"{c[1]}-{i}" for i, c in enumerate(CASES)])
def test_setup_requests_match_aws_api(service, operation, params):
    validate(service, operation, params)


def test_policies_are_least_privilege():
    app = aws.app_policy(N, "us.amazon.nova-lite-v1:0", KB, AGENT, "ALIAS00001", "arn:aws:bedrock:us-east-1:123456789012:guardrail/gr123")
    for st in app["Statement"]:
        assert st["Resource"] != "*" and "*" not in st["Action"]
    docs = next(s for s in app["Statement"] if s["Sid"] == "Docs")
    assert docs["Resource"].endswith("/tenants/*")
    kb = aws.kb_role_policy(N)
    assert all(s["Resource"] != "*" for s in kb["Statement"])
    assert aws.trust("bedrock.amazonaws.com", "123456789012")["Statement"][0]["Condition"]["StringEquals"]["aws:SourceAccount"] == "123456789012"


def test_lambda_zip_contains_handler():
    import io
    import zipfile

    z = zipfile.ZipFile(io.BytesIO(aws.lambda_zip()))
    assert z.namelist() == ["handler.py"]


HSPEC = importlib.util.spec_from_file_location("hybrid_experiment", Path(__file__).resolve().parents[2] / "aws" / "hybrid_experiment.py")
hybrid = importlib.util.module_from_spec(HSPEC)
HSPEC.loader.exec_module(hybrid)

HYBRID_CASES = [
    ("opensearchserverless", "CreateSecurityPolicy", hybrid.encryption_policy("policydesk-hybrid")),
    ("opensearchserverless", "CreateSecurityPolicy", hybrid.network_policy("policydesk-hybrid")),
    ("opensearchserverless", "CreateAccessPolicy", hybrid.access_policy("policydesk-hybrid", [ARN])),
    ("opensearchserverless", "CreateCollection", hybrid.collection_params("policydesk-hybrid")),
    ("bedrock-agent", "CreateKnowledgeBase", hybrid.oss_kb_params(N, ARN, "arn:aws:aoss:us-east-1:123456789012:collection/abc")),
    ("bedrock-agent-runtime", "Retrieve", hybrid.retrieve_params(KB, "q", "t-1", "HYBRID", 5)),
]


@pytest.mark.parametrize("service,operation,params", HYBRID_CASES, ids=[f"{c[1]}-{i}" for i, c in enumerate(HYBRID_CASES)])
def test_hybrid_experiment_requests_match_aws_api(service, operation, params):
    validate(service, operation, params)
