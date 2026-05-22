import json
from datetime import datetime, timedelta
from typing import List

from loguru import logger
from motor.motor_asyncio import AsyncIOMotorClient

from .config import MONGODB_URI, SPA_TZ

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


async def db_get_booking(booking_id: str) -> dict | None:
    return await get_db().bookings.find_one({"booking_id": booking_id})


async def db_update_booking(booking_id: str, update: dict):
    await get_db().bookings.update_one(
        {"booking_id": booking_id}, {"$set": update}
    )


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