# Employee Attendance & Analytics API

Python 3.11+ · FastAPI · PyMongo · MongoDB 6.0+

## Start

1. Start a MongoDB 6.0+ server and create a Python virtual environment.
2. `pip install -r requirements.txt`
3. Set `MONGO_URI` and `MONGO_DB` in your environment (or optionally use a **local, untracked** `.env`; environment values take priority). For a local development server, examples are `MONGO_URI=mongodb://localhost:27017` and `MONGO_DB=attendance_db`.
4. `uvicorn app.main:app --port 8000`
5. Check `http://localhost:8000/health` (ready only if MongoDB replies) and interactive API reference at `http://localhost:8000/docs`.

The service creates its indexes automatically on startup. It **never** resets, loads, or modifies preexisting test data at startup. Timestamps in the JSON API are epoch milliseconds; the database stores BSON UTC dates. Working dates and shifts use IST.

## Checks and notes

- Attendance identity is `(emp_code, date)` enforced by a unique index. Duplicate creates raise 409.
- Punch-out uses an atomic conditional update. Manual corrections append audit entries using optimistic concurrency checks.
- All four analytics are MongoDB aggregation pipelines, including an employee-anchored department summary and an in-pipeline generated calendar for empty trend days.
- `GET /admin/explain/{endpoint}` provides MongoDB `executionStats` for the matching indexed query/pipeline.
- Read `REVIEW.md` for corrected starter defects and `DECISIONS.md` for design explanations.

**Verification status:** Syntax compilation and isolated helper/contract checks have been performed. This environment did not provide a running MongoDB server or the PyMongo package, so **live database, concurrency, explain-plan and 100k-document tests remain unverified**. Run those checks on MongoDB 7 before treating this as a validated submission. No intentionally omitted API routes.