"""Employee Attendance & Analytics API for the HROne assignment.

The database is authoritative: analytics are computed with MongoDB aggregation
pipelines, while the API converts BSON datetimes to/from epoch milliseconds.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
STATUSES = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_employee_attendance_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING)], name="attendance_date_status")


@app.on_event("startup")
def startup() -> None:
    create_indexes()


def err(status: int, detail: str):
    raise HTTPException(status_code=status, detail=detail)


def parse_date(value: str, field: str = "date") -> date:
    try:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        err(422, f"{field} must be a valid YYYY-MM-DD date")


def parse_month(value: str) -> tuple[date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        err(422, "month must use YYYY-MM format")
    year, month = map(int, value.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return start, end


def epoch_to_dt(value: int | None, default_now: bool = False) -> datetime:
    if value is None:
        if default_now:
            now = datetime.now(UTC).replace(microsecond=0)
            return now
        return None  # type: ignore[return-value]
    if type(value) is not int or not MIN_EPOCH_MS <= value <= MAX_EPOCH_MS:
        err(422, "timestamp must be an epoch-milliseconds integer")
    try:
        return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
    except (OverflowError, OSError, ValueError):
        err(422, "invalid timestamp")


def dt_to_ms(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp() * 1000)


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1") if places == 0 else Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def public_doc(doc: dict) -> dict:
    """Convert MongoDB document values to the public API contract."""
    if not doc:
        return doc
    result = {k: v for k, v in doc.items() if k != "_id"}
    for key in ("created_at", "punch_in", "punch_out", "at"):
        if key in result and isinstance(result[key], datetime):
            result[key] = dt_to_ms(result[key])
    # Attendance documents have a date field; employee documents do not.
    if "date" in result:
        hist = result.get("history") or []
        for entry in hist:
            if isinstance(entry.get("at"), datetime):
                entry["at"] = dt_to_ms(entry["at"])
            for change in (entry.get("changes") or {}).values():
                for side in ("from", "to"):
                    if isinstance(change.get(side), datetime):
                        change[side] = dt_to_ms(change[side])
        result["history"] = hist
        result.setdefault("late_minutes", 0)
        result.setdefault("overtime_minutes", 0)
        result.setdefault("half_day", False)
    return result


def shift_anchor(attendance_date: str, shift_time: str, overnight_next: bool = False) -> datetime:
    d = date.fromisoformat(attendance_date)
    if overnight_next:
        d += timedelta(days=1)
    h, m = map(int, shift_time.split(":"))
    return datetime.combine(d, time(h, m), IST).astimezone(UTC)


def attendance_day(punch_in: datetime, employee: dict) -> str:
    local = punch_in.astimezone(IST)
    day = local.date()
    start = employee["shift_start"]
    end = employee["shift_end"]
    if end <= start and local.strftime("%H:%M") < end:
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    start = shift_anchor(attendance_date, shift_start)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_overtime(punch_out: datetime, shift_end: str, attendance_date: str, shift_start: str) -> int:
    overnight = shift_end <= shift_start
    end = shift_anchor(attendance_date, shift_end, overnight_next=overnight)
    seconds = int((punch_out - end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def derive_fields(doc: dict, employee: dict) -> dict:
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if doc.get("status") in ("ABSENT", "LEAVE"):
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    late = compute_late_minutes(pi, employee["shift_start"], doc["date"]) if pi else 0
    if po and pi:
        hours = compute_work_hours(pi, po)
        overtime = compute_overtime(po, employee["shift_end"], doc["date"], employee["shift_start"])
        half = hours < 4.50
    else:
        hours, overtime, half = None, 0, False
    return {"work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half}


def serialize_history(doc: dict) -> dict:
    return public_doc(doc)


class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls, v): parse_date(v, "joined_on"); return v
    @model_validator(mode="after")
    def different_shift(self):
        if self.shift_start == self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self


class EpochInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    punched_at: StrictInt | None = None
    @field_validator("punched_at")
    @classmethod
    def valid_epoch(cls, v):
        if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS: raise ValueError("must be epoch milliseconds")
        return v


class PunchInRequest(EpochInput):
    emp_code: str
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(EpochInput):
    emp_code: str


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None
    punch_in: StrictInt | None = None
    punch_out: StrictInt | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    @field_validator("punch_in", "punch_out")
    @classmethod
    def valid_epoch(cls, v):
        if v is not None and not MIN_EPOCH_MS <= v <= MAX_EPOCH_MS: raise ValueError("must be epoch milliseconds")
        return v


def pagination(page: int, page_size: int):
    if page < 1: err(422, "page must be at least 1")
    if not 1 <= page_size <= 100: err(422, "page_size must be between 1 and 100")
    return (page - 1) * page_size


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        err(503, "MongoDB is unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        err(409, "emp_code already exists")
    return public_doc(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(pagination(page, page_size)).limit(page_size)
    return {"items": [public_doc(x) for x in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: err(404, "employee not found")
    ts = epoch_to_dt(body.punched_at, default_now=True)
    day = attendance_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts,
           "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        err(409, "already punched in for this attendance date")
    return public_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp: err(404, "employee not found")
    ts = epoch_to_dt(body.punched_at, default_now=True)
    current = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)])
    if not current: err(404, "no punch-in found")
    if current.get("punch_out") is not None: err(409, "record already punched out")
    pi = current.get("punch_in")
    elapsed = (ts - pi).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600: err(422, "punch-out must be after punch-in and within 24 hours")
    fields = derive_fields({**current, "punch_out": ts}, emp)
    result = db.attendance_logs.find_one_and_update(
        {"_id": current["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **fields}}, return_document=ReturnDocument.AFTER)
    if result is None: err(409, "record was already punched out")
    return public_doc(result)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None, date_to: str | None = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    q = {}
    if emp_code is not None: q["emp_code"] = emp_code
    if date_from:
        parse_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
    if date_to:
        parse_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
    if date_from and date_to and date_from > date_to: err(422, "date_from must not be after date_to")
    if status: q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip(pagination(page, page_size)).limit(page_size)
    return {"items": [public_doc(x) for x in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeRequest):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp: err(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not old: err(404, "attendance record not found")
    changes_input = body.model_dump(exclude_unset=True)
    reason = changes_input.pop("reason")
    by = changes_input.pop("regularized_by")
    new = dict(old)
    if "status" in changes_input and changes_input["status"] is not None: new["status"] = changes_input["status"]
    for key in ("punch_in", "punch_out"):
        if key in changes_input:
            new[key] = epoch_to_dt(changes_input[key]) if changes_input[key] is not None else None
    status = new.get("status")
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in changes_input and changes_input["punch_in"] is not None or "punch_out" in changes_input and changes_input["punch_out"] is not None:
            err(422, "ABSENT and LEAVE cannot have punch times")
        new["punch_in"] = new["punch_out"] = None
    else:
        if new.get("punch_in") is None: err(422, "presence status requires punch_in")
        if new["punch_in"].astimezone(IST).date().isoformat() != date:
            # R1 allows a post-midnight punch-in for an overnight shift only when its attendance date is previous day.
            if attendance_day(new["punch_in"], emp) != date: err(422, "punch_in must belong to the record attendance date")
        if new.get("punch_out") is not None:
            elapsed = (new["punch_out"] - new["punch_in"]).total_seconds()
            if elapsed <= 0 or elapsed > 24 * 3600: err(422, "punch_out must be after punch_in and within 24 hours")
    derived = derive_fields(new, emp)
    new.update(derived)
    tracked = ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]
    changes = {}
    for key in tracked:
        before, after = old.get(key), new.get(key)
        if key == "late_minutes": before = old.get(key, 0); after = new.get(key, 0)
        if key == "overtime_minutes": before = old.get(key, 0); after = new.get(key, 0)
        if key == "half_day": before = old.get(key, False); after = new.get(key, False)
        if before != after: changes[key] = {"from": before, "to": after}
    if not changes: err(422, "request does not change the record")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": by, "reason": reason, "changes": changes}
    # Compare-and-set on the original history and key fields prevents lost audit entries.
    guard = {"_id": old["_id"]}
    if "history" in old:
        guard["history"] = old["history"]
    else:
        guard["$or"] = [{"history": []}, {"history": {"$exists": False}}]
    for key in ("status", "punch_in", "punch_out"):
        if key in old: guard[key] = old[key]
        else: guard[key] = {"$exists": False}
    result = db.attendance_logs.find_one_and_update(guard, {"$set": {**{k: new.get(k) for k in tracked}, "history": old.get("history", []) + [entry]}}, return_document=ReturnDocument.AFTER)
    if result is None: err(409, "record changed concurrently; retry the correction")
    return public_doc(result)


def month_filter(month: str):
    start, end = parse_month(month)
    return start.isoformat(), end.isoformat(), start, end


def working_days(start: date, end: date) -> int:
    if end < start: return 0
    count = 0
    cur = start
    while cur <= end:
        if cur.weekday() < 5: count += 1
        cur += timedelta(days=1)
    return count


def monthly_pipeline(emp_code: str, start_s: str, end_s: str):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start_s, "$lte": end_s}}},
        {"$set": {"_half": {"$ifNull": ["$half_day", False]}, "_late": {"$ifNull": ["$late_minutes", 0]}, "_ot": {"$ifNull": ["$overtime_minutes", 0]}}},
        {"$set": {"_weekday": {"$not": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 7]]}]}}},
        {"$group": {"_id": None,
                    "present_days": {"$sum": {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]}, "$_weekday"]}, {"$cond": ["$_half", 0.5, 1]}, 0]}},
                    "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": ["$_late", 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": "$_late"}, "total_overtime_minutes": {"$sum": "$_ot"}}}
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start_s, end_s, start, end = month_filter(month)
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "joined_on": 1})
    if not emp: err(404, "employee not found")
    result = list(db.attendance_logs.aggregate(monthly_pipeline(emp_code, start_s, end_s)))
    totals = result[0] if result else {"present_days": 0, "leave_days": 0, "late_count": 0, "total_late_minutes": 0, "total_overtime_minutes": 0}
    work_start = max(start, parse_date(emp["joined_on"], "joined_on"))
    wd = working_days(work_start, end)
    pct = None if wd == 0 else round_half_up(Decimal(str(totals["present_days"])) / Decimal(wd) * 100, 4)
    return {"emp_code": emp_code, "month": month, "working_days": wd,
            "present_days": round_half_up(totals["present_days"], 2), "leave_days": totals["leave_days"],
            "late_count": totals["late_count"], "total_late_minutes": totals["total_late_minutes"],
            "total_overtime_minutes": totals["total_overtime_minutes"], "attendance_pct": pct}


def department_summary_pipeline(start_s: str, end_s: str, department: str | None):
    match = {"joined_on": {"$lte": end_s}}
    if department is not None: match["department"] = department
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lte": ["$date", end_s]}]}}}
        ], "as": "logs"}},
        {"$set": {"_present": {"$sum": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$in": ["$$l.status", PRESENCE]}}}, "as": "l", "in": {"$cond": [{"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$$l.date"}}}, [1, 7]]}, 0, {"$cond": [{"$ifNull": ["$$l.half_day", False]}, 0.5, 1]}]}}}},
                   "_late": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]}}}},
                   "_late_minutes": {"$sum": {"$map": {"input": "$logs", "as": "l", "in": {"$ifNull": ["$$l.late_minutes", 0]}}}},
                   "_leave": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "LEAVE"]}}}},
                   "_onduty": {"$size": {"$filter": {"input": "$logs", "as": "l", "cond": {"$eq": ["$$l.status", "ON_DUTY"]}}}},
                   "_work": {"$map": {"input": {"$filter": {"input": "$logs", "as": "l", "cond": {"$and": [{"$in": ["$$l.status", PRESENCE]}, {"$ne": ["$$l.work_hours", None]}]}}}, "as": "l", "in": "$$l.work_hours"}}}},
        {"$set": {"_avg_work": {"$cond": [{"$gt": [{"$size": "$_work"}, 0]}, {"$avg": "$_work"}, None]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$_present"},
                    "late_count": {"$sum": "$_late"}, "total_late_minutes": {"$sum": "$_late_minutes"},
                    "leave_count": {"$sum": "$_leave"}, "on_duty_count": {"$sum": "$_onduty"},
                    "_work_values": {"$push": "$_work"}}},
        {"$set": {"_all_work": {"$reduce": {"input": "$_work_values", "initialValue": [], "in": {"$concatArrays": ["$$value", "$$this"]}}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "late_count": 1, "total_late_minutes": 1,
                       "leave_count": 1, "on_duty_count": 1, "avg_work_hours": {"$cond": [{"$gt": [{"$size": "$_all_work"}, 0]}, {"$avg": "$_all_work"}, None]}}},
        {"$sort": {"department": 1}}
    ]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    start_s, end_s, _, _ = month_filter(month)
    pipeline = department_summary_pipeline(start_s, end_s, department)
    items = list(db.employees.aggregate(pipeline))
    return {"month": month, "items": [{**x, "present_days": round_half_up(x["present_days"], 2), "avg_work_hours": round_half_up(x["avg_work_hours"], 2)} for x in items]}


def leaderboard_pipeline(start_s: str, end_s: str, limit: int, department: str | None):
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lte": end_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None: pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    start_s, end_s, _, _ = month_filter(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(start_s, end_s, limit, department)))}


def trend_pipeline(department: str, from_s: str, to_s: str):
    start_dt = datetime.combine(date.fromisoformat(from_s), time.min, UTC)
    n_days = (date.fromisoformat(to_s) - date.fromisoformat(from_s)).days
    return [
        {"$documents": [{"day": start_dt}]},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, start_dt + timedelta(days=n_days + 1)]}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "UTC"}},
                   "is_working_day": {"$not": [{"$in": [{"$dayOfWeek": "$day"}, [1, 7]]}]}}},
        {"$lookup": {"from": "employees", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", department]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}
        ], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                        "present_count": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, {"$divide": ["$present_count", "$headcount"]}, None]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    start, end = parse_date(from_, "from"), parse_date(to, "to")
    if end < start: err(422, "to must not be before from")
    if (end - start).days + 1 > 92: err(422, "date range cannot exceed 92 days")
    if not db.employees.find_one({"department": department}, {"_id": 1}): err(404, "department not found")
    pipeline = trend_pipeline(department, start.isoformat(), end.isoformat())
    items = list(db.aggregate(pipeline))
    for item in items:
        item["attendance_rate"] = round_half_up(item.get("attendance_rate"), 4)
        item["moving_avg_7d"] = round_half_up(item.get("moving_avg_7d"), 4)
        item["present_count"] = round_half_up(item.get("present_count", 0), 2)
    # $densify bounds are end-exclusive in MongoDB; trim defensively to the requested inclusive range.
    items = [x for x in items if from_ <= x["date"] <= to]
    return {"department": department, "items": items}


def explain_command(collection: str, pipeline: list, find_query: dict | None = None, sort: list | None = None, skip: int = 0, limit: int = 20):
    if find_query is not None:
        command = {"find": collection, "filter": find_query, "sort": dict(sort or []), "skip": skip, "limit": limit}
    else:
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return db.command({"explain": command, "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
                    emp_code: str | None = None, month: str | None = None, department: str | None = None,
                    limit: int = Query(10, ge=1, le=50), date_from: str | None = None, date_to: str | None = None,
                    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                    from_: str | None = Query(None, alias="from"), to: str | None = None):
    off = pagination(page, page_size)
    if endpoint == "attendance_list":
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from:
            parse_date(date_from, "date_from"); q.setdefault("date", {})["$gte"] = date_from
        if date_to:
            parse_date(date_to, "date_to"); q.setdefault("date", {})["$lte"] = date_to
        if date_from and date_to and date_from > date_to: err(422, "date_from must not be after date_to")
        if status: q["status"] = status
        exp = explain_command("attendance_logs", [], q, [("date", -1), ("emp_code", 1)], off, page_size)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "employee_monthly":
        if not emp_code or not month: err(422, "emp_code and month are required")
        start_s, end_s, _, _ = month_filter(month)
        exp = explain_command("attendance_logs", monthly_pipeline(emp_code, start_s, end_s))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_summary":
        if not month: err(422, "month is required")
        start_s, end_s, _, _ = month_filter(month)
        exp = explain_command("employees", department_summary_pipeline(start_s, end_s, department))
        return {"endpoint": endpoint, "collection": "employees", "explain": exp}
    if endpoint == "late_leaderboard":
        if not month: err(422, "month is required")
        start_s, end_s, _, _ = month_filter(month)
        exp = explain_command("attendance_logs", leaderboard_pipeline(start_s, end_s, limit, department))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": exp}
    if endpoint == "department_trend":
        if not department or not from_ or not to: err(422, "department, from and to are required")
        start, end = parse_date(from_, "from"), parse_date(to, "to")
        if end < start or (end - start).days + 1 > 92: err(422, "invalid trend date range")
        exp = db.command({"explain": {"aggregate": 1, "pipeline": trend_pipeline(department, from_, to), "cursor": {}}, "verbosity": "executionStats"})
        return {"endpoint": endpoint, "collection": "1", "explain": exp}
    err(422, "unsupported endpoint")
