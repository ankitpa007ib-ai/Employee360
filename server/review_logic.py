from collections import defaultdict
from statistics import mean
from typing import Any


RELATIONSHIP_ORDER = ("self", "manager", "peer", "direct_report")
ANONYMOUS_RELATIONSHIPS = {"peer", "direct_report"}


def build_assignments(
    cycle_id: str,
    employees: list[dict[str, Any]],
    peer_reviewers: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    active = [employee for employee in employees if employee.get("account_status") == "active"]
    by_id = {employee["employee_id"]: employee for employee in active}
    peer_reviewers = peer_reviewers or {}
    assignments: dict[tuple[str, str], dict[str, Any]] = {}

    def add(reviewer_id: str, subject_id: str, relationship: str) -> None:
        if reviewer_id == subject_id or reviewer_id not in by_id or subject_id not in by_id:
            return
        assignments[(reviewer_id, subject_id)] = {
            "cycleId": cycle_id,
            "reviewerId": reviewer_id,
            "subjectId": subject_id,
            "relationship": relationship,
            "status": "pending",
        }

    for subject in active:
        subject_id = subject["employee_id"]
        assignments[(subject_id, subject_id)] = {
            "cycleId": cycle_id,
            "reviewerId": subject_id,
            "subjectId": subject_id,
            "relationship": "self",
            "status": "pending",
        }

        manager_id = subject.get("manager_id")
        manager = by_id.get(manager_id)
        if manager and manager.get("role") in {"manager", "hr_admin"}:
            add(manager_id, subject_id, "manager")

        if subject.get("role") in {"manager", "hr_admin"}:
            for report in active:
                if report.get("manager_id") == subject_id:
                    add(report["employee_id"], subject_id, "direct_report")

        if manager_id and subject_id not in peer_reviewers:
            for peer in active:
                peer_id = peer["employee_id"]
                is_peer = (
                    peer_id != subject_id
                    and peer.get("manager_id") == manager_id
                    and peer.get("department") == subject.get("department")
                    and peer.get("role") == subject.get("role")
                    and peer.get("role") in {"employee", "manager"}
                )
                if is_peer:
                    add(peer_id, subject_id, "peer")

        for reviewer_id in peer_reviewers.get(subject_id, []):
            if (reviewer_id, subject_id) not in assignments and reviewer_id != subject_id:
                add(reviewer_id, subject_id, "peer")

    return list(assignments.values())


def build_report_sections(
    responses: list[dict[str, Any]], anonymity_threshold: int = 3
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for response in responses:
        grouped[response["relationship"]].append(response)

    sections = []
    for relationship in RELATIONSHIP_ORDER:
        group = grouped.get(relationship, [])
        if relationship in ANONYMOUS_RELATIONSHIPS and len(group) < anonymity_threshold:
            sections.append({
                "relationship": relationship,
                "available": False,
                "message": f"Feedback is hidden until at least {anonymity_threshold} reviewers respond.",
            })
            continue
        if not group:
            sections.append({"relationship": relationship, "available": False, "message": "No feedback yet."})
            continue

        by_category: dict[str, list[int]] = defaultdict(list)
        for response in group:
            for answer in response.get("answers", []):
                by_category[answer["category"]].append(answer["rating"])

        sections.append({
            "relationship": relationship,
            "available": True,
            "reviewerCount": len(group),
            "categoryAverages": {
                category: round(mean(ratings), 2)
                for category, ratings in sorted(by_category.items())
            },
            "comments": [
                response["comment"]
                for response in group
                if response.get("comment")
            ],
            "anonymityNotice": (
                "Responses are grouped without reviewer names, but comments may still be identifiable from context."
                if relationship in ANONYMOUS_RELATIONSHIPS
                else None
            ),
        })

    return sections
