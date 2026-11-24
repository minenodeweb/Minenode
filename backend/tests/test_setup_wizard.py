from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from app import api, auth, config
from app.models import PanelSettings, SERVER_TEXTS_EN
from app.store import JsonStore

H = {"X-Requested-With": "minenode"}


class FakeManager:
    """Just the settings part of the manager the setup endpoints use."""

    def __init__(self, data=None):
        self.store = SimpleNamespace(data=data or {})
        self.instances = {}
        self.bedrock = None

    @property
    def settings(self):
        return PanelSettings.model_validate(self.store.data)

    def save_settings(self, s):
        self.store.data = s.model_dump()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_users", JsonStore(tmp_path / "users.json", dict))
    monkeypatch.setattr(auth, "_secret_file", tmp_path / "secret.key")
    monkeypatch.setattr(auth, "_attempts", {})
    monkeypatch.setattr(auth, "_revoked", JsonStore(tmp_path / "revoked.json", dict))
    m = FakeManager()
    monkeypatch.setattr(api, "manager", m, raising=False)
    monkeypatch.setattr(api.rpacks, "resync_urls", lambda manager: 0)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), m


def test_no_account_comes_from_the_environment():
    assert not hasattr(config, "BOOTSTRAP_USER") and not hasattr(config, "BOOTSTRAP_PASSWORD")
    assert not hasattr(auth, "bootstrap")


def test_wizard_steps_account_then_panel(client):
    c, m = client
    assert c.get("/api/auth/state").json()["setup_step"] == "account"
    r = c.post("/api/auth/setup", json={"username": "Bernard", "password": "tajne-haslo-123"}, headers=H)
    assert r.status_code == 200
    state = c.get("/api/auth/state").json()
    assert state["setup_step"] == "panel" and state["user"] == "Bernard"
    # a second account cannot be created through the wizard
    assert c.post("/api/auth/setup", json={"username": "x", "password": "12345678"}, headers=H).status_code == 403
    info = c.get("/api/setup/info").json()
    assert {p["port"] for p in info["ports"]} >= {config.PANEL_PORT, config.PROXY_PORT, config.BEDROCK_PORT}
    r = c.post("/api/setup/panel", json={"language": "en", "base_domain": "*.MC.Example.com",
                                         "extra_domains": ["play.drugi.pl"], "pack_subdomain": "rp"}, headers=H)
    assert r.status_code == 200, r.text
    s = m.settings
    assert s.setup_completed and s.default_language == "en"
    assert s.domains() == ["mc.example.com", "play.drugi.pl"] and s.pack_subdomain == "rp"
    assert c.get("/api/auth/state").json()["setup_step"] is None
    assert c.post("/api/setup/panel", json={"language": "pl"}, headers=H).status_code == 409


def test_english_sets_english_server_texts_once(client):
    c, m = client
    m.store.data = {"kick_offline": "Mój własny tekst"}  # something the owner wrote stays
    c.post("/api/auth/setup", json={"username": "admin", "password": "tajne-haslo-123"}, headers=H)
    c.post("/api/setup/panel", json={"language": "en"}, headers=H)
    s = m.settings
    assert s.motd_offline == SERVER_TEXTS_EN["motd_offline"] and s.join_hint == SERVER_TEXTS_EN["join_hint"]
    assert s.kick_offline == "Mój własny tekst"
    defaults = c.get("/api/settings").json()["defaults"]
    assert defaults["motd_starting"] == SERVER_TEXTS_EN["motd_starting"]  # "reset" keeps English


def test_polish_keeps_polish_server_texts(client):
    c, m = client
    c.post("/api/auth/setup", json={"username": "admin", "password": "tajne-haslo-123"}, headers=H)
    c.post("/api/setup/panel", json={"language": "pl"}, headers=H)
    assert m.settings.motd_offline == PanelSettings().motd_offline and m.settings.default_language == "pl"


def test_panels_from_before_the_wizard_are_not_sent_back_to_it(tmp_path):
    from app.manager import Manager
    m = object.__new__(Manager)
    m.settings_store = SimpleNamespace(data={"base_domain": "mc.example.com"}, save=lambda *a: None)
    m.save_settings = lambda s: m.settings_store.data.update(s.model_dump())
    m.migrate_setup_state(has_users=True)
    assert m.settings.setup_completed
    fresh = object.__new__(Manager)
    fresh.settings_store = SimpleNamespace(data={}, save=lambda *a: None)
    fresh.save_settings = lambda s: fresh.settings_store.data.update(s.model_dump())
    fresh.migrate_setup_state(has_users=False)
    assert not fresh.settings.setup_completed
