from dataclasses import dataclass, field

from src.services import luck_mail as luck_mail_module
from src.services.luck_mail import LuckMailService


@dataclass
class FakeTokenAliveResult:
    email_address: str
    project: str
    alive: bool
    status: str
    message: str = ""


@dataclass
class FakeTokenMailItem:
    message_id: str
    from_addr: str = ""
    subject: str = ""
    body: str = ""
    html_body: str = ""
    received_at: str = ""


@dataclass
class FakeTokenMailList:
    email_address: str
    project: str
    warranty_until: str = ""
    mails: list[FakeTokenMailItem] = field(default_factory=list)


@dataclass
class FakeTokenMailDetail:
    message_id: str
    from_addr: str = ""
    to: str = ""
    subject: str = ""
    body_text: str = ""
    body_html: str = ""
    received_at: str = ""
    verification_code: str = ""


@dataclass
class FakePageResult:
    list: list
    total: int


@dataclass
class FakeProjectItem:
    id: int
    name: str
    code: str


@dataclass
class FakePurchaseItem:
    id: int
    email_address: str
    token: str
    project_name: str
    tag_id: int = 0
    tag_name: str = ""
    warranty_until: str = ""


class FakeLuckMailUser:
    def __init__(self):
        self.mail_lists = [
            FakeTokenMailList(
                email_address="demo@outlook.com",
                project="OpenAI",
                mails=[
                    FakeTokenMailItem(
                        message_id="msg-old",
                        from_addr="noreply@tm.openai.com",
                        subject="Your ChatGPT code is 111111",
                        body="Your ChatGPT code is 111111",
                        received_at="2026-03-30 18:00:00",
                    )
                ],
            ),
            FakeTokenMailList(
                email_address="demo@outlook.com",
                project="OpenAI",
                mails=[
                    FakeTokenMailItem(
                        message_id="msg-old",
                        from_addr="noreply@tm.openai.com",
                        subject="Your ChatGPT code is 111111",
                        body="Your ChatGPT code is 111111",
                        received_at="2026-03-30 18:00:00",
                    )
                ],
            ),
            FakeTokenMailList(
                email_address="demo@outlook.com",
                project="OpenAI",
                mails=[
                    FakeTokenMailItem(
                        message_id="msg-new",
                        from_addr="noreply@tm.openai.com",
                        subject="Your ChatGPT code is 222222",
                        body="Your ChatGPT code is 222222",
                        received_at="2026-03-30 18:20:05",
                    ),
                    FakeTokenMailItem(
                        message_id="msg-old",
                        from_addr="noreply@tm.openai.com",
                        subject="Your ChatGPT code is 111111",
                        body="Your ChatGPT code is 111111",
                        received_at="2026-03-30 18:00:00",
                    ),
                ],
            ),
        ]
        self.mail_list_calls = 0
        self.detail_calls = []

    def check_token_alive(self, token: str):
        assert token == "tok-fixed"
        return FakeTokenAliveResult(
            email_address="demo@outlook.com",
            project="OpenAI",
            alive=True,
            status="ok",
        )

    def get_token_mails(self, token: str):
        assert token == "tok-fixed"
        index = min(self.mail_list_calls, len(self.mail_lists) - 1)
        self.mail_list_calls += 1
        return self.mail_lists[index]

    def get_token_mail_detail(self, token: str, message_id: str):
        assert token == "tok-fixed"
        self.detail_calls.append(message_id)
        if message_id == "msg-new":
            return FakeTokenMailDetail(
                message_id="msg-new",
                from_addr="noreply@tm.openai.com",
                to="demo@outlook.com",
                subject="Your ChatGPT code is 222222",
                body_text="Your ChatGPT code is 222222",
                body_html="<p>Your ChatGPT code is <b>222222</b></p>",
                received_at="2026-03-30 18:20:05",
                verification_code="222222",
            )
        raise AssertionError(f"unexpected detail fetch for {message_id}")


class FakeLuckMailClient:
    user = None

    def __init__(self, *args, **kwargs):
        self.user = self.__class__.user


def test_luck_mail_uses_token_mail_timeline(monkeypatch):
    fake_user = FakeLuckMailUser()
    monkeypatch.setattr(luck_mail_module, "LuckMailClient", FakeLuckMailClient)
    FakeLuckMailClient.user = fake_user

    service = LuckMailService(
        config={
            "base_url": "https://mails.luckyous.com",
            "api_key": "lm_test_key",
            "token": "tok-fixed",
            "email_address": "demo@outlook.com",
            "timeout": 5,
            "poll_interval": 0.01,
        }
    )

    email_info = service.create_email()
    code = service.get_verification_code(
        email=email_info["email"],
        email_id=email_info["service_id"],
        timeout=1,
        otp_sent_at=1774894802.0,
    )

    assert email_info["email"] == "demo@outlook.com"
    assert email_info["service_id"] == "tok-fixed"
    assert code == "222222"
    assert fake_user.mail_list_calls == 3
    assert fake_user.detail_calls == ["msg-new"]


class FakeLuckMailProjectPurchaseUser:
    def __init__(self):
        self.project_calls = 0
        self.purchase_calls = 0

    def get_projects(self, page: int = 1, page_size: int = 100):
        self.project_calls += 1
        return FakePageResult(
            list=[FakeProjectItem(id=7, name="OpenAI", code="openai")],
            total=1,
        )

    def purchase_emails(
        self,
        project_code: str,
        quantity: int,
        email_type: str = None,
        domain: str = None,
        variant_mode: str = None,
    ):
        assert project_code == "openai"
        assert quantity == 1
        assert email_type == "ms_graph"
        assert domain == "outlook.com"
        assert variant_mode is None
        self.purchase_calls += 1
        return {
            "purchases": [
                {
                    "id": 99,
                    "email_address": "legacy@outlook.com",
                    "token": "tok-legacy",
                    "project_name": "OpenAI",
                    "price": "2.0000",
                }
            ],
            "total_cost": "2.0000",
            "balance_after": "98.0000",
        }

    def get_token_mails(self, token: str):
        assert token == "tok-legacy"
        return FakeTokenMailList(
            email_address="legacy@outlook.com",
            project="OpenAI",
            mails=[
                FakeTokenMailItem(
                    message_id="msg-legacy",
                    from_addr="noreply@tm.openai.com",
                    subject="Your ChatGPT code is 333333",
                    body="Your ChatGPT code is 333333",
                    received_at="2026-03-30 19:05:20",
                )
            ],
        )

    def get_token_mail_detail(self, token: str, message_id: str):
        assert token == "tok-legacy"
        assert message_id == "msg-legacy"
        return FakeTokenMailDetail(
            message_id="msg-legacy",
            from_addr="noreply@tm.openai.com",
            to="legacy@outlook.com",
            subject="Your ChatGPT code is 333333",
            body_text="Your ChatGPT code is 333333",
            body_html="<p>Your ChatGPT code is <b>333333</b></p>",
            received_at="2026-03-30 19:05:20",
            verification_code="333333",
        )


def test_luck_mail_purchases_new_mailbox_for_project_code(monkeypatch):
    fake_user = FakeLuckMailProjectPurchaseUser()
    monkeypatch.setattr(luck_mail_module, "LuckMailClient", FakeLuckMailClient)
    FakeLuckMailClient.user = fake_user

    service = LuckMailService(
        config={
            "base_url": "https://mails.luckyous.com",
            "api_key": "lm_test_key",
            "project_code": "openai",
            "email_type": "ms_graph",
            "domain": "outlook.com",
            "timeout": 5,
            "poll_interval": 0.01,
        }
    )

    email_info = service.create_email()
    code = service.get_verification_code(
        email=email_info["email"],
        email_id=email_info["service_id"],
        timeout=1,
    )

    assert email_info["email"] == "legacy@outlook.com"
    assert email_info["service_id"] == "tok-legacy"
    assert email_info["project_code"] == "openai"
    assert email_info["price"] == "2.0000"
    assert code == "333333"
    assert fake_user.project_calls == 1
    assert fake_user.purchase_calls == 1
