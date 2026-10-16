from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class DailyFileHandler(logging.FileHandler):
    """将日志写入当天的 YYYY-MM-DD.log，并在跨天时自动切换文件。"""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current_day = date.today().isoformat()
        super().__init__(
            self.log_dir / f"{self.current_day}.log",
            encoding="utf-8",
        )

    def emit(self, record: logging.LogRecord) -> None:
        today = date.today().isoformat()
        if today != self.current_day:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.current_day = today
            self.baseFilename = os.path.abspath(
                self.log_dir / f"{self.current_day}.log"
            )
        super().emit(record)


def configure_logging() -> logging.Logger:
    logger = logging.getLogger("qqbot")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler = DailyFileHandler(BASE_DIR / "logs")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    return logger


logger = configure_logging()


def read_flag(environment_name: str, default_value: bool) -> bool:
    raw_value = os.getenv(environment_name, "").strip().lower()
    if not raw_value:
        return default_value
    if raw_value in {"1", "true", "yes", "on"}:
        return True
    if raw_value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(
        f"环境变量 {environment_name} 只能是 true 或 false"
    )


def read_positive_seconds(
    environment_name: str,
    default_seconds: float,
) -> float:
    raw_value = os.getenv(environment_name, "").strip()
    if not raw_value:
        return default_seconds
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是正数秒数"
        ) from exc
    if not value > 0:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是大于 0 的秒数"
        )
    return value


def read_header_map(environment_name: str) -> dict[str, str]:
    raw_value = os.getenv(environment_name, "").strip()
    if not raw_value:
        return {}
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是 JSON 字符串字典"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(name, str)
        and name.strip()
        and isinstance(header_value, str)
        and header_value.strip()
        for name, header_value in value.items()
    ):
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是请求头名和值组成的 JSON 字符串字典"
        )
    return {
        name.strip(): header_value.strip()
        for name, header_value in value.items()
    }


def read_positive_int(environment_name: str, default_value: int) -> int:
    raw_value = os.getenv(environment_name, "").strip()
    if not raw_value:
        return default_value
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是正整数"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是大于 0 的整数"
        )
    return value


def read_openid_list(environment_name: str) -> tuple[str, ...]:
    raw_value = os.getenv(environment_name, "").strip()
    if not raw_value:
        return ()
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是 JSON 字符串列表"
        ) from exc
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是非空字符串组成的 JSON 列表"
        )
    return tuple(dict.fromkeys(item.strip() for item in value))


def read_group_routes(environment_name: str) -> dict[str, str]:
    raw_value = os.getenv(environment_name, "").strip()
    if not raw_value:
        return {}
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是 JSON 字符串字典"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(source, str)
        and source.strip()
        and isinstance(target, str)
        and target.strip()
        for source, target in value.items()
    ):
        raise RuntimeError(
            f"环境变量 {environment_name} 必须是接收群和发送群组成的 JSON 字符串字典"
        )
    return {
        source.strip(): target.strip()
        for source, target in value.items()
    }


@dataclass(frozen=True)
class Settings:
    qq_app_id: str
    qq_app_secret: str
    qq_group_routes: dict[str, str]
    ai_base_url: str
    ai_api_key: str
    ai_model: str
    ai_extra_headers: dict[str, str]
    ai_timeout_seconds: float
    qq_timeout_seconds: float
    qq_fetch_member_nickname: bool
    ai_mention_instruction: bool
    bot_mention_reply_enabled: bool
    bot_mention_rate_limit: int
    bot_mention_rate_window_seconds: float
    bot_mention_busy_message: str
    qq_private_chat_enabled: bool
    qq_private_chat_allowlist: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            qq_app_id=os.getenv("QQ_APP_ID", "").strip(),
            qq_app_secret=os.getenv("QQ_APP_SECRET", "").strip(),
            qq_group_routes=read_group_routes("QQ_GROUP_ROUTES"),
            ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
            ai_api_key=os.getenv("AI_API_KEY", "").strip(),
            ai_model=os.getenv("AI_MODEL", "").strip(),
            ai_extra_headers=read_header_map("AI_EXTRA_HEADERS"),
            ai_timeout_seconds=read_positive_seconds("AI_TIMEOUT_SECONDS", 60.0),
            qq_timeout_seconds=read_positive_seconds(
                "QQ_TIMEOUT_SECONDS",
                30.0,
            ),
            qq_fetch_member_nickname=read_flag(
                "QQ_FETCH_MEMBER_NICKNAME",
                True,
            ),
            ai_mention_instruction=read_flag("AI_MENTION_INSTRUCTION", True),
            bot_mention_reply_enabled=read_flag(
                "BOT_MENTION_REPLY_ENABLED",
                True,
            ),
            bot_mention_rate_limit=read_positive_int(
                "BOT_MENTION_RATE_LIMIT",
                5,
            ),
            bot_mention_rate_window_seconds=read_positive_seconds(
                "BOT_MENTION_RATE_WINDOW_SECONDS",
                60.0,
            ),
            bot_mention_busy_message=os.getenv(
                "BOT_MENTION_BUSY_MESSAGE",
                "哎呀，不要AT我了喵",
            ).strip()
            or "哎呀，不要AT我了喵",
            qq_private_chat_enabled=read_flag(
                "QQ_PRIVATE_CHAT_ENABLED",
                True,
            ),
            qq_private_chat_allowlist=read_openid_list(
                "QQ_PRIVATE_CHAT_ALLOWLIST",
            ),
        )

    def missing_variables(self) -> list[str]:
        values = {
            "QQ_APP_ID": self.qq_app_id,
            "QQ_APP_SECRET": self.qq_app_secret,
            "QQ_GROUP_ROUTES": self.qq_group_routes,
            "AI_BASE_URL": self.ai_base_url,
            "AI_API_KEY": self.ai_api_key,
            "AI_MODEL": self.ai_model,
        }
        return [name for name, value in values.items() if not value]


settings = Settings.from_environment()


class SignatureService:
    """负责 QQ 回调签名生成与校验。"""

    def __init__(self, bot_secret: str) -> None:
        self._signing_key: SigningKey | None = None
        if bot_secret:
            secret_bytes = bot_secret.encode("utf-8")
            repeats = (32 + len(secret_bytes) - 1) // len(secret_bytes)
            seed = (secret_bytes * repeats)[:32]
            self._signing_key = SigningKey(seed)

    def _require_key(self) -> SigningKey:
        if self._signing_key is None:
            raise RuntimeError("未配置 QQ_APP_SECRET")
        return self._signing_key

    def sign_validation(self, event_ts: str, plain_token: str) -> str:
        message = f"{event_ts}{plain_token}".encode("utf-8")
        signature = self._require_key().sign(message).signature
        return signature.hex()

    def verify_callback(
        self,
        timestamp: str,
        raw_body: bytes,
        signature_hex: str,
    ) -> bool:
        try:
            signature = bytes.fromhex(signature_hex)
            message = timestamp.encode("utf-8") + raw_body
            self._require_key().verify_key.verify(message, signature)
            return True
        except (BadSignatureError, ValueError, RuntimeError):
            return False


class QQTokenManager:
    TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
    REFRESH_AHEAD_SECONDS = 300

    def __init__(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_access_token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.monotonic() < self._expires_at:
            return self._access_token

        async with self._lock:
            if self._access_token and time.monotonic() < self._expires_at:
                return self._access_token
            if not settings.qq_app_id or not settings.qq_app_secret:
                raise RuntimeError("未配置 QQ_APP_ID 或 QQ_APP_SECRET")

            response = await client.post(
                self.TOKEN_URL,
                json={
                    "appId": settings.qq_app_id,
                    "clientSecret": settings.qq_app_secret,
                },
            )
            response.raise_for_status()
            data = response.json()
            access_token = data.get("access_token")
            if not access_token:
                raise RuntimeError(f"QQ access_token 响应异常: {data}")

            try:
                expires_in = int(data.get("expires_in", 7200))
            except (TypeError, ValueError):
                expires_in = 7200
            usable_seconds = max(60, expires_in - self.REFRESH_AHEAD_SECONDS)
            self._access_token = str(access_token)
            self._expires_at = time.monotonic() + usable_seconds
            logger.info("QQ access_token 获取成功，有效缓存 %s 秒", usable_seconds)
            return self._access_token

    def invalidate(self) -> None:
        self._access_token = ""
        self._expires_at = 0.0


class QQMessageService:
    def __init__(self, token_manager: QQTokenManager) -> None:
        self._token_manager = token_manager

    async def get_member_nickname(
        self,
        client: httpx.AsyncClient,
        group_openid: str,
        member_openid: str,
    ) -> str:
        token = await self._token_manager.get_access_token(client)
        url = (
            "https://api.bot.qq.com/v2/groups/"
            f"{group_openid}/members/{member_openid}"
        )
        response = await client.get(
            url,
            headers={"Authorization": f"QQBot {token}"},
        )
        if response.status_code == 401:
            self._token_manager.invalidate()
        if response.is_error:
            logger.warning(
                "获取群成员信息失败，status=%s，group_openid=%s，member_openid=%s，response=%s",
                response.status_code,
                group_openid,
                member_openid,
                response.text[:300],
            )
            return ""
        data = response.json()
        for key in ("nick", "nickname", "member_nick", "name"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    async def send_group_message(
        self,
        client: httpx.AsyncClient,
        group_openid: str,
        content: str,
        msg_id: str | None = None,
    ) -> None:
        token = await self._token_manager.get_access_token(client)
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id

        url = f"https://api.bot.qq.com/v2/groups/{group_openid}/messages"
        response = await client.post(
            url,
            headers={"Authorization": f"QQBot {token}"},
            json=body,
        )
        if response.status_code == 401:
            self._token_manager.invalidate()
        if response.is_error:
            logger.error(
                "QQ 消息发送失败，status=%s，response=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()

    async def send_private_message(
        self,
        client: httpx.AsyncClient,
        user_openid: str,
        content: str,
        msg_id: str | None = None,
    ) -> None:
        token = await self._token_manager.get_access_token(client)
        body: dict[str, Any] = {"content": content, "msg_type": 0}
        if msg_id:
            body["msg_id"] = msg_id

        url = f"https://api.bot.qq.com/v2/users/{user_openid}/messages"
        response = await client.post(
            url,
            headers={"Authorization": f"QQBot {token}"},
            json=body,
        )
        if response.status_code == 401:
            self._token_manager.invalidate()
        if response.is_error:
            logger.error(
                "QQ 私聊消息发送失败，status=%s，response=%s",
                response.status_code,
                response.text[:500],
            )
        response.raise_for_status()


signature_service = SignatureService(settings.qq_app_secret)
token_manager = QQTokenManager()
qq_message_service = QQMessageService(token_manager)


def ai_chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


GROUP_PROMPT_FILE = "prompt-group.txt"
CHAT_PROMPT_FILE = "prompt-chat.txt"


async def read_system_prompt(prompt_file: str) -> str:
    prompt_path = BASE_DIR / prompt_file
    return (await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")).strip()


async def call_ai(
    client: httpx.AsyncClient,
    user_message: str,
    prompt_file: str,
) -> str:
    try:
        if not settings.ai_base_url or not settings.ai_api_key or not settings.ai_model:
            raise RuntimeError("AI API 环境变量未完整配置")
        system_prompt = await read_system_prompt(prompt_file)
        if not system_prompt:
            raise RuntimeError(f"{prompt_file} 为空")

        response = await client.post(
            ai_chat_completions_url(settings.ai_base_url),
            headers={
                "Authorization": f"Bearer {settings.ai_api_key}",
                "Content-Type": "application/json",
                **settings.ai_extra_headers,
            },
            json={
                "model": settings.ai_model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=settings.ai_timeout_seconds,
        )
        if response.is_error:
            logger.error(
                "AI API 返回错误状态，status=%s，url=%s，response=%s",
                response.status_code,
                response.request.url,
                response.text[:500],
            )
        response.raise_for_status()
        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                str(part.get("text", ""))
                for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            ).strip()
            if text:
                return text
        raise RuntimeError("AI API 未返回有效文本")
    except FileNotFoundError:
        logger.exception("找不到系统提示词文件 %s", prompt_file)
        return f"系统提示词文件 {prompt_file} 不存在，请联系管理员。"
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, RuntimeError):
        logger.exception("AI API 调用失败")
        return "AI 服务暂时不可用，请稍后再试。"


def split_reply(
    content: str,
    limit: int = 1900,
    max_parts: int = 5,
) -> tuple[list[str], bool]:
    """按长度切分文本，尽量避免在代码块的某一行中间切断。"""
    if len(content) <= limit:
        return [content], False

    parts: list[str] = []
    remaining = content
    inside_code_block = False

    while remaining and len(parts) < max_parts:
        if len(remaining) <= limit:
            cut = len(remaining)
        else:
            cut = limit
            state_at_cut = inside_code_block ^ (
                remaining[:cut].count("```") % 2 == 1
            )
            search_start = max(0, cut - 600)
            newline = remaining.rfind("\n", search_start, cut + 1)
            if state_at_cut and newline > 0:
                cut = newline + 1
            elif newline >= int(limit * 0.75):
                cut = newline + 1
            else:
                space = remaining.rfind(" ", int(limit * 0.75), cut + 1)
                if space > 0:
                    cut = space + 1

        part = remaining[:cut]
        if not part:
            part = remaining[:limit]
            cut = len(part)
        parts.append(part)
        inside_code_block ^= part.count("```") % 2 == 1
        remaining = remaining[cut:]

    truncated = bool(remaining)
    if truncated:
        suffix = "\n\n（内容过长，已截断）"
        parts[-1] = parts[-1][: limit - len(suffix)].rstrip() + suffix
    return parts, truncated


def format_event_dump(payload: Any) -> str:
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(payload)
    return text[:4000]


MENTION_TAG_PATTERN = re.compile(r"<@[^>\s]{1,64}>")


@dataclass(frozen=True)
class MessageItem:
    member_openid: str
    nickname: str
    content: str

    @property
    def sender_label(self) -> str:
        if self.nickname:
            return f"{self.nickname}（{self.member_openid}）"
        return self.member_openid


class MessageBatcher:
    def __init__(self, batch_size: int = 5) -> None:
        self.batch_size = batch_size
        self._buffers: dict[str, list[MessageItem]] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        group_openid: str,
        item: MessageItem,
    ) -> tuple[list[MessageItem] | None, int]:
        async with self._lock:
            buffer = self._buffers.setdefault(group_openid, [])
            buffer.append(item)
            count = len(buffer)
            if count < self.batch_size:
                return None, count

            batch = buffer[: self.batch_size]
            del buffer[: self.batch_size]
            if not buffer:
                self._buffers.pop(group_openid, None)
            return batch, self.batch_size


message_batcher = MessageBatcher(batch_size=5)


class MentionRateLimiter:
    """按成员限制单位时间内的被 @ 次数。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def is_limited(self, member_openid: str) -> bool:
        if not member_openid:
            return True
        now = time.monotonic()
        window_start = now - self.window_seconds
        recent = [
            timestamp
            for timestamp in self._hits.get(member_openid, ())
            if timestamp > window_start
        ]
        if len(recent) >= self.limit:
            self._hits[member_openid] = recent
            return True
        recent.append(now)
        self._hits[member_openid] = recent
        return False


mention_rate_limiter = MentionRateLimiter(
    settings.bot_mention_rate_limit,
    settings.bot_mention_rate_window_seconds,
)


def strip_mention_tags(text: str) -> str:
    return MENTION_TAG_PATTERN.sub("", text).strip()


async def resolve_member_nickname(
    client: httpx.AsyncClient,
    group_openid: str,
    member_openid: str,
) -> str:
    if not settings.qq_fetch_member_nickname or not member_openid:
        return ""
    try:
        return await qq_message_service.get_member_nickname(
            client,
            group_openid,
            member_openid,
        )
    except (httpx.HTTPError, ValueError, RuntimeError):
        logger.exception(
            "获取群成员昵称异常，group_openid=%s，member_openid=%s",
            group_openid,
            member_openid,
        )
        return ""


def build_mention_instruction(messages: list[MessageItem]) -> str:
    if not settings.ai_mention_instruction:
        return ""

    member_openids = list(
        dict.fromkeys(
            message.member_openid
            for message in messages
            if message.member_openid
        )
    )
    if not member_openids:
        return ""

    available = "、".join(
        f"<@{member_openid}>"
        for member_openid in member_openids
    )
    return (
        "\n\n【@ 成员说明】\n"
        "如果你想在回复中点名某位群友，请在需要的位置使用 "
        "<@成员openid> 的形式，"
        "例如 <@ABC123> 你说的这个问题我知道。\n"
        "可以 @ 一位或多位，多位之间用空格分隔，且必须写在回复正文中。\n"
        f"本批消息中可 @ 的成员有：{available}\n"
        "是否 @ 由你根据语境自行判断；"
        "如果不需要点名任何人，请不要输出 <@ 形式的文本。"
    )


def format_message_batch(messages: list[MessageItem]) -> str:
    formatted_messages = "\n\n".join(
        f"第 {index} 条消息，发送者：{message.sender_label}（ID：{message.member_openid}）\n{message.content}"
        for index, message in enumerate(messages, start=1)
    )
    batch_text = (
        f"以下是群聊中连续收到的 {len(messages)} 条消息：\n\n"
        f"{formatted_messages}"
    )
    return batch_text + build_mention_instruction(messages)


async def send_ai_reply(
    client: httpx.AsyncClient,
    send_part: Callable[[httpx.AsyncClient, str, str | None], Awaitable[None]],
    target_label: str,
    source_msg_id: str | None,
    ai_reply: str,
) -> None:
    parts, truncated = split_reply(ai_reply)
    await send_part(client, parts[0], source_msg_id)
    logger.info(
        "已发送第 1/%s 片回复，target=%s，truncated=%s",
        len(parts),
        target_label,
        truncated,
    )

    for index, part in enumerate(parts[1:], start=2):
        await asyncio.sleep(0.8)
        try:
            await send_part(client, part, None)
            logger.info(
                "已发送第 %s/%s 片主动回复，target=%s",
                index,
                len(parts),
                target_label,
            )
        except httpx.HTTPError:
            logger.warning(
                "主动消息发送失败，停止后续分片，target=%s",
                target_label,
            )
            if source_msg_id:
                try:
                    await send_part(client, "（内容过长已截断）", source_msg_id)
                except (httpx.HTTPError, RuntimeError):
                    logger.exception("发送内容截断提示失败")
            break


async def deliver_group_reply(
    client: httpx.AsyncClient,
    event_id: str,
    source_group_openid: str,
    source_msg_id: str,
    ai_reply: str,
) -> None:
    target_group_openid = settings.qq_group_routes[source_group_openid]
    passive_msg_id = (
        source_msg_id
        if target_group_openid == source_group_openid
        else None
    )
    mention_count = ai_reply.count("<@")
    if mention_count:
        logger.info(
            "AI 回复中包含 @ 成员，group_openid=%s，target_group_openid=%s，count=%s",
            source_group_openid,
            target_group_openid,
            mention_count,
        )

    async def send_group_part(
        target_client: httpx.AsyncClient,
        content: str,
        msg_id: str | None,
    ) -> None:
        await qq_message_service.send_group_message(
            target_client,
            target_group_openid,
            content,
            msg_id=msg_id,
        )

    try:
        await send_ai_reply(
            client,
            send_group_part,
            f"group:{target_group_openid}",
            passive_msg_id,
            ai_reply,
        )
    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "向目标群发送回复失败，event_id=%s，target_group_openid=%s",
            event_id,
            target_group_openid,
        )
    except Exception:
        logger.exception(
            "向目标群发送回复时出现未预期异常，event_id=%s，target_group_openid=%s",
            event_id,
            target_group_openid,
        )


async def send_mention_busy_reply(
    client: httpx.AsyncClient,
    source_group_openid: str,
    source_msg_id: str,
    member_openid: str,
) -> None:
    target_group_openid = settings.qq_group_routes[source_group_openid]
    busy_text = settings.bot_mention_busy_message
    if member_openid:
        busy_text = f"<@{member_openid}> {busy_text}"
    passive_msg_id = (
        source_msg_id
        if target_group_openid == source_group_openid
        else None
    )
    try:
        await qq_message_service.send_group_message(
            client,
            target_group_openid,
            busy_text,
            msg_id=passive_msg_id,
        )
    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "发送 @ 频率提示失败，group_openid=%s，member_openid=%s",
            source_group_openid,
            member_openid,
        )
    except Exception:
        logger.exception(
            "发送 @ 频率提示时出现未预期异常，group_openid=%s，member_openid=%s",
            source_group_openid,
            member_openid,
        )


async def process_mentioned_group_message(
    client: httpx.AsyncClient,
    event_id: str,
    source_group_openid: str,
    source_msg_id: str,
    item: MessageItem,
) -> None:
    if mention_rate_limiter.is_limited(item.member_openid):
        logger.info(
            "触发 @ 频率限制，忽略 AI 调用，group_openid=%s，member_openid=%s，limit=%s，window=%s",
            source_group_openid,
            item.member_openid,
            mention_rate_limiter.limit,
            mention_rate_limiter.window_seconds,
        )
        await send_mention_busy_reply(
            client,
            source_group_openid,
            source_msg_id,
            item.member_openid,
        )
        return

    logger.info(
        "机器人被 @，立即调用 AI，group_openid=%s，member_openid=%s",
        source_group_openid,
        item.member_openid,
    )
    try:
        ai_reply = await call_ai(
            client,
            format_message_batch([item]),
            GROUP_PROMPT_FILE,
        )
    except Exception:
        logger.exception("被 @ 消息 AI 处理出现未预期异常，event_id=%s", event_id)
        return

    await deliver_group_reply(
        client,
        event_id,
        source_group_openid,
        source_msg_id,
        ai_reply,
    )


async def process_group_message(
    client: httpx.AsyncClient,
    event_id: str,
    source_group_openid: str,
    member_openid: str,
    source_msg_id: str,
    content: str,
    is_mentioned: bool,
) -> None:
    nickname = await resolve_member_nickname(
        client,
        source_group_openid,
        member_openid,
    )
    logger.info(
        "开始处理群消息，event_id=%s，group_openid=%s，member_openid=%s，nickname=%s，mentioned=%s，length=%s，content=%s",
        event_id,
        source_group_openid,
        member_openid,
        nickname,
        is_mentioned,
        len(content),
        content,
    )
    item = MessageItem(
        member_openid=member_openid,
        nickname=nickname,
        content=strip_mention_tags(content) or content,
    )

    if is_mentioned and settings.bot_mention_reply_enabled:
        await process_mentioned_group_message(
            client,
            event_id,
            source_group_openid,
            source_msg_id,
            item,
        )
        return

    batch, buffered_count = await message_batcher.add(
        source_group_openid,
        item,
    )
    if batch is None:
        logger.info(
            "群消息已加入 AI 批次，group_openid=%s，progress=%s/%s",
            source_group_openid,
            buffered_count,
            message_batcher.batch_size,
        )
        return

    logger.info(
        "群消息批次已满，开始调用 AI，group_openid=%s，count=%s",
        source_group_openid,
        len(batch),
    )
    try:
        ai_reply = await call_ai(
            client,
            format_message_batch(batch),
            GROUP_PROMPT_FILE,
        )
    except Exception:
        logger.exception("AI 消息后台处理出现未预期异常，event_id=%s", event_id)
        return

    await deliver_group_reply(
        client,
        event_id,
        source_group_openid,
        source_msg_id,
        ai_reply,
    )


PRIVATE_CHAT_DENIED_MESSAGE = "无私聊权限"


async def process_private_message_denied(
    client: httpx.AsyncClient,
    event_id: str,
    user_openid: str,
    source_msg_id: str,
) -> None:
    logger.info(
        "私聊白名单校验失败，忽略该消息，event_id=%s，user_openid=%s",
        event_id,
        user_openid,
    )
    try:
        await qq_message_service.send_private_message(
            client,
            user_openid,
            PRIVATE_CHAT_DENIED_MESSAGE,
            msg_id=source_msg_id,
        )
    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "发送私聊权限提示失败，event_id=%s，user_openid=%s",
            event_id,
            user_openid,
        )
    except Exception:
        logger.exception(
            "发送私聊权限提示时出现未预期异常，event_id=%s，user_openid=%s",
            event_id,
            user_openid,
        )


async def process_private_message(
    client: httpx.AsyncClient,
    event_id: str,
    user_openid: str,
    source_msg_id: str,
    content: str,
) -> None:
    logger.info(
        "开始处理私聊消息，event_id=%s，user_openid=%s，length=%s，content=%s",
        event_id,
        user_openid,
        len(content),
        content,
    )
    try:
        ai_reply = await call_ai(client, content, CHAT_PROMPT_FILE)
    except Exception:
        logger.exception("私聊 AI 消息处理出现未预期异常，event_id=%s", event_id)
        return

    async def send_private_part(
        target_client: httpx.AsyncClient,
        part: str,
        msg_id: str | None,
    ) -> None:
        await qq_message_service.send_private_message(
            target_client,
            user_openid,
            part,
            msg_id=msg_id,
        )

    try:
        await send_ai_reply(
            client,
            send_private_part,
            f"user:{user_openid}",
            source_msg_id,
            ai_reply,
        )
    except (httpx.HTTPError, RuntimeError):
        logger.exception(
            "私聊回复发送失败，event_id=%s，user_openid=%s",
            event_id,
            user_openid,
        )
    except Exception:
        logger.exception(
            "私聊回复发送时出现未预期异常，event_id=%s，user_openid=%s",
            event_id,
            user_openid,
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    missing = settings.missing_variables()
    if missing:
        logger.warning("以下环境变量尚未配置：%s", ", ".join(missing))
    app.state.http_client = httpx.AsyncClient(
        timeout=settings.qq_timeout_seconds,
        trust_env=False,
    )
    logger.info("QQ Bot Webhook 服务启动")
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        logger.info("QQ Bot Webhook 服务停止")


app = FastAPI(title="QQ Bot AI Webhook", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("收到无法解析的 Webhook JSON")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid payload")

    op = payload.get("op")
    if op == 13:
        data = payload.get("d") or {}
        plain_token = str(data.get("plain_token", ""))
        event_ts = str(data.get("event_ts", ""))
        if not plain_token or not event_ts:
            raise HTTPException(status_code=400, detail="Invalid validation payload")
        try:
            signature = signature_service.sign_validation(event_ts, plain_token)
        except RuntimeError as exc:
            logger.error("回调地址验证失败：%s", exc)
            raise HTTPException(status_code=500, detail=str(exc))
        logger.info("已处理 op=13 回调地址验证")
        return JSONResponse(
            {"plain_token": plain_token, "signature": signature}
        )

    if op != 0:
        logger.info("忽略未处理的 Webhook op=%s", op)
        return JSONResponse({"op": 12})

    signature = request.headers.get("X-Signature-Ed25519", "")
    timestamp = request.headers.get("X-Signature-Timestamp", "")
    if not signature or not timestamp:
        logger.warning("Webhook 请求缺少签名请求头")
        raise HTTPException(status_code=401, detail="Missing signature")
    if not signature_service.verify_callback(timestamp, raw_body, signature):
        logger.warning("Webhook 签名校验失败")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = payload.get("t")
    if event_type == "C2C_MESSAGE_CREATE":
        if not settings.qq_private_chat_enabled:
            logger.info("私聊功能已关闭，忽略事件，event_id=%s", payload.get("id", ""))
            return JSONResponse({"op": 12})

        chat_data = payload.get("d") or {}
        chat_author = chat_data.get("author") or {}
        if chat_author.get("bot") is True:
            logger.info("忽略机器人自身消息，event_id=%s", payload.get("id", ""))
            return JSONResponse({"op": 12})

        chat_event_id = str(payload.get("id", ""))
        user_openid = str(
            chat_author.get("user_openid")
            or chat_data.get("user_openid")
            or ""
        )
        chat_content = str(chat_data.get("content", ""))
        chat_msg_id = str(chat_data.get("id", ""))

        logger.info(
            "收到私聊消息事件原文，event_id=%s，payload=%s",
            chat_event_id,
            format_event_dump(payload),
        )

        if not user_openid or not chat_msg_id or not chat_content.strip():
            logger.warning(
                "私聊消息字段不完整，event_id=%s，user_openid=%s，msg_id=%s",
                chat_event_id,
                bool(user_openid),
                bool(chat_msg_id),
            )
            return JSONResponse({"op": 12})

        if (
            settings.qq_private_chat_allowlist
            and user_openid not in settings.qq_private_chat_allowlist
        ):
            logger.info(
                "忽略不在私聊白名单中的消息，event_id=%s，user_openid=%s",
                chat_event_id,
                user_openid,
            )
            background_tasks.add_task(
                process_private_message_denied,
                request.app.state.http_client,
                chat_event_id,
                user_openid,
                chat_msg_id,
            )
            return JSONResponse({"op": 12})

        logger.info(
            "已接收私聊消息，event_id=%s，user_openid=%s，msg_id=%s",
            chat_event_id,
            user_openid,
            chat_msg_id,
        )
        background_tasks.add_task(
            process_private_message,
            request.app.state.http_client,
            chat_event_id,
            user_openid,
            chat_msg_id,
            chat_content,
        )
        return JSONResponse({"op": 12})

    if event_type != "GROUP_MESSAGE_CREATE":
        logger.info("忽略事件类型 t=%s", event_type)
        return JSONResponse({"op": 12})

    data = payload.get("d") or {}
    author = data.get("author") or {}
    if author.get("bot") is True:
        logger.info("忽略机器人自身消息，event_id=%s", payload.get("id", ""))
        return JSONResponse({"op": 12})

    logger.info(
        "收到群消息事件原文，event_id=%s，payload=%s",
        payload.get("id", ""),
        format_event_dump(payload),
    )

    event_id = str(payload.get("id", ""))
    group_openid = str(data.get("group_openid", ""))
    member_openid = str(author.get("member_openid", ""))
    content = str(data.get("content", ""))
    source_msg_id = str(data.get("id", ""))
    mentions = data.get("mentions") or []
    is_mentioned = any(
        isinstance(item, dict) and item.get("is_you") is True
        for item in mentions
    )

    if not group_openid or not source_msg_id or not content.strip():
        logger.warning(
            "群消息字段不完整，event_id=%s，group_openid=%s，msg_id=%s",
            event_id,
            bool(group_openid),
            bool(source_msg_id),
        )
        return JSONResponse({"op": 12})

    if group_openid not in settings.qq_group_routes:
        logger.info(
            "忽略不在接收列表中的群消息，event_id=%s，group_openid=%s",
            event_id,
            group_openid,
        )
        return JSONResponse({"op": 12})

    background_tasks.add_task(
        process_group_message,
        request.app.state.http_client,
        event_id,
        group_openid,
        member_openid,
        source_msg_id,
        content,
        is_mentioned,
    )
    return JSONResponse({"op": 12})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8080)
