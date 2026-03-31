import asyncio
from contextlib import contextmanager
from pathlib import Path

from src.config.constants import EmailServiceType
from src.database.models import Base, EmailService
from src.database.session import DatabaseSessionManager
from src.services.base import EmailServiceFactory
from src.services.luck_mail import LUCKMAIL_SDK_AVAILABLE
from src.web.routes import email as email_routes
from src.web.routes import registration as registration_routes


class DummySettings:
    custom_domain_base_url = ""
    custom_domain_api_key = None


def test_luck_mail_service_registered():
    service_type = EmailServiceType("luck_mail")
    service_class = EmailServiceFactory.get_service_class(service_type)
    assert service_class is not None
    assert service_class.__name__ == "LuckMailService"


def test_bundled_luckmail_sdk_available():
    assert LUCKMAIL_SDK_AVAILABLE is True


def test_email_service_types_include_luck_mail():
    result = asyncio.run(email_routes.get_service_types())
    luckmail_type = next(item for item in result["types"] if item["value"] == "luck_mail")

    assert luckmail_type["label"] == "LuckMail"
    field_names = [field["name"] for field in luckmail_type["config_fields"]]
    assert "base_url" in field_names
    assert "api_key" in field_names
    assert "token" in field_names
    assert "email_address" in field_names
    assert "project_code" in field_names
    assert "email_type" in field_names
    assert "domain" in field_names
    assert "variant_mode" in field_names
    assert "tag_name" in field_names


def test_registration_available_services_include_luck_mail(monkeypatch):
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / "luckmail_routes.db"
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)

    with manager.session_scope() as session:
        session.add(
            EmailService(
                service_type="luck_mail",
                name="LuckMail 主服务",
                config={
                    "base_url": "https://mails.luckyous.com",
                    "api_key": "lm_test_key",
                    "token": "tok_abc123def456",
                    "email_address": "demo@outlook.com",
                    "project_code": "openai",
                    "tag_name": "主力号",
                },
                enabled=True,
                priority=0,
            )
        )

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    monkeypatch.setattr(registration_routes, "get_db", fake_get_db)

    import src.config.settings as settings_module

    monkeypatch.setattr(settings_module, "get_settings", lambda: DummySettings())

    result = asyncio.run(registration_routes.get_available_email_services())

    assert result["luck_mail"]["available"] is True
    assert result["luck_mail"]["count"] == 1
    assert result["luck_mail"]["services"][0]["name"] == "LuckMail 主服务"
    assert result["luck_mail"]["services"][0]["type"] == "luck_mail"
    assert result["luck_mail"]["services"][0]["email_address"] == "demo@outlook.com"
    assert result["luck_mail"]["services"][0]["project_code"] == "openai"
    assert result["luck_mail"]["services"][0]["tag_name"] == "主力号"
    assert result["luck_mail"]["services"][0]["has_token"] is True
