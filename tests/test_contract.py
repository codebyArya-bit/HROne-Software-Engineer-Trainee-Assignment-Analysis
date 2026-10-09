"""MongoDB-backed smoke and business-rule checks. Uses an isolated throwaway database.

Start MongoDB 6+ first (docker run -d --name hrone-mongo -p 27017:27017 mongo:7),
then: python -m pytest -q. WARNING: drops the isolated TEST_MONGO_DB database.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MONGO_URI", "mongodb://localhost:27017")
os.environ["MONGO_DB"] = os.getenv("TEST_MONGO_DB", "hrone_candidate_test_isolated")

from app.main import app, db  # noqa: E402; environment must precede import

IST = timezone(timedelta(hours=5, minutes=30))


def ms(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 7):
    return int(datetime(2026, month, day, hour, minute, second, tzinfo=IST).timestamp() * 1000)


def employee(code, department="Engineering", joined="2026-07-01", start="09:30", end="18:30"):
    return {"emp_code": code, "name": f"Test {code}", "email": f"{code.lower()}@example.org",
            "department": department, "joined_on": joined,
            "shift_start": start, "shift_end": end}


@pytest.fixture
def api():
    # Never clean the application's configured database unless it is the explicitly named test database.
    assert db.name == "hrone_candidate_test_isolated" or db.name.startswith("hrone_test_")
    db.client.drop_database(db.name)
    with TestClient(app) as client:
        yield client
    db.client.drop_database(db.name)


def test_startup_health_and_unique_indexes(api):
    assert api.get("/health").json() == {"status": "ok"}
    assert db.employees.index_information()["employee_code_unique"]["unique"]
    assert db.attendance_logs.index_information()["attendance_natural_key"]["unique"]


def test_employees_validation_and_pagination(api):
    for number in (3, 1, 2):
        response = api.post("/employees", json=employee(f"EMP{number:04d}"))
        assert response.status_code == 201, response.text
        assert isinstance(response.json()["created_at"], int)
        assert "_id" not in response.json()
    assert api.post("/employees", json=employee("EMP0001")).status_code == 409
    assert api.post("/employees", json=employee("INVALID")).status_code == 422
    r = api.get("/employees", params={"page": 2, "page_size": 2})
    assert r.status_code == 200 and r.json()["total"] == 3
    assert [x["emp_code"] for x in r.json()["items"]] == ["EMP0003"]
    assert api.get("/employees", params={"page": 0}).status_code == 422


def test_punch_business_rules_history_and_explain(api):
    assert api.post("/employees", json=employee("EMP0001")).status_code == 201
    early = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 40, 0)})
    assert early.status_code == 201, early.text
    assert early.json()["late_minutes"] == 0
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(6, 9, 40)}).status_code == 409
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 19, 0)})
    assert out.status_code == 200, out.text
    assert out.json()["overtime_minutes"] == 30
    assert out.json()["work_hours"] == 9.33
    assert out.json()["history"] == []
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(6, 19, 1)}).status_code == 409
    changed = api.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ms(6, 9, 40, 1), "reason": "Manager corrected time", "regularized_by": "Manager"})
    assert changed.status_code == 200, changed.text
    entry = changed.json()["history"][0]
    assert entry["changes"]["late_minutes"] == {"from": 0, "to": 10}
    assert entry["changes"]["punch_in"] == {"from": ms(6, 9, 40), "to": ms(6, 9, 40, 1)}
    assert api.patch("/attendance/EMP0001/2026-07-06", json={
        "punch_in": ms(6, 9, 40, 1), "reason": "Manager corrected time", "regularized_by": "Manager"}).status_code == 422
    explanation = api.get("/admin/explain/attendance_list")
    assert explanation.status_code == 200, explanation.text
    assert explanation.json()["endpoint"] == "attendance_list"
    assert "IXSCAN" in str(explanation.json()["explain"])


def test_half_day_overnight_and_invalid_ms(api):
    api.post("/employees", json=employee("EMP0001", start="22:00", end="06:00"))
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms(7, 2)}).json()["date"] == "2026-07-06"
    out = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(7, 6, 40)})
    assert out.status_code == 200, out.text
    assert out.json()["overtime_minutes"] == 40
    assert out.json()["half_day"] is False
    for bad in (1783312500, "1783312500000", 1783312500000.2, True):
        assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": bad}).status_code == 422
    api.post("/employees", json=employee("EMP0002"))
    api.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": ms(6, 9, 30)})
    half = api.post("/attendance/punch-out", json={"emp_code": "EMP0002", "punched_at": ms(6, 13)})
    assert half.status_code == 200 and half.json()["half_day"] is True
    assert half.json()["work_hours"] == 3.5


def test_concurrent_punchin_and_punchout(api):
    api.post("/employees", json=employee("EMP0001"))
    body = {"emp_code": "EMP0001", "punched_at": ms(6, 9, 30)}
    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(lambda _: api.post("/attendance/punch-in", json=body).status_code, range(5)))
    assert codes.count(201) == 1 and codes.count(409) == 4, codes
    body["punched_at"] = ms(6, 18, 30)
    with ThreadPoolExecutor(max_workers=5) as pool:
        codes = list(pool.map(lambda _: api.post("/attendance/punch-out", json=body).status_code, range(5)))
    assert codes.count(200) == 1 and codes.count(409) == 4, codes


def test_analytics_ties_joiners_gaps_and_weekends(api):
    for code, dep, joined in [("EMP0001", "Engineering", "2026-07-01"),
                              ("EMP0002", "Engineering", "2026-07-01"),
                              ("EMP0003", "Engineering", "2026-07-15"),
                              ("EMP0004", "Sales", "2026-07-01")]:
        assert api.post("/employees", json=employee(code, dep, joined)).status_code == 201
    # Independent stored values are authoritative for analytics; weekends count for lateness, not presence.
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 30,
         "work_hours": 8.0, "half_day": False},
        {"emp_code": "EMP0001", "date": "2026-07-11", "status": "PRESENT", "late_minutes": 15,
         "work_hours": 3.0, "half_day": False},
        {"emp_code": "EMP0002", "date": "2026-07-06", "status": "WFH", "late_minutes": 45,
         "work_hours": 3.5, "half_day": True},
        {"emp_code": "EMP0002", "date": "2026-07-07", "status": "LEAVE", "late_minutes": 0},
    ])
    leaderboard = api.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 1})
    assert leaderboard.status_code == 200, leaderboard.text
    assert [(r["rank"], r["emp_code"]) for r in leaderboard.json()["items"]] == [(1, "EMP0001"), (1, "EMP0002")]
    monthly = api.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"})
    assert monthly.status_code == 200, monthly.text
    assert monthly.json()["present_days"] == 0.5
    assert monthly.json()["late_count"] == 1
    assert monthly.json()["leave_days"] == 1
    summary = api.get("/analytics/departments/summary", params={"month": "2026-07"})
    assert summary.status_code == 200, summary.text
    rows = {r["department"]: r for r in summary.json()["items"]}
    assert rows["Engineering"]["headcount"] == 3
    assert rows["Engineering"]["present_days"] == 1.5
    assert rows["Engineering"]["avg_work_hours"] == 4.83
    assert rows["Engineering"]["late_count"] == 3
    trend = api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-06", "to": "2026-07-12"})
    assert trend.status_code == 200, trend.text
    days = trend.json()["items"]
    assert len(days) == 7 and [d["date"] for d in days] == [f"2026-07-{x:02d}" for x in range(6, 13)]
    assert days[0]["headcount"] == 2 and days[0]["present_count"] == 1.5
    assert days[0]["attendance_rate"] == 0.75
    assert days[5]["is_working_day"] is False and days[5]["attendance_rate"] is None
    assert days[1]["attendance_rate"] == 0 and days[1]["moving_avg_7d"] == 0.375
    assert api.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-01", "to": "2027-01-01"}).status_code == 422