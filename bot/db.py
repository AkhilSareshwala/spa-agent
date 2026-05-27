import json
from datetime import datetime, timedelta
from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient

from .config import MONGODB_URI, SPA_TZ
from .calendar import get_calendar_service, cal_find_event, parse_google_dt

# ── Connection singleton ──────────────────────────────────────────────────────
_mongo_client = None
_mongo_db     = None


def get_db():
    global _mongo_client, _mongo_db
    if _mongo_db is None:
        if not MONGODB_URI:
            raise RuntimeError("MONGODB_URI not set")
        _mongo_client = AsyncIOMotorClient(MONGODB_URI)
        _mongo_db     = _mongo_client["serenity_spa"]
    return _mongo_db


# ── Service lookups ───────────────────────────────────────────────────────────

async def db_get_service(service_id: str) -> dict | None:
    doc = await get_db().services.find_one({"_id": service_id})
    if doc:
        doc["id"] = doc.pop("_id")
    return doc


async def db_get_therapist(name: str, service_id: str) -> dict | None:
    query = {"services": service_id}
    if name and name.lower() != "any":
        query["name"] = {"$regex": f"^{name}$", "$options": "i"}
    doc = await get_db().therapists.find_one(query)
    if doc:
        doc["id"] = doc.pop("_id")
    return doc


async def db_get_addon(addon_id: str) -> dict | None:
    return await get_db().addons.find_one({"_id": addon_id})


async def db_addon_totals(ids: list) -> tuple[int, int]:
    price = mins = 0
    for aid in ids:
        a = await db_get_addon(aid)
        if a:
            price += a["price"]
            mins  += a["extra_min"]
    return price, mins


async def db_addon_names(ids: list) -> list[str]:
    names = []
    for aid in ids:
        a = await db_get_addon(aid)
        if a:
            names.append(a["name"])
    return names


# ── Booking CRUD ──────────────────────────────────────────────────────────────

async def db_save_booking(booking: dict):
    doc = {**booking, "created_at": datetime.now(SPA_TZ)}
    await get_db().bookings.insert_one(doc)


def normalize_booking_id(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    cleaned = raw.strip().upper()
    cleaned = cleaned.replace(" ", "")
    cleaned = cleaned.rstrip(".",)
    if cleaned.startswith("SPA") and len(cleaned) >= 14:
        return cleaned
    return ""


async def db_get_booking(booking_id: str) -> dict | None:
    booking_id = normalize_booking_id(booking_id)
    if not booking_id:
        return None
    # Primary: try MongoDB
    doc = await get_db().bookings.find_one({"booking_id": booking_id})
    if doc:
        return doc

    # Fallback: try Google Calendar event with private extended property booking_id
    try:
        gcal = get_calendar_service()
        if not gcal:
            return None
        ev = await cal_find_event(gcal, booking_id)
        if not ev:
            return None

        # Extract start/end (could be dateTime or date)
        start_raw = ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date")
        end_raw = ev.get("end", {}).get("dateTime") or ev.get("end", {}).get("date")

        # Parse Google datetimes to local SPA_TZ ISO strings when possible
        try:
            start_iso = parse_google_dt(start_raw).astimezone(SPA_TZ).isoformat()
        except Exception:
            start_iso = start_raw
        try:
            end_iso = parse_google_dt(end_raw).astimezone(SPA_TZ).isoformat()
        except Exception:
            end_iso = end_raw

        priv = ev.get("extendedProperties", {}).get("private", {})
        desc = ev.get("description", "") or ""

        booking = {
            "booking_id": booking_id,
            "client_name": None,
            "client_contact": priv.get("client_contact") or "",
            "service_id": priv.get("service_id"),
            "service_name": None,
            "therapist_id": priv.get("therapist_id"),
            "therapist_name": None,
            "start": start_iso,
            "end": end_iso,
            "addon_ids": priv.get("addon_ids", "").split(",") if priv.get("addon_ids") else [],
            "addon_names": [],
            "total_price": int(priv.get("total_price")) if priv.get("total_price") else 0,
            "status": "confirmed",
        }

        # Try to pull client name / contact / service from description or summary
        for line in desc.splitlines():
            if line.strip().startswith("Client:") and not booking["client_name"]:
                booking["client_name"] = line.split("Client:", 1)[1].strip()
            if line.strip().startswith("Contact:") and not booking["client_contact"]:
                booking["client_contact"] = line.split("Contact:", 1)[1].strip()

        summary = ev.get("summary", "") or ""
        if "—" in summary:
            parts = summary.split("—", 1)
            if not booking["service_name"]:
                booking["service_name"] = parts[0].strip()
            if not booking["client_name"] and len(parts) > 1:
                booking["client_name"] = parts[1].strip()

        return booking
    except Exception:
        return None


async def db_update_booking(booking_id: str, update: dict):
    await get_db().bookings.update_one(
        {"booking_id": booking_id}, {"$set": update}
    )


async def db_get_therapist_bookings(therapist_id: str, date_iso: str) -> list[dict]:
    bookings = []
    query = {
        "therapist_id": therapist_id,
        "start": {"$regex": f"^{date_iso}"},
    }
    async for doc in get_db().bookings.find(query):
        bookings.append(doc)
    return bookings


async def db_delete_booking(booking_id: str):
    await get_db().bookings.delete_one({"booking_id": booking_id})


# ── Menu loader (used by prompt builder) ─────────────────────────────────────

async def db_load_menu() -> tuple[list, list, list]:
    """Returns (services, addons, therapists) from MongoDB."""
    db = get_db()
    services   = []
    addons     = []
    therapists = []

    async for doc in db.services.find():
        doc["id"] = doc.pop("_id")
        services.append(doc)

    async for doc in db.addons.find():
        doc["id"] = doc.pop("_id")
        addons.append(doc)

    async for doc in db.therapists.find():
        doc["id"] = doc.pop("_id")
        therapists.append(doc)

    return services, addons, therapists