#!/usr/bin/env python
"""私聊事件路由冒烟测试，仅供本地验证使用。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

import main


async def run() -> int:
    app_secret = os.getenv("QQ_APP_SECRET", "").strip()
    if not app_secret:
        print("skip: 未配置 QQ_APP_SECRET")
        return 0

    settings_ok = main.settings.qq_private_chat_enabled
    print("private_chat_enabled:", settings_ok)

    payload = {
        "id": "C2C_MESSAGE_CREATE:test",
        "op": 0,
        "t": "C2C_MESSAGE_CREATE",
        "d": {
            "id": "msg-1",
            "content": "你好",
            "author": {"user_openid": "USER1", "bot": False},
        },
    }
    raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    timestamp = "1700000000"
    signature = (
        main.signature_service.sign_validation(timestamp, "token")
    )
    del signature

    sign_hex = (
        main.signature_service._require_key()
        .sign(timestamp.encode("utf-8") + raw_body)
        .signature.hex()
    )

    sent: list[str] = []

    async def fake_send(
        client: httpx.AsyncClient,
        user_openid: str,
        content: str,
        msg_id: str | None = None,
    ) -> None:
        sent.append(f"{user_openid}|{msg_id}|{content}")

    main.qq_message_service.send_private_message = fake_send  # type: ignore

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/webhook",
            content=raw_body,
            headers={
                "Content-Type": "application/json",
                "X-Signature-Ed25519": sign_hex,
                "X-Signature-Timestamp": timestamp,
            },
        )

    print("status_code:", response.status_code)
    print("body:", response.text)
    print("sent:", sent)
    ok = response.status_code == 200 and sent and sent[0].startswith(
        "USER1|msg-1|"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
