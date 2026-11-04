from getpass import getpass
from pathlib import Path
import re

from dotenv import load_dotenv
from pymongo import MongoClient

from server.main import MONGODB_DB, MONGODB_URI
from server.security import hash_mobile_number


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOBILE_PATTERN = re.compile(r"^\d{10}$")


def main() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
        employees = client[MONGODB_DB].employees
        pending = list(employees.find(
            {"account_status": "active", "password_hash": {"$exists": False}},
            {"_id": 0, "employee_id": 1, "name": 1},
        ).sort("employee_id", 1))

        if not pending:
            print("No active employee accounts need provisioning.")
            return

        print(f"Provisioning {len(pending)} active employee accounts.")
        print("Enter each employee's 10-digit mobile number. Input is hidden and only a hash is stored.")
        for employee in pending:
            while True:
                mobile_number = getpass(f"{employee['employee_id']} ({employee['name']}): ")
                confirmation = getpass("Confirm mobile number: ")
                if mobile_number != confirmation:
                    print("The entries did not match. Try again.")
                    continue
                if not MOBILE_PATTERN.fullmatch(mobile_number):
                    print("Enter exactly 10 digits. Try again.")
                    continue
                break

            employees.update_one(
                {"employee_id": employee["employee_id"]},
                {"$set": {"password_hash": hash_mobile_number(mobile_number)}},
            )
            print("Account provisioned.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
