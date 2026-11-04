# Employee360

Employee360 is a local 360-degree feedback and development application for IB Group. It collects self, manager, peer, and direct-report feedback in review cycles, stores the submissions in MongoDB, and presents role-limited summaries and employee-owned development plans.

## Project report

### Current capabilities

- HR administrators can create dated review cycles, see participation progress, and view employee reports.
- Cycle setup generates self, manager, and direct-report assignments from employee `manager_id` links. HR can select peer reviewers for each employee; the app excludes the subject, their manager, and their direct reports from peer assignments.
- Employees and managers see their assigned reviews in an inbox and submit answers to the 50 shared value questions, with an optional comment.
- Employees can view their own report and create or complete development-plan goals. Managers can view reports for direct reports. HR administrators can view reports for all employees.
- Peer and direct-report report sections are hidden until at least 3 reviewers in that group submit. This reduces exposure in small groups but cannot guarantee anonymity; comments can identify a reviewer by context.
- The mobile number is used as the login password, but the account provisioning script stores only a PBKDF2 hash. The raw mobile number is not saved with a response.

### Technologies

- Frontend: Vue 3, HTML, and responsive CSS. Vue is loaded from a CDN.
- Backend: Python 3.11+, FastAPI, Pydantic, and Uvicorn.
- Database: MongoDB, accessed through PyMongo.
- Configuration: `python-dotenv` reads local settings from `.env`.
- Tests: Python `unittest` with in-memory MongoDB collection stubs.

### Main files

- `index.html`: Vue interface for login, HR cycle management, review inbox, reports, and action plans.
- `server/main.py`: FastAPI app, MongoDB connection, seed setup, and frontend serving.
- `server/routes.py`: authentication, review-cycle, assignment, report, and development-plan routes.
- `server/review_logic.py`: assignment generation and privacy-threshold report aggregation.
- `server/security.py`: PBKDF2 mobile-number password hashing and signed session tokens.
- `server/question-bank.json`: the 50 seed questions across six values.
- `server/provision_accounts.py`: interactive one-time password-hash provisioning for active employees.
- `tests/test_api.py`: backend validation and review workflow tests.

## Requirements

- Python 3.11 or newer
- MongoDB Community running locally, or a MongoDB Atlas database
- MongoDB Compass (optional, for browsing database documents)

## Start the application

First, make sure the MongoDB service is running. On Windows, if needed, open PowerShell as Administrator and run:

```powershell
Start-Service -Name MongoDB
Get-Service -Name MongoDB
```

Then, from the project directory in a regular PowerShell terminal:

```powershell
if (-not (Test-Path .venv)) { py -3.11 -m venv .venv }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m server.main
```

Open **http://127.0.0.1:3000/** in a browser. Keep the terminal running while you use the app. FastAPI connects to MongoDB, creates the required collections/indexes, and inserts any missing questions from `server/question-bank.json`.

### Provision employee accounts before sign-in

Every active document in the `employees` collection needs a `password_hash` before its employee can sign in. Run the following in a second project terminal:

```powershell
.\.venv\Scripts\python.exe -m server.provision_accounts
```

The script prompts for each active employee's 10-digit mobile number twice using hidden input, then stores only a PBKDF2 hash. Enter the numbers directly into your terminal; never put them in source files or paste them into chat. The script skips accounts that already have a hash and can be run again when new employee records are added.

Employee records in `employees` must have a unique `employee_id`, `account_status: "active"`, a `role` (`hr_admin`, `manager`, or `employee`), and valid `manager_id` links. At least one account must have the `hr_admin` role so someone can create review cycles.

## End-to-end workflow

1. HR prepares employee profiles in MongoDB with IDs, roles, departments, and manager links, then runs the account-provisioning command above.
2. HR signs in, names a review cycle, sets its opening and closing dates, and selects eligible peers for each employee. Self, manager, and direct-report assignments are generated from the reporting links.
3. Each employee signs in and completes their assigned reviews from **Review inbox**. Each assignment can be submitted once while its cycle is open.
4. An employee reviews their own summary and can add development goals. A manager can view direct-report summaries; HR can view all summaries and cycle participation.
5. Peer and direct-report results appear only when that relationship group has at least 3 submissions. Use a larger peer pool where possible; smaller groups remain hidden.

There is no LLM scoring in this version. The reports show grouped rating averages and comments for development discussions. They are not a validated psychometric test and should not be used as the sole basis for employment decisions.

## Where to see results

- In the app, employees use **My report**, managers use **Direct reports**, and HR uses **Employee reports**. HR can select a cycle and open **View progress** for submission counts by relationship.
- In MongoDB Compass, connect to `mongodb://127.0.0.1:27017`, open the `employee360` database, then browse `reviewCycles`, `assignments`, `responses`, or `actionPlans`.
- In `mongosh`, inspect the data with:

```javascript
use employee360
show collections
db.reviewCycles.find().pretty()
db.assignments.find({}, { cycleId: 1, reviewerId: 1, subjectId: 1, relationship: 1, status: 1, _id: 0 }).pretty()
db.responses.find({}, { cycleId: 1, subjectId: 1, reviewerId: 1, relationship: 1, submittedAt: 1, answers: 1, comment: 1, _id: 0 }).sort({ submittedAt: -1 }).pretty()
db.actionPlans.find().sort({ createdAt: -1 }).pretty()
```

`responses` contains `cycleId`, `subjectId`, `reviewerId`, `relationship`, timestamp, question snapshots, ratings, and an optional comment. Reviewer IDs stay in MongoDB for audit but are not returned by the report API. Older self-reflection test records may lack a `cycleId` and therefore do not appear in 360 reports; they are not automatically migrated.

## API routes

- `GET /api/health`, `GET /api/questions`
- `POST /api/login`, `POST /api/logout`, `GET /api/me`
- `GET/POST /api/review-cycles`, `GET /api/review-cycles/{cycle_id}/progress`
- `GET /api/employees` (HR only), `GET /api/management/direct-reports`
- `GET /api/my/assignments`, `GET /api/assignments/{assignment_id}`, `POST /api/assignments/{assignment_id}/responses`
- `GET /api/reports/{cycle_id}/{subject_id}`
- `GET/POST /api/action-plans`, `PATCH /api/action-plans/{plan_id}`

Interactive API documentation is available at **http://127.0.0.1:3000/docs** while the server is running.

## Configuration and tests

For MongoDB Atlas, set `MONGODB_URI` in `.env` to the Atlas connection string and keep the file private; `.env` is ignored by Git. Set `MONGODB_DB` to the intended database. Set a stable random `SESSION_SECRET` if sessions should survive server restarts, and set `COOKIE_SECURE=true` when serving over HTTPS. The default bind address is `127.0.0.1`.

Run the backend tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## Security and production notes

The 10-digit mobile number is a low-entropy password even when hashed. Before real employee use, prefer OTP or SSO, add login rate limiting, secure MongoDB with authentication, enable HTTPS, maintain backups, and define audit and retention policies. The local MongoDB instance may warn that access control is disabled; keep it on loopback and do not expose it to a network. Review anonymity and privacy expectations with your organization; comments can still identify people by context.

Run backend tests with:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

For Atlas, replace `MONGODB_URI` in `.env` with its connection string. Keep `.env` private; it is ignored by Git. Use a stable, randomly generated `SESSION_SECRET` if sessions should remain valid across server restarts. Set `COOKIE_SECURE=true` when serving over HTTPS.

## MongoDB collections

- `employees`: employee profile, role, reporting link, account status, and a password hash. Mobile numbers are not stored.
- `questions`: stable question ID, value category, order, and prompt text.
- `reviewCycles`: cycle dates, creator, and peer/direct-report anonymity threshold.
- `assignments`: one reviewer-to-subject task per relationship and cycle, with pending/submitted status.
- `responses`: cycle, subject, rater relationship, ratings, comments, and submission time. Reviewer IDs stay in the database and are not returned in reports.
- `actionPlans`: employee-owned goal, value area, due date, and status.

The API includes login/session routes, cycle creation and progress, reviewer inbox and submissions, role-protected reports, and employee-owned action plans. Peer and direct-report group results are hidden below the threshold of 3 responses. Comments can still reveal identity from context, so avoid sensitive details and never promise anonymity absolutely.

## Before production

The mobile number is a low-entropy password even when stored as a hash; use a stronger sign-in method such as OTP or SSO before real deployment. Add rate limiting, database authentication, backups, audit/retention policies, HTTPS, and organizational privacy review. The API is bound to `127.0.0.1` by default. These questions are not a validated psychometric instrument; reports are for structured development conversations, not automated employment decisions.
