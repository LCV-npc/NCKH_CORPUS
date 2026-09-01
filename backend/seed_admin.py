"""Create or reset the one administrator account without exposing a default password."""

from __future__ import annotations

import argparse
import getpass

from core.auth import create_or_update_admin, ensure_auth_review_schema
from main import db_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or reset an ADMIN user for MedNLP Studio")
    parser.add_argument("--name", required=True, help="Administrator full name")
    parser.add_argument("--email", required=True, help="Administrator email")
    args = parser.parse_args()
    password = getpass.getpass("Administrator password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Password confirmation does not match.")
    ensure_auth_review_schema(db_config)
    user = create_or_update_admin(db_config, args.name, args.email, password)
    print(f"Admin ready: {user['email']} (id={user['id']})")


if __name__ == "__main__":
    main()
