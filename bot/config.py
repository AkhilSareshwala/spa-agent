import os
from zoneinfo import ZoneInfo
from datetime import datetime
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Identity ──────────────────────────────────────────────────────────────────
AGENT_NAME = "Luna"
SPA_NAME   = "Serenity Spa"
SPA_PHONE  = "+91-800-SERENE"

# ── Timezone & hours ──────────────────────────────────────────────────────────
SPA_TZ         = ZoneInfo("Asia/Kolkata")
TODAY          = datetime.now(SPA_TZ).strftime("%A, %B %d %Y")
SPA_OPEN_HOUR  = 9
SPA_CLOSE_HOUR = 20

# ── API keys ──────────────────────────────────────────────────────────────────
DEEPGRAM_API_KEY   = os.getenv("DEEPGRAM_API_KEY")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
CARTESIA_API_KEY   = os.getenv("CARTESIA_API_KEY")
GOOGLE_API_KEY     = os.getenv("GOOGLE_API_KEY")
MONGODB_URI        = os.getenv("MONGODB_URI")
GOOGLE_CALENDAR_ID          = os.getenv("GOOGLE_CALENDAR_ID", "primary")
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

# ── Upsell map: service_id → best addon_id ───────────────────────────────────
UPSELL_MAP = {
    "sw60": "aroma",
    "dt60": "sports_wrap",
    "hs75": "aroma",
    "cf60": "eye_collagen",
    "cr90": "rose_bath",
}