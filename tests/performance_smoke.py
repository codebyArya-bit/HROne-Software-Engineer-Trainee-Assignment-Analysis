"""Disposable 100k-record smoke benchmark; never run against a real application DB."""
from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta

from fastapi.testclient import TestClient
from pymongo import InsertOne

# Must be isolated from any normal database and set before importing app.main.
assert os.environ.get("MONGO_DB") == "hrone_perf_disposable", "Unsafe database name"
from app.main import app, db  # noqa: E402

COUNT_EMPLOYEES = 500
DAYS_PER_EMPLOYEE = 200
TOTAL = COUNT_EMPLOYEES * DAYS_PER_EMPLOYEE
assert TOTAL == 100000


def main():
    db.client.admin.command("ping")
    assert db.name == "hrone_perf_disposable"
    started = time.perf_counter()
    with TestClient(app) as api:
        try:
            db.employees.insert_many([
                {"emp_code": f"EMP{i:04d}", "name": f"Performance employee {i}",
                 "email": f"perf{i}@example.com",
                 "department": "Engineering" if i % 2 == 0 else "Sales",
                 "joined_on": "2026-01-01", "shift_start": "09:30", "shift_end": "18:30"}
                for i in range(1, COUNT_EMPLOYEES + 1)
            ], ordered=False)
            base = date(2026, 1, 1)
            batch = []
            for day_index in range(DAYS_PER_EMPLOYEE):
                day = (base + timedelta(days=day_index)).isoformat()
                for i in range(1, COUNT_EMPLOYEES + 1):
                    status = "PRESENT" if i % 5 else "WFH"
                    batch.append(InsertOne({
                        "emp_code": f"EMP{i:04d}", "date": day, "status": status,
                        "punch_in": None, "punch_out": None,
                        "late_minutes": i % 15, "work_hours": 8.5,
                        "overtime_minutes": 0, "half_day": False, "history": []
                    }))
                if len(batch) >= 5000:
                    db.attendance_logs.bulk_write(batch, ordered=False)
                    batch.clear()
            if batch:
                db.attendance_logs.bulk_write(batch, ordered=False)
            actual = db.attendance_logs.count_documents({})
            assert actual == TOTAL, f"Expected {TOTAL}, saw {actual}"
            print(f"Seeded {actual} attendance records and {COUNT_EMPLOYEES} employees in {time.perf_counter()-started:.2f}s", flush=True)

            endpoints = [
                ("attendance_list", "/attendance", {"page": 1, "page_size": 20}),
                ("employee_monthly", "/analytics/employees/EMP0002/monthly", {"month": "2026-07"}),
                ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
                ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
                ("department_trend", "/analytics/departments/Engineering/trend",
                 {"from": "2026-07-06", "to": "2026-07-12"}),
            ]
            for name, path, params in endpoints:
                tick = time.perf_counter()
                resp = api.get(path, params=params)
                duration = time.perf_counter() - tick
                assert resp.status_code == 200, f"{name}: {resp.status_code} {resp.text[:500]}"
                print(f"{name}: HTTP 200, {duration:.3f}s", flush=True)
                assert duration < 30, f"{name} took {duration:.3f}s (limit 30s on CI)"
                exp_params = dict(params)
                if name == "employee_monthly":
                    exp_params["emp_code"] = "EMP0002"
                if name == "department_trend":
                    exp_params["department"] = "Engineering"
                tick = time.perf_counter()
                explain = api.get(f"/admin/explain/{name}", params=exp_params)
                assert explain.status_code == 200, f"{name} explain: {explain.status_code} {explain.text[:500]}"
                plan = json.dumps(explain.json()["explain"])
                print(f"{name} explain: HTTP 200, {time.perf_counter()-tick:.3f}s, IXSCAN={'IXSCAN' in plan}, COLLSCAN={'COLLSCAN' in plan}", flush=True)
                assert '"IXSCAN"' in plan, f"{name} does not show an index scan"
                assert '"COLLSCAN"' not in plan, f"{name} shows a collection scan"
            print(f"PASS: 100,000 records; 5 routes and 5 explain plans; total {time.perf_counter()-started:.2f}s", flush=True)
        finally:
            db.client.drop_database(db.name)
            print("Dropped disposable benchmark database", flush=True)


if __name__ == "__main__":
    main()
