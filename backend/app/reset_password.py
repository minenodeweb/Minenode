"""Reset a panel user's password from the terminal (``reset_password.sh``).

Lists the panel accounts, lets you pick one and gives it a new random 16-character password, printed
once in the terminal. Every session of that user is logged out. The running panel notices the
changed users.json by itself – no restart needed.

    python -m app.reset_password            # pick from the list
    python -m app.reset_password <username> # reset this one directly
"""
from __future__ import annotations

import os
import secrets
import string
import sys
import time

# Look-alike characters (0/O, 1/l/I) are left out so the password can be typed from the screen.
ALPHABET = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")
LENGTH = 16


def new_password(length: int = LENGTH) -> str:
    """Random password with at least one lower-case letter, one upper-case letter and one digit."""
    while True:
        pw = "".join(secrets.choice(ALPHABET) for _ in range(length))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw):
            return pw


def _choose(names: list[str]) -> str | None:
    print("Panel users:")
    for i, name in enumerate(names, 1):
        print(f"  {i}) {name}")
    while True:
        try:
            raw = input("Choose a user (number or name, empty to cancel): ").strip()
        except EOFError:
            return None
        if not raw:
            return None
        if raw.isdigit() and 1 <= int(raw) <= len(names):
            return names[int(raw) - 1]
        match = next((n for n in names if n.lower() == raw.lower()), None)
        if match:
            return match
        print("  No such user – try again.")


def _keep_owner(path) -> None:
    """Run as root (e.g. docker exec without -u): give users.json back to the owner of its folder."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            st = path.parent.stat()
            os.chown(path, st.st_uid, st.st_gid)
        except OSError:
            pass


def main(argv: list[str]) -> int:
    from fastapi import HTTPException

    from . import auth

    users = auth.users()
    if not users:
        print("This panel has no users yet – open it in a browser to create the administrator account.")
        return 1
    names = sorted(users, key=str.lower)
    if argv:
        wanted = argv[0]
        name = next((n for n in names if n.lower() == wanted.lower()), None)
        if name is None:
            print(f"No user named {wanted!r}. Users: {', '.join(names)}")
            return 1
    else:
        name = _choose(names)
        if name is None:
            print("Cancelled – nothing changed.")
            return 1
    password = new_password()
    try:
        auth.set_password(name, password)  # also logs out every session of this user
    except HTTPException as exc:
        print(f"Could not reset the password: {exc.detail}")
        return 1
    _keep_owner(auth._users.path)
    print()
    print(f"New password for {name}:")
    print()
    print(f"    {password}")
    print()
    print("It is shown only now. Log in with it and change it in Panel settings → Panel users.")
    print("Locked out after many wrong passwords? The login limit clears itself within 5 minutes.")
    print(f"({time.strftime('%Y-%m-%d %H:%M:%S')}; existing sessions of {name} were logged out.)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
