from datetime import date, datetime, timezone
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from server.review_logic import build_assignments, build_report_sections
from server.security import create_session_token, read_session_token, verify_mobile_number


router = APIRouter()
SESSION_COOKIE = "employee360_session"


class LoginRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    employee_name: str = Field(alias="employeeName", min_length=1, max_length=120)
    mobile_number: str = Field(alias="mobileNumber", pattern=r"^\d{10}$")


class ReviewCycleCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    starts_on: date = Field(alias="startsOn")
    ends_on: date = Field(alias="endsOn")
    peer_reviewers: dict[str, list[str]] = Field(alias="peerReviewers", default_factory=dict)

    model_config = ConfigDict(populate_by_name=True)


class ReviewAnswer(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    question_id: str = Field(alias="questionId", min_length=1)
    value: int = Field(strict=True, ge=1, le=5)


class ReviewSubmission(BaseModel):
    answers: list[ReviewAnswer]
    comment: str = Field(default="", max_length=2000)


class ActionPlanCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    cycle_id: str = Field(alias="cycleId", min_length=1)
    category: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=3, max_length=500)
    due_on: date = Field(alias="dueOn")


class ActionPlanUpdate(BaseModel):
    status: str = Field(pattern=r"^(open|completed)$")


def public_employee(employee: dict[str, Any]) -> dict[str, Any]:
    return {
        "employeeId": employee["employee_id"],
        "name": employee.get("name", ""),
        "role": employee.get("role", "employee"),
        "department": employee.get("department", ""),
        "designation": employee.get("designation", ""),
    }


def current_employee(request: Request) -> dict[str, Any]:
    secret = request.app.state.session_secret
    employee_id = read_session_token(request.cookies.get(SESSION_COOKIE), secret)
    if not employee_id:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    employee = request.app.state.employees.find_one({
        "employee_id": employee_id,
        "account_status": "active",
    })
    if not employee:
        raise HTTPException(status_code=401, detail="Sign in to continue.")
    return employee


def require_role(employee: dict[str, Any], *roles: str) -> None:
    if employee.get("role") not in roles:
        raise HTTPException(status_code=403, detail="You do not have permission to do that.")


def report_permission(viewer: dict[str, Any], subject: dict[str, Any]) -> bool:
    return (
        viewer.get("role") == "hr_admin"
        or viewer.get("employee_id") == subject.get("employee_id")
        or (
            viewer.get("role") == "manager"
            and subject.get("manager_id") == viewer.get("employee_id")
        )
    )


def validate_answers(answers: list[ReviewAnswer], questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    question_by_id = {question["id"]: question for question in questions}
    if not question_by_id or len(answers) != len(question_by_id):
        raise HTTPException(status_code=400, detail="Answer every question before submitting.")

    saved_answers = []
    seen_question_ids: set[str] = set()
    for answer in answers:
        question = question_by_id.get(answer.question_id)
        if question is None or answer.question_id in seen_question_ids:
            raise HTTPException(status_code=400, detail="The submitted answers are invalid.")
        seen_question_ids.add(answer.question_id)
        saved_answers.append({
            "questionId": answer.question_id,
            "category": question["value"],
            "questionText": question["text"],
            "rating": answer.value,
        })

    if seen_question_ids != question_by_id.keys():
        raise HTTPException(status_code=400, detail="Answer every question before submitting.")
    return saved_answers


def assignment_for_reviewer(assignment_id: str, reviewer_id: str, request: Request) -> dict[str, Any]:
    assignment = request.app.state.assignments.find_one({
        "assignmentId": assignment_id,
        "reviewerId": reviewer_id,
    }, {"_id": 0})
    if not assignment:
        raise HTTPException(status_code=404, detail="Review assignment not found.")
    return assignment


def cycle_is_open(cycle: dict[str, Any]) -> bool:
    today = datetime.now(timezone.utc).date().isoformat()
    return cycle["startsOn"] <= today <= cycle["endsOn"]


@router.post("/api/login")
def login(credentials: LoginRequest, request: Request, response: Response) -> dict[str, Any]:
    name = credentials.employee_name.strip().casefold()
    if not name:
        raise HTTPException(status_code=401, detail="Name or password is incorrect.")

    candidates = request.app.state.employees.find({"account_status": "active"})
    employee = next((
        candidate for candidate in candidates
        if candidate.get("name", "").strip().casefold() == name
        and verify_mobile_number(credentials.mobile_number, candidate.get("password_hash"))
    ), None)
    if not employee:
        raise HTTPException(status_code=401, detail="Name or password is incorrect.")

    token = create_session_token(employee["employee_id"], request.app.state.session_secret)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=28_800,
        httponly=True,
        secure=request.app.state.cookie_secure,
        samesite="lax",
        path="/",
    )
    return {"employee": public_employee(employee)}


@router.post("/api/logout", status_code=204)
def logout(response: Response) -> Response:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@router.get("/api/me")
def me(request: Request) -> dict[str, Any]:
    return {"employee": public_employee(current_employee(request))}


@router.post("/api/review-cycles", status_code=201)
def create_review_cycle(cycle_input: ReviewCycleCreate, request: Request) -> dict[str, Any]:
    administrator = current_employee(request)
    require_role(administrator, "hr_admin")
    if cycle_input.ends_on < cycle_input.starts_on:
        raise HTTPException(status_code=400, detail="The end date must be on or after the start date.")

    employees = list(request.app.state.employees.find({"account_status": "active"}, {"_id": 0}))
    if not employees:
        raise HTTPException(status_code=400, detail="Add active employee profiles before creating a cycle.")
    employees_by_id = {employee["employee_id"]: employee for employee in employees}
    for subject_id, reviewer_ids in cycle_input.peer_reviewers.items():
        subject = employees_by_id.get(subject_id)
        if not subject or len(reviewer_ids) != len(set(reviewer_ids)):
            raise HTTPException(status_code=400, detail="Peer reviewer selections are invalid.")
        for reviewer_id in reviewer_ids:
            reviewer = employees_by_id.get(reviewer_id)
            if (
                not reviewer
                or reviewer_id == subject_id
                or reviewer.get("role") not in {"employee", "manager"}
                or reviewer_id == subject.get("manager_id")
                or reviewer.get("manager_id") == subject_id
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Peer reviewers cannot be the employee, their manager, or their direct reports.",
                )

    cycle_id = secrets.token_urlsafe(16)
    cycle = {
        "cycleId": cycle_id,
        "name": cycle_input.name.strip(),
        "startsOn": cycle_input.starts_on.isoformat(),
        "endsOn": cycle_input.ends_on.isoformat(),
        "anonymityThreshold": request.app.state.anonymity_threshold,
        "createdBy": administrator["employee_id"],
        "createdAt": datetime.now(timezone.utc),
    }
    assignment_docs = [
        {**assignment, "assignmentId": secrets.token_urlsafe(16)}
        for assignment in build_assignments(cycle_id, employees, cycle_input.peer_reviewers)
    ]
    request.app.state.cycles.insert_one(cycle)
    if assignment_docs:
        request.app.state.assignments.insert_many(assignment_docs)
    counts = {
        relationship: sum(item["relationship"] == relationship for item in assignment_docs)
        for relationship in ("self", "manager", "peer", "direct_report")
    }
    return {"cycleId": cycle_id, "name": cycle["name"], "assignmentCounts": counts}


@router.get("/api/review-cycles")
def list_review_cycles(request: Request) -> list[dict[str, Any]]:
    current_employee(request)
    today = datetime.now(timezone.utc).date().isoformat()
    return [
        {
            "cycleId": cycle["cycleId"],
            "name": cycle["name"],
            "startsOn": cycle["startsOn"],
            "endsOn": cycle["endsOn"],
            "status": "scheduled" if today < cycle["startsOn"] else "closed" if today > cycle["endsOn"] else "open",
        }
        for cycle in request.app.state.cycles.find({}, {"_id": 0}).sort("createdAt", -1)
    ]


@router.get("/api/employees")
def list_employees(request: Request) -> list[dict[str, Any]]:
    administrator = current_employee(request)
    require_role(administrator, "hr_admin")
    return [
        {**public_employee(employee), "managerId": employee.get("manager_id")}
        for employee in request.app.state.employees.find({"account_status": "active"})
    ]


@router.get("/api/review-cycles/{cycle_id}/progress")
def cycle_progress(cycle_id: str, request: Request) -> dict[str, Any]:
    administrator = current_employee(request)
    require_role(administrator, "hr_admin")
    cycle = request.app.state.cycles.find_one({"cycleId": cycle_id}, {"_id": 0})
    if not cycle:
        raise HTTPException(status_code=404, detail="Review cycle not found.")
    counts = list(request.app.state.assignments.aggregate([
        {"$match": {"cycleId": cycle_id}},
        {"$group": {"_id": {"relationship": "$relationship", "status": "$status"}, "count": {"$sum": 1}}},
    ]))
    summary = {"total": 0, "submitted": 0, "pending": 0}
    by_relationship: dict[str, dict[str, int]] = {}
    for item in counts:
        relationship = item["_id"]["relationship"]
        status = item["_id"]["status"]
        count = item["count"]
        summary["total"] += count
        summary["submitted" if status == "submitted" else "pending"] += count
        by_relationship.setdefault(relationship, {"submitted": 0, "pending": 0})
        by_relationship[relationship]["submitted" if status == "submitted" else "pending"] += count
    return {"cycle": cycle, "summary": summary, "byRelationship": by_relationship}


@router.get("/api/my/assignments")
def my_assignments(request: Request) -> list[dict[str, Any]]:
    employee = current_employee(request)
    assignments = list(request.app.state.assignments.find(
        {"reviewerId": employee["employee_id"]}, {"_id": 0}
    ).sort([("cycleId", -1), ("relationship", 1)]))
    employee_ids = {item["subjectId"] for item in assignments}
    subjects = {
        subject["employee_id"]: subject
        for subject in request.app.state.employees.find(
            {"employee_id": {"$in": list(employee_ids)}},
            {"_id": 0, "employee_id": 1, "name": 1, "department": 1},
        )
    }
    cycle_ids = list({item["cycleId"] for item in assignments})
    cycles = {
        cycle["cycleId"]: cycle
        for cycle in request.app.state.cycles.find(
            {"cycleId": {"$in": cycle_ids}},
            {"_id": 0, "cycleId": 1, "name": 1, "endsOn": 1},
        )
    } if cycle_ids else {}
    return [
        {
            "assignmentId": assignment["assignmentId"],
            "cycleId": assignment["cycleId"],
            "cycleName": cycles.get(assignment["cycleId"], {}).get("name", "Review cycle"),
            "dueOn": cycles.get(assignment["cycleId"], {}).get("endsOn"),
            "relationship": assignment["relationship"],
            "status": assignment["status"],
            "subject": {
                "employeeId": assignment["subjectId"],
                "name": subjects.get(assignment["subjectId"], {}).get("name", "Employee"),
                "department": subjects.get(assignment["subjectId"], {}).get("department", ""),
            },
        }
        for assignment in assignments
    ]


@router.get("/api/assignments/{assignment_id}")
def assignment_detail(assignment_id: str, request: Request) -> dict[str, Any]:
    employee = current_employee(request)
    assignment = assignment_for_reviewer(assignment_id, employee["employee_id"], request)
    cycle = request.app.state.cycles.find_one({"cycleId": assignment["cycleId"]}, {"_id": 0})
    subject = request.app.state.employees.find_one(
        {"employee_id": assignment["subjectId"]},
        {"_id": 0, "employee_id": 1, "name": 1, "department": 1},
    )
    return {
        "assignmentId": assignment_id,
        "cycleName": cycle["name"],
        "dueOn": cycle["endsOn"],
        "relationship": assignment["relationship"],
        "status": assignment["status"],
        "isOpen": cycle_is_open(cycle) and assignment["status"] == "pending",
        "subject": {
            "employeeId": subject["employee_id"],
            "name": subject.get("name", "Employee"),
            "department": subject.get("department", ""),
        },
        "questions": list(request.app.state.questions.find({}, {"_id": 0}).sort("order", 1)),
    }


@router.post("/api/assignments/{assignment_id}/responses", status_code=201)
def submit_assignment_response(
    assignment_id: str,
    submission: ReviewSubmission,
    request: Request,
) -> dict[str, bool]:
    employee = current_employee(request)
    assignment = assignment_for_reviewer(assignment_id, employee["employee_id"], request)
    if assignment["status"] != "pending":
        raise HTTPException(status_code=409, detail="This review has already been submitted.")
    cycle = request.app.state.cycles.find_one({"cycleId": assignment["cycleId"]}, {"_id": 0})
    if not cycle_is_open(cycle):
        raise HTTPException(status_code=409, detail="This review cycle is not open.")

    questions = list(request.app.state.questions.find({}, {"_id": 0, "id": 1, "value": 1, "text": 1}))
    saved_answers = validate_answers(submission.answers, questions)
    response_document = {
        "assignmentId": assignment_id,
        "cycleId": assignment["cycleId"],
        "subjectId": assignment["subjectId"],
        "reviewerId": employee["employee_id"],
        "relationship": assignment["relationship"],
        "submittedAt": datetime.now(timezone.utc),
        "answers": saved_answers,
        "comment": submission.comment.strip(),
    }
    request.app.state.responses.insert_one(response_document)
    request.app.state.assignments.update_one(
        {"assignmentId": assignment_id, "status": "pending"},
        {"$set": {"status": "submitted", "submittedAt": response_document["submittedAt"]}},
    )
    return {"saved": True}


@router.get("/api/management/direct-reports")
def direct_reports(request: Request) -> list[dict[str, Any]]:
    manager = current_employee(request)
    require_role(manager, "manager", "hr_admin")
    return [
        public_employee(employee)
        for employee in request.app.state.employees.find(
            {"manager_id": manager["employee_id"], "account_status": "active"}
        )
    ]


@router.get("/api/reports/{cycle_id}/{subject_id}")
def employee_report(cycle_id: str, subject_id: str, request: Request) -> dict[str, Any]:
    viewer = current_employee(request)
    subject = request.app.state.employees.find_one({"employee_id": subject_id})
    cycle = request.app.state.cycles.find_one({"cycleId": cycle_id}, {"_id": 0})
    if not subject or not cycle:
        raise HTTPException(status_code=404, detail="Report not found.")
    if not report_permission(viewer, subject):
        raise HTTPException(status_code=403, detail="You do not have permission to view this report.")

    responses = list(request.app.state.responses.find(
        {"cycleId": cycle_id, "subjectId": subject_id}, {"_id": 0}
    ))
    threshold = max(request.app.state.anonymity_threshold, cycle.get("anonymityThreshold", 3))
    return {
        "cycle": {"cycleId": cycle_id, "name": cycle["name"]},
        "subject": public_employee(subject),
        "anonymityThreshold": threshold,
        "sections": build_report_sections(responses, threshold),
    }


@router.get("/api/action-plans")
def list_action_plans(request: Request, cycle_id: str = Query(alias="cycleId")) -> list[dict[str, Any]]:
    employee = current_employee(request)
    return list(request.app.state.action_plans.find(
        {"employeeId": employee["employee_id"], "cycleId": cycle_id},
        {"_id": 0, "planId": 1, "cycleId": 1, "category": 1, "goal": 1, "dueOn": 1, "status": 1, "createdAt": 1},
    ).sort("createdAt", -1))


@router.post("/api/action-plans", status_code=201)
def create_action_plan(plan: ActionPlanCreate, request: Request) -> dict[str, Any]:
    employee = current_employee(request)
    if not request.app.state.cycles.find_one({"cycleId": plan.cycle_id}):
        raise HTTPException(status_code=404, detail="Review cycle not found.")
    document = {
        "planId": secrets.token_urlsafe(16),
        "employeeId": employee["employee_id"],
        "cycleId": plan.cycle_id,
        "category": plan.category,
        "goal": plan.goal.strip(),
        "dueOn": plan.due_on.isoformat(),
        "status": "open",
        "createdAt": datetime.now(timezone.utc),
    }
    request.app.state.action_plans.insert_one(document)
    return {key: value for key, value in document.items() if key != "employeeId"}


@router.patch("/api/action-plans/{plan_id}")
def update_action_plan(plan_id: str, update: ActionPlanUpdate, request: Request) -> dict[str, bool]:
    employee = current_employee(request)
    result = request.app.state.action_plans.update_one(
        {"planId": plan_id, "employeeId": employee["employee_id"]},
        {"$set": {"status": update.status}},
    )
    if not result.matched_count:
        raise HTTPException(status_code=404, detail="Action plan not found.")
    return {"updated": True}
