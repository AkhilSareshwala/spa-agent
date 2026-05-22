import json
import asyncio
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from loguru import logger

from .config import GOOGLE_SERVICE_ACCOUNT_JSON, GOOGLE_CALENDAR_ID, SPA_TZ


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_calendar_service():
    """Returns an authenticated Google Calendar API service, or None if not configured."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        raw = GOOGLE_SERVICE_ACCOUNT_JSON
        if not raw:
            logger.warning("GOOGLE_SERVICE_ACCOUNT_JSON not set — calendar offline")
            return None

        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw),
            scopes=["https://www.googleapis.com/auth/calendar"],
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:
        logger.warning(f"Calendar auth skipped: {e}")
        return None


def cal_id() -> str:
    return GOOGLE_CALENDAR_ID


# ── Datetime helpers ──────────────────────────────────────────────────────────

def parse_google_dt(s: str) -> datetime:
    """Parse any Google Calendar datetime string → UTC-aware datetime."""
    s = s.strip()
    if "T" not in s:
        d = date.fromisoformat(s)
        return datetime(d.year, d.month, d.day, tzinfo=SPA_TZ).astimezone(ZoneInfo("UTC"))
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SPA_TZ)
    return dt.astimezone(ZoneInfo("UTC"))


def local_slot(dt_utc: datetime) -> str:
    """Format a UTC datetime as spa-local time string e.g. '10:00 AM'."""
    return dt_utc.astimezone(SPA_TZ).strftime("%I:%M %p")


# ── Read operations ───────────────────────────────────────────────────────────

async def cal_get_free_slots(
    gcal,
    service_duration: int,
    date_iso: str,
    open_hour: int,
    close_hour: int,
) -> list[str]:
    """
    Query freebusy and return list of free slot strings in local time.
    Raises on calendar error so caller can handle.
    """
    d = date.fromisoformat(date_iso)
    window_start = datetime(d.year, d.month, d.day, open_hour,  0, tzinfo=SPA_TZ).astimezone(ZoneInfo("UTC"))
    window_end   = datetime(d.year, d.month, d.day, close_hour, 0, tzinfo=SPA_TZ).astimezone(ZoneInfo("UTC"))

    loop = asyncio.get_event_loop()
    fb = await loop.run_in_executor(
        None,
        lambda: gcal.freebusy().query(body={
            "timeMin": window_start.isoformat(),
            "timeMax": window_end.isoformat(),
            "items":   [{"id": cal_id()}],
        }).execute()
    )

    busy_utc = [
        (parse_google_dt(b["start"]), parse_google_dt(b["end"]))
        for b in fb["calendars"][cal_id()]["busy"]
    ]

    free_slots = []
    slot = window_start
    while slot + timedelta(minutes=service_duration) <= window_end:
        slot_end = slot + timedelta(minutes=service_duration)
        clash = any(bs < slot_end and be > slot for bs, be in busy_utc)
        if not clash:
            free_slots.append(local_slot(slot))
        slot += timedelta(minutes=30)

    return free_slots


# ── Write operations (all run in executor — Google SDK is sync) ───────────────

async def cal_insert_event(gcal, body: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: gcal.events().insert(calendarId=cal_id(), body=body).execute()
    )


async def cal_find_event(gcal, booking_id: str) -> dict | None:
    loop = asyncio.get_event_loop()
    items = await loop.run_in_executor(
        None,
        lambda: gcal.events().list(
            calendarId=cal_id(),
            privateExtendedProperty=f"booking_id={booking_id}",
        ).execute().get("items", [])
    )
    return items[0] if items else None


async def cal_update_event(gcal, event_id: str, body: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: gcal.events().update(
            calendarId=cal_id(), eventId=event_id, body=body
        ).execute()
    )


async def cal_delete_event(gcal, event_id: str):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: gcal.events().delete(
            calendarId=cal_id(), eventId=event_id
        ).execute()
    )