import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pymongo import MongoClient, UpdateOne
from pymongo.collection import Collection

from server.routes import router as review_router


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://127.0.0.1:27017")
MONGODB_DB = os.getenv("MONGODB_DB", "employee360")
HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "3000"))
SEED_QUESTIONS_PATH = Path(__file__).with_name("question-bank.json")

with SEED_QUESTIONS_PATH.open(encoding="utf-8") as seed_file:
    SEED_QUESTIONS: list[dict[str, Any]] = json.load(seed_file)


class Question(BaseModel):
    id: str
    value: str
    order: int
    text: str


@asynccontextmanager
async def lifespan(app: FastAPI):
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
        database = client[MONGODB_DB]
        questions: Collection = database["questions"]
        employees: Collection = database["employees"]
        cycles: Collection = database["reviewCycles"]
        assignments: Collection = database["assignments"]
        responses: Collection = database["responses"]
        action_plans: Collection = database["actionPlans"]

        questions.create_index("id", unique=True)
        employees.create_index("employee_id", unique=True)
        cycles.create_index("cycleId", unique=True)
        assignments.create_index(
            [("cycleId", 1), ("reviewerId", 1), ("subjectId", 1)],
            unique=True,
        )
        responses.create_index([("submittedAt", -1)])
        responses.create_index(
            [("assignmentId", 1)],
            unique=True,
            partialFilterExpression={"assignmentId": {"$type": "string"}},
        )
        action_plans.create_index([("employeeId", 1), ("cycleId", 1), ("createdAt", -1)])
        if SEED_QUESTIONS:
            questions.bulk_write([
                UpdateOne(
                    {"id": question["id"]},
                    {"$setOnInsert": question},
                    upsert=True,
                )
                for question in SEED_QUESTIONS
            ])

        app.state.mongo_client = client
        app.state.questions = questions
        app.state.employees = employees
        app.state.cycles = cycles
        app.state.assignments = assignments
        app.state.responses = responses
        app.state.action_plans = action_plans
        app.state.session_secret = os.getenv("SESSION_SECRET") or secrets.token_urlsafe(32)
        app.state.cookie_secure = os.getenv("COOKIE_SECURE", "false").lower() == "true"
        app.state.anonymity_threshold = max(3, int(os.getenv("ANONYMITY_THRESHOLD", "3")))
        yield
    finally:
        client.close()


app = FastAPI(title="Employee360 API", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=PROJECT_ROOT / "assets"), name="assets")
app.include_router(review_router)


@app.exception_handler(HTTPException)
async def handle_http_error(request: Request, error: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=error.status_code, content={"error": error.detail})


@app.get("/", include_in_schema=False)
def frontend() -> FileResponse:
    return FileResponse(PROJECT_ROOT / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/questions", response_model=list[Question])
def list_questions(request: Request) -> list[dict[str, Any]]:
    return list(request.app.state.questions.find({}, {"_id": 0}).sort("order", 1))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
