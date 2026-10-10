import os
import random
import uuid

from locust import HttpUser, between, events, task

MODE = os.environ.get("LOAD_MODE", "miss")
PASSWORD = os.environ.get("LOAD_PASSWORD", "Password123!")
ACCOUNTS = os.environ.get("LOAD_ACCOUNTS", "employee@acme.example,employee@globex.example").split(",")

QUESTIONS = [
    "How many days of annual leave do I get?",
    "How much sick leave do I get each year?",
    "What is the hotel cap per night?",
    "What is the daily allowance when travelling?",
    "How long must my password be?",
    "Who do I contact about leave questions?",
    "How many unused leave days can I carry forward?",
    "What is the paternity leave policy?",
]

instances: dict[str, int] = {}


@events.request.add_listener
def count_instance(request_type, name, response_time, response_length, response=None, exception=None, **kw):
    if response is not None and getattr(response, "headers", None):
        inst = response.headers.get("X-Instance")
        if inst:
            instances[inst] = instances.get(inst, 0) + 1


@events.quitting.add_listener
def report_instances(environment, **kw):
    print("requests per instance:", dict(sorted(instances.items())))


class Employee(HttpUser):
    wait_time = between(0.5, 1.5)

    def on_start(self):
        email = random.choice(ACCOUNTS)
        r = self.client.post("/api/auth/login", json={"email": email, "password": PASSWORD}, name="/api/auth/login")
        self.headers = {"Authorization": f"Bearer {r.json()['token']}"}

    @task(10)
    def ask(self):
        q = random.choice(QUESTIONS)
        if MODE == "miss":
            q = f"{q} ref {uuid.uuid4().hex[:8]}"
        with self.client.post("/api/chat", json={"question": q}, headers=self.headers, name=f"/api/chat [{MODE}]", catch_response=True) as r:
            if r.status_code != 200:
                r.failure(f"{r.status_code}: {r.text[:120]}")

    @task(1)
    def history(self):
        self.client.get("/api/conversations", headers=self.headers, name="/api/conversations")
