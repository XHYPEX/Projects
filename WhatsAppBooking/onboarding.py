"""Sambungin nomor WhatsApp Business app ke Cloud API lewat Embedded Signup (coexistence).

Nomor tetap jalan di HP; Cloud API dapet salinan pesan masuk lewat webhook yang sama.
Halaman /onboard cuma aktif kalau META_APP_ID, FB_LOGIN_CONFIG_ID dan ONBOARD_KEY di-set.
"""
import json
import logging
from dataclasses import dataclass

import httpx
from dotenv import set_key

log = logging.getLogger(__name__)


class OnboardingError(Exception):
    pass


@dataclass
class OnboardingConfig:
    app_id: str
    config_id: str
    graph_version: str
    key: str
    env_path: str
    graph: "GraphOnboarding"


class GraphOnboarding:
    """Tukar code dari popup jadi token, lalu subscribe app ke WABA yang baru disambung."""

    def __init__(self, app_id: str, app_secret: str, graph_version: str):
        self._app_id = app_id
        self._app_secret = app_secret
        self._graph = f"https://graph.facebook.com/{graph_version}"

    def exchange_code(self, code: str) -> str:
        data = self._call("GET", "/oauth/access_token", params={
            "client_id": self._app_id, "client_secret": self._app_secret, "code": code,
        })
        return data["access_token"]

    def subscribe_app(self, waba_id: str, token: str) -> None:
        # Sengaja gak manggil /register: buat nomor coexistence itu bakal nendang nomornya dari app di HP.
        self._call("POST", f"/{waba_id}/subscribed_apps", headers={"Authorization": f"Bearer {token}"})

    def _call(self, method: str, path: str, **kwargs) -> dict:
        resp = httpx.request(method, self._graph + path, timeout=30, **kwargs)
        data = resp.json()
        if resp.status_code != 200 or "error" in data:
            raise OnboardingError(data.get("error", {}).get("message", resp.text))
        return data


def complete_onboarding(graph, env_path: str, code: str, waba_id: str, phone_number_id: str) -> None:
    token = graph.exchange_code(code)
    graph.subscribe_app(waba_id, token)
    # Token & nomor lama (test number) diganti; server harus di-restart biar kepake.
    set_key(env_path, "WA_ACCESS_TOKEN", token, quote_mode="never")
    set_key(env_path, "WA_PHONE_NUMBER_ID", phone_number_id, quote_mode="never")
    set_key(env_path, "WA_BUSINESS_ACCOUNT_ID", waba_id, quote_mode="never")
    log.info("Onboarding selesai: WABA %s, nomor %s", waba_id, phone_number_id)


def render_page(app_id: str, config_id: str, graph_version: str, key: str) -> str:
    settings = json.dumps({"appId": app_id, "configId": config_id, "version": graph_version, "key": key})
    return _PAGE.replace("__SETTINGS__", settings)


_PAGE = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sambungkan WhatsApp</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 32rem; margin: 3rem auto; padding: 0 1rem; color: #1c1e21; }
  button { font-size: 1rem; padding: .75rem 1.25rem; border: 0; border-radius: .5rem; background: #1877f2; color: #fff; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  #status { margin-top: 1.5rem; white-space: pre-wrap; }
</style>
</head>
<body>
<h1>Sambungkan WhatsApp Business</h1>
<ol>
  <li>Update aplikasi WhatsApp Business di HP ke versi terbaru.</li>
  <li>Klik tombol di bawah, login Facebook, lalu pilih
    <b>Connect your existing WhatsApp Business app</b>.</li>
  <li>Scan QR yang muncul pakai WhatsApp Business di HP.</li>
</ol>
<button id="connect" disabled>Sambungkan</button>
<div id="status"></div>
<script>
const S = __SETTINGS__;
const status = document.getElementById("status");
const button = document.getElementById("connect");
let code = null, signup = null, sent = false;

function show(text) { status.textContent = text; }

async function submit() {
  if (sent || !code || !signup) return;
  sent = true;
  show("Menyimpan...");
  const resp = await fetch("/onboard/complete?key=" + encodeURIComponent(S.key), {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({code, waba_id: signup.waba_id, phone_number_id: signup.phone_number_id}),
  });
  const body = await resp.json().catch(() => ({}));
  show(resp.ok
    ? "Berhasil ✅ Restart server biar nomor baru kepake."
    : "Gagal: " + (body.detail || resp.status));
}

// Popup ngirim waba_id & phone_number_id lewat postMessage, terpisah dari code di callback login.
window.addEventListener("message", (event) => {
  if (!event.origin.endsWith("facebook.com")) return;
  let data;
  try { data = JSON.parse(event.data); } catch { return; }
  if (data.type !== "WA_EMBEDDED_SIGNUP") return;
  if (data.event === "CANCEL") { show("Dibatalkan" + (data.data?.current_step ? " di langkah " + data.data.current_step : "") + "."); return; }
  if (data.event?.startsWith("FINISH") && data.data?.phone_number_id) { signup = data.data; submit(); }
});

window.fbAsyncInit = () => {
  FB.init({appId: S.appId, autoLogAppEvents: true, xfbml: false, version: S.version});
  button.disabled = false;
};

button.addEventListener("click", () => {
  show("Ikuti langkah di popup...");
  FB.login((resp) => {
    if (!resp.authResponse?.code) { show("Popup ditutup sebelum selesai."); return; }
    code = resp.authResponse.code;
    submit();
  }, {
    config_id: S.configId,
    response_type: "code",
    override_default_response_type: true,
    extras: {setup: {}, featureType: "whatsapp_business_app_onboarding", sessionInfoVersion: "3"},
  });
});
</script>
<script async defer crossorigin="anonymous" src="https://connect.facebook.net/en_US/sdk.js"></script>
</body>
</html>
"""
