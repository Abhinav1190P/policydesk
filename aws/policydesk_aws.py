import argparse
import io
import json
import os
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
EMBED_MODEL = "amazon.titan-embed-text-v2:0"
DIMENSIONS = 1024

CHUNKING = {
    "fixed": {"chunkingStrategy": "FIXED_SIZE", "fixedSizeChunkingConfiguration": {"maxTokens": 300, "overlapPercentage": 20}},
    "hierarchical": {
        "chunkingStrategy": "HIERARCHICAL",
        "hierarchicalChunkingConfiguration": {"levelConfigurations": [{"maxTokens": 1500}, {"maxTokens": 300}], "overlapTokens": 60},
    },
    "semantic": {"chunkingStrategy": "SEMANTIC", "semanticChunkingConfiguration": {"maxTokens": 300, "bufferSize": 1, "breakpointPercentileThreshold": 95}},
}

AGENT_INSTRUCTION = (
    "You are PolicyDesk Assistant, helping employees with their own company's HR and IT policies. "
    "Use the knowledge base for policy questions and answer only from what it returns. "
    "Use get_leave_balance when the employee asks about their own remaining leave; the employee is already identified, never ask for an id. "
    "Use create_it_ticket when the employee reports an IT problem or asks for access, choosing category and priority from their words. "
    "If nothing relevant is found, say you could not find it in the policies."
)


class Names:
    def __init__(self, prefix: str, account: str, region: str):
        self.prefix = prefix
        self.account = account
        self.region = region
        self.docs_bucket = f"{prefix}-docs-{account}-{region}"
        self.vector_bucket = f"{prefix}-vectors-{account}"
        self.vector_index = "policies"
        self.kb_role = f"{prefix}-kb-role"
        self.agent_role = f"{prefix}-agent-role"
        self.lambda_role = f"{prefix}-lambda-role"
        self.logging_role = f"{prefix}-bedrock-logging-role"
        self.lambda_name = f"{prefix}-leave-balance"
        self.kb_name = f"{prefix}-kb"
        self.agent_name = f"{prefix}-agent"
        self.guardrail_name = f"{prefix}-guardrail"
        self.log_group = f"/{prefix}/bedrock-invocations"
        self.app_user = f"{prefix}-app"
        self.budget = f"{prefix}-monthly"

    def model_arns(self, model_id: str) -> list[str]:
        base = model_id.split(".", 1)[1] if model_id.split(".")[0] in ("us", "eu", "apac", "global") else model_id
        return [
            f"arn:aws:bedrock:*::foundation-model/{base}",
            f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/{model_id}",
        ]


def trust(service: str, account: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": service},
                "Action": "sts:AssumeRole",
                "Condition": {"StringEquals": {"aws:SourceAccount": account}},
            }
        ],
    }


def kb_role_policy(n: Names) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": f"arn:aws:bedrock:{n.region}::foundation-model/{EMBED_MODEL}"},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:ListBucket"], "Resource": [f"arn:aws:s3:::{n.docs_bucket}", f"arn:aws:s3:::{n.docs_bucket}/*"]},
            {
                "Effect": "Allow",
                "Action": ["s3vectors:PutVectors", "s3vectors:GetVectors", "s3vectors:DeleteVectors", "s3vectors:QueryVectors", "s3vectors:GetIndex"],
                "Resource": f"arn:aws:s3vectors:{n.region}:{n.account}:bucket/{n.vector_bucket}/index/{n.vector_index}",
            },
        ],
    }


def agent_role_policy(n: Names, model_id: str, kb_id: str, guardrail_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:GetInferenceProfile"], "Resource": n.model_arns(model_id)},
            {"Effect": "Allow", "Action": "bedrock:Retrieve", "Resource": f"arn:aws:bedrock:{n.region}:{n.account}:knowledge-base/{kb_id}"},
            {"Effect": "Allow", "Action": ["bedrock:ApplyGuardrail", "bedrock:GetGuardrail"], "Resource": guardrail_arn},
        ],
    }


def app_policy(n: Names, model_id: str, kb_id: str, agent_id: str, alias_id: str, guardrail_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Sid": "Models", "Effect": "Allow", "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"], "Resource": n.model_arns(model_id) + [f"arn:aws:bedrock:{n.region}::foundation-model/{EMBED_MODEL}"]},
            {"Sid": "Guardrail", "Effect": "Allow", "Action": "bedrock:ApplyGuardrail", "Resource": guardrail_arn},
            {"Sid": "KnowledgeBase", "Effect": "Allow", "Action": ["bedrock:Retrieve", "bedrock:StartIngestionJob", "bedrock:GetIngestionJob"], "Resource": f"arn:aws:bedrock:{n.region}:{n.account}:knowledge-base/{kb_id}"},
            {"Sid": "Agent", "Effect": "Allow", "Action": "bedrock:InvokeAgent", "Resource": f"arn:aws:bedrock:{n.region}:{n.account}:agent-alias/{agent_id}/{alias_id}"},
            {"Sid": "Docs", "Effect": "Allow", "Action": ["s3:PutObject", "s3:DeleteObject"], "Resource": f"arn:aws:s3:::{n.docs_bucket}/tenants/*"},
        ],
    }


def logging_role_policy(n: Names) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": f"arn:aws:logs:{n.region}:{n.account}:log-group:{n.log_group}:*",
            }
        ],
    }


def vector_index_params(n: Names) -> dict:
    return {
        "vectorBucketName": n.vector_bucket,
        "indexName": n.vector_index,
        "dataType": "float32",
        "dimension": DIMENSIONS,
        "distanceMetric": "cosine",
        "metadataConfiguration": {"nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]},
    }


def knowledge_base_params(n: Names, role_arn: str, vector_bucket_arn: str, index_arn: str) -> dict:
    return {
        "name": n.kb_name,
        "description": "PolicyDesk tenant policy documents",
        "roleArn": role_arn,
        "knowledgeBaseConfiguration": {
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{n.region}::foundation-model/{EMBED_MODEL}",
                "embeddingModelConfiguration": {"bedrockEmbeddingModelConfiguration": {"dimensions": DIMENSIONS, "embeddingDataType": "FLOAT32"}},
            },
        },
        "storageConfiguration": {"type": "S3_VECTORS", "s3VectorsConfiguration": {"vectorBucketArn": vector_bucket_arn, "indexArn": index_arn}},
    }


def data_source_params(n: Names, kb_id: str, chunking: str) -> dict:
    return {
        "knowledgeBaseId": kb_id,
        "name": f"{n.prefix}-docs-{chunking}",
        "dataSourceConfiguration": {"type": "S3", "s3Configuration": {"bucketArn": f"arn:aws:s3:::{n.docs_bucket}", "inclusionPrefixes": ["tenants/"]}},
        "vectorIngestionConfiguration": {"chunkingConfiguration": CHUNKING[chunking]},
        "dataDeletionPolicy": "DELETE",
    }


def guardrail_params(n: Names) -> dict:
    filters = [{"type": t, "inputStrength": "HIGH", "outputStrength": "HIGH"} for t in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")]
    filters.append({"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"})
    return {
        "name": n.guardrail_name,
        "description": "PolicyDesk content, PII and grounding guardrail",
        "blockedInputMessaging": "I can't help with that request. Please ask about your company's HR or IT policies.",
        "blockedOutputsMessaging": "I couldn't produce a safe, grounded answer from your company's policies. Please contact HR or IT.",
        "contentPolicyConfig": {"filtersConfig": filters},
        "topicPolicyConfig": {
            "topicsConfig": [
                {
                    "name": "LegalOrMedicalAdvice",
                    "definition": "Requests for personal legal, tax or medical advice beyond what company policy states.",
                    "examples": ["Should I sue my manager?", "What medicine should I take for my back pain?"],
                    "type": "DENY",
                }
            ]
        },
        "sensitiveInformationPolicyConfig": {
            "piiEntitiesConfig": [
                {"type": "CREDIT_DEBIT_CARD_NUMBER", "action": "BLOCK"},
                {"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"},
                {"type": "PASSWORD", "action": "BLOCK"},
                {"type": "PHONE", "action": "ANONYMIZE"},
            ],
            "regexesConfig": [{"name": "aadhaar", "description": "Indian Aadhaar number", "pattern": "\\b\\d{4}\\s?\\d{4}\\s?\\d{4}\\b", "action": "ANONYMIZE"}],
        },
        "contextualGroundingPolicyConfig": {"filtersConfig": [{"type": "GROUNDING", "threshold": 0.75}, {"type": "RELEVANCE", "threshold": 0.5}]},
    }


def lambda_zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "lambda" / "leave_balance" / "handler.py", "handler.py")
    return buf.getvalue()


def lambda_params(n: Names, role_arn: str, app_url: str, secret: str) -> dict:
    return {
        "FunctionName": n.lambda_name,
        "Runtime": "python3.12",
        "Role": role_arn,
        "Handler": "handler.lambda_handler",
        "Code": {"ZipFile": lambda_zip()},
        "Timeout": 15,
        "MemorySize": 128,
        "Environment": {"Variables": {"POLICYDESK_URL": app_url, "INTERNAL_TOOLS_SECRET": secret}},
        "Description": "PolicyDesk agent action group: leave balance lookup",
    }


def agent_params(n: Names, role_arn: str, model_id: str, guardrail_id: str, guardrail_version: str) -> dict:
    return {
        "agentName": n.agent_name,
        "agentResourceRoleArn": role_arn,
        "foundationModel": model_id,
        "instruction": AGENT_INSTRUCTION,
        "idleSessionTTLInSeconds": 900,
        "description": "PolicyDesk employee assistant",
        "guardrailConfiguration": {"guardrailIdentifier": guardrail_id, "guardrailVersion": guardrail_version},
    }


def leave_action_group(agent_id: str, lambda_arn: str) -> dict:
    return {
        "agentId": agent_id,
        "agentVersion": "DRAFT",
        "actionGroupName": "employee_tools",
        "description": "Look up the signed-in employee's leave balance",
        "actionGroupExecutor": {"lambda": lambda_arn},
        "functionSchema": {
            "functions": [
                {
                    "name": "get_leave_balance",
                    "description": "Get the signed-in employee's remaining annual and sick leave for this year.",
                    "parameters": {"leave_type": {"type": "string", "description": "annual, sick or all", "required": False}},
                }
            ]
        },
    }


def ticket_action_group(agent_id: str) -> dict:
    return {
        "agentId": agent_id,
        "agentVersion": "DRAFT",
        "actionGroupName": "it_actions",
        "description": "Raise IT tickets; the application asks the employee to confirm first",
        "actionGroupExecutor": {"customControl": "RETURN_CONTROL"},
        "functionSchema": {
            "functions": [
                {
                    "name": "create_it_ticket",
                    "description": "Raise an IT support ticket for the signed-in employee.",
                    "parameters": {
                        "category": {"type": "string", "description": "hardware, software, access, network or other", "required": True},
                        "summary": {"type": "string", "description": "One or two sentences describing the problem", "required": True},
                        "priority": {"type": "string", "description": "low, medium or high", "required": True},
                    },
                    "requireConfirmation": "DISABLED",
                }
            ]
        },
    }


def logging_config(n: Names, role_arn: str) -> dict:
    return {
        "loggingConfig": {
            "cloudWatchConfig": {"logGroupName": n.log_group, "roleArn": role_arn},
            "textDataDeliveryEnabled": True,
            "imageDataDeliveryEnabled": False,
            "embeddingDataDeliveryEnabled": False,
        }
    }


def budget_params(n: Names, amount: str, email: str) -> dict:
    return {
        "AccountId": n.account,
        "Budget": {
            "BudgetName": n.budget,
            "BudgetLimit": {"Amount": amount, "Unit": "USD"},
            "TimeUnit": "MONTHLY",
            "BudgetType": "COST",
            "CostTypes": {"IncludeCredit": False, "IncludeRefund": False, "IncludeDiscount": False},
        },
        "NotificationsWithSubscribers": [
            {
                "Notification": {"NotificationType": nt, "ComparisonOperator": "GREATER_THAN", "Threshold": th, "ThresholdType": "PERCENTAGE"},
                "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
            }
            for nt, th in (("ACTUAL", 50.0), ("ACTUAL", 90.0), ("FORECASTED", 100.0))
        ],
    }


DENY_BEDROCK = {"Version": "2012-10-17", "Statement": [{"Effect": "Deny", "Action": "bedrock:*", "Resource": "*"}]}


def budget_action_role_policy(n: Names, deny_arn: str) -> dict:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["iam:AttachUserPolicy", "iam:DetachUserPolicy"],
                "Resource": f"arn:aws:iam::{n.account}:user/{n.app_user}",
                "Condition": {"ArnEquals": {"iam:PolicyARN": deny_arn}},
            }
        ],
    }


def budget_action_params(n: Names, deny_arn: str, role_arn: str, email: str) -> dict:
    return {
        "AccountId": n.account,
        "BudgetName": n.budget,
        "NotificationType": "ACTUAL",
        "ActionType": "APPLY_IAM_POLICY",
        "ActionThreshold": {"ActionThresholdValue": 100.0, "ActionThresholdType": "PERCENTAGE"},
        "Definition": {"IamActionDefinition": {"PolicyArn": deny_arn, "Users": [n.app_user]}},
        "ExecutionRoleArn": role_arn,
        "ApprovalModel": "AUTOMATIC",
        "Subscribers": [{"SubscriptionType": "EMAIL", "Address": email}],
    }


def load_state() -> dict:
    return json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def log(msg: str) -> None:
    print(f"[policydesk-aws] {msg}", flush=True)


def wait(fn, ok, bad=(), timeout=600, every=5, what="resource"):
    start = time.time()
    while time.time() - start < timeout:
        status = fn()
        if status in ok:
            return status
        if status in bad:
            raise RuntimeError(f"{what} ended in {status}")
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}")


class Setup:
    def __init__(self, args):
        import boto3

        self.args = args
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region) if args.profile else boto3.Session(region_name=args.region)
        self.region = args.region
        account = self.session.client("sts").get_caller_identity()["Account"]
        self.n = Names(args.prefix, account, args.region)
        self.state = load_state()
        self.state.update({"region": args.region, "account": account, "prefix": args.prefix})

    def c(self, service):
        return self.session.client(service)

    def role(self, name: str, service: str, policy: dict, managed: list[str] = ()) -> str:
        iam = self.c("iam")
        try:
            arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        except iam.exceptions.NoSuchEntityException:
            arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust(service, self.n.account)), Tags=[{"Key": "app", "Value": "policydesk"}])["Role"]["Arn"]
            log(f"created role {name}")
            time.sleep(10)
        iam.put_role_policy(RoleName=name, PolicyName=f"{name}-inline", PolicyDocument=json.dumps(policy))
        for m in managed:
            iam.attach_role_policy(RoleName=name, PolicyArn=m)
        return arn

    def buckets(self):
        s3 = self.c("s3")
        n = self.n
        try:
            s3.head_bucket(Bucket=n.docs_bucket)
        except Exception:
            kwargs = {"Bucket": n.docs_bucket}
            if self.region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            s3.create_bucket(**kwargs)
            log(f"created bucket {n.docs_bucket}")
        s3.put_public_access_block(Bucket=n.docs_bucket, PublicAccessBlockConfiguration={"BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_encryption(Bucket=n.docs_bucket, ServerSideEncryptionConfiguration={"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        sv = self.c("s3vectors")
        try:
            vb = sv.get_vector_bucket(vectorBucketName=n.vector_bucket)["vectorBucket"]
        except sv.exceptions.NotFoundException:
            sv.create_vector_bucket(vectorBucketName=n.vector_bucket)
            vb = sv.get_vector_bucket(vectorBucketName=n.vector_bucket)["vectorBucket"]
            log(f"created vector bucket {n.vector_bucket}")
        try:
            idx = sv.get_index(vectorBucketName=n.vector_bucket, indexName=n.vector_index)["index"]
        except sv.exceptions.NotFoundException:
            sv.create_index(**vector_index_params(n))
            idx = sv.get_index(vectorBucketName=n.vector_bucket, indexName=n.vector_index)["index"]
            log("created vector index")
        self.state.update({"docs_bucket": n.docs_bucket, "vector_bucket_arn": vb["vectorBucketArn"], "index_arn": idx["indexArn"]})

    def knowledge_base(self):
        ba = self.c("bedrock-agent")
        role_arn = self.role(self.n.kb_role, "bedrock.amazonaws.com", kb_role_policy(self.n))
        if not self.state.get("kb_id"):
            kb = ba.create_knowledge_base(**knowledge_base_params(self.n, role_arn, self.state["vector_bucket_arn"], self.state["index_arn"]))["knowledgeBase"]
            self.state["kb_id"] = kb["knowledgeBaseId"]
            save_state(self.state)
            log(f"created knowledge base {kb['knowledgeBaseId']}")
        wait(lambda: ba.get_knowledge_base(knowledgeBaseId=self.state["kb_id"])["knowledgeBase"]["status"], ok=("ACTIVE",), bad=("FAILED",), what="knowledge base")
        if not self.state.get("data_source_id"):
            self.data_source(self.args.chunking)

    def data_source(self, chunking: str):
        ba = self.c("bedrock-agent")
        ds = ba.create_data_source(**data_source_params(self.n, self.state["kb_id"], chunking))["dataSource"]
        self.state.update({"data_source_id": ds["dataSourceId"], "chunking": chunking})
        save_state(self.state)
        log(f"created data source {ds['dataSourceId']} with {chunking} chunking")

    def guardrail(self):
        b = self.c("bedrock")
        if not self.state.get("guardrail_id"):
            g = b.create_guardrail(**guardrail_params(self.n))
            self.state.update({"guardrail_id": g["guardrailId"], "guardrail_arn": g["guardrailArn"]})
            v = b.create_guardrail_version(guardrailIdentifier=g["guardrailId"], description="v1")
            self.state["guardrail_version"] = v["version"]
            save_state(self.state)
            log(f"created guardrail {g['guardrailId']} version {v['version']}")

    def lambda_fn(self):
        lam = self.c("lambda")
        role_arn = self.role(self.n.lambda_role, "lambda.amazonaws.com", {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "logs:CreateLogGroup", "Resource": "*"}]}, ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
        params = lambda_params(self.n, role_arn, self.args.app_url, self.args.internal_secret)
        try:
            arn = lam.get_function(FunctionName=self.n.lambda_name)["Configuration"]["FunctionArn"]
            lam.update_function_code(FunctionName=self.n.lambda_name, ZipFile=params["Code"]["ZipFile"])
            lam.get_waiter("function_updated_v2").wait(FunctionName=self.n.lambda_name)
            lam.update_function_configuration(FunctionName=self.n.lambda_name, Environment=params["Environment"])
        except lam.exceptions.ResourceNotFoundException:
            for attempt in range(6):
                try:
                    arn = lam.create_function(**params)["FunctionArn"]
                    break
                except lam.exceptions.InvalidParameterValueException:
                    time.sleep(5)
            log(f"created lambda {self.n.lambda_name}")
        lam.get_waiter("function_active_v2").wait(FunctionName=self.n.lambda_name)
        self.state["lambda_arn"] = arn

    def agent(self):
        ba = self.c("bedrock-agent")
        role_arn = self.role(self.n.agent_role, "bedrock.amazonaws.com", agent_role_policy(self.n, self.args.model, self.state["kb_id"], self.state["guardrail_arn"]))
        if not self.state.get("agent_id"):
            for attempt in range(6):
                try:
                    a = ba.create_agent(**agent_params(self.n, role_arn, self.args.model, self.state["guardrail_id"], self.state["guardrail_version"]))["agent"]
                    break
                except ba.exceptions.ValidationException:
                    if attempt == 5:
                        raise
                    time.sleep(10)
            self.state["agent_id"] = a["agentId"]
            save_state(self.state)
            log(f"created agent {a['agentId']}")
        agent_id = self.state["agent_id"]
        wait(lambda: ba.get_agent(agentId=agent_id)["agent"]["agentStatus"], ok=("NOT_PREPARED", "PREPARED"), bad=("FAILED",), what="agent")
        if not self.state.get("lambda_permission"):
            self.c("lambda").add_permission(
                FunctionName=self.n.lambda_name,
                StatementId=f"bedrock-agent-{agent_id}",
                Action="lambda:InvokeFunction",
                Principal="bedrock.amazonaws.com",
                SourceArn=f"arn:aws:bedrock:{self.region}:{self.n.account}:agent/{agent_id}",
            )
            self.state["lambda_permission"] = True
        if not self.state.get("action_groups"):
            ba.create_agent_action_group(**leave_action_group(agent_id, self.state["lambda_arn"]))
            ba.create_agent_action_group(**ticket_action_group(agent_id))
            ba.associate_agent_knowledge_base(agentId=agent_id, agentVersion="DRAFT", knowledgeBaseId=self.state["kb_id"], description="Company HR and IT policies, filtered per tenant at query time", knowledgeBaseState="ENABLED")
            self.state["action_groups"] = True
            save_state(self.state)
        ba.prepare_agent(agentId=agent_id)
        wait(lambda: ba.get_agent(agentId=agent_id)["agent"]["agentStatus"], ok=("PREPARED",), bad=("FAILED",), what="agent prepare")
        if not self.state.get("agent_alias_id"):
            alias = ba.create_agent_alias(agentId=agent_id, agentAliasName="live")["agentAlias"]
            self.state["agent_alias_id"] = alias["agentAliasId"]
            save_state(self.state)
        wait(lambda: ba.get_agent_alias(agentId=agent_id, agentAliasId=self.state["agent_alias_id"])["agentAlias"]["agentAliasStatus"], ok=("PREPARED",), bad=("FAILED",), what="agent alias")
        log(f"agent ready: {agent_id} alias {self.state['agent_alias_id']}")

    def logging(self):
        logs = self.c("logs")
        try:
            logs.create_log_group(logGroupName=self.n.log_group)
        except logs.exceptions.ResourceAlreadyExistsException:
            pass
        logs.put_retention_policy(logGroupName=self.n.log_group, retentionInDays=7)
        role_arn = self.role(self.n.logging_role, "bedrock.amazonaws.com", logging_role_policy(self.n))
        self.c("bedrock").put_model_invocation_logging_configuration(**logging_config(self.n, role_arn))
        log(f"model invocation logging -> {self.n.log_group} (7 day retention)")

    def budget(self):
        if not self.args.budget_email:
            log("no --budget-email given; skipping budget alert")
            return
        bud = self.session.client("budgets", region_name="us-east-1")
        try:
            bud.create_budget(**budget_params(self.n, self.args.budget_usd, self.args.budget_email))
            log(f"budget alert at ${self.args.budget_usd}/month -> {self.args.budget_email}")
        except bud.exceptions.DuplicateRecordException:
            log("budget already exists")

    def budget_stop(self):
        if not self.args.budget_email:
            log("no --budget-email given; skipping automatic Bedrock stop")
            return
        iam = self.c("iam")
        name = f"{self.n.prefix}-deny-bedrock"
        deny_arn = f"arn:aws:iam::{self.n.account}:policy/{name}"
        try:
            iam.create_policy(PolicyName=name, PolicyDocument=json.dumps(DENY_BEDROCK), Description="Attached automatically by AWS Budgets when PolicyDesk spend passes the limit")
        except iam.exceptions.EntityAlreadyExistsException:
            pass
        budgets_trust = {
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "budgets.amazonaws.com"}, "Action": "sts:AssumeRole", "Condition": {"StringEquals": {"aws:SourceAccount": self.n.account}}}],
        }
        role_name = f"{self.n.prefix}-budget-action-role"
        try:
            role_arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        except iam.exceptions.NoSuchEntityException:
            role_arn = iam.create_role(RoleName=role_name, AssumeRolePolicyDocument=json.dumps(budgets_trust))["Role"]["Arn"]
            time.sleep(10)
        iam.put_role_policy(RoleName=role_name, PolicyName=f"{role_name}-inline", PolicyDocument=json.dumps(budget_action_role_policy(self.n, deny_arn)))
        bud = self.session.client("budgets", region_name="us-east-1")
        existing = bud.describe_budget_actions_for_budget(AccountId=self.n.account, BudgetName=self.n.budget).get("Actions", [])
        if existing:
            log("automatic Bedrock stop already configured")
            return
        for attempt in range(6):
            try:
                bud.create_budget_action(**budget_action_params(self.n, deny_arn, role_arn, self.args.budget_email))
                break
            except Exception as e:
                if attempt == 5:
                    raise
                log(f"waiting for IAM to propagate ({type(e).__name__})")
                time.sleep(10)
        log(f"when spend passes ${self.args.budget_usd} (credits excluded), AWS Budgets attaches {name} to {self.n.app_user} and Bedrock stops")

    def app_identity(self):
        iam = self.c("iam")
        policy = app_policy(self.n, self.args.model, self.state["kb_id"], self.state["agent_id"], self.state["agent_alias_id"], self.state["guardrail_arn"])
        (HERE / "app-policy.json").write_text(json.dumps(policy, indent=2))
        try:
            iam.get_user(UserName=self.n.app_user)
        except iam.exceptions.NoSuchEntityException:
            iam.create_user(UserName=self.n.app_user, Tags=[{"Key": "app", "Value": "policydesk"}])
            log(f"created IAM user {self.n.app_user}; create its access key yourself in the IAM console")
        iam.put_user_policy(UserName=self.n.app_user, PolicyName="policydesk-app", PolicyDocument=json.dumps(policy))

    def env_snippet(self) -> str:
        s = self.state
        return "\n".join(
            [
                "AI_BACKEND=bedrock",
                "EMBED_BACKEND=bedrock",
                "RETRIEVER=bedrock_kb",
                "AGENT_MODE=bedrock_agent",
                "FALLBACK_BACKEND=groq",
                f"AWS_REGION={s['region']}",
                f"BEDROCK_MODEL_ID={self.args.model}",
                f"BEDROCK_KB_ID={s.get('kb_id', '')}",
                f"BEDROCK_KB_DATA_SOURCE_ID={s.get('data_source_id', '')}",
                f"BEDROCK_GUARDRAIL_ID={s.get('guardrail_id', '')}",
                f"BEDROCK_GUARDRAIL_VERSION={s.get('guardrail_version', '')}",
                f"BEDROCK_AGENT_ID={s.get('agent_id', '')}",
                f"BEDROCK_AGENT_ALIAS_ID={s.get('agent_alias_id', '')}",
                f"S3_BUCKET={s.get('docs_bucket', '')}",
                "MIN_RELEVANCE=0.3",
            ]
        )

    def run(self):
        steps = [self.buckets, self.knowledge_base, self.guardrail, self.lambda_fn, self.agent, self.logging, self.budget, self.app_identity, self.budget_stop]
        only = set(self.args.only.split(",")) if self.args.only else None
        for step in steps:
            if only and step.__name__ not in only:
                continue
            log(f"step {step.__name__}")
            step()
            save_state(self.state)
        print("\nAdd these to backend/.env (no secrets here):\n")
        print(self.env_snippet())


class Teardown(Setup):
    def run(self):
        s = self.state
        ba = self.c("bedrock-agent")

        def attempt(label, fn):
            try:
                fn()
                log(f"deleted {label}")
            except Exception as e:
                log(f"skip {label}: {type(e).__name__}: {e}")

        if s.get("agent_id"):
            if s.get("agent_alias_id"):
                attempt("agent alias", lambda: ba.delete_agent_alias(agentId=s["agent_id"], agentAliasId=s["agent_alias_id"]))
                time.sleep(5)
            attempt("agent", lambda: ba.delete_agent(agentId=s["agent_id"], skipResourceInUseCheck=True))
        attempt("lambda", lambda: self.c("lambda").delete_function(FunctionName=self.n.lambda_name))
        if s.get("kb_id"):
            if s.get("data_source_id"):
                attempt("data source", lambda: ba.delete_data_source(knowledgeBaseId=s["kb_id"], dataSourceId=s["data_source_id"]))
                time.sleep(10)
            attempt("knowledge base", lambda: ba.delete_knowledge_base(knowledgeBaseId=s["kb_id"]))
        if s.get("guardrail_id"):
            attempt("guardrail", lambda: self.c("bedrock").delete_guardrail(guardrailIdentifier=s["guardrail_id"]))
        sv = self.c("s3vectors")
        attempt("vector index", lambda: sv.delete_index(vectorBucketName=self.n.vector_bucket, indexName=self.n.vector_index))
        attempt("vector bucket", lambda: sv.delete_vector_bucket(vectorBucketName=self.n.vector_bucket))
        if not self.args.keep_docs:
            def empty_and_delete():
                s3 = self.session.resource("s3")
                bucket = s3.Bucket(self.n.docs_bucket)
                bucket.objects.all().delete()
                bucket.delete()

            attempt("docs bucket", empty_and_delete)
        attempt("invocation logging", lambda: self.c("bedrock").delete_model_invocation_logging_configuration())
        attempt("log group", lambda: self.c("logs").delete_log_group(logGroupName=self.n.log_group))
        iam = self.c("iam")
        for role in (self.n.kb_role, self.n.agent_role, self.n.lambda_role, self.n.logging_role):
            def drop(role=role):
                for p in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                    iam.delete_role_policy(RoleName=role, PolicyName=p)
                for p in iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"]:
                    iam.detach_role_policy(RoleName=role, PolicyArn=p["PolicyArn"])
                iam.delete_role(RoleName=role)

            attempt(f"role {role}", drop)
        if self.args.delete_app_user:
            def drop_user():
                for k in iam.list_access_keys(UserName=self.n.app_user)["AccessKeyMetadata"]:
                    iam.delete_access_key(UserName=self.n.app_user, AccessKeyId=k["AccessKeyId"])
                for p in iam.list_user_policies(UserName=self.n.app_user)["PolicyNames"]:
                    iam.delete_user_policy(UserName=self.n.app_user, PolicyName=p)
                iam.delete_user(UserName=self.n.app_user)

            attempt("app user", drop_user)
        keep = {k: s[k] for k in ("region", "account", "prefix") if k in s}
        save_state(keep)
        log("teardown finished; the budget alert is kept so you stay protected")


class Rechunk(Setup):
    def run(self):
        ba = self.c("bedrock-agent")
        if self.state.get("data_source_id"):
            ba.delete_data_source(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"])
            log("deleted old data source; waiting for vector cleanup")
            time.sleep(20)
        self.data_source(self.args.chunking)
        job = ba.start_ingestion_job(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"])["ingestionJob"]
        status = wait(
            lambda: ba.get_ingestion_job(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"], ingestionJobId=job["ingestionJobId"])["ingestionJob"]["status"],
            ok=("COMPLETE",),
            bad=("FAILED", "STOPPED"),
            what="ingestion",
        )
        log(f"re-ingested with {self.args.chunking} chunking: {status}")
        print(f"BEDROCK_KB_DATA_SOURCE_ID={self.state['data_source_id']}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Create or remove PolicyDesk's AWS resources")
    p.add_argument("command", choices=["setup", "teardown", "rechunk", "env"])
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--profile", default=os.environ.get("AWS_PROFILE"))
    p.add_argument("--prefix", default="policydesk")
    p.add_argument("--model", default="us.amazon.nova-lite-v1:0")
    p.add_argument("--chunking", choices=list(CHUNKING), default="hierarchical")
    p.add_argument("--app-url", default=os.environ.get("POLICYDESK_URL", "https://example.invalid"))
    p.add_argument("--internal-secret", default=os.environ.get("INTERNAL_TOOLS_SECRET", ""))
    p.add_argument("--budget-email", default=os.environ.get("BUDGET_EMAIL"))
    p.add_argument("--budget-usd", default="3")
    p.add_argument("--only", help="comma separated setup steps to run")
    p.add_argument("--keep-docs", action="store_true")
    p.add_argument("--delete-app-user", action="store_true")
    args = p.parse_args(argv)
    if args.command == "setup" and not args.internal_secret:
        sys.exit("set INTERNAL_TOOLS_SECRET (same value as the app) before running setup")
    if args.command == "setup":
        Setup(args).run()
    elif args.command == "teardown":
        Teardown(args).run()
    elif args.command == "rechunk":
        Rechunk(args).run()
    else:
        print(Setup(args).env_snippet())


if __name__ == "__main__":
    main()
