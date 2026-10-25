"""Webhook server WhatsApp Cloud API.

Jalankan: uvicorn app:app --host 0.0.0.0 --port 8000
"""
import hmac
import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

from onboarding import OnboardingConfig, OnboardingError, complete_onboarding, render_page
from service import BookingService
from state import ProcessedMessages
from whatsapp_client import SignatureCheck, WhatsAppClient, kirim_signature_check, meta_signature_check

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wa-booking")


def extract_text_messages(payload: dict) -> list[tuple[str, str, str]]:
    """Ambil (nomor pengirim, message id, teks) dari payload webhook. Status update
    (delivered/read) dan pesan non-teks diabaikan."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Status pengiriman balasan kita. Kalau gagal, Meta cuma ngasih tahu lewat sini.
            for status in value.get("statuses", []):
                if status.get("status") == "failed":
                    log.warning("Balasan ke %s gagal terkirim: %s", status.get("recipient_id"), status.get("errors"))
            for msg in value.get("messages", []):
                if msg.get("type") == "text":
                    messages.append((msg["from"], msg["id"], msg["text"]["body"]))
    return messages


class SignupResult(BaseModel):
    code: str
    waba_id: str
    phone_number_id: str


def create_app(
    service: BookingService,
    verify_token: str,
    check_signature: SignatureCheck,
    onboarding: OnboardingConfig | None = None,
) -> FastAPI:
    app = FastAPI(title="WhatsApp Booking")

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/webhook", response_class=PlainTextResponse)
    def verify(
        mode: str = Query(alias="hub.mode", default=""),
        token: str = Query(alias="hub.verify_token", default=""),
        challenge: str = Query(alias="hub.challenge", default=""),
    ):
        if mode == "subscribe" and verify_token and token == verify_token:
            return challenge
        raise HTTPException(status_code=403)

    @app.post("/webhook")
    async def receive(request: Request, background: BackgroundTasks):
        body = await request.body()
        if not check_signature(body, request.headers):
            log.warning("Signature webhook gak valid, request ditolak")
            raise HTTPException(status_code=401)
        # Balas 200 secepatnya; proses di background biar Meta gak timeout & kirim ulang.
        for sender, message_id, text in extract_text_messages(await request.json()):
            background.add_task(service.handle_message, sender, message_id, text)
        return {"ok": True}

    def check_key(key: str) -> None:
        # Halaman ini kebuka lewat tunnel publik; tanpa key orang lain bisa nimpa nomor kita.
        if onboarding is None or not hmac.compare_digest(key, onboarding.key):
            raise HTTPException(status_code=404)

    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_page(key: str = ""):
        check_key(key)
        return render_page(onboarding.app_id, onboarding.config_id, onboarding.graph_version, key)

    @app.post("/onboard/complete")
    def onboard_complete(result: SignupResult, key: str = ""):
        check_key(key)
        try:
            complete_onboarding(
                onboarding.graph, onboarding.env_path, result.code, result.waba_id, result.phone_number_id
            )
        except OnboardingError as e:
            log.warning("Onboarding gagal: %s", e)
            raise HTTPException(status_code=502, detail=str(e))
        return {"ok": True}

    return app


def _build_default_app() -> FastAPI:
    import config
    from calendar_client import CalendarClient
    from onboarding import GraphOnboarding

    if config.WHATSAPP_PROVIDER == "kirimdev":
        whatsapp = WhatsAppClient.kirimdev(config.KIRIM_API_KEY, config.KIRIM_PHONE_ID)
        check_signature = kirim_signature_check(config.KIRIM_WEBHOOK_SECRET)
    else:
        whatsapp = WhatsAppClient.meta(config.WA_ACCESS_TOKEN, config.WA_PHONE_NUMBER_ID, config.WA_GRAPH_VERSION)
        check_signature = meta_signature_check(config.WA_APP_SECRET)

    service = BookingService(
        calendar=CalendarClient(config.GOOGLE_CREDENTIALS_FILE, config.GOOGLE_CALENDAR_ID, config.TIMEZONE.key),
        whatsapp=whatsapp,
        processed=ProcessedMessages(config.STATE_DB_PATH),
        tz=config.TIMEZONE,
        default_duration=config.DEFAULT_DURATION_MINUTES,
        business_start=config.BUSINESS_HOUR_START,
        business_end=config.BUSINESS_HOUR_END,
    )
    onboarding = None
    if config.WHATSAPP_PROVIDER == "meta" and config.META_APP_ID and config.FB_LOGIN_CONFIG_ID and config.ONBOARD_KEY:
        onboarding = OnboardingConfig(
            app_id=config.META_APP_ID,
            config_id=config.FB_LOGIN_CONFIG_ID,
            graph_version=config.WA_GRAPH_VERSION,
            key=config.ONBOARD_KEY,
            env_path=".env",
            graph=GraphOnboarding(config.META_APP_ID, config.WA_APP_SECRET, config.WA_GRAPH_VERSION),
        )
    return create_app(service, config.WA_VERIFY_TOKEN, check_signature, onboarding)


def __getattr__(name):
    # `uvicorn app:app` -> baru build di sini, jadi test bisa import modul ini tanpa .env.
    if name == "app":
        globals()["app"] = _build_default_app()
        return globals()["app"]
    raise AttributeError(name)
