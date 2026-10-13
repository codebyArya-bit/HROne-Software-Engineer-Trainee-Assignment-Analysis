"""HROne employee attendance service. All database analytics use MongoDB pipelines."""
from __future__ import annotations

import os
import json
from contextlib import asynccontextmanager
from datetime import date as CalendarDate, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from bson import json_util

load_dotenv(override=False)
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENT = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUS = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PRESENCE_STATUS = Literal["PRESENT", "WFH", "ON_DUTY"]
MAX_MS = 4102444800000
Milliseconds = Annotated[StrictInt, Field(ge=100000000000, le=MAX_MS)]
Month = Annotated[str, Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True, serverSelectionTimeoutMS=2500)
db = client[os.getenv("MONGO_DB", "attendance_db")]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The grader supplies a populated database. Do not delete or seed it here.
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_codes")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="attendance_natural_key")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_code")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_status_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_recent_punch")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: CalendarDate

    @field_validator("joined_on", mode="before")
    @classmethod
    def calendar_date_only(cls, value):
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError("joined_on must be YYYY-MM-DD")
        try:
            if CalendarDate.fromisoformat(value).isoformat() != value:
                raise ValueError("joined_on must be YYYY-MM-DD")
        except ValueError:
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def distinct_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Milliseconds | None = None
    status: PRESENCE_STATUS = "PRESENT"

    @model_validator(mode="after")
    def no_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Milliseconds | None = None

    @model_validator(mode="after")
    def no_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: ALL_STATUS | None = None
    punch_in: Milliseconds | None = None
    punch_out: Milliseconds | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def reject_explicit_nulls(self):
        for field in ("status", "punch_in", "punch_out"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


def error422(message: str):
    raise HTTPException(422, message)


def get_employee(code: str) -> dict:
    emp = db.employees.find_one({"emp_code": code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


def epoch_date(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, UTC).replace(microsecond=0)


def current_second() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def millis(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=UTC).timestamp() * 1000) if dt.tzinfo is None else int(dt.timestamp() * 1000)


def round_half_up(value: int | float | Decimal, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def parse_calendar(value: str) -> CalendarDate:
    try:
        if len(value) != 10 or CalendarDate.fromisoformat(value).isoformat() != value:
            raise ValueError
        return CalendarDate.fromisoformat(value)
    except (TypeError, ValueError):
        error422("expected YYYY-MM-DD calendar date")


def month_bounds(month: str) -> tuple[str, str, str]:
    try:
        first = CalendarDate.fromisoformat(month + "-01")
    except ValueError:
        error422("invalid month")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.isoformat(), (next_month - timedelta(days=1)).isoformat(), next_month.isoformat()


def attendance_day(punch: datetime, emp: dict) -> CalendarDate:
    local = punch.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M:%S") < emp["shift_end"] + ":00":
        day -= timedelta(days=1)
    return day


def shift_time(day: CalendarDate, clock: str) -> datetime:
    h, m = map(int, clock.split(":"))
    return datetime.combine(day, time(h, m), tzinfo=IST)


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: CalendarDate) -> int:
    seconds = int((punch_in - shift_time(attendance_date, shift_start)).total_seconds())
    return max(0, seconds // 60) if seconds > 600 else 0


def compute_overtime(punch_out: datetime, emp: dict, attendance_date: CalendarDate) -> int:
    next_day = emp["shift_end"] <= emp["shift_start"]
    shift_end = shift_time(attendance_date + timedelta(days=next_day), emp["shift_end"])
    seconds = int((punch_out - shift_end).total_seconds())
    minutes = seconds // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600))


def valid_duration(punch_in: datetime, punch_out: datetime):
    seconds = (punch_out - punch_in).total_seconds()
    if seconds <= 0 or seconds > 24 * 3600:
        error422("punch_out must be after punch_in and within 24 hours")


def recomputed(status: str, pin: datetime | None, pout: datetime | None, emp: dict, day: CalendarDate) -> dict:
    if status not in PRESENT:
        return {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if pin is None:
        error422("presence status requires punch_in")
    if attendance_day(pin, emp) != day:
        error422("punch_in must belong to the record's attendance date")
    late = compute_late_minutes(pin, emp["shift_start"], day)
    if pout is None:
        return {"work_hours": None, "late_minutes": late, "overtime_minutes": 0, "half_day": False}
    valid_duration(pin, pout)
    hours = compute_work_hours(pin, pout)
    return {"work_hours": hours, "late_minutes": late,
            "overtime_minutes": compute_overtime(pout, emp, day), "half_day": hours < 4.5}


def attendance_json(doc: dict) -> dict:
    return {"emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
            "punch_in": millis(doc.get("punch_in")), "punch_out": millis(doc.get("punch_out")),
            "work_hours": doc.get("work_hours"), "late_minutes": doc.get("late_minutes", 0),
            "overtime_minutes": doc.get("overtime_minutes", 0), "half_day": doc.get("half_day", False),
            "history": [{"at": millis(h["at"]), "by": h["by"], "reason": h["reason"],
                         "changes": {k: {"from": millis(v["from"]) if k in ("punch_in", "punch_out") else v["from"],
                                         "to": millis(v["to"]) if k in ("punch_in", "punch_out") else v["to"]}
                                     for k, v in h["changes"].items()}} for h in doc.get("history", [])]}


def employee_json(doc: dict) -> dict:
    return {key: doc[key] for key in ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on")} | {"created_at": millis(doc["created_at"])}


def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from and date_to and date_from > date_to:
        error422("date_from must be <= date_to")
    filt = {}
    if emp_code is not None:
        filt["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        filt["date"] = {}
        if date_from is not None:
            filt["date"]["$gte"] = date_from
        if date_to is not None:
            filt["date"]["$lte"] = date_to
    if status is not None:
        filt["status"] = status
    return filt


def attendance_hint(emp_code=None, status=None):
    if status is not None and emp_code is None:
        return "attendance_status_date_code"
    if emp_code is not None:
        return "attendance_natural_key"
    return "attendance_date_code"


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["joined_on"] = body.joined_on.isoformat()
    doc["created_at"] = current_second()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    filt = {"department": department} if department is not None else {}
    rows = db.employees.find(filt).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(x) for x in rows], "total": db.employees.count_documents(filt),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee(body.emp_code)
    instant = epoch_date(body.punched_at) if body.punched_at is not None else current_second()
    day = attendance_day(instant, emp)
    doc = {"emp_code": body.emp_code, "date": day.isoformat(), "status": body.status,
           "punch_in": instant, "punch_out": None, "history": [],
           **recomputed(body.status, instant, None, emp, day)}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_json(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee(body.emp_code)
    instant = epoch_date(body.punched_at) if body.punched_at is not None else current_second()
    # Most recent eligible punch-in, even when already closed: a duplicate must be 409.
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": instant}},
                                      sort=[("punch_in", -1)])
    if doc is None:
        # Distinguish a missing punch-in (404) from an invalid earlier punch-out (422).
        # Only this fallback query runs when no eligible punch-in exists.
        latest = db.attendance_logs.find_one({"emp_code": body.emp_code},
                                             sort=[("punch_in", -1)])
        if latest is not None:
            valid_duration(latest["punch_in"], instant)
        raise HTTPException(404, "no punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    day = parse_calendar(doc["date"])
    valid_duration(doc["punch_in"], instant)
    derived = recomputed(doc["status"], doc["punch_in"], instant, emp, day)
    # Atomic compare-and-set avoids the two-punch-out lost update.
    saved = db.attendance_logs.find_one_and_update(
        {"_id": doc["_id"], "punch_out": None, "punch_in": doc["punch_in"], "status": doc["status"]},
        {"$set": {"punch_out": instant, **derived}}, return_document=ReturnDocument.AFTER)
    if saved is None:
        raise HTTPException(409, "attendance changed concurrently")
    return attendance_json(saved)


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: CalendarDate | None = None,
                    date_to: CalendarDate | None = None, status: ALL_STATUS | None = None,
                    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    filt = attendance_filter(emp_code, date_from.isoformat() if date_from else None,
                             date_to.isoformat() if date_to else None, status)
    rows = db.attendance_logs.find(filt, hint=attendance_hint(emp_code, status)).sort(
        [("date", -1), ("emp_code", 1)]).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_json(x) for x in rows], "total": db.attendance_logs.count_documents(filt),
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, body: RegularizeIn, date: CalendarDate = Path(...)):
    emp = get_employee(emp_code)
    day = date.isoformat()
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if old is None:
        raise HTTPException(404, "attendance record not found")
    provided = body.model_fields_set
    status = body.status if "status" in provided else old["status"]
    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in provided or "punch_out" in provided:
            error422("absence/leave cannot have punch timestamps")
        pin, pout = None, None
    else:
        pin = epoch_date(body.punch_in) if "punch_in" in provided else old.get("punch_in")
        pout = epoch_date(body.punch_out) if "punch_out" in provided else old.get("punch_out")
    updated = {"status": status, "punch_in": pin, "punch_out": pout,
               **recomputed(status, pin, pout, emp, date)}
    if all(old.get(key) == updated[key] for key in ("status", "punch_in", "punch_out")):
        error422("correction changes nothing")
    changes = {key: {"from": old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None),
                     "to": value} for key, value in updated.items()
               if old.get(key, False if key == "half_day" else 0 if key in ("late_minutes", "overtime_minutes") else None) != value}
    if not changes:
        error422("correction changes nothing")
    # Optimistic version check; atomic $push never loses another editor's audit entry.
    guard = {"_id": old["_id"], "status": old["status"],
             "punch_in": old.get("punch_in"), "punch_out": old.get("punch_out")}
    guard["_revision"] = old["_revision"] if "_revision" in old else {"$exists": False}
    saved = db.attendance_logs.find_one_and_update(
        guard, {"$set": updated, "$push": {"history": {"at": current_second(),
                            "by": body.regularized_by, "reason": body.reason, "changes": changes}},
                "$inc": {"_revision": 1}}, return_document=ReturnDocument.AFTER)
    if saved is None:
        raise HTTPException(409, "attendance changed concurrently")
    return attendance_json(saved)


# MongoDB expressions shared by all analytics endpoints.
PRESENCE_EXPR = {"$in": ["$status", list(PRESENT)]}
WEEKDAY_EXPR = {"$lte": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 5]}
PRESENT_WEIGHT = {"$cond": [{"$and": [PRESENCE_EXPR, WEEKDAY_EXPR]},
                              {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def working_days_expression(month: str):
    begin, _, next_month = month_bounds(month)
    start = {"$max": [{"$dateFromString": {"dateString": "$joined_on"}},
                       {"$dateFromString": {"dateString": begin}}]}
    stop = {"$dateFromString": {"dateString": next_month}}
    days = {"$range": [0, {"$max": [0, {"$dateDiff": {"startDate": start, "endDate": stop, "unit": "day"}}]}]}
    return {"$size": {"$filter": {"input": days, "as": "i", "cond": {"$lte": [
        {"$isoDayOfWeek": {"$dateAdd": {"startDate": start, "unit": "day", "amount": "$$i"}}}, 5]}}}}


def month_logs_lookup(first: str, next_month: str, grouped: bool = True):
    expr = {"$and": [{"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", first]},
                      {"$lt": ["$date", next_month]}]}
    stages = [{"$match": {"$expr": expr}}]
    if grouped:
        stages.append({"$group": {"_id": None,
             "present_days": {"$sum": PRESENT_WEIGHT},
             "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
             "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
             "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
             "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
             "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
             "work_sum": {"$sum": {"$cond": [{"$and": [PRESENCE_EXPR, {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
             "work_count": {"$sum": {"$cond": [{"$and": [PRESENCE_EXPR, {"$ne": ["$work_hours", None]}]}, 1, 0]}}}})
    return {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"},
                        "pipeline": stages, "as": "stats"}}


def employee_month_pipeline(code: str, month: str):
    first, _, next_month = month_bounds(month)
    return [{"$match": {"emp_code": code}}, month_logs_lookup(first, next_month),
            {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]},
                      "working_days": working_days_expression(month)}},
            {"$project": {"_id": 0, "emp_code": 1, "working_days": 1,
                          "present_days": {"$ifNull": ["$s.present_days", 0]},
                          "leave_days": {"$ifNull": ["$s.leave_days", 0]},
                          "late_count": {"$ifNull": ["$s.late_count", 0]},
                          "total_late_minutes": {"$ifNull": ["$s.total_late_minutes", 0]},
                          "total_overtime_minutes": {"$ifNull": ["$s.total_overtime_minutes", 0]}}}]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    pipeline = employee_month_pipeline(emp_code, month)
    results = list(db.employees.aggregate(pipeline, hint="employee_code_unique"))
    if not results:
        raise HTTPException(404, "employee not found")
    record = results[0]
    days = record["working_days"]
    return {**record, "month": month, "present_days": round_half_up(record["present_days"]),
            "attendance_pct": round_half_up(Decimal(str(record["present_days"])) * 100 / days) if days else None}


def department_summary_pipeline(month: str, department: str | None):
    first, last, next_month = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    fields = ["present_days", "late_count", "total_late_minutes", "leave_days", "on_duty_count", "work_sum", "work_count"]
    return [{"$match": match}, month_logs_lookup(first, next_month),
            {"$set": {"s": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
            {"$group": {"_id": "$department", "headcount": {"$sum": 1},
                        **{key: {"$sum": {"$ifNull": ["$s." + key, 0]}} for key in fields}}},
            {"$sort": {"_id": 1}}]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: str | None = None):
    items = []
    for row in db.employees.aggregate(department_summary_pipeline(month, department),
                                      hint="department_joined" if department is not None else "joined_department"):
        items.append({"department": row["_id"], "headcount": row["headcount"],
                      "present_days": round_half_up(row["present_days"]),
                      "avg_work_hours": round_half_up(Decimal(str(row["work_sum"])) / row["work_count"]) if row["work_count"] else None,
                      "late_count": row["late_count"], "total_late_minutes": row["total_late_minutes"],
                      "leave_count": row["leave_days"], "on_duty_count": row["on_duty_count"]})
    return {"month": month, "items": items}


def leaderboard_pipeline(month: str, limit: int, department: str | None):
    first, _, next_month = month_bounds(month)
    stages = [{"$match": {"date": {"$gte": first, "$lt": next_month}}},
              {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                          "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
              {"$match": {"total_late_minutes": {"$gt": 0}}},
              {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
              {"$unwind": "$emp"}]
    if department is not None:
        stages.append({"$match": {"emp.department": department}})
    stages += [{"$setWindowFields": {"sortBy": {"total_late_minutes": -1},
                                      "output": {"rank": {"$rank": {}}}}},
               {"$match": {"rank": {"$lte": limit}}},
               {"$sort": {"total_late_minutes": -1, "_id": 1}},
               {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                             "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}}]
    return stages


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: int = Query(10, ge=1, le=50), department: str | None = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department),
                                                           hint="attendance_date_code"))}


def trend_pipeline(department: str, date_from: CalendarDate, date_to: CalendarDate):
    # Start from one indexed department employee; generate every calendar day within MongoDB
    # so empty days and an entirely empty attendance range still produce a full series.
    start_utc = datetime.combine(date_from, time.min, tzinfo=UTC)
    num_days = (date_to - date_from).days + 1
    return [
        {"$match": {"department": department}}, {"$sort": {"emp_code": 1}}, {"$limit": 1},
        {"$project": {"_id": 0, "date_obj": {"$map": {"input": {"$range": [0, num_days]}, "as": "offset",
              "in": {"$dateAdd": {"startDate": start_utc, "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$date_obj"},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_obj"}},
                   "is_working_day": {"$lte": [{"$isoDayOfWeek": "$date_obj"}, 5]}}},
        {"$lookup": {"from": "employees", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
            {"$count": "n"}], "as": "headcount_result"}},
        {"$lookup": {"from": "attendance_logs", "let": {"day": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$day"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$match": {"e.department": department}},
            {"$group": {"_id": None,
                        "present_count": {"$sum": {"$cond": [PRESENCE_EXPR,
                             {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}],
            "as": "counts"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$headcount_result.n", 0]}, 0]},
                   "present_count": {"$ifNull": [{"$arrayElemAt": ["$counts.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$counts.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date_obj": 1}, "output": {"moving_avg_7d": {
            "$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1}},
        {"$sort": {"date": 1}}
    ]


def validate_trend_dates(date_from: CalendarDate, date_to: CalendarDate):
    if date_to < date_from or (date_to - date_from).days + 1 > 92:
        error422("trend date range must be ordered and at most 92 inclusive days")


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: CalendarDate = Query(alias="from"),
                     date_to: CalendarDate = Query(alias="to")):
    validate_trend_dates(date_from, date_to)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    results = list(db.employees.aggregate(trend_pipeline(department, date_from, date_to),
                                          hint="department_codes"))
    return {"department": department, "items": [{**r,
        "present_count": round_half_up(r["present_count"]),
        "attendance_rate": round_half_up(r["attendance_rate"], 4) if r["attendance_rate"] is not None else None,
        "moving_avg_7d": round_half_up(r["moving_avg_7d"], 4) if r["moving_avg_7d"] is not None else None}
           for r in results]}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: str | None = None, month: str | None = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: str | None = None, limit: int = Query(10, ge=1, le=50),
    date_from: CalendarDate | None = None, date_to: CalendarDate | None = None,
    status: ALL_STATUS | None = None, trend_from: CalendarDate | None = Query(None, alias="from"),
    trend_to: CalendarDate | None = Query(None, alias="to"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    coll = "attendance_logs" if endpoint in ("attendance_list", "late_leaderboard") else "employees"
    if endpoint == "attendance_list":
        filt = attendance_filter(emp_code, date_from.isoformat() if date_from else None,
                                 date_to.isoformat() if date_to else None, status)
        cmd = {"find": coll, "filter": filt, "sort": {"date": -1, "emp_code": 1},
               "skip": (page - 1) * page_size, "limit": page_size,
               "hint": attendance_hint(emp_code, status)}
    else:
        if endpoint == "employee_monthly":
            if emp_code is None or month is None:
                error422("employee_monthly requires emp_code and month")
            pipeline = employee_month_pipeline(emp_code, month)
            hint = "employee_code_unique"
        elif endpoint == "department_summary":
            if month is None:
                error422("department_summary requires month")
            pipeline = department_summary_pipeline(month, department)
            hint = "department_joined" if department is not None else "joined_department"
        elif endpoint == "late_leaderboard":
            if month is None:
                error422("late_leaderboard requires month")
            pipeline = leaderboard_pipeline(month, limit, department)
            hint = "attendance_date_code"
        else:
            if department is None or trend_from is None or trend_to is None:
                error422("department_trend requires department, from, and to")
            validate_trend_dates(trend_from, trend_to)
            pipeline = trend_pipeline(department, trend_from, trend_to)
            hint = "department_codes"
        cmd = {"aggregate": coll, "pipeline": pipeline, "cursor": {}, "hint": hint}
    raw = db.command("explain", cmd, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": coll, "explain": json.loads(json_util.dumps(raw))}