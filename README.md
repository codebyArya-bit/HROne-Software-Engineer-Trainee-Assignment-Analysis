# Employee Attendance & Analytics API

Python 3.11+ | FastAPI | PyMongo | MongoDB 6.0+

Implements the API contract from the HROne candidate kit. The application connects to a **running** MongoDB instance, creates indexes idempotently on startup, and never wipes or seeds the grader's database. API instants are integer epoch milliseconds; MongoDB stores UTC BSON dates; attendance days and shifts are interpreted in IST. Derived attendance values are saved on writes; all four analytics run as MongoDB aggregation pipelines.

## Start (Windows PowerShell)

1. Start **Docker Desktop**; wait until it says the engine is running, and ensure **Linux containers** mode. Verify with `docker info`.
2. From the repository root, run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
# First use only: create and start the MongoDB container.
docker run -d --name hrone-mongo -p 27017:27017 mongo:7
# On later runs: docker start hrone-mongo
# Confirm MongoDB responds before starting the API:
docker exec hrone-mongo mongosh --quiet --eval "db.adminCommand({ping:1})"
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
python -m uvicorn app.main:app --port 8000
```

3. Keep that PowerShell terminal open. In a **second** terminal, check:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Start-Process http://127.0.0.1:8000/docs
```

If Docker reports `dockerDesktopLinuxEngine ... The system cannot find the file specified`, the Docker Desktop engine is not running. Fix Docker first; `MONGO_URI` is only a connection address and **does not** launch MongoDB. If Uvicorn exits with `ServerSelectionTimeoutError`, check `docker ps`, `docker start hrone-mongo` and the MongoDB ping above. Until Uvicorn is running, `Invoke-RestMethod` will report `Unable to connect to the remote server`; this is not an API response.

## Tests (use a disposable test database)

After MongoDB is running, in an activated virtual environment:

```powershell
python -m pip install -r requirements-dev.txt
$env:TEST_MONGO_DB = "hrone_candidate_test_isolated"
python -m pytest -q
```

**WARNING:** The test fixture drops the database named `hrone_candidate_test_isolated` before and after every test; do not put valuable data there. GitHub Actions also runs the test suite against its own disposable MongoDB 7 service. For a clean installation without Docker, a MongoDB 6+ server or MongoDB Atlas cluster works by setting `MONGO_URI` and `MONGO_DB` appropriately.

## Design, review, and limits

- `REVIEW.md`: defects in the starter, concrete reproductions, and fixes.
- `DECISIONS.md`: indexes, concurrency, tie ranking, headcount, and scaling.
- Unique `(emp_code, date)` index makes duplicate punch-ins race-safe; punch-out is a conditional atomic update. Corrections use a conditional update and `$push` to preserve audit history.
- MongoDB indexes support list queries, analytics source filters, and the `/admin/explain/{endpoint}` endpoint.
- The candidate kit's tiny sample data is not included in this submitted repository; the grader provides its own dataset.
- Workload-scale testing (100k records), all hidden edge cases, and examiner grading remain unverified, even when integration tests pass.