# eWebGen: AI Website Builder with PayPal Checkout

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)

eWebGen turns a plain-language description ("a website for my dental clinic in Pune") into a
complete, multi-page, responsive website, then lets you edit it visually or by chatting with the
AI and publish it to your own hosting. Plans, AI key purchases and renewals are paid through
**PayPal** (sandbox for testing).

Built by Orgif Solutions LLP · https://ewebgen.com · live demo: https://demo.ewebgen.com

> This repository is the open-source **hackathon edition**: the full web app with 3 sample
> website templates.

---

## PayPal developer platform: what we use

| PayPal tool / API | Endpoint | What it does in eWebGen |
|---|---|---|
| **PayPal Sandbox** | `api-m.sandbox.paypal.com` | All payments in this project run against the free sandbox. The admin switches Sandbox ↔ Live in *Admin CMS → Payment Gateways*. |
| **OAuth 2.0 (client credentials)** | `POST /v1/oauth2/token` | Gets an access token with the app's Client ID + Secret before every PayPal call. |
| **Orders API v2: Create Order** | `POST /v2/checkout/orders` | Creates a `CAPTURE` order for the plan / AI-key purchase, with `reference_id`/`custom_id` = our transaction ID and an idempotency key (`PayPal-Request-Id`). The buyer is redirected to PayPal to approve (`payer-action` link). INR prices are converted to USD. |
| **Orders API v2: Show Order + Capture Order** | `GET /v2/checkout/orders/{id}`, `POST /v2/checkout/orders/{id}/capture` | When the buyer returns, the **server** captures the order and checks the result. The plan is activated only if the capture is `COMPLETED`, the `reference_id` matches the transaction and the paid amount/currency match. The return URL alone is never trusted. |
| **Payments API v2: Refund Capture** | `POST /v2/payments/captures/{id}/refund` | Admin CMS → Transactions & Refunds → **Refund** issues a full or partial refund through PayPal and stores the refund ID. |
| **Webhooks + Verify Webhook Signature** | `POST /v1/notifications/verify-webhook-signature` | `POST /api/payment/webhook/paypal` receives `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED/REVERSED`, `PAYMENT.CAPTURE.DENIED`. Each event's signature is verified with PayPal first. The webhook completes payments even if the buyer closes the browser before returning, and records refunds made in the PayPal dashboard. |

Code: [`services_payment.py`](services_payment.py) (PayPal client) and
[`routes_payment.py`](routes_payment.py) (checkout, callback, webhook, refunds, order fulfilment).

### Payment flow

```
Buyer picks a plan ──► POST /api/pay/initiate ──► PayPal Create Order (sandbox)
       │                                                   │
       │◄────────────── redirect to PayPal approval ◄──────┘
       ▼
Buyer approves on PayPal ──► /api/payment/callback/paypal?token=ORDER_ID
                                   │
                                   ▼
               Server: Show Order ─► Capture Order ─► verify status, reference, amount
                                   │
                                   ▼
               Activate plan / create order / queue AI-key request ─► success message
(Webhook PAYMENT.CAPTURE.COMPLETED does the same if the buyer never returns)
Admin ──► Refund ──► PayPal Refund Capture ──► transaction marked refunded
```

## AI tools and models

eWebGen is multi-provider: the admin picks a provider and model in *Admin CMS → AI & System*,
and users can bring their own keys.

| AI tool / model | What it does |
|---|---|
| **Google Gemini** (default `gemini-3.1-pro-preview`, via `google-genai`) | Generates the full website (HTML pages + `styles.css`) as JSON from the prompt, and applies chat edits ("make the header dark", "add a pricing page"). |
| **Google Imagen 3** (`imagen-3.0-generate-002`) | Optional AI-generated images (hero images, logos) for the generated sites. |
| **OpenAI GPT** (`gpt-4.1`) | Alternative website generation / editing model. |
| **OpenAI GPT Image / DALL-E** (`gpt-image-1`, `dall-e-3`) | Alternative AI image generation. |
| **Anthropic Claude** (`claude-sonnet-4.6`) | Alternative website generation / editing model. |
| **DeepSeek** (`deepseek-chat`) | Low-cost alternative generation model. |
| **xAI Grok** (`grok-4.3`, fallbacks `grok-4`, `grok-3`) | Alternative generation model via the OpenAI-compatible xAI API. |
| **Meta Llama** (Llama 3.3 70B / Llama 4 Maverick via Groq, OpenRouter or Meta Llama API) | Fast open-model generation. |
| **Perplexity Sonar** (`sonar-pro`) | Alternative generation model (web search disabled for site generation). |
| **rembg (U²-Net model)** | AI background removal for uploaded images in the editor. |

Providers are in [`ai_agents/`](ai_agents). All of them share one design brief and output contract
(`ai_agents/openai_compat.py`, `ai_agents/base_agent.py`): JSON with each page + `styles.css`,
design tokens, responsive layout and animations. The response is streamed to the editor live.

## Setup (run it locally)

Requirements: Python 3.10+ and git.

```bash
git clone https://github.com/<your-account>/ewebgen.git
cd ewebgen
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # Windows: copy .env.example .env
python import_templates.py           # loads the 3 sample templates
python main.py
```

Open **http://localhost:3003** (`.env.example` uses SQLite and skips the license check on localhost).
On first start you are asked to set the admin password (login: `admin`).

1. **AI key:** in the "Set up your AI keys" popup choose a provider (Google Gemini has a free tier:
   https://aistudio.google.com/apikey) and paste the key. Then type a prompt and click Generate.
2. **PayPal sandbox:** create an app at https://developer.paypal.com → *Apps & Credentials* (Sandbox).
   In eWebGen open *Admin CMS → Payment Gateways → PayPal*, paste the **Client ID** and **Secret**,
   set mode to **Sandbox** and enable it.
3. **Create a paid plan:** *Admin CMS → Plans*, e.g. "Pro", 9.99 USD, 30 days.
4. **Pay:** register a normal user (without SMTP configured, mark the user as verified in
   *Admin CMS → Users*), open the plans/upgrade screen and pay with a **sandbox personal account**
   (*Sandbox → Accounts* in the PayPal developer dashboard). The order shows as *Completed* in the
   sandbox, and *Admin CMS → Transactions & Refunds* shows the capture ID.
5. **Refund:** click **Refund** on the transaction (full or partial). It is refunded through PayPal.
6. **Webhook (optional):** needs a public HTTPS URL (deploy on Render below, or `ngrok http 3003`).
   Add `https://<your-host>/api/payment/webhook/paypal` in your PayPal app with the events
   `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.REFUNDED`,
   `PAYMENT.CAPTURE.REVERSED`, `PAYMENT.CAPTURE.DENIED`, then paste the **Webhook ID** in the PayPal gateway settings.

### Deploy on Render

`render.yaml` is a Render Blueprint: *New → Blueprint* on https://render.com and select this repo.
It installs the requirements, imports the sample templates and runs the app with gunicorn on SQLite
(the free plan's disk is temporary, so data resets on redeploy). Public servers are not covered by the localhost license
bypass: activate the free plan on the first screen, then follow steps 1-6.

## Sponsor and developer tools used

| Tool | How we use it |
|---|---|
| **PayPal Developer (Sandbox, REST APIs, Webhooks)** | Checkout, server-side capture, refunds and webhooks (see above). |
| **Postman** | [`postman/eWebGen-PayPal.postman_collection.json`](postman/eWebGen-PayPal.postman_collection.json): every PayPal sandbox call eWebGen makes (token → create order → show → capture → refund → verify webhook), with tests that save the order and capture IDs, plus eWebGen's own payment endpoints. Import it, set `client_id` and `client_secret`, run in order. |
| **Render** | `render.yaml` Blueprint for one-click hosting of the app. |

## Other technologies

Python 3 · Flask · Flask-SQLAlchemy (SQLite for local/demo, MySQL in production) · Flask-Login ·
Flask-Limiter · bleach (HTML sanitising) · cryptography / PyCryptodome (encrypted stored API keys
and FTP credentials) · requests (Server-Sent Events streaming from the AI APIs) · vanilla
JavaScript visual editor and live preview · gunicorn · FTP deployment of generated sites ·
Pexels / Unsplash / Pixabay stock photo APIs.

### Project structure

```
main.py                  app entry, license + first-run guards, generation endpoint
services_ai.py           picks the AI provider/model, builds the prompt, streams the result
ai_agents/               one module per AI provider + shared OpenAI-compatible engine
services_payment.py      PayPal client (OAuth, Orders v2, capture, refunds, webhook verify)
routes_payment.py        checkout, PayPal callback, webhook, refunds, order fulfilment
routes_*.py              auth, projects, admin CMS, templates/media, updates
templates/               editor UI (Jinja + JS)
postman/                 Postman collection
render.yaml              Render deploy blueprint
update_seed.json         3 sample website templates
```

## License

Copyright (C) 2026 Orgif Solutions LLP.

Licensed under the **GNU Affero General Public License v3.0 only** (AGPL-3.0-only), an
OSI-approved open-source license. See [LICENSE](LICENSE) and [NOTICE](NOTICE). If you run a
modified version as a network service, the AGPL requires you to offer its source code to its users.

The eWebGen and Orgif names and logos are trademarks of Orgif Solutions LLP and are not covered by
this license. See [TRADEMARKS.md](TRADEMARKS.md).
