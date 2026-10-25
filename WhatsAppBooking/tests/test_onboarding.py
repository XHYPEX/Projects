from zoneinfo import ZoneInfo

from dotenv import dotenv_values
from fastapi.testclient import TestClient

from app import create_app
from onboarding import OnboardingConfig, OnboardingError
from service import BookingService
from state import ProcessedMessages
from whatsapp_client import meta_signature_check

KEY = "secret-key"


class FakeGraph:
    def __init__(self, fail=False):
        self.fail = fail
        self.subscribed = []

    def exchange_code(self, code):
        if self.fail:
            raise OnboardingError("Invalid verification code")
        return f"token-for-{code}"

    def subscribe_app(self, waba_id, token):
        self.subscribed.append((waba_id, token))


def make(tmp_path, graph=None, enabled=True):
    env = tmp_path / ".env"
    env.write_text("WA_ACCESS_TOKEN=old\nWA_PHONE_NUMBER_ID=111\nWA_VERIFY_TOKEN=keep\n")
    graph = graph or FakeGraph()
    onboarding = OnboardingConfig("app-1", "cfg-1", "v23.0", KEY, str(env), graph) if enabled else None
    service = BookingService(None, None, ProcessedMessages(":memory:"), ZoneInfo("Asia/Jakarta"))
    return TestClient(create_app(service, "verify", meta_signature_check("secret"), onboarding)), env, graph


def complete(client, key=KEY):
    return client.post(f"/onboard/complete?key={key}", json={"code": "abc", "waba_id": "999", "phone_number_id": "222"})


def test_page_needs_key(tmp_path):
    client, _, _ = make(tmp_path)
    assert client.get("/onboard").status_code == 404
    assert client.get("/onboard?key=wrong").status_code == 404
    page = client.get(f"/onboard?key={KEY}")
    assert page.status_code == 200
    assert '"configId": "cfg-1"' in page.text and "whatsapp_business_app_onboarding" in page.text


def test_disabled_without_config(tmp_path):
    client, _, _ = make(tmp_path, enabled=False)
    assert client.get(f"/onboard?key={KEY}").status_code == 404
    assert complete(client).status_code == 404


def test_complete_subscribes_and_updates_env(tmp_path):
    client, env, graph = make(tmp_path)
    assert complete(client).status_code == 200
    assert graph.subscribed == [("999", "token-for-abc")]
    values = dotenv_values(env)
    assert values["WA_ACCESS_TOKEN"] == "token-for-abc"
    assert values["WA_PHONE_NUMBER_ID"] == "222"
    assert values["WA_BUSINESS_ACCOUNT_ID"] == "999"
    assert values["WA_VERIFY_TOKEN"] == "keep"


def test_complete_rejects_wrong_key(tmp_path):
    client, env, graph = make(tmp_path)
    assert complete(client, key="wrong").status_code == 404
    assert graph.subscribed == [] and dotenv_values(env)["WA_PHONE_NUMBER_ID"] == "111"


def test_graph_error_leaves_env_alone(tmp_path):
    client, env, _ = make(tmp_path, graph=FakeGraph(fail=True))
    resp = complete(client)
    assert resp.status_code == 502 and "Invalid verification code" in resp.json()["detail"]
    assert dotenv_values(env)["WA_ACCESS_TOKEN"] == "old"
