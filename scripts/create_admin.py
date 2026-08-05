#!/usr/bin/env python3
"""Create or reset a console admin from the command line.

The recovery path for the one failure the console cannot fix from inside
itself: no usable admin account. User management is admin-only, so if the last
admin is lost there is no way in through the web UI — this script talks to the
database directly.

    # inside the agent pod
    python scripts/create_admin.py alice
    python scripts/create_admin.py alice --reset

It reads the same database configuration as the agent (``KNOWLEDGE_BASE_PG_*``
or ``CFOP_AUTH_DB_URL``), so run it where those are set.
"""

import argparse
import getpass
import sys

sys.path.insert(0, ".")

from auth.models import ROLE_ADMIN, ROLE_MEMBER  # noqa: E402
from auth.store import AuthError, AuthStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or reset a console user.")
    parser.add_argument("username")
    parser.add_argument(
        "--role", choices=[ROLE_ADMIN, ROLE_MEMBER], default=ROLE_ADMIN,
        help="role to assign (default: admin)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="set a new password on an existing user instead of creating one",
    )
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    if not password:
        print("error: password cannot be empty", file=sys.stderr)
        return 1
    if password != getpass.getpass("Confirm: "):
        print("error: passwords do not match", file=sys.stderr)
        return 1

    store = AuthStore()
    store.ensure_schema()

    try:
        if args.reset:
            existing = store.get_user_by_username(args.username)
            if existing is None:
                print(f"error: no such user {args.username!r}", file=sys.stderr)
                return 1
            store.set_password(existing["id"], password, actor="create_admin.py")
            # A password reset on a locked-out admin is worthless if the account
            # is also deactivated or demoted, which is exactly the state that
            # sends someone to this script.
            store.update_user(existing["id"], role=args.role, is_active=True, actor="create_admin.py")
            print(f"reset password for {args.username} (role={args.role})")
        else:
            user = store.create_user(args.username, password, role=args.role, actor="create_admin.py")
            print(f"created {user['username']} (role={user['role']}, id={user['id']})")
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
