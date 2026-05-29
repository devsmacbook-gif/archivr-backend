"""
ARCHIVR SaaS Backend
====================
Single Python service that handles:
  - User auth (via Supabase)
  - Stripe subscription webhooks
  - Extension API (analyse a listing)
  - Autonomous scraper (background thread)
  - Telegram alerts

Deploy to Railway.app for ~$5/month.
"""

import os, json, time, hashlib, re, threading, logging
from datetime import datetime, timezone
from urllib.parse import quote as url_quote

import requests
import stripe
from anthropic import Anthropic
from fastapi import FastAPI, HTTPException, Header, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from supabase import create_client, Client

# ─── LOGGING ─────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARCHIVR] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("archivr")

# ─── ENV VARS ─────────────────────────────────────────────────────────────────
# Set these in Railway dashboard — never hardcode keys

ANTHROPIC_API_KEY     = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL          = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]   # service_role key
STRIPE_SECRET_KEY     = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
STRIPE_PRICE_ID       = os.environ["STRIPE_PRICE_ID"]        # your £20/mo price ID

# Optional — if not set, Telegram alerts are skipped
TELEGRAM_BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")  # your own alerts

SCAN_INTERVAL_MINUTES = int(os.environ.get("SCAN_INTERVAL_MINUTES", "15"))
MIN_FLIP_SCORE        = int(os.environ.get("MIN_FLIP_SCORE", "65"))
APIFY_TOKEN           = os.environ.get("APIFY_TOKEN", "")

# ─── CLIENTS ─────────────────────────────────────────────────────────────────

anthropic_client: Anthropic = Anthropic(api_key=ANTHROPIC_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
stripe.api_key = STRIPE_SECRET_KEY

app = FastAPI(title="ARCHIVR API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten to your dashboard domain in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── MODELS ──────────────────────────────────────────────────────────────────

class AnalyseRequest(BaseModel):
    title: str
    price: str | None = None
    condition: str | None = None
    description: str | None = None
    platform: str | None = None
    url: str | None = None

class AlertCreate(BaseModel):
    brand: str
    keywords: list[str]
    max_price_gbp: float
    size: str | None = None

class CheckoutRequest(BaseModel):
    email: str
    success_url: str
    cancel_url: str

# ─── TIER LIMITS ─────────────────────────────────────────────────────────────

TIER_LIMITS = {
    "starter": {"analyses_per_month": 100,    "alerts_max": 5},
    "pro":     {"analyses_per_month": 500,    "alerts_max": 20},
    "store":   {"analyses_per_month": 999999, "alerts_max": 999999},
}

PRICE_TO_TIER = {
    os.environ.get("STRIPE_PRICE_ID_STARTER", ""): "starter",
    os.environ.get("STRIPE_PRICE_ID",          ""): "pro",
    os.environ.get("STRIPE_PRICE_ID_STORE",    ""): "store",
}

def get_user_tier(user: dict) -> str:
    price_id = user.get("stripe_price_id", "")
    return PRICE_TO_TIER.get(price_id, "pro")

def get_monthly_usage(user_id: str) -> int:
    from datetime import date
    month_start = date.today().replace(day=1).isoformat()
    try:
        result = (
            supabase.table("analyses")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .gte("created_at", month_start)
            .execute()
        )
        return result.count or 0
    except Exception:
        return 0

def check_usage_limit(user: dict):
    tier     = get_user_tier(user)
    limits   = TIER_LIMITS.get(tier, TIER_LIMITS["pro"])
    max_uses = limits["analyses_per_month"]
    used     = get_monthly_usage(user["id"])
    if used >= max_uses:
        raise HTTPException(
            status_code=429,
            detail=f"Monthly limit of {max_uses} analyses reached. Upgrade your plan at archivr.app"
        )
    return {"used": used, "limit": max_uses, "tier": tier}

def check_alert_limit(user: dict, current_alert_count: int):
    tier       = get_user_tier(user)
    limits     = TIER_LIMITS.get(tier, TIER_LIMITS["pro"])
    max_alerts = limits["alerts_max"]
    if current_alert_count >= max_alerts:
        raise HTTPException(
            status_code=429,
            detail=f"Alert limit of {max_alerts} reached on your plan. Upgrade at archivr.app"
        )


# ─── AUTH HELPERS ─────────────────────────────────────────────────────────────

def get_user_from_token(token: str) -> dict:
    """Validate Supabase JWT and return user row from our users table."""
    try:
        user = supabase.auth.get_user(token)
        if not user or not user.user:
            raise HTTPException(status_code=401, detail="Invalid token")
        
        row = (
            supabase.table("users")
            .select("*")
            .eq("id", user.user.id)
            .single()
            .execute()
        )
        if not row.data:
            raise HTTPException(status_code=404, detail="User not found")
        return row.data
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"Auth error: {e}")
        raise HTTPException(status_code=401, detail="Authentication failed")


def require_active_subscription(user: dict):
    """Raise 403 if user doesn't have an active subscription."""
    if user.get("subscription_status") not in ("active", "trialing"):
        raise HTTPException(
            status_code=403,
            detail="Active subscription required. Subscribe at archivr.app"
        )

# ─── AI ANALYSIS ─────────────────────────────────────────────────────────────

def run_ai_analysis(listing: dict, user_platforms: list[str] | None = None) -> dict:
    """Call Claude to analyse a listing. Uses Sonnet for quality."""
    platforms = user_platforms or ["Depop", "eBay", "Grailed", "Instagram", "Own Site"]

    prompt = f"""You are an expert vintage/archive streetwear dealer specialising in 2000s archive pieces.

Listing:
- Platform: {listing.get('platform', 'Unknown')}
- Title: {listing.get('title', 'Unknown')}
- Listed Price: {listing.get('price', 'Unknown')}
- Condition: {listing.get('condition', 'Not specified')}
- Description: {listing.get('description', 'None')}

Analyse resale potential on: {', '.join(platforms)}

Return ONLY valid JSON (no markdown, no backticks):
{{
  "itemName": "clean item name",
  "era": "estimated year/season",
  "buyPrice": number in GBP,
  "sellPrice": number in GBP,
  "profit": number in GBP,
  "sellSpeed": 0-100,
  "marketDemand": 0-100,
  "flipScore": 0-100,
  "verdict": "BUY NOW"|"WORTH IT"|"WAIT"|"SKIP",
  "verdictReason": "one punchy sentence",
  "japanArbitrage": true|false,
  "japanNote": "one sentence if relevant",
  "platformBreakdown": {{"Depop": number, "eBay": number, "Grailed": number, "Instagram": number, "Own Site": number}},
  "marketInsight": "2-3 sentence market analysis",
  "watchOuts": ["risk 1", "risk 2"],
  "buyTips": ["tip 1", "tip 2"]
}}"""

    response = anthropic_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    return json.loads(text)


def run_quick_analysis(title: str, price, platform: str, alert: dict) -> dict:
    """Lightweight Haiku analysis for the scraper — cheaper and faster."""
    prompt = f"""Archive streetwear dealer. Quick analysis.

Item: {title}
Platform: {platform}  
Listed: £{price}
Budget: £{alert.get('max_price_gbp', '?')}

Return ONLY JSON:
{{"flipScore": 0-100, "sellSpeed": 0-100, "estimatedSellPrice": number, "estimatedProfit": number, "verdict": "BUY NOW"|"WORTH IT"|"WAIT"|"SKIP", "reason": "one sentence", "japanArbitrage": true|false}}"""

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",   # 10x cheaper than Sonnet
        max_tokens=200,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

class LoginRequest(BaseModel):
    email: str
    password: str

class TelegramUpdate(BaseModel):
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

@app.post("/auth/login")
async def login(body: LoginRequest):
    """Sign in with email + password, returns Supabase JWT."""
    try:
        session = supabase.auth.sign_in_with_password({
            "email": body.email,
            "password": body.password,
        })
        return {
            "access_token": session.session.access_token,
            "user": {"email": session.user.email, "id": str(session.user.id)},
        }
    except Exception as e:
        raise HTTPException(status_code=401, detail="Invalid email or password")


@app.patch("/me/telegram")
async def update_telegram(body: TelegramUpdate, authorization: str = Header(...)):
    """Save the user's Telegram credentials for deal alerts."""
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)

    update_data = {}
    if body.telegram_bot_token is not None:
        update_data["telegram_bot_token"] = body.telegram_bot_token
    if body.telegram_chat_id is not None:
        update_data["telegram_chat_id"] = body.telegram_chat_id

    if update_data:
        supabase.table("users").update(update_data).eq("id", user["id"]).execute()

    return {"updated": True}


# ─── ROUTES — PUBLIC ─────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "ARCHIVR API"}


@app.post("/checkout")
async def create_checkout(body: CheckoutRequest):
    """Create a Stripe Checkout session for new subscribers."""
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            customer_email=body.email,
            success_url=body.success_url + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=body.cancel_url,
            metadata={"email": body.email},
        )
        return {"checkout_url": session.url}
    except Exception as e:
        log.error(f"Checkout error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe events — activate/deactivate subscriptions."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        log.error(f"Webhook signature error: {e}")
        raise HTTPException(status_code=400, detail="Invalid signature")

    etype = event["type"]
    data  = event["data"]["object"]

    if etype == "checkout.session.completed":
        _handle_new_subscription(data)

    elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
        _handle_subscription_change(data)

    return {"received": True}


def _handle_new_subscription(session: dict):
    """New subscriber — create or activate user in Supabase."""
    email      = session.get("customer_email") or session.get("metadata", {}).get("email")
    customer   = session.get("customer")
    sub_id     = session.get("subscription")

    if not email:
        log.warning("Webhook: no email on checkout session")
        return

    existing = supabase.table("users").select("id").eq("email", email).execute()

    if existing.data:
        supabase.table("users").update({
            "subscription_status": "active",
            "stripe_customer_id": customer,
            "stripe_subscription_id": sub_id,
        }).eq("email", email).execute()
        log.info(f"Subscription activated: {email}")
    else:
        # New user — sign them up in Supabase Auth
        auth_user = supabase.auth.admin.create_user({
            "email": email,
            "email_confirm": True,
            "password": _random_temp_password(),
        })
        supabase.table("users").insert({
            "id": auth_user.user.id,
            "email": email,
            "subscription_status": "active",
            "stripe_customer_id": customer,
            "stripe_subscription_id": sub_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        log.info(f"New user created: {email}")

        # Send magic link so they can set password
        supabase.auth.admin.generate_link({
            "type": "magiclink",
            "email": email,
        })


def _handle_subscription_change(sub: dict):
    """Update subscription status when Stripe fires an update/delete event."""
    customer = sub.get("customer")
    status   = sub.get("status", "inactive")

    supabase.table("users").update({
        "subscription_status": status,
    }).eq("stripe_customer_id", customer).execute()

    log.info(f"Subscription updated for customer {customer}: {status}")


def _random_temp_password() -> str:
    import secrets, string
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(24))

# ─── ROUTES — AUTHENTICATED ──────────────────────────────────────────────────

@app.post("/analyse")
async def analyse_listing(
    body: AnalyseRequest,
    authorization: str = Header(...),
):
    """Extension calls this to analyse a listing page."""
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)
    require_active_subscription(user)
    check_usage_limit(user)

    try:
        result = run_ai_analysis(body.dict())
    except json.JSONDecodeError:
        raise HTTPException(status_code=500, detail="AI returned invalid JSON")
    except Exception as e:
        log.error(f"Analysis error: {e}")
        raise HTTPException(status_code=500, detail="Analysis failed")

    # Save to history
    try:
        supabase.table("analyses").insert({
            "user_id": user["id"],
            "listing_title": body.title,
            "listing_url": body.url,
            "platform": body.platform,
            "listed_price": body.price,
            "result": result,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.warning(f"Failed to save analysis to history: {e}")

    return result


@app.get("/alerts")
async def get_alerts(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)
    require_active_subscription(user)

    rows = (
        supabase.table("alerts")
        .select("*")
        .eq("user_id", user["id"])
        .eq("active", True)
        .execute()
    )
    return rows.data


@app.post("/alerts")
async def create_alert(
    body: AlertCreate,
    authorization: str = Header(...),
):
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)
    require_active_subscription(user)

    # Check alert limit for this tier
    existing = supabase.table("alerts").select("id", count="exact").eq("user_id", user["id"]).eq("active", True).execute()
    check_alert_limit(user, existing.count or 0)

    row = supabase.table("alerts").insert({
        "user_id":       user["id"],
        "brand":         body.brand,
        "keywords":      body.keywords,
        "max_price_gbp": body.max_price_gbp,
        "size":          body.size,
        "active":        True,
        "created_at":    datetime.now(timezone.utc).isoformat(),
    }).execute()

    return row.data[0]


@app.delete("/alerts/{alert_id}")
async def delete_alert(alert_id: str, authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)

    supabase.table("alerts").update({"active": False}).eq("id", alert_id).eq("user_id", user["id"]).execute()
    return {"deleted": True}


@app.get("/analyses")
async def get_analysis_history(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)
    require_active_subscription(user)

    rows = (
        supabase.table("analyses")
        .select("*")
        .eq("user_id", user["id"])
        .order("created_at", desc=True)
        .limit(50)
        .execute()
    )
    return rows.data


@app.get("/me")
async def get_me(authorization: str = Header(...)):
    token = authorization.replace("Bearer ", "").strip()
    user  = get_user_from_token(token)
    usage = get_monthly_usage(user["id"])
    tier  = get_user_tier(user)
    limits = TIER_LIMITS.get(tier, TIER_LIMITS["pro"])
    return {
        "email": user["email"],
        "subscription_status": user["subscription_status"],
        "created_at": user["created_at"],
        "tier": tier,
        "analyses_used": usage,
        "analyses_limit": limits["analyses_per_month"],
        "alerts_limit": limits["alerts_max"],
    }

# ─── SCRAPER ─────────────────────────────────────────────────────────────────

_seen_listings: set[str] = set()


def extract_price_gbp(text: str) -> float | None:
    if not text:
        return None
    gbp = re.findall(r"£\s*(\d[\d,]*\.?\d*)", str(text))
    if gbp:
        return round(float(gbp[0].replace(",", "")), 2)
    usd = re.findall(r"\$\s*(\d[\d,]*\.?\d*)", str(text))
    if usd:
        return round(float(usd[0].replace(",", "")) * 0.79, 2)
    eur = re.findall(r"€\s*(\d[\d,]*\.?\d*)", str(text))
    if eur:
        return round(float(eur[0].replace(",", "")) * 0.86, 2)
    return None


def send_telegram_alert(message: str, chat_id: str, bot_token: str):
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }, timeout=10)
    except Exception as e:
        log.error(f"Telegram error: {e}")


def build_telegram_message(title, price, platform, analysis, url, alert) -> str:
    emoji = {"BUY NOW": "🟢", "WORTH IT": "🟡", "WAIT": "🟠", "SKIP": "🔴"}.get(
        analysis.get("verdict", ""), "⚪"
    )
    japan = "\n🇯🇵 <b>JAPAN ARBITRAGE</b>" if analysis.get("japanArbitrage") else ""
    return f"""{emoji} <b>ARCHIVR DEAL ALERT</b>

<b>{title[:80]}</b>

💰 Listed: <b>£{price}</b>
📈 Flip Score: <b>{analysis.get('flipScore', '?')}/100</b>
⚡ Sell Speed: <b>{analysis.get('sellSpeed', '?')}/100</b>
💵 Est. Profit: <b>+£{analysis.get('estimatedProfit', '?')}</b>
🏷 Sell At: <b>£{analysis.get('estimatedSellPrice', '?')}</b>
📍 {platform}
🎯 <b>{analysis.get('verdict', '?')}</b> — {analysis.get('reason', '')}
{japan}

🔗 <a href="{url}">View Listing</a>
<i>ARCHIVR · {datetime.now().strftime('%H:%M %d %b')}</i>"""


def scraper_loop():
    """Background thread — scans RSS feeds for all active users' alerts."""
    log.info("Scraper thread started")
    time.sleep(30)  # Let the server boot first

    while True:
        try:
            _run_scraper_cycle()
        except Exception as e:
            log.error(f"Scraper cycle error: {e}")
        time.sleep(SCAN_INTERVAL_MINUTES * 60)


def _fetch_mercari_jp(query: str, max_price_gbp: float | None = None) -> list[dict]:
    """Fetch Mercari Japan listings using Playwright headless Chrome."""
    from playwright.sync_api import sync_playwright
    JPY_TO_GBP = 0.0052
    listings = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="en-GB",
            )
            page = context.new_page()
            url = f"https://jp.mercari.com/search?keyword={url_quote(query)}&status=on_sale&sort=created_time&order=desc"
            page.goto(url, wait_until="networkidle", timeout=25000)
            page.wait_for_timeout(3000)

            all_links = page.eval_on_selector_all(
                "a[href*='/item/m']",
                "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))"
            )
            content = page.content()
            browser.close()

        # Extract item IDs from links
        item_ids = []
        seen_ids = set()
        for link in all_links:
            m = re.search(r"/(m\d+)", link.get("href", ""))
            if m and m.group(1) not in seen_ids:
                seen_ids.add(m.group(1))
                item_ids.append(m.group(1))

        # Extract prices and names from page JSON
        names_map  = {}
        prices_map = {}
        for pat in [
            r'"id":"(m\d+)"[^}]*?"name":"([^"]{5,100})"[^}]*?"price":(\d+)',
            r'"itemId":"(m\d+)"[^}]*?"itemName":"([^"]{5,100})"[^}]*?"price":(\d+)',
        ]:
            for item_id, name, price in re.findall(pat, content):
                names_map[item_id]  = name
                prices_map[item_id] = int(price)

        for item_id in item_ids[:20]:
            name      = names_map.get(item_id, "")
            price_jpy = prices_map.get(item_id, 0)
            if not name or not price_jpy or price_jpy < 100:
                continue
            price_gbp = round(price_jpy * JPY_TO_GBP, 2)
            if max_price_gbp and price_gbp > max_price_gbp:
                continue
            listings.append({
                "title":    name,
                "price":    price_gbp,
                "price_jpy": price_jpy,
                "link":     f"https://jp.mercari.com/item/{item_id}",
                "platform": "Mercari JP",
            })
    except Exception as e:
        log.error(f"Mercari JP fetch error: {e}")

    return listings[:10]



# ─── PLAYWRIGHT SCRAPER ──────────────────────────────────────────────────────
# Uses headless Chrome via Playwright — installed in Docker on Railway
# Smart delays and user-agent rotation to avoid rate limiting


import random

PLAYWRIGHT_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

def _fetch_mercari_jp(query: str, max_price_gbp: float | None = None) -> list[dict]:
    """Fetch Mercari Japan listings using Playwright with smart delays."""
    from playwright.sync_api import sync_playwright
    JPY_TO_GBP = 0.0052
    listings = []

    # Random delay before each request to avoid rate limiting
    time.sleep(random.uniform(8, 15))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(PLAYWRIGHT_USER_AGENTS),
                locale="en-GB",
                viewport={"width": 1280, "height": 800},
                extra_http_headers={
                    "Accept-Language": "en-GB,en;q=0.9",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                }
            )
            page = context.new_page()
            url = f"https://jp.mercari.com/search?keyword={url_quote(query)}&status=on_sale&sort=created_time&order=desc"

            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(random.randint(2000, 4000))

            all_links = page.eval_on_selector_all(
                "a[href*='/item/m']",
                "els => els.map(e => ({href: e.href, text: e.innerText.trim()}))"
            )
            content = page.content()
            browser.close()

        item_ids = []
        seen_ids = set()
        for link in all_links:
            m = re.search(r"/(m\d+)", link.get("href", ""))
            if m and m.group(1) not in seen_ids:
                seen_ids.add(m.group(1))
                item_ids.append(m.group(1))

        names_map  = {}
        prices_map = {}
        for pat in [
            r'"id":"(m\d+)"[^}]*?"name":"([^"]{5,100})"[^}]*?"price":(\d+)',
            r'"itemId":"(m\d+)"[^}]*?"itemName":"([^"]{5,100})"[^}]*?"price":(\d+)',
        ]:
            for item_id, name, price in re.findall(pat, content):
                names_map[item_id]  = name
                prices_map[item_id] = int(price)

        for item_id in item_ids[:20]:
            name      = names_map.get(item_id, "")
            price_jpy = prices_map.get(item_id, 0)
            if not name or not price_jpy or price_jpy < 100:
                continue
            price_gbp = round(price_jpy * JPY_TO_GBP, 2)
            if max_price_gbp and price_gbp > max_price_gbp:
                continue
            listings.append({
                "title":    name,
                "price":    price_gbp,
                "price_jpy": price_jpy,
                "link":     f"https://jp.mercari.com/item/{item_id}",
                "platform": "Mercari JP",
            })
    except Exception as e:
        log.error(f"Mercari JP fetch error: {e}")

    return listings[:10]


def _search_all_platforms(query: str, max_price_gbp: float | None = None) -> list[dict]:
    """Search all available platforms."""
    all_listings = []
    all_listings.extend(_fetch_mercari_jp(query, max_price_gbp))
    return all_listings

def _run_scraper_cycle():
    """Scan all platforms via Apify for all active user alerts."""
    rows = (
        supabase.table("alerts")
        .select("*, users(telegram_bot_token, telegram_chat_id)")
        .eq("active", True)
        .execute()
    )
    alerts = rows.data
    if not alerts:
        return

    log.info(f"Scraper: scanning {len(alerts)} alerts across all platforms")

    # Deduplicate search terms across all users to minimise Apify calls
    # e.g. 100 users all watching Stone Island = 1 search, not 100
    search_map: dict[str, list[dict]] = {}
    for alert in alerts:
        brand    = alert["brand"]
        keywords = alert.get("keywords", [])
        terms    = [brand] + [f"{brand} {kw}" for kw in keywords[:1]]
        for term in terms[:2]:
            if term not in search_map:
                search_map[term] = []
            search_map[term].append(alert)

    log.info(f"Scraper: {len(search_map)} unique search terms")

    for term, matching_alerts in search_map.items():
        try:
            max_price = max(a.get("max_price_gbp", 9999) for a in matching_alerts)
            listings  = _search_all_platforms(term, max_price)
            log.info(f"  '{term}': {len(listings)} listings across all platforms")

            for listing in listings:
                title = listing["title"]
                lid   = hashlib.md5(f"{listing.get('link','')}{title}".encode()).hexdigest()

                if lid in _seen_listings:
                    continue
                _seen_listings.add(lid)

                # Check which alerts this listing matches
                for alert in matching_alerts:
                    brand     = alert["brand"]
                    keywords  = alert.get("keywords", [])
                    max_price = alert.get("max_price_gbp", 9999)
                    user_data = alert.get("users") or {}
                    bot_token = user_data.get("telegram_bot_token") or TELEGRAM_BOT_TOKEN
                    chat_id   = user_data.get("telegram_chat_id") or TELEGRAM_CHAT_ID

                    # Price check
                    if listing["price"] > max_price:
                        continue

                    # Keyword match
                    title_lower = title.lower()
                    if not (brand.lower() in title_lower or
                            any(k.lower() in title_lower for k in keywords)):
                        continue

                    try:
                        analysis = run_quick_analysis(
                            title, listing["price"], listing["platform"], alert
                        )
                    except Exception as e:
                        log.error(f"Analysis error: {e}")
                        continue

                    if (analysis.get("flipScore", 0) >= MIN_FLIP_SCORE and
                            analysis.get("verdict") in ["BUY NOW", "WORTH IT"]):
                        msg = build_telegram_message(
                            title, listing["price"], listing["platform"],
                            analysis, listing.get("link", ""), alert
                        )
                        send_telegram_alert(msg, chat_id, bot_token)
                        log.info(f"Alert sent to user: {analysis['verdict']} — {title[:50]}")

                time.sleep(0.2)

        except Exception as e:
            log.error(f"Scraper cycle error for '{term}': {e}")
            continue

    # Trim seen set
    if len(_seen_listings) > 10000:
        trimmed = list(_seen_listings)[-5000:]
        _seen_listings.clear()
        _seen_listings.update(trimmed)


# ─── STARTUP ─────────────────────────────────────────────────────────────────

@app.on_event("startup")
def startup():
    log.info("ARCHIVR API starting up")
    t = threading.Thread(target=scraper_loop, daemon=True)
    t.start()
    log.info("Scraper thread launched")
