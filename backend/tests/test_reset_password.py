import json

import pytest

from app import auth, reset_password
from app.store import JsonStore


@pytest.fixture
def users(tmp_path, monkeypatch):
    store = JsonStore(tmp_path / "users.json", dict, follow_file=True)
    monkeypatch.setattr(auth, "_users", store)
    monkeypatch.setattr(auth, "_secret_file", tmp_path / "secret.key")
    monkeypatch.setattr(auth, "_attempts", {})
    monkeypatch.setattr(auth, "_revoked", JsonStore(tmp_path / "revoked.json", dict))
    auth.create_user("admin", "old-password-1")
    auth.create_user("Piotr", "old-password-2")
    return tmp_path


def test_password_is_16_readable_characters():
    for _ in range(200):
        pw = reset_password.new_password()
        assert len(pw) == 16 and not set(pw) & set("0O1lI")
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)


def test_pick_a_user_from_the_list(users, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt="": "2")  # sorted: admin, Piotr
    assert reset_password.main([]) == 0
    out = capsys.readouterr().out
    assert "1) admin" in out and "2) Piotr" in out
    pw = out.split("New password for Piotr:")[1].split()[0]
    assert len(pw) == 16
    assert auth.verify_password(pw, auth._users.data["Piotr"]["password_hash"])
    assert auth.verify_password("old-password-1", auth._users.data["admin"]["password_hash"])  # untouched


def test_username_argument_and_unknown_user(users, capsys):
    assert reset_password.main(["ADMIN"]) == 0
    assert reset_password.main(["nobody"]) == 1
    assert "No user named 'nobody'" in capsys.readouterr().out


def test_cancel_changes_nothing(users, monkeypatch):
    before = (users / "users.json").read_text()
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert reset_password.main([]) == 1
    assert (users / "users.json").read_text() == before


def test_running_panel_sees_the_new_password_without_a_restart(users, monkeypatch, capsys):
    panel_copy = JsonStore(users / "users.json", dict, follow_file=True)  # what the running panel holds
    old_version = panel_copy.data["admin"].get("token_version", 0)
    reset_password.main(["admin"])
    pw = capsys.readouterr().out.split("New password for admin:")[1].split()[0]
    assert auth.verify_password(pw, panel_copy.data["admin"]["password_hash"])
    assert panel_copy.data["admin"]["token_version"] == old_version + 1  # old sessions end
    panel_copy.data["Piotr"]["note"] = "x"
    panel_copy.save()  # a later panel save keeps the reset password
    assert auth.verify_password(pw, json.loads((users / "users.json").read_text())["admin"]["password_hash"])


def test_empty_panel(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(auth, "_users", JsonStore(tmp_path / "users.json", dict, follow_file=True))
    assert reset_password.main([]) == 1
    assert "no users yet" in capsys.readouterr().out
