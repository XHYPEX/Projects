# WhatsAppBooking

When a customer sends your WhatsApp Business number a message in the booking format, the system:
1. Parses and validates it (required fields, email/phone/date/time format, not in the past, within business hours)
2. Checks Google Calendar for conflicts (freebusy)
3. Creates the event, or turns the booking down if the slot is taken
4. Replies to the customer with a confirmation or an explanation

A message counts as a booking when it has `Nama Lengkap` plus `Tanggal Booking` or `Jam Booking`. Normal chat is ignored.

## Customer message format

```
Nama Lengkap: Budi Santoso
Email: budi@gmail.com
No Telp: 08123456789
Tanggal Booking: 05-10-2026
Jam Booking: 14:00
```
- All five fields are required. Keys are case-insensitive and the colon is optional.
- Dates accept `DD-MM-YYYY`, `DD/MM/YYYY` or `YYYY-MM-DD`; times accept `14:00` or `14.00`.
- Phone numbers starting with `0` are stored as `62...`.
- Optional extras: `Durasi: 90` (minutes, default `DEFAULT_DURATION_MINUTES`) and `Catatan: ...`.
- The email and phone go into the event description. The customer isn't added as a calendar guest, because a service account can't invite guests without Google Workspace domain-wide delegation.

## Setup

### 1. Google Calendar
1. In Google Cloud Console, create a project and enable the **Google Calendar API**.
2. Go to IAM & Admin > Service Accounts, create a service account, then under Keys > Add key > JSON, download the key and save it as `credentials/service-account.json`.
3. In Google Calendar, open your calendar's Settings > *Share with specific people*, add the service account's email (`...@...iam.gserviceaccount.com`), and give it **Make changes to events**.
4. Copy the *Calendar ID* (under Integrate calendar) into `GOOGLE_CALENDAR_ID`.

### 2. WhatsApp Cloud API
1. On developers.facebook.com, create an app of type **Business** and add the **WhatsApp** product.
2. Under WhatsApp > API Setup, add and verify your business number, then copy the **Phone number ID**.
3. Create a permanent token: Business Settings > System Users > Generate token with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions.
4. Copy the App secret from App Settings > Basic.
5. Once the server is live, go to WhatsApp > Configuration > Webhook and set:
   - Callback URL: `https://your-domain/webhook`
   - Verify token: the value of `WA_VERIFY_TOKEN`
   - Subscribe to the **messages** field.

> Once a number is registered to the Cloud API, you can no longer use it in the regular WhatsApp app.

### 2b. Use your existing WhatsApp Business app number (coexistence, optional)
To keep using the number in the WhatsApp Business app on your phone while the bot reads its bookings:
1. In your Meta app, open **Facebook Login for Business > Configurations**, create one with the **WhatsApp Embedded Signup** variation, and put its ID in `FB_LOGIN_CONFIG_ID`. Also set `META_APP_ID` and a random `ONBOARD_KEY`.
2. Add your public domain (e.g. the tunnel URL) to **App Domains** and to Facebook Login's **Allowed Domains for the JavaScript SDK**.
3. Start the server, open `https://your-domain/onboard?key=<ONBOARD_KEY>`, choose **Connect your existing WhatsApp Business app** and scan the QR code with the app.
4. The page writes the new `WA_ACCESS_TOKEN`, `WA_PHONE_NUMBER_ID` and `WA_BUSINESS_ACCOUNT_ID` into `.env`. Restart the server.

Open the WhatsApp Business app at least every 14 days, or Meta disconnects the number from the API.

### 2c. Use Kirimdev instead (coexistence, no Meta app review)
[Kirimdev](https://kirimdev.com) connects your WhatsApp Business app number to the Cloud API while it stays on your phone.
1. Sign up, connect your number with their **Coexistence** option, and create an API key.
2. In `.env`, set `WHATSAPP_PROVIDER=kirimdev`, `KIRIM_API_KEY` and `KIRIM_PHONE_ID` (the number's ID in Kirimdev). The `WA_*` Meta settings aren't needed in this mode.
3. Start the server and tunnel, then run `python register_kirimdev_webhook.py https://your-domain`. It saves the signing secret as `KIRIM_WEBHOOK_SECRET` in `.env`. Restart the server. Until the secret is set, every webhook is rejected.

Kirimdev webhooks are checked against `X-Kirim-Signature`; requests older than 5 minutes are rejected.

### 3. Run
```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill it in
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```
Meta requires an HTTPS webhook. For local testing, use `ngrok http 8000`. In production, put a reverse proxy (Caddy/nginx) in front of the service, which you can find at `deploy/wa-booking.service`.

### Tests
```bash
.venv/bin/pip install -r requirements-dev.txt && .venv/bin/pytest
```

## Notes
- Every webhook is checked against its `X-Hub-Signature-256` signature, and fake requests are rejected.
- Message IDs are stored in `data/state.db`, so a webhook Meta sends twice still creates only one event.
- Each event stores the customer's WhatsApp number in its description and in `extendedProperties.private`.
