import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError

from server.main import SEED_QUESTIONS, lifespan, list_questions
from server.review_logic import build_assignments, build_report_sections
from server.routes import (
    ReviewCycleCreate,
    ReviewSubmission,
    create_review_cycle,
    report_permission,
    validate_answers,
)
from server.security import (
    create_session_token,
    hash_mobile_number,
    read_session_token,
    verify_mobile_number,
)


class FakeCursor(list):
    def sort(self, key: str, direction: int):
        super().sort(key=lambda item: item[key], reverse=direction < 0)
        return self


class FakeCollection:
    def __init__(self, documents=None):
        self.documents = list(documents or [])
        self.inserted_document = None
        self.index_calls = []
        self.bulk_operations = []

    def create_index(self, keys, **options):
        self.index_calls.append((keys, options))

    def bulk_write(self, operations):
        self.bulk_operations.extend(operations)

    def find(self, query=None, projection=None):
        documents = self.documents
        if projection:
            included_fields = {key for key, included in projection.items() if included}
            if included_fields:
                documents = [
                    {key: document[key] for key in included_fields if key in document}
                    for document in documents
                ]
        return FakeCursor(documents)

    def insert_one(self, document):
        self.inserted_document = document
        return SimpleNamespace(inserted_id="response-id")

    def insert_many(self, documents):
        self.documents.extend(documents)
        return SimpleNamespace(inserted_ids=list(range(len(documents))))


def make_request():
    questions = FakeCollection(SEED_QUESTIONS)
    responses = FakeCollection()
    state = SimpleNamespace(questions=questions, responses=responses)
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return request, questions, responses


class FakeDatabase:
    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return get_fake_collection(self, name)


class FakeMongoClient:
    def __init__(self, database):
        self.admin = SimpleNamespace(command=lambda name: {"ok": 1})
        self.database = database
        self.closed = False

    def __getitem__(self, name):
        return self.database

    def close(self):
        self.closed = True


def get_fake_collection(database, name):
    if name not in database.collections:
        database.collections[name] = FakeCollection()
    return database.collections[name]


class ApiHandlerTests(unittest.TestCase):
    def test_report_access_is_limited_by_role_and_relationship(self):
        manager = {"employee_id": "manager-1", "role": "manager"}
        direct_report = {"employee_id": "employee-1", "manager_id": "manager-1"}
        unrelated_employee = {"employee_id": "employee-2", "manager_id": "manager-2"}
        hr_admin = {"employee_id": "hr-1", "role": "hr_admin"}

        self.assertTrue(report_permission(manager, direct_report))
        self.assertFalse(report_permission(manager, unrelated_employee))
        self.assertTrue(report_permission(direct_report, direct_report))
        self.assertTrue(report_permission(hr_admin, unrelated_employee))

    def test_assignment_generation_covers_360_relationships(self):
        employees = [
            {"employee_id": "manager", "role": "manager", "account_status": "active", "department": "Sales", "manager_id": None},
            {"employee_id": "employee-a", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "manager"},
            {"employee_id": "employee-b", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "manager"},
            {"employee_id": "employee-c", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "manager"},
        ]

        assignments = build_assignments("cycle-1", employees)
        relationships = {
            (assignment["reviewerId"], assignment["subjectId"]): assignment["relationship"]
            for assignment in assignments
        }

        self.assertEqual(relationships[("employee-a", "employee-a")], "self")
        self.assertEqual(relationships[("manager", "employee-a")], "manager")
        self.assertEqual(relationships[("employee-b", "employee-a")], "peer")
        self.assertEqual(relationships[("employee-a", "manager")], "direct_report")

    def test_selected_cross_team_peers_create_peer_assignments(self):
        employees = [
            {"employee_id": "subject", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "sales-manager"},
            {"employee_id": "sales-manager", "role": "manager", "account_status": "active", "department": "Sales", "manager_id": None},
            {"employee_id": "peer-a", "role": "employee", "account_status": "active", "department": "Finance", "manager_id": "finance-manager"},
            {"employee_id": "peer-b", "role": "employee", "account_status": "active", "department": "Operations", "manager_id": "ops-manager"},
            {"employee_id": "peer-c", "role": "manager", "account_status": "active", "department": "People", "manager_id": "people-manager"},
            {"employee_id": "finance-manager", "role": "manager", "account_status": "active", "department": "Finance", "manager_id": None},
            {"employee_id": "ops-manager", "role": "manager", "account_status": "active", "department": "Operations", "manager_id": None},
            {"employee_id": "people-manager", "role": "manager", "account_status": "active", "department": "People", "manager_id": None},
        ]

        assignments = build_assignments("cycle-1", employees, {"subject": ["peer-a", "peer-b", "peer-c"]})
        peer_assignments = [
            item for item in assignments
            if item["subjectId"] == "subject" and item["relationship"] == "peer"
        ]

        self.assertEqual({item["reviewerId"] for item in peer_assignments}, {"peer-a", "peer-b", "peer-c"})

    def test_explicit_empty_peer_selection_overrides_automatic_peers(self):
        employees = [
            {"employee_id": "manager", "role": "manager", "account_status": "active", "department": "Sales", "manager_id": None},
            {"employee_id": "employee-a", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "manager"},
            {"employee_id": "employee-b", "role": "employee", "account_status": "active", "department": "Sales", "manager_id": "manager"},
        ]

        assignments = build_assignments("cycle-1", employees, {"employee-a": []})

        self.assertFalse(any(
            item["subjectId"] == "employee-a" and item["relationship"] == "peer"
            for item in assignments
        ))

    def test_hr_cycle_creation_stores_selected_peers(self):
        employees = [
            {"employee_id": "hr", "role": "hr_admin", "account_status": "active"},
            {"employee_id": "manager", "role": "manager", "department": "Sales", "manager_id": None, "account_status": "active"},
            {"employee_id": "subject", "role": "employee", "department": "Sales", "manager_id": "manager", "account_status": "active"},
            {"employee_id": "peer-a", "role": "employee", "department": "Finance", "manager_id": None, "account_status": "active"},
            {"employee_id": "peer-b", "role": "employee", "department": "Operations", "manager_id": None, "account_status": "active"},
            {"employee_id": "peer-c", "role": "manager", "department": "People", "manager_id": None, "account_status": "active"},
        ]
        state = SimpleNamespace(
            employees=FakeCollection(employees),
            cycles=FakeCollection(),
            assignments=FakeCollection(),
            anonymity_threshold=3,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        cycle_input = ReviewCycleCreate.model_validate({
            "name": "Annual review",
            "startsOn": "2026-11-04",
            "endsOn": "2026-12-04",
            "peerReviewers": {"subject": ["peer-a", "peer-b", "peer-c"]},
        })

        with patch("server.routes.current_employee", return_value=employees[0]):
            result = create_review_cycle(cycle_input, request)

        peer_assignments = [
            item for item in state.assignments.documents
            if item["subjectId"] == "subject" and item["relationship"] == "peer"
        ]
        self.assertEqual(result["assignmentCounts"]["peer"], 3)
        self.assertEqual(len(peer_assignments), 3)

    def test_hr_cycle_rejects_manager_as_peer(self):
        employees = [
            {"employee_id": "hr", "role": "hr_admin", "account_status": "active"},
            {"employee_id": "manager", "role": "manager", "manager_id": None, "account_status": "active"},
            {"employee_id": "subject", "role": "employee", "manager_id": "manager", "account_status": "active"},
        ]
        state = SimpleNamespace(
            employees=FakeCollection(employees),
            cycles=FakeCollection(),
            assignments=FakeCollection(),
            anonymity_threshold=3,
        )
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        cycle_input = ReviewCycleCreate.model_validate({
            "name": "Annual review",
            "startsOn": "2026-11-04",
            "endsOn": "2026-12-04",
            "peerReviewers": {"subject": ["manager"]},
        })

        with patch("server.routes.current_employee", return_value=employees[0]):
            with self.assertRaises(HTTPException) as error:
                create_review_cycle(cycle_input, request)

        self.assertEqual(error.exception.status_code, 400)

    def test_report_hides_small_anonymous_groups(self):
        responses = [{"relationship": "peer", "answers": [], "comment": "feedback"}]

        sections = build_report_sections(responses, anonymity_threshold=3)
        peer_section = next(section for section in sections if section["relationship"] == "peer")

        self.assertFalse(peer_section["available"])
        self.assertNotIn("reviewerCount", peer_section)
        self.assertNotIn("comments", peer_section)

    def test_mobile_number_password_is_hashed_and_verified(self):
        mobile_hash = hash_mobile_number("1234567890")

        self.assertNotEqual(mobile_hash, "1234567890")
        self.assertTrue(verify_mobile_number("1234567890", mobile_hash))
        self.assertFalse(verify_mobile_number("0000000000", mobile_hash))

    def test_signed_session_token_rejects_tampering(self):
        token = create_session_token("employee-1", "test-secret")
        payload, signature = token.split(".", 1)

        self.assertEqual(read_session_token(token, "test-secret"), "employee-1")
        self.assertIsNone(read_session_token(f"{payload}.{signature}x", "test-secret"))

    def test_startup_connects_indexes_and_seeds_questions(self):
        from server import main

        database = FakeDatabase()
        client = FakeMongoClient(database)

        async def run_lifespan():
            async with lifespan(main.app):
                self.assertIs(main.app.state.mongo_client, client)

        with patch("server.main.MongoClient", return_value=client):
            asyncio.run(run_lifespan())

        questions = get_fake_collection(database, "questions")
        responses = get_fake_collection(database, "responses")
        self.assertEqual(len(questions.bulk_operations), 50)
        self.assertEqual(questions.index_calls, [("id", {"unique": True})])
        self.assertEqual(responses.index_calls, [
            ([('submittedAt', -1)], {}),
            ([('assignmentId', 1)], {
                'unique': True,
                'partialFilterExpression': {'assignmentId': {'$type': 'string'}},
            }),
        ])
        self.assertTrue(client.closed)

    def test_questions_are_sorted_for_frontend(self):
        request, _, _ = make_request()
        questions = list_questions(request)

        self.assertEqual(len(questions), 50)
        self.assertEqual([question["order"] for question in questions], list(range(1, 51)))

    def test_complete_review_answers_are_snapshotted(self):
        submission = ReviewSubmission.model_validate({
            "answers": [
                {"questionId": question["id"], "value": 4}
                for question in SEED_QUESTIONS
            ],
            "comment": "Clear communication and dependable follow-through.",
        })

        saved = validate_answers(submission.answers, SEED_QUESTIONS)

        self.assertEqual(len(saved), 50)
        self.assertEqual(saved[0]["rating"], 4)
        self.assertEqual(saved[0]["questionId"], "honesty-integrity-01")

    def test_incomplete_review_is_rejected(self):
        submission = ReviewSubmission.model_validate({
            "answers": [
                {"questionId": question["id"], "value": 4}
                for question in SEED_QUESTIONS[:-1]
            ],
        })

        with self.assertRaises(HTTPException) as error:
            validate_answers(submission.answers, SEED_QUESTIONS)

        self.assertEqual(error.exception.status_code, 400)

    def test_duplicate_question_is_rejected(self):
        answers = [
            {"questionId": question["id"], "value": 4}
            for question in SEED_QUESTIONS
        ]
        answers[-1] = answers[0]
        submission = ReviewSubmission.model_validate({"answers": answers})

        with self.assertRaises(HTTPException) as error:
            validate_answers(submission.answers, SEED_QUESTIONS)

        self.assertEqual(error.exception.status_code, 400)

    def test_rating_outside_scale_is_rejected(self):
        with self.assertRaises(ValidationError):
            ReviewSubmission.model_validate({
                "answers": [{"questionId": "honesty-integrity-01", "value": 6}],
            })


if __name__ == "__main__":
    unittest.main()
