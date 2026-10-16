from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, AsyncIterator

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


signature_service = SignatureService(settings.qq_app_secret)
token_manager = QQTokenManager()
qq_message_service = QQMessageService(token_manager)


def ai_chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


async def read_system_prompt() -> str:
    prompt_path = BASE_DIR / "prompt.txt"
    return (await asyncio.to_thread(prompt_path.read_text, encoding="utf-8")).strip()


async def call_ai(client: httpx.AsyncClient, user_message: str) -> str:
    try:
        if not settings.ai_base_url or not settings.ai_api_key or not settings.ai_model:
            raise RuntimeError("AI API 环境变量未完整配置")
        system_prompt = await read_system_prompt()
        if not system_prompt:
            raise RuntimeError("prompt.txt 为空")

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
        logger.exception("找不到系统提示词文件 prompt.txt")
        return "系统提示词文件 prompt.txt 不存在，请联系管理员。"
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


class MessageBatcher:
    def __init__(self, batch_size: int = 5) -> None:
        self.batch_size = batch_size
        self._buffers: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()

    async def add(
        self,
        group_openid: str,
        content: str,
    ) -> tuple[list[str] | None, int]:
        async with self._lock:
            buffer = self._buffers.setdefault(group_openid, [])
            buffer.append(content)
            count = len(buffer)
            if count < self.batch_size:
                return None, count

            batch = buffer[: self.batch_size]
            del buffer[: self.batch_size]
            if not buffer:
                self._buffers.pop(group_openid, None)
            return batch, self.batch_size


message_batcher = MessageBatcher(batch_size=5)


def format_message_batch(messages: list[str]) -> str:
    formatted_messages = "\n\n".join(
        f"第 {index} 条消息：\n{message}"
        for index, message in enumerate(messages, start=1)
    )
    return f"以下是群聊中连续收到的 {len(messages)} 条消息：\n\n{formatted_messages}"


async def send_ai_reply(
    client: httpx.AsyncClient,
    group_openid: str,
    source_msg_id: str | None,
    ai_reply: str,
) -> None:
    parts, truncated = split_reply(ai_reply)
    await qq_message_service.send_group_message(
        client,
        group_openid,
        parts[0],
        msg_id=source_msg_id,
    )
    logger.info(
        "已发送第 1/%s 片回复，group_openid=%s，truncated=%s",
        len(parts),
        group_openid,
        truncated,
    )

    for index, part in enumerate(parts[1:], start=2):
        await asyncio.sleep(0.8)
        try:
            await qq_message_service.send_group_message(
                client,
                group_openid,
                part,
            )
            logger.info(
                "已发送第 %s/%s 片主动回复，group_openid=%s",
                index,
                len(parts),
                group_openid,
            )
        except httpx.HTTPError:
            logger.warning(
                "主动消息发送失败，停止后续分片，group_openid=%s",
                group_openid,
            )
            if source_msg_id:
                try:
                    await qq_message_service.send_group_message(
                        client,
                        group_openid,
                        "（内容过长已截断）",
                        msg_id=source_msg_id,
                    )
                except (httpx.HTTPError, RuntimeError):
                    logger.exception("发送内容截断提示失败")
            break


async def process_group_message(
    client: httpx.AsyncClient,
    event_id: str,
    source_group_openid: str,
    member_openid: str,
    source_msg_id: str,
    content: str,
    is_mentioned: bool,
) -> None:
    logger.info(
        "开始处理群消息，event_id=%s，group_openid=%s，member_openid=%s，mentioned=%s，length=%s，content=%s",
        event_id,
        source_group_openid,
        member_openid,
        is_mentioned,
        len(content),
        content,
    )
    batch, buffered_count = await message_batcher.add(
        source_group_openid,
        content,
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
        ai_reply = await call_ai(client, format_message_batch(batch))
    except Exception:
        logger.exception("AI 消息后台处理出现未预期异常，event_id=%s", event_id)
        return

    target_group_openid = settings.qq_group_routes[source_group_openid]
    passive_msg_id = (
        source_msg_id
        if target_group_openid == source_group_openid
        else None
    )
    try:
        await send_ai_reply(
            client,
            target_group_openid,
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
