import argparse
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from policydesk_aws import CHUNKING, EMBED_MODEL, Names, load_state, log, wait

QUESTIONS = HERE.parent / "backend" / "eval" / "questions.json"
RESULTS = HERE / "hybrid_results.md"
STATE = HERE / "hybrid_state.json"
COMPANY = {"acme": "Acme Cloud", "globex": "Globex Manufacturing"}


def collection_name(prefix: str) -> str:
    return f"{prefix}-hybrid"[:32]


def encryption_policy(name: str) -> dict:
    return {"name": f"{name}-enc", "type": "encryption", "policy": json.dumps({"Rules": [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}], "AWSOwnedKey": True})}


def network_policy(name: str) -> dict:
    rules = [{"ResourceType": "collection", "Resource": [f"collection/{name}"]}, {"ResourceType": "dashboard", "Resource": [f"collection/{name}"]}]
    return {"name": f"{name}-net", "type": "network", "policy": json.dumps([{"Rules": rules, "AllowFromPublic": True}])}


def access_policy(name: str, principals: list[str]) -> dict:
    rules = [
        {"ResourceType": "collection", "Resource": [f"collection/{name}"], "Permission": ["aoss:CreateCollectionItems", "aoss:DescribeCollectionItems", "aoss:UpdateCollectionItems"]},
        {"ResourceType": "index", "Resource": [f"index/{name}/*"], "Permission": ["aoss:CreateIndex", "aoss:DescribeIndex", "aoss:UpdateIndex", "aoss:ReadDocument", "aoss:WriteDocument"]},
    ]
    return {"name": f"{name}-access", "type": "data", "policy": json.dumps([{"Rules": rules, "Principal": principals}])}


def collection_params(name: str) -> dict:
    return {"name": name, "type": "VECTORSEARCH", "standbyReplicas": "DISABLED", "description": "PolicyDesk hybrid search experiment (temporary)"}


INDEX_BODY = {
    "settings": {"index.knn": True},
    "mappings": {
        "properties": {
            "vector": {"type": "knn_vector", "dimension": 1024, "method": {"name": "hnsw", "engine": "faiss", "space_type": "l2"}},
            "AMAZON_BEDROCK_TEXT_CHUNK": {"type": "text"},
            "AMAZON_BEDROCK_METADATA": {"type": "text", "index": False},
        }
    },
}


def oss_kb_params(n: Names, role_arn: str, collection_arn: str) -> dict:
    return {
        "name": f"{n.prefix}-kb-hybrid",
        "roleArn": role_arn,
        "knowledgeBaseConfiguration": {
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": f"arn:aws:bedrock:{n.region}::foundation-model/{EMBED_MODEL}",
                "embeddingModelConfiguration": {"bedrockEmbeddingModelConfiguration": {"dimensions": 1024, "embeddingDataType": "FLOAT32"}},
            },
        },
        "storageConfiguration": {
            "type": "OPENSEARCH_SERVERLESS",
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
                "vectorIndexName": "policies",
                "fieldMapping": {"vectorField": "vector", "textField": "AMAZON_BEDROCK_TEXT_CHUNK", "metadataField": "AMAZON_BEDROCK_METADATA"},
            },
        },
    }


def retrieve_params(kb_id: str, query: str, tenant_id: str, search_type: str, k: int) -> dict:
    return {
        "knowledgeBaseId": kb_id,
        "retrievalQuery": {"text": query},
        "retrievalConfiguration": {
            "vectorSearchConfiguration": {"numberOfResults": k, "overrideSearchType": search_type, "filter": {"equals": {"key": "tenant_id", "value": tenant_id}}}
        },
    }


class Experiment:
    def __init__(self, args):
        import boto3

        self.args = args
        self.session = boto3.Session(profile_name=args.profile, region_name=args.region) if args.profile else boto3.Session(region_name=args.region)
        self.main = load_state()
        if not self.main.get("kb_id"):
            sys.exit("run policydesk_aws.py setup first; the experiment reuses its docs bucket and KB role")
        self.n = Names(self.main["prefix"], self.main["account"], args.region)
        self.name = collection_name(self.n.prefix)
        self.state = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.started = time.time()

    def c(self, s):
        return self.session.client(s)

    def save(self):
        STATE.write_text(json.dumps(self.state, indent=2))

    def caller_principal(self) -> str:
        arn = self.c("sts").get_caller_identity()["Arn"]
        if ":assumed-role/" in arn:
            role = arn.split(":assumed-role/")[1].split("/")[0]
            return f"arn:aws:iam::{self.n.account}:role/{role}"
        return arn

    def kb_role_arn(self) -> str:
        return self.c("iam").get_role(RoleName=self.n.kb_role)["Role"]["Arn"]

    def create(self):
        aoss = self.c("opensearchserverless")
        for p in (encryption_policy(self.name), network_policy(self.name)):
            try:
                aoss.create_security_policy(**p)
            except aoss.exceptions.ConflictException:
                pass
        try:
            aoss.create_access_policy(**access_policy(self.name, [self.kb_role_arn(), self.caller_principal()]))
        except aoss.exceptions.ConflictException:
            pass
        if not self.state.get("collection_id"):
            col = aoss.create_collection(**collection_params(self.name))["createCollectionDetail"]
            self.state.update({"collection_id": col["id"], "collection_arn": col["arn"]})
            self.save()
            log(f"creating collection {self.name}; OCU billing starts now")

        def status():
            d = aoss.batch_get_collection(ids=[self.state["collection_id"]])["collectionDetails"]
            if d:
                self.state["endpoint"] = d[0].get("collectionEndpoint")
                return d[0]["status"]
            return "CREATING"

        wait(status, ok=("ACTIVE",), bad=("FAILED",), timeout=1200, every=15, what="collection")
        self.save()
        self.create_index()
        iam = self.c("iam")
        iam.put_role_policy(
            RoleName=self.n.kb_role,
            PolicyName=f"{self.n.kb_role}-aoss",
            PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "aoss:APIAccessAll", "Resource": self.state["collection_arn"]}]}),
        )
        time.sleep(15)
        ba = self.c("bedrock-agent")
        if not self.state.get("kb_id"):
            for attempt in range(6):
                try:
                    kb = ba.create_knowledge_base(**oss_kb_params(self.n, self.kb_role_arn(), self.state["collection_arn"]))["knowledgeBase"]
                    break
                except ba.exceptions.ValidationException as e:
                    if attempt == 5:
                        raise
                    log(f"waiting for permissions: {e}")
                    time.sleep(20)
            self.state["kb_id"] = kb["knowledgeBaseId"]
            self.save()
        wait(lambda: ba.get_knowledge_base(knowledgeBaseId=self.state["kb_id"])["knowledgeBase"]["status"], ok=("ACTIVE",), bad=("FAILED",), what="hybrid KB")
        if not self.state.get("data_source_id"):
            ds = ba.create_data_source(
                knowledgeBaseId=self.state["kb_id"],
                name=f"{self.n.prefix}-hybrid-docs",
                dataSourceConfiguration={"type": "S3", "s3Configuration": {"bucketArn": f"arn:aws:s3:::{self.n.docs_bucket}", "inclusionPrefixes": ["tenants/"]}},
                vectorIngestionConfiguration={"chunkingConfiguration": CHUNKING[self.main.get("chunking", "hierarchical")]},
                dataDeletionPolicy="DELETE",
            )["dataSource"]
            self.state["data_source_id"] = ds["dataSourceId"]
            self.save()
        job = ba.start_ingestion_job(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"])["ingestionJob"]
        wait(
            lambda: ba.get_ingestion_job(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"], ingestionJobId=job["ingestionJobId"])["ingestionJob"]["status"],
            ok=("COMPLETE",),
            bad=("FAILED", "STOPPED"),
            what="hybrid ingestion",
        )
        log("hybrid KB ingested")

    def create_index(self):
        from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

        host = self.state["endpoint"].replace("https://", "")
        auth = AWSV4SignerAuth(self.session.get_credentials(), self.args.region, "aoss")
        client = OpenSearch(hosts=[{"host": host, "port": 443}], http_auth=auth, use_ssl=True, verify_certs=True, connection_class=RequestsHttpConnection, timeout=60)
        for attempt in range(10):
            try:
                if not client.indices.exists(index="policies"):
                    client.indices.create(index="policies", body=INDEX_BODY)
                    log("created vector index in collection")
                break
            except Exception as e:
                log(f"index not ready yet ({type(e).__name__}); retrying")
                time.sleep(20)
        time.sleep(30)

    def tenant_ids(self) -> dict:
        s3 = self.c("s3")
        out = {}
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=self.n.docs_bucket, Prefix="tenants/"):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith(".metadata.json"):
                    meta = json.loads(s3.get_object(Bucket=self.n.docs_bucket, Key=obj["Key"])["Body"].read())["metadataAttributes"]
                    for slug, company in COMPANY.items():
                        if meta.get("title", "").startswith(company):
                            out[slug] = meta["tenant_id"]
        return out

    def compare(self):
        rt = self.c("bedrock-agent-runtime")
        tenants = self.tenant_ids()
        qs = [q for q in json.loads(QUESTIONS.read_text()) if q["type"] == "answerable" and q["tenant"] in tenants]
        configs = [("S3 Vectors KB, semantic", self.main["kb_id"], "SEMANTIC"), ("OpenSearch KB, semantic", self.state["kb_id"], "SEMANTIC"), ("OpenSearch KB, hybrid", self.state["kb_id"], "HYBRID")]
        rows = []
        for label, kb, st in configs:
            hits, ranks, lat = 0, [], []
            for q in qs:
                t0 = time.perf_counter()
                r = rt.retrieve(**retrieve_params(kb, q["q"], tenants[q["tenant"]], st, self.args.k))
                lat.append((time.perf_counter() - t0) * 1000)
                titles = [(x.get("metadata") or {}).get("title") for x in r["retrievalResults"]]
                if q["doc"] in titles:
                    hits += 1
                    ranks.append(1 / (titles.index(q["doc"]) + 1))
                else:
                    ranks.append(0)
            lat.sort()
            rows.append((label, hits / len(qs), sum(ranks) / len(qs), lat[len(lat) // 2]))
        lines = [f"# Hybrid vs semantic retrieval ({len(qs)} answerable questions, top-{self.args.k}, tenant-filtered)", "", "| Store / search type | Hit@k | MRR | p50 ms |", "|---|---:|---:|---:|"]
        lines += [f"| {l} | {h:.0%} | {m:.3f} | {p:.0f} |" for l, h, m, p in rows]
        lines += ["", f"Collection lifetime: {(time.time() - self.started) / 60:.0f} minutes"]
        RESULTS.write_text("\n".join(lines) + "\n")
        print("\n".join(lines))

    def teardown(self):
        ba = self.c("bedrock-agent")
        aoss = self.c("opensearchserverless")

        def attempt(label, fn):
            try:
                fn()
                log(f"deleted {label}")
            except Exception as e:
                log(f"skip {label}: {type(e).__name__}: {e}")

        if self.state.get("data_source_id"):
            attempt("hybrid data source", lambda: ba.delete_data_source(knowledgeBaseId=self.state["kb_id"], dataSourceId=self.state["data_source_id"]))
            time.sleep(10)
        if self.state.get("kb_id"):
            attempt("hybrid KB", lambda: ba.delete_knowledge_base(knowledgeBaseId=self.state["kb_id"]))
        if self.state.get("collection_id"):
            attempt("collection (stops OCU billing)", lambda: aoss.delete_collection(id=self.state["collection_id"]))
        for kind, name in (("encryption", f"{self.name}-enc"), ("network", f"{self.name}-net")):
            attempt(f"{kind} policy", lambda kind=kind, name=name: aoss.delete_security_policy(name=name, type=kind))
        attempt("access policy", lambda: aoss.delete_access_policy(name=f"{self.name}-access", type="data"))
        attempt("aoss role policy", lambda: self.c("iam").delete_role_policy(RoleName=self.n.kb_role, PolicyName=f"{self.n.kb_role}-aoss"))
        STATE.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description="Temporary OpenSearch Serverless KB to measure hybrid search; always tears itself down")
    p.add_argument("command", choices=["run", "teardown"])
    p.add_argument("--region", default="us-east-1")
    p.add_argument("--profile")
    p.add_argument("--k", type=int, default=5)
    args = p.parse_args()
    ex = Experiment(args)
    if args.command == "teardown":
        ex.teardown()
        return
    try:
        ex.create()
        ex.compare()
    finally:
        ex.teardown()
        log("experiment resources removed; check the OpenSearch Serverless console shows no collections")


if __name__ == "__main__":
    main()
