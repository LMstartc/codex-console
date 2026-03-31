"""
LuckMail service backed by mailbox purchase and token mail APIs.
"""

import logging
import re
import sys
import time
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .base import BaseEmailService, EmailServiceError, EmailServiceType
from ..config.constants import (
    OPENAI_EMAIL_SENDERS,
    OPENAI_VERIFICATION_KEYWORDS,
    OTP_CODE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
)

logger = logging.getLogger(__name__)

OTP_TIME_SKEW_SECONDS = 5
UNKNOWN_TS_GRACE_SECONDS = 15


def _load_luckmail_client():
    """Load LuckMail SDK from site-packages first, then from the vendored SDK."""
    try:
        from luckmail import LuckMailClient as client

        return client, True
    except ImportError:
        bundled_sdk_root = Path(__file__).resolve().parents[2] / "LuckMailSdk-Python"
        bundled_package = bundled_sdk_root / "luckmail"

        if bundled_package.exists():
            sdk_path = str(bundled_sdk_root)
            if sdk_path not in sys.path:
                sys.path.insert(0, sdk_path)

            try:
                from luckmail import LuckMailClient as client

                return client, True
            except ImportError:
                pass

        return None, False


LuckMailClient, LUCKMAIL_SDK_AVAILABLE = _load_luckmail_client()


class LuckMailService(BaseEmailService):
    """LuckMail mailbox-purchase service."""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.LUCK_MAIL, name)

        if not LUCKMAIL_SDK_AVAILABLE:
            raise ImportError(
                "LuckMail SDK unavailable. Install it with "
                "`pip install -e ./LuckMailSdk-Python` or keep the vendored SDK."
            )

        required_keys = ["base_url", "api_key"]
        missing_keys = [key for key in required_keys if not (config or {}).get(key)]
        if missing_keys:
            raise ValueError(f"Missing required LuckMail config: {missing_keys}")

        default_config = {
            "token": "",
            "email_address": "",
            "project_id": None,
            "project_code": "",
            "project_name": "",
            "email_type": "",
            "domain": "",
            "variant_mode": "",
            "tag_id": None,
            "tag_name": "",
            "mark_tag_id": None,
            "mark_tag_name": "",
            "timeout": 300,
            "poll_interval": 3.0,
        }
        self.config = {**default_config, **(config or {})}
        self.config["base_url"] = str(self.config["base_url"]).rstrip("/")

        self._client = LuckMailClient(
            base_url=self.config["base_url"],
            api_key=self.config["api_key"],
            timeout=self.config.get("timeout", 30),
        )

        self._mailboxes_by_token: Dict[str, Dict[str, Any]] = {}
        self._mailboxes_by_email: Dict[str, Dict[str, Any]] = {}
        self._token_message_state: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _normalize_stage_marker(otp_sent_at: Optional[float]) -> Optional[float]:
        if otp_sent_at is None:
            return None
        try:
            return round(float(otp_sent_at), 3)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _strip_html(value: Any) -> str:
        text = str(value or "")
        return unescape(re.sub(r"<[^>]+>", " ", text))

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[float]:
        if value is None:
            return None

        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10**12:
                ts = ts / 1000.0
            return ts if ts > 0 else None

        text = str(value).strip()
        if not text:
            return None

        if text.isdigit():
            ts = float(text)
            if ts > 10**12:
                ts = ts / 1000.0
            return ts if ts > 0 else None

        try:
            ts = float(text)
            if ts > 10**12:
                ts = ts / 1000.0
            if ts > 0:
                return ts
        except ValueError:
            pass

        normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
        try:
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass

        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                continue

        return None

    @staticmethod
    def _extract_otp_code(content: str, pattern: str) -> Tuple[Optional[str], bool]:
        text = str(content or "")
        if not text:
            return None, False

        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1), True

        simple_match = re.search(pattern, text)
        if simple_match:
            return simple_match.group(1), False

        return None, False

    @staticmethod
    def _item_value(item: Any, key: str, default: Any = None) -> Any:
        if isinstance(item, dict):
            return item.get(key, default)
        return getattr(item, key, default)

    @classmethod
    def _mailbox_from_purchase_item(cls, item: Any, source: str = "purchase") -> Dict[str, Any]:
        return {
            "purchase_id": cls._item_value(item, "id"),
            "email": str(cls._item_value(item, "email_address", "") or "").strip(),
            "token": str(cls._item_value(item, "token", "") or "").strip(),
            "project_name": str(cls._item_value(item, "project_name", "") or "").strip(),
            "tag_id": cls._item_value(item, "tag_id", None),
            "tag_name": str(cls._item_value(item, "tag_name", "") or "").strip(),
            "warranty_until": str(cls._item_value(item, "warranty_until", "") or "").strip(),
            "price": str(cls._item_value(item, "price", "") or "").strip(),
            "created_at": str(cls._item_value(item, "created_at", "") or "").strip(),
            "source": source,
        }

    @staticmethod
    def _sort_candidate(item: Dict[str, Any]) -> Tuple[int, int, float]:
        mail_ts = item.get("mail_ts")
        is_recent = item.get("is_recent", False)
        return (
            1 if is_recent else 0,
            1 if mail_ts is not None else 0,
            float(mail_ts or 0.0),
        )

    def _cache_mailbox(self, mailbox: Dict[str, Any]) -> Dict[str, Any]:
        token = str(mailbox.get("token") or "").strip()
        email = str(mailbox.get("email") or "").strip().lower()
        if token:
            self._mailboxes_by_token[token] = mailbox
        if email:
            self._mailboxes_by_email[email] = mailbox
        return mailbox

    def _resolve_mailbox_from_token(self, token: str, email_address: Optional[str] = None) -> Dict[str, Any]:
        normalized_token = str(token or "").strip()
        if not normalized_token:
            raise EmailServiceError("LuckMail token is empty")

        cached = self._mailboxes_by_token.get(normalized_token)
        if cached:
            return cached

        alive_result = self._client.user.check_token_alive(normalized_token)
        if not getattr(alive_result, "alive", False):
            raise EmailServiceError(
                f"LuckMail token unavailable: {getattr(alive_result, 'message', '') or getattr(alive_result, 'status', 'unknown')}"
            )

        resolved_email = str(
            email_address or getattr(alive_result, "email_address", "") or ""
        ).strip()
        project_name = str(getattr(alive_result, "project", "") or "").strip()

        if not resolved_email:
            mail_list = self._client.user.get_token_mails(normalized_token)
            resolved_email = str(getattr(mail_list, "email_address", "") or "").strip()
            project_name = project_name or str(getattr(mail_list, "project", "") or "").strip()

        if not resolved_email:
            raise EmailServiceError("LuckMail token did not resolve an email address")

        return self._cache_mailbox(
            {
                "purchase_id": None,
                "email": resolved_email,
                "token": normalized_token,
                "project_name": project_name,
                "tag_id": None,
                "tag_name": "",
                "warranty_until": "",
                "source": "token",
            }
        )

    def _resolve_mailbox_from_email_address(self, email_address: str) -> Dict[str, Any]:
        normalized_email = str(email_address or "").strip().lower()
        if not normalized_email:
            raise EmailServiceError("LuckMail email address is empty")

        cached = self._mailboxes_by_email.get(normalized_email)
        if cached:
            return cached

        page = 1
        page_size = 100
        while True:
            result = self._client.user.get_purchases(
                page=page,
                page_size=page_size,
                keyword=normalized_email,
                user_disabled=0,
            )
            items = list(getattr(result, "list", []) or [])
            for item in items:
                item_email = str(getattr(item, "email_address", "") or "").strip().lower()
                if item_email == normalized_email:
                    return self._cache_mailbox(self._mailbox_from_purchase_item(item))

            total = int(getattr(result, "total", 0) or 0)
            if page * page_size >= total or not items:
                break
            page += 1

        raise EmailServiceError(f"LuckMail did not find a purchased mailbox for {email_address}")

    def _resolve_project(
        self,
        project_id: Optional[Any] = None,
        project_code: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        normalized_project_id: Optional[int] = None
        if project_id is not None and str(project_id).strip():
            try:
                normalized_project_id = int(project_id)
            except (TypeError, ValueError) as exc:
                raise EmailServiceError(f"LuckMail project_id is invalid: {project_id}") from exc
        normalized_code = str(project_code or "").strip().lower()
        normalized_name = str(project_name or "").strip().lower()
        if normalized_project_id is None and not normalized_code and not normalized_name:
            raise EmailServiceError("LuckMail mailbox creation requires project_id, project_code, or project_name")

        page = 1
        page_size = 100
        while True:
            result = self._client.user.get_projects(page=page, page_size=page_size)
            items = list(getattr(result, "list", []) or [])
            for item in items:
                item_id = self._item_value(item, "id")
                item_code = str(self._item_value(item, "code", "") or "").strip()
                item_name = str(self._item_value(item, "name", "") or "").strip()
                if (
                    normalized_project_id is not None and int(item_id or 0) == normalized_project_id
                ) or (
                    normalized_code and item_code.lower() == normalized_code
                ) or (
                    normalized_name and item_name.lower() == normalized_name
                ):
                    return {
                        "id": int(item_id or 0),
                        "code": item_code,
                        "name": item_name or str(project_code or project_name or project_id).strip(),
                    }

            total = int(getattr(result, "total", 0) or 0)
            if page * page_size >= total or not items:
                break
            page += 1

        identifier = project_code or project_name or project_id
        raise EmailServiceError(f"LuckMail project not found: {identifier}")

    def _apply_purchase_tag(self, mailbox: Dict[str, Any], request_config: Dict[str, Any]) -> Dict[str, Any]:
        purchase_id = mailbox.get("purchase_id")
        if not purchase_id:
            return mailbox

        preferred_tag_id = request_config.get("mark_tag_id")
        preferred_tag_name = str(request_config.get("mark_tag_name") or "").strip()
        fallback_tag_id = request_config.get("tag_id")
        fallback_tag_name = str(request_config.get("tag_name") or "").strip()

        selected_tag_id = preferred_tag_id if preferred_tag_id not in (None, "") else fallback_tag_id
        selected_tag_name = preferred_tag_name or (
            fallback_tag_name if selected_tag_id in (None, "") else ""
        )
        if selected_tag_id in (None, "") and not selected_tag_name:
            return mailbox

        if selected_tag_id not in (None, ""):
            self._client.user.set_purchase_tag(int(purchase_id), tag_id=int(selected_tag_id))
            mailbox["tag_id"] = int(selected_tag_id)
        else:
            self._client.user.set_purchase_tag(int(purchase_id), tag_name=selected_tag_name)
        mailbox["tag_name"] = selected_tag_name
        return mailbox

    def _purchase_mailbox(self, request_config: Dict[str, Any]) -> Dict[str, Any]:
        project = self._resolve_project(
            project_id=request_config.get("project_id"),
            project_code=str(request_config.get("project_code") or "").strip(),
            project_name=str(request_config.get("project_name") or "").strip(),
        )

        email_type = str(request_config.get("email_type") or "").strip()
        domain = str(request_config.get("domain") or "").strip()
        variant_mode = str(request_config.get("variant_mode") or "").strip()

        result = self._client.user.purchase_emails(
            project_code=project["code"],
            quantity=1,
            email_type=email_type or None,
            domain=domain or None,
            variant_mode=variant_mode or None,
        )
        purchases = list((result or {}).get("purchases", []) or [])
        if not purchases:
            raise EmailServiceError(
                f"LuckMail purchase returned no mailbox for project={project['code'] or project['name']}"
            )

        mailbox = self._mailbox_from_purchase_item(purchases[0], source="purchase_create")
        mailbox["project_name"] = mailbox.get("project_name") or project["name"]
        mailbox["project_code"] = project["code"]
        mailbox["total_cost"] = str((result or {}).get("total_cost", "") or "").strip()
        mailbox["balance_after"] = str((result or {}).get("balance_after", "") or "").strip()
        mailbox = self._apply_purchase_tag(mailbox, request_config)
        return self._cache_mailbox(mailbox)

    def _resolve_mailbox(self, request_config: Dict[str, Any]) -> Dict[str, Any]:
        token = str(request_config.get("token") or "").strip()
        email_address = str(request_config.get("email_address") or "").strip()
        project_id = request_config.get("project_id")
        project_code = str(request_config.get("project_code") or "").strip()
        project_name = str(request_config.get("project_name") or "").strip()
        if token:
            return self._resolve_mailbox_from_token(token, email_address=email_address or None)
        if email_address:
            return self._resolve_mailbox_from_email_address(email_address)
        if project_id or project_code or project_name:
            return self._purchase_mailbox(request_config)
        raise EmailServiceError(
            "LuckMail mailbox creation requires token, email_address, or project_id/project_code/project_name"
        )

    def _resolve_mailbox_for_fetch(self, email: str, email_id: str = None) -> Dict[str, Any]:
        token = str(email_id or "").strip()
        if token and token in self._mailboxes_by_token:
            return self._mailboxes_by_token[token]
        if token and self.config.get("token") and token == str(self.config.get("token") or "").strip():
            return self._resolve_mailbox_from_token(token, email_address=self.config.get("email_address"))

        normalized_email = str(email or "").strip().lower()
        if normalized_email and normalized_email in self._mailboxes_by_email:
            return self._mailboxes_by_email[normalized_email]
        if normalized_email and self.config.get("email_address") and normalized_email == str(self.config.get("email_address") or "").strip().lower():
            return self._resolve_mailbox_from_email_address(normalized_email)

        if self.config.get("token"):
            return self._resolve_mailbox_from_token(
                str(self.config.get("token") or "").strip(),
                email_address=self.config.get("email_address"),
            )
        if normalized_email:
            return self._resolve_mailbox_from_email_address(normalized_email)

        raise EmailServiceError("LuckMail mailbox context is unavailable")

    def _snapshot_existing_message_ids(self, token: str, otp_sent_at: Optional[float]) -> set[str]:
        try:
            mail_list = self._client.user.get_token_mails(token)
        except Exception as exc:
            logger.warning("LuckMail token %s snapshot failed: %s", token, exc)
            return set()

        message_ids: set[str] = set()
        for mail in list(getattr(mail_list, "mails", []) or []):
            message_id = str(getattr(mail, "message_id", "") or "").strip()
            if not message_id:
                continue

            if otp_sent_at:
                mail_ts = self._parse_timestamp(getattr(mail, "received_at", None))
                if mail_ts is not None and mail_ts + OTP_TIME_SKEW_SECONDS >= otp_sent_at:
                    continue
            message_ids.add(message_id)

        return message_ids

    def _is_openai_otp_mail(self, sender: str, subject: str, body_text: str, body_html: str) -> bool:
        sender_l = str(sender or "").lower()
        subject_l = str(subject or "").lower()
        body_l = str(body_text or "").lower()
        html_l = str(body_html or "").lower()
        blob = "\n".join([sender_l, subject_l, body_l, html_l])

        sender_hit = any(token in sender_l for token in OPENAI_EMAIL_SENDERS)
        keyword_hit = any(keyword in blob for keyword in OPENAI_VERIFICATION_KEYWORDS)
        otp_hint_hit = any(
            keyword in blob
            for keyword in (
                "verification",
                "verify",
                "one-time code",
                "one time code",
                "otp",
                "log in",
                "login",
                "security code",
            )
        )
        return sender_hit and (keyword_hit or otp_hint_hit)

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        request_config = {**self.config, **(config or {})}
        mailbox = self._resolve_mailbox(request_config)

        if not mailbox.get("email") or not mailbox.get("token"):
            raise EmailServiceError("LuckMail mailbox info is incomplete")

        email_info = {
            "email": mailbox["email"],
            "service_id": mailbox["token"],
            "id": mailbox["token"],
            "token": mailbox["token"],
            "purchase_id": mailbox.get("purchase_id"),
            "project_code": mailbox.get("project_code"),
            "project_name": mailbox.get("project_name"),
            "tag_name": mailbox.get("tag_name"),
            "price": mailbox.get("price"),
            "created_at": time.time(),
        }

        self._cache_mailbox({**mailbox, **email_info})
        logger.info(
            "Prepared LuckMail mailbox: %s (source=%s)",
            mailbox["email"],
            mailbox.get("source", "unknown"),
        )
        self.update_status(True)
        return email_info

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 120,
        pattern: str = OTP_CODE_PATTERN,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        mailbox = self._resolve_mailbox_for_fetch(email=email, email_id=email_id)
        token = str(mailbox.get("token") or "").strip()
        if not token:
            raise EmailServiceError(f"LuckMail mailbox {email or email_id} has no token")

        timeout_limit = int(self.config.get("timeout", 300) or 300)
        requested_timeout = int(timeout) if timeout and int(timeout) > 0 else timeout_limit
        timeout = min(requested_timeout, timeout_limit)
        poll_interval = float(self.config.get("poll_interval", 3.0) or 3.0)

        state = self._token_message_state.setdefault(
            token,
            {
                "current_stage_marker": None,
                "baseline_message_ids": set(),
                "used_message_ids": set(),
                "last_returned_message_id": None,
                "last_returned_code": None,
            },
        )
        stage_marker = self._normalize_stage_marker(otp_sent_at)
        if stage_marker is not None and stage_marker != state.get("current_stage_marker"):
            state["current_stage_marker"] = stage_marker
            state["baseline_message_ids"] = self._snapshot_existing_message_ids(token, otp_sent_at)
            state["used_message_ids"] = set()
            logger.info(
                "LuckMail token %s started a new OTP stage, baseline messages=%s",
                token[:8] + "***",
                len(state["baseline_message_ids"]),
            )

        logger.info("Waiting for LuckMail token mailbox %s verification code...", mailbox.get("email") or token)

        start_time = time.time()
        poll_count = 0
        baseline_message_ids: set[str] = state["baseline_message_ids"]
        used_message_ids: set[str] = state["used_message_ids"]

        while time.time() - start_time < timeout:
            poll_count += 1
            try:
                mail_list = self._client.user.get_token_mails(token)
            except Exception as exc:
                logger.warning("LuckMail token %s mail poll failed: %s", token[:8] + "***", exc)
                time.sleep(poll_interval)
                continue

            mails = list(getattr(mail_list, "mails", []) or [])
            candidates: List[Dict[str, Any]] = []
            unknown_ts_candidates: List[Dict[str, Any]] = []

            for mail in mails:
                message_id = str(getattr(mail, "message_id", "") or "").strip()
                if not message_id:
                    continue
                if message_id in baseline_message_ids or message_id in used_message_ids:
                    continue

                mail_ts = self._parse_timestamp(getattr(mail, "received_at", None))
                if otp_sent_at and mail_ts is not None and mail_ts + OTP_TIME_SKEW_SECONDS < otp_sent_at:
                    baseline_message_ids.add(message_id)
                    continue

                candidate = {
                    "message_id": message_id,
                    "mail": mail,
                    "mail_ts": mail_ts,
                    "is_recent": bool(
                        otp_sent_at and mail_ts is not None and mail_ts + OTP_TIME_SKEW_SECONDS >= otp_sent_at
                    ),
                }
                if otp_sent_at and mail_ts is None:
                    unknown_ts_candidates.append(candidate)
                else:
                    candidates.append(candidate)

            elapsed = time.time() - start_time
            if otp_sent_at and not candidates and unknown_ts_candidates and elapsed < UNKNOWN_TS_GRACE_SECONDS:
                time.sleep(poll_interval)
                continue

            ordered_candidates = sorted(
                candidates + unknown_ts_candidates,
                key=self._sort_candidate,
                reverse=True,
            )

            for candidate in ordered_candidates:
                message_id = candidate["message_id"]
                try:
                    detail = self._client.user.get_token_mail_detail(token, message_id)
                except Exception as exc:
                    logger.warning(
                        "LuckMail token %s detail fetch failed for %s: %s",
                        token[:8] + "***",
                        message_id,
                        exc,
                    )
                    continue

                detail_ts = self._parse_timestamp(getattr(detail, "received_at", None)) or candidate.get("mail_ts")
                if otp_sent_at and detail_ts is not None and detail_ts + OTP_TIME_SKEW_SECONDS < otp_sent_at:
                    baseline_message_ids.add(message_id)
                    continue

                sender = str(
                    getattr(detail, "from_addr", "")
                    or getattr(candidate["mail"], "from_addr", "")
                    or ""
                ).strip()
                subject = str(
                    getattr(detail, "subject", "")
                    or getattr(candidate["mail"], "subject", "")
                    or ""
                ).strip()
                body_text = str(
                    getattr(detail, "body_text", "")
                    or getattr(candidate["mail"], "body", "")
                    or ""
                ).strip()
                body_html = str(
                    getattr(detail, "body_html", "")
                    or getattr(candidate["mail"], "html_body", "")
                    or ""
                ).strip()

                if not self._is_openai_otp_mail(sender, subject, body_text, body_html):
                    used_message_ids.add(message_id)
                    continue

                code = str(getattr(detail, "verification_code", "") or "").strip()
                if not code:
                    search_text = "\n".join(
                        part for part in [sender, subject, body_text, self._strip_html(body_html)] if part
                    )
                    code, _semantic_hit = self._extract_otp_code(search_text, pattern)

                if not code:
                    used_message_ids.add(message_id)
                    continue

                used_message_ids.add(message_id)
                state["last_returned_message_id"] = message_id
                state["last_returned_code"] = code
                logger.info(
                    "Got LuckMail token verification code: %s, from=%s, subject=%s, message_id=%s",
                    code,
                    sender,
                    subject,
                    message_id,
                )
                self.update_status(True)
                return code

            if poll_count == 1 or poll_count % 5 == 0:
                logger.debug(
                    "LuckMail token %s polling[%s]: candidates=%s visible=%s",
                    token[:8] + "***",
                    poll_count,
                    len(ordered_candidates),
                    len(mails),
                )
            time.sleep(poll_interval)

        logger.warning(
            "Timeout while waiting for LuckMail token mailbox %s verification code (%ss)",
            mailbox.get("email") or token,
            timeout,
        )
        return None

    def list_emails(self, limit: int = 100, offset: int = 0, **kwargs) -> List[Dict[str, Any]]:
        if self._mailboxes_by_token:
            mailboxes = list(self._mailboxes_by_token.values())
            return mailboxes[offset : offset + limit]

        preview_email = str(self.config.get("email_address") or "").strip()
        preview_token = str(self.config.get("token") or "").strip()
        if preview_email or preview_token:
            return [
                {
                    "id": preview_token,
                    "service_id": preview_token,
                    "email": preview_email,
                    "token": preview_token,
                    "source": "config",
                }
            ]
        return []

    def delete_email(self, email_id: str) -> bool:
        logger.warning("LuckMail purchased mailboxes are not deleted by this service: %s", email_id)
        return False

    def check_health(self) -> bool:
        try:
            self._client.user.get_user_info()
            configured_token = str(self.config.get("token") or "").strip()
            configured_email = str(self.config.get("email_address") or "").strip()
            configured_project_id = self.config.get("project_id")
            configured_project_code = str(self.config.get("project_code") or "").strip()
            configured_project_name = str(self.config.get("project_name") or "").strip()
            if configured_token:
                alive = self._client.user.check_token_alive(configured_token)
                healthy = bool(getattr(alive, "alive", False))
            elif configured_email:
                self._resolve_mailbox_from_email_address(configured_email)
                healthy = True
            elif configured_project_id or configured_project_code or configured_project_name:
                self._resolve_project(
                    project_id=configured_project_id,
                    project_code=configured_project_code,
                    project_name=configured_project_name,
                )
                healthy = True
            else:
                healthy = True
            self.update_status(healthy)
            return healthy
        except Exception as exc:
            logger.warning("LuckMail health check failed: %s", exc)
            self.update_status(False, exc)
            return False

    def get_email_messages(self, email_id: str, **kwargs) -> List[Dict[str, Any]]:
        token = str(email_id or "").strip()
        if not token:
            return []

        try:
            mail_list = self._client.user.get_token_mails(token)
            messages = []
            for mail in list(getattr(mail_list, "mails", []) or []):
                messages.append(
                    {
                        "id": str(getattr(mail, "message_id", "") or "").strip(),
                        "from": str(getattr(mail, "from_addr", "") or "").strip(),
                        "subject": str(getattr(mail, "subject", "") or "").strip(),
                        "content": str(getattr(mail, "body", "") or "").strip(),
                        "received_at": str(getattr(mail, "received_at", "") or "").strip(),
                    }
                )
            return messages
        except Exception as exc:
            logger.warning("LuckMail get_email_messages failed: %s", exc)
            return []

    def get_message_content(self, email_id: str, message_id: str) -> Optional[Dict[str, Any]]:
        token = str(email_id or "").strip()
        if not token or not message_id:
            return None

        try:
            detail = self._client.user.get_token_mail_detail(token, message_id)
            return {
                "id": str(getattr(detail, "message_id", "") or "").strip(),
                "from": str(getattr(detail, "from_addr", "") or "").strip(),
                "subject": str(getattr(detail, "subject", "") or "").strip(),
                "content": str(getattr(detail, "body_text", "") or "").strip(),
                "html": str(getattr(detail, "body_html", "") or "").strip(),
                "received_at": str(getattr(detail, "received_at", "") or "").strip(),
                "verification_code": str(getattr(detail, "verification_code", "") or "").strip(),
            }
        except Exception as exc:
            logger.warning("LuckMail get_message_content failed: %s", exc)
            return None

    def get_service_info(self) -> Dict[str, Any]:
        info = {
            "service_type": self.service_type.value,
            "name": self.name,
            "base_url": self.config["base_url"],
            "email_address": self.config.get("email_address", ""),
            "has_token": bool(self.config.get("token")),
            "project_code": self.config.get("project_code", ""),
            "project_name": self.config.get("project_name", ""),
            "email_type": self.config.get("email_type", ""),
            "domain": self.config.get("domain", ""),
            "variant_mode": self.config.get("variant_mode", ""),
            "tag_name": self.config.get("tag_name", ""),
            "mark_tag_name": self.config.get("mark_tag_name", ""),
            "status": self.status.value,
        }
        try:
            user_info = self._client.user.get_user_info()
            info["balance"] = getattr(user_info, "balance", "")
            info["username"] = getattr(user_info, "username", "")
        except Exception:
            pass
        return info
