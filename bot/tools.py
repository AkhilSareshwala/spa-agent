import asyncio
import random
import string
from datetime import datetime, timedelta
from typing import List

from loguru import logger
from pipecat.services.llm_service import FunctionCallParams

from .config import SPA_TZ, SPA_OPEN_HOUR, SPA_CLOSE_HOUR, UPSELL_MAP
from .db import (
    db_get_service, db_get_therapist, db_get_addon,
    db_addon_totals, db_addon_names,
    db_save_booking, db_get_booking, db_update_booking, db_delete_booking,
)
from .calendar import (
    get_calendar_service, cal_get_free_slots,
    cal_insert_event, cal_find_event, cal_update_event, cal_delete_event,
    parse_google_dt,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def new_booking_id() -> str:
    suffix = "".join(random.choices(string.ascii_uppercase, k=3))
    return "SPA" + datetime.now(SPA_TZ).strftime("%Y%m%d") + suffix


def clean_addon_ids(raw) -> List[str]:
    """Coerce any LLM output for addon_ids into a clean List[str]."""
    import json
    if raw is None:
        return []
    if isinstance(raw, list):
        return [i.strip() for i in raw
                if isinstance(i, str) and i.strip().lower() not in ("", "none", "null")]
    if isinstance(raw, str):
        s = raw.strip()
        if s.lower() in ("", "none", "null", "[]"):
            return []
        if s.startswith("["):
            try:
                return clean_addon_ids(json.loads(s))
            except Exception:
                pass
        if "," in s:
            return [x.strip() for x in s.split(",")
                    if x.strip().lower() not in ("", "none", "null")]
        return [s]
    return []


# ═════════════════════════════════════════════════════════════════════════════
# TOOL HANDLERS
# ═════════════════════════════════════════════════════════════════════════════

async def check_availability(
    params: FunctionCallParams,
    service_id: str,
    date_iso: str,
    therapist_name: str,
):
    """Check free appointment slots for a service on a given date.

    Args:
        service_id: Service id e.g. 'sw60'.
        date_iso: Date to check in YYYY-MM-DD format.
        therapist_name: Preferred therapist name or 'any'.
    """
    svc = await db_get_service(service_id)
    if not svc:
        await params.result_callback(f"Unknown service id '{service_id}'.")
        return

    therapist = await db_get_therapist(therapist_name, service_id)
    if not therapist:
        await params.result_callback(f"No therapist available for '{service_id}'.")
        return

    gcal = get_calendar_service()
    if not gcal:
        await params.result_callback(
            f"Mock slots with {therapist['name']} on {date_iso} (calendar offline): "
            "9:00 AM, 10:00 AM, 11:00 AM, 2:00 PM, 3:00 PM, 4:00 PM"
        )
        return

    try:
        free_slots = await cal_get_free_slots(
            gcal, svc["duration"], date_iso, SPA_OPEN_HOUR, SPA_CLOSE_HOUR
        )
        slots_str = ", ".join(free_slots[:8]) if free_slots else "No slots available."
        logger.info(f"[check_availability] {date_iso} {therapist['name']}: {slots_str}")
        await params.result_callback(
            f"Available slots with {therapist['name']} on {date_iso}: {slots_str}"
        )
    except Exception as e:
        logger.error(f"Calendar freebusy error: {e}")
        await params.result_callback(f"Calendar error: {e}")


async def create_booking(
    params: FunctionCallParams,
    client_name: str,
    client_contact: str,
    service_id: str,
    datetime_iso: str,
    therapist_name: str,
    addon_ids: List[str],
):
    """Create a new appointment on Google Calendar and save to database.

    Args:
        client_name: Client full name. Must be a real name, never empty.
        client_contact: Phone or email. Must be real, never empty.
        service_id: Service id e.g. 'sw60'.
        datetime_iso: ISO local datetime e.g. '2025-05-19T10:00:00'.
        therapist_name: Therapist name or 'any'.
        addon_ids: Addon id list. Must be a JSON array. Use [] for none.
    """
    if not client_name or client_name.strip().lower() in ("", "unknown", "none"):
        await params.result_callback(
            "ERROR: Client name missing. Ask the caller for their full name before booking."
        )
        return
    if not client_contact or client_contact.strip().lower() in ("", "unknown", "none"):
        await params.result_callback(
            "ERROR: Client contact missing. Ask the caller for their phone or email before booking."
        )
        return

    addon_ids = clean_addon_ids(addon_ids)

    svc = await db_get_service(service_id)
    if not svc:
        await params.result_callback(f"Booking failed: unknown service '{service_id}'.")
        return

    therapist = await db_get_therapist(therapist_name, service_id)
    if not therapist:
        await params.result_callback(f"Booking failed: no therapist for '{service_id}'.")
        return

    extra_price, extra_mins = await db_addon_totals(addon_ids)
    total_mins  = svc["duration"] + extra_mins
    total_price = svc["price"] + extra_price
    names       = await db_addon_names(addon_ids)
    bid         = new_booking_id()

    try:
        start_local = datetime.fromisoformat(datetime_iso)
        if start_local.tzinfo is None:
            start_local = start_local.replace(tzinfo=SPA_TZ)
    except ValueError:
        await params.result_callback(f"Invalid datetime '{datetime_iso}'.")
        return

    end_local = start_local + timedelta(minutes=total_mins)

    cal_body = {
        "summary": f"{svc['name']} — {client_name}",
        "description": (
            f"Client: {client_name}\n"
            f"Contact: {client_contact}\n"
            f"Therapist: {therapist['name']}\n"
            f"Add-ons: {', '.join(names) or 'None'}\n"
            f"Total: ₹{total_price}\n"
            f"Booking ID: {bid}"
        ),
        "start": {"dateTime": start_local.isoformat(), "timeZone": str(SPA_TZ)},
        "end":   {"dateTime": end_local.isoformat(),   "timeZone": str(SPA_TZ)},
        "extendedProperties": {"private": {
            "booking_id":     bid,
            "service_id":     service_id,
            "therapist_id":   therapist["id"],
            "client_contact": client_contact,
            "total_price":    str(total_price),
            "addon_ids":      ",".join(addon_ids),
        }},
    }

    booking_doc = {
        "booking_id":     bid,
        "client_name":    client_name,
        "client_contact": client_contact,
        "service_id":     service_id,
        "service_name":   svc["name"],
        "therapist_id":   therapist["id"],
        "therapist_name": therapist["name"],
        "start":          start_local.isoformat(),
        "end":            end_local.isoformat(),
        "addon_ids":      addon_ids,
        "addon_names":    names,
        "total_price":    total_price,
        "status":         "confirmed",
    }

    async def _save_mongo():
        await db_save_booking(booking_doc)
        logger.info(f"✅ Booking saved to MongoDB: {bid}")

    async def _save_calendar():
        gcal = get_calendar_service()
        if not gcal:
            return
        try:
            await cal_insert_event(gcal, cal_body)
            logger.info(f"✅ Booking saved to Google Calendar: {bid}")
        except Exception as e:
            logger.error(f"Calendar insert failed: {e}")

    await asyncio.gather(_save_mongo(), _save_calendar())

    display = start_local.astimezone(SPA_TZ).strftime("%A %b %d at %I:%M %p")
    await params.result_callback(
        f"Booking confirmed. ID: {bid}. "
        f"{client_name} booked {svc['name']} with {therapist['name']} on {display}. "
        f"Duration: {total_mins} min. Total: ₹{total_price}. "
        f"Add-ons: {', '.join(names) or 'none'}."
    )


async def add_addon_to_booking(
    params: FunctionCallParams,
    booking_id: str,
    addon_id: str,
):
    """Add an add-on to an existing confirmed booking. Use this when caller accepts upsell.
    Do NOT call create_booking again — this updates the existing event.

    Args:
        booking_id: The confirmed booking ID e.g. 'SPA20260518XYZ'.
        addon_id: The addon id to add e.g. 'aroma'.
    """
    addon = await db_get_addon(addon_id)
    if not addon:
        await params.result_callback(f"Unknown addon '{addon_id}'.")
        return

    booking = await db_get_booking(booking_id)
    if not booking:
        await params.result_callback(f"Booking '{booking_id}' not found.")
        return

    old_total  = booking.get("total_price", 0)
    new_total  = old_total + addon["price"]
    new_addons = booking.get("addon_ids",   []) + [addon_id]
    new_names  = booking.get("addon_names", []) + [addon["name"]]

    mongo_update = {
        "addon_ids":   new_addons,
        "addon_names": new_names,
        "total_price": new_total,
    }
    if addon["extra_min"]:
        old_end = datetime.fromisoformat(booking["end"])
        mongo_update["end"] = (old_end + timedelta(minutes=addon["extra_min"])).isoformat()

    async def _addon_mongo():
        await db_update_booking(booking_id, mongo_update)
        logger.info(f"✅ Addon '{addon_id}' added to {booking_id} in MongoDB")

    async def _addon_calendar():
        gcal = get_calendar_service()
        if not gcal:
            return
        try:
            ev = await cal_find_event(gcal, booking_id)
            if not ev:
                logger.warning(f"Booking {booking_id} not found in Google Calendar for addon update")
                return

            desc = ev.get("description", "")
            if "Add-ons: None" in desc:
                desc = desc.replace("Add-ons: None", f"Add-ons: {addon['name']}")
            elif "Add-ons:" in desc:
                line_start = desc.find("Add-ons:")
                line_end   = desc.find("\n", line_start)
                old_line   = desc[line_start:line_end]
                desc       = desc.replace(old_line, old_line + f", {addon['name']}")

            priv = ev.get("extendedProperties", {}).get("private", {})
            old_cal_total = int(priv.get("total_price", "0"))
            if old_cal_total == 0 and "Total: ₹" in desc:
                try:
                    t_start       = desc.find("Total: ₹") + 8
                    t_end         = desc.find("\n", t_start)
                    old_cal_total = int(desc[t_start:t_end].strip())
                except Exception:
                    pass
            new_cal_total = old_cal_total + addon["price"]

            if "Total: ₹" in desc:
                t_start = desc.find("Total: ₹")
                t_end   = desc.find("\n", t_start)
                desc    = desc[:t_start] + f"Total: ₹{new_cal_total}" + desc[t_end:]

            ev["description"] = desc
            if addon["extra_min"]:
                end_utc = parse_google_dt(ev["end"]["dateTime"])
                ev["end"]["dateTime"] = (end_utc + timedelta(minutes=addon["extra_min"])).isoformat()
            ev.setdefault("extendedProperties", {}).setdefault("private", {})
            ev["extendedProperties"]["private"]["total_price"] = str(new_cal_total)

            await cal_update_event(gcal, ev["id"], ev)
            logger.info(f"✅ Addon '{addon_id}' added to {booking_id} in Google Calendar")
        except Exception as e:
            logger.error(f"add_addon calendar error: {e}")

    await asyncio.gather(_addon_mongo(), _addon_calendar())

    extra = f" Duration extended by {addon['extra_min']} min." if addon["extra_min"] else ""
    await params.result_callback(
        f"Added {addon['name']} to booking {booking_id}. "
        f"New total: ₹{new_total}.{extra}"
    )


async def reschedule_booking(
    params: FunctionCallParams,
    booking_id: str,
    new_datetime_iso: str,
):
    """Reschedule an existing booking to a new date and time.

    Args:
        booking_id: Existing booking ID e.g. 'SPA20250614XYZ'.
        new_datetime_iso: New local ISO datetime e.g. '2025-06-20T11:00:00'.
    """
    booking = await db_get_booking(booking_id)
    if not booking:
        await params.result_callback(f"Booking '{booking_id}' not found.")
        return

    ns = datetime.fromisoformat(new_datetime_iso)
    if ns.tzinfo is None:
        ns = ns.replace(tzinfo=SPA_TZ)

    old_start = datetime.fromisoformat(booking["start"])
    old_end   = datetime.fromisoformat(booking["end"])
    duration  = old_end - old_start
    new_end   = ns + duration

    async def _reschedule_mongo():
        await db_update_booking(booking_id, {
            "start": ns.isoformat(),
            "end":   new_end.isoformat(),
        })
        logger.info(f"✅ Booking {booking_id} rescheduled in MongoDB")

    async def _reschedule_calendar():
        gcal = get_calendar_service()
        if not gcal:
            return
        try:
            ev = await cal_find_event(gcal, booking_id)
            if not ev:
                logger.warning(f"Booking {booking_id} not found in Google Calendar for reschedule")
                return
            ev["start"]["dateTime"] = ns.isoformat()
            ev["end"]["dateTime"]   = new_end.isoformat()
            await cal_update_event(gcal, ev["id"], ev)
            logger.info(f"✅ Booking {booking_id} rescheduled in Google Calendar")
        except Exception as e:
            logger.error(f"Reschedule calendar error: {e}")

    await asyncio.gather(_reschedule_mongo(), _reschedule_calendar())

    await params.result_callback(
        f"Booking {booking_id} rescheduled to "
        f"{ns.astimezone(SPA_TZ).strftime('%A %b %d at %I:%M %p')}."
    )


async def cancel_booking(params: FunctionCallParams, booking_id: str):
    """Cancel an existing booking by its ID.

    Args:
        booking_id: Booking ID to cancel e.g. 'SPA20250614XYZ'.
    """
    booking = await db_get_booking(booking_id)
    if not booking:
        await params.result_callback(
            f"No booking found with ID {booking_id}. Please check the ID and try again."
        )
        return

    async def _cancel_mongo():
        await db_delete_booking(booking_id)
        logger.info(f"✅ Booking {booking_id} deleted from MongoDB")

    async def _cancel_calendar():
        gcal = get_calendar_service()
        if not gcal:
            return
        try:
            ev = await cal_find_event(gcal, booking_id)
            if ev:
                await cal_delete_event(gcal, ev["id"])
                logger.info(f"✅ Booking {booking_id} deleted from Google Calendar")
            else:
                logger.warning(f"Booking {booking_id} not found in Google Calendar")
        except Exception as e:
            logger.error(f"Cancel calendar error: {e}")

    await asyncio.gather(_cancel_mongo(), _cancel_calendar())

    client_name  = booking.get("client_name", "")
    service_name = booking.get("service_name", "")
    await params.result_callback(
        f"Booking {booking_id} for {client_name} ({service_name}) has been successfully cancelled."
    )


async def get_upsell_suggestion(params: FunctionCallParams, service_id: str):
    """Get a personalised add-on suggestion right after a booking is confirmed.
    Always call this immediately after create_booking succeeds.

    Args:
        service_id: The service that was just booked e.g. 'sw60'.
    """
    aid   = UPSELL_MAP.get(service_id)
    addon = await db_get_addon(aid) if aid else None
    if not addon:
        await params.result_callback("no_upsell")
        return
    await params.result_callback(
        f"Suggest this addon to the caller: "
        f"addon_id='{aid}' name='{addon['name']}' price=₹{addon['price']}. "
        f"Ask: 'Would you like to add our {addon['name']} for just ₹{addon['price']} more?'"
    )