"""Review-page accounts, managed from the command line (there is no self-registration).

  docker compose exec review-web python -m app.web.users add --email a@example.com --name "A Person" \\
      --role reviewer --scope live
  docker compose exec review-web python -m app.web.users list
  ... set-password --email …   deactivate --email …   activate --email …   unlock --email …

Passwords are typed at a prompt (twice), never passed as arguments (they would stay in the shell
history and the process list). --password-stdin reads one line instead, for scripted setups.
Changing a password or deactivating an account ends its sessions.
"""

import argparse
import getpass
import sys
from collections.abc import Callable
from typing import get_args

from .. import db
from ..review import Scope
from .accounts import Role, UserError, add_user, list_users, set_active, set_password, unlock
from .security import PasswordPolicyError
from .settings import WebSettings


def _password(from_stdin: bool) -> str:
    if from_stdin:
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass("Password: ")
    if first != getpass.getpass("Password again: "):
        raise UserError("the two passwords differ")
    return first


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m app.web.users", description="Review-page accounts")
    sub = p.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="create an account")
    add.add_argument("--email", required=True)
    add.add_argument("--name", required=True, help="display name, shown in the audit trail")
    add.add_argument("--role", required=True, choices=get_args(Role))
    add.add_argument("--scope", required=True, choices=get_args(Scope))
    add.add_argument("--password-stdin", action="store_true")
    pw = sub.add_parser("set-password", help="new password (ends the account's sessions)")
    pw.add_argument("--email", required=True)
    pw.add_argument("--password-stdin", action="store_true")
    for name in ("deactivate", "activate", "unlock"):
        sub.add_parser(name).add_argument("--email", required=True)
    sub.add_parser("list")
    return p


def _show(conn: db.Conn) -> None:
    for u in list_users(conn):
        state = "active" if u["active"] else "inactive"
        locked = f", locked until {u['locked_until']:%Y-%m-%d %H:%M}" if u["locked_until"] else ""
        print(f"{u['id']:>3}  {u['email']:<32} {u['display_name']:<24} {u['role']:<8} {u['scope']:<5} {state}{locked}")


def _run(conn: db.Conn, a: argparse.Namespace) -> str:
    commands: dict[str, Callable[[], object]] = {
        "add": lambda: add_user(conn, a.email, a.name, a.role, a.scope, _password(a.password_stdin)),
        "set-password": lambda: set_password(conn, a.email, _password(a.password_stdin)),
        "deactivate": lambda: set_active(conn, a.email, False),
        "activate": lambda: set_active(conn, a.email, True),
        "unlock": lambda: unlock(conn, a.email),
        "list": lambda: _show(conn),
    }
    result = commands[a.command]()
    return f"done: {a.command}" + (f" (user {result})" if isinstance(result, int) else "")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    url = WebSettings().ap_database_url.get_secret_value()  # from the environment
    try:
        with db.connect(url) as conn:
            message = _run(conn, args)
    except (UserError, PasswordPolicyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.command != "list":
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
