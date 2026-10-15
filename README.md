# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6.0+ implementation of the HROne Software Engineer Trainee assignment.

## Run locally (Windows PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a local `.env` file (do not commit it):

```text
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Load the supplied examples and start the API:

```powershell
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation. `GET /health` checks MongoDB connectivity. Indexes are created idempotently at startup.

## Important implementation notes

- Instants cross the API boundary as epoch milliseconds and are stored as UTC BSON dates.
- The unique indexes enforce employee-code and employee/date uniqueness under concurrent requests.
- Analytics use MongoDB aggregation pipelines. Review the pipelines and run edge-case tests before submitting.
- This is an assignment implementation, not production HR software: it has no authentication/authorization, holiday calendar, or deployment hardening.
