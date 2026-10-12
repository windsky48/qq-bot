# QQ 群聊 AI Webhook 机器人

基于 FastAPI 的 QQ 官方机器人 Webhook 服务。程序接收 QQ 开放平台 API v2 的全量群消息，按群累计 5 条消息后调用兼容 OpenAI Chat Completions 格式的 AI API，并将回复发送到指定群聊。

## 功能

- 监听 `POST /webhook`
- 支持 `op=13` 回调地址验证
- 使用 Ed25519 校验 `op=0` 事件签名
- 处理 `GROUP_MESSAGE_CREATE` 全量群消息
- 分别配置允许接收消息和发送回复的群列表
- 每个接收群独立累计 5 条消息后调用一次 AI
- 缓存并提前刷新 QQ `access_token`
- AI 回复超过 1900 字符时最多拆分为 5 片
- 后台异步调用 AI，不阻塞 Webhook 响应
- 日志写入 `logs/YYYY-MM-DD.log`

## 环境要求

- Python 3.10 或更高版本
- 已在 QQ 开放平台创建机器人并取得 AppID、AppSecret
- 已订阅 `GROUP_MESSAGE_CREATE` 事件
- 群主已在手机 QQ 群设置中，将机器人消息范围设置为“获取群内全部消息”
- 可公网访问的 HTTP/HTTPS 回调地址，端口使用 `80`、`443`、`8080` 或 `8443`

## 安装

在 PowerShell 中执行：

```powershell
Set-Location E:\qqbot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

如果 PowerShell 禁止执行激活脚本，也可以不激活虚拟环境，直接使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py
```

## 配置

编辑 `.env`：

```env
QQ_APP_ID=your_qq_app_id
QQ_APP_SECRET=your_qq_app_secret

QQ_RECEIVE_GROUP_OPENIDS=["receive_group_openid_1","receive_group_openid_2"]
QQ_SEND_GROUP_OPENIDS=["send_group_openid_1","send_group_openid_2"]

AI_BASE_URL=https://your-ai-api.example.com/v1
AI_API_KEY=your_ai_api_key
AI_MODEL=your_model_name
```

### 环境变量说明

| 变量 | 说明 |
| --- | --- |
| `QQ_APP_ID` | QQ 开放平台机器人 AppID |
| `QQ_APP_SECRET` | QQ 开放平台机器人 AppSecret，同时用于生成 Ed25519 签名密钥 |
| `QQ_RECEIVE_GROUP_OPENIDS` | 允许接收消息的群 `group_openid` JSON 字符串列表 |
| `QQ_SEND_GROUP_OPENIDS` | AI 回复的目标群 `group_openid` JSON 字符串列表 |
| `AI_BASE_URL` | 兼容 OpenAI 格式的 API 基础地址，程序会自动追加 `/chat/completions` |
| `AI_API_KEY` | AI API 密钥 |
| `AI_MODEL` | AI 模型名称 |

群列表必须使用 JSON 字符串数组，即使 ID 看起来是数字也必须加双引号：

```env
QQ_RECEIVE_GROUP_OPENIDS=["123456789"]
```

这里需要填写事件日志中的 `group_openid`，不能填写普通 QQ 群号。程序会忽略不在接收列表中的群消息，并将每个完成的消息批次广播到发送列表中的所有群。

目标群与消息来源群相同时，第一条回复会携带原消息的 `msg_id` 作为被动回复；其他目标群使用主动消息，因此机器人必须具备主动推送权限。

## 系统提示词

在项目目录创建 `prompt.txt`：

```text
你是一个群聊机器人，请根据最近的群聊内容给出自然、简洁的回复。
```

程序每次调用 AI 时都会读取该文件，因此修改提示词后通常不需要重启服务。

## 启动

```powershell
Set-Location E:\qqbot
uvicorn main:app --host 0.0.0.0 --port 8080
```

也可以直接运行：

```powershell
python main.py
```

启动成功后会显示：

```text
Uvicorn running on http://0.0.0.0:8080
```

## 配置 QQ 回调

QQ 开放平台的回调地址必须包含 `/webhook`：

```text
https://你的公网域名/webhook
```

如果直接开放 8080 端口：

```text
http://你的公网地址:8080/webhook
```

程序只处理 `POST /webhook`。访问根路径 `/` 返回 404 属于正常现象。QQ 平台发送 `op=13` 验证请求时，程序会自动生成并返回签名。

## 消息处理流程

1. QQ 平台向 `/webhook` 推送事件。
2. 程序使用原始请求体和签名请求头进行 Ed25519 验签。
3. 程序忽略机器人自身消息以及不在接收群列表中的消息。
4. 每个来源群独立累计消息，前 4 条只缓存。
5. 第 5 条到达后，将 5 条消息按顺序合并并调用一次 AI。
6. AI 回复发送到 `QQ_SEND_GROUP_OPENIDS` 中的每个群。
7. 每个群完成一批后重新从 1/5 开始计数。

消息批次保存在内存中。程序重启后，尚未凑满 5 条的消息会丢失并重新计数。

## 日志

日志文件位置：

```text
logs/YYYY-MM-DD.log
```

典型批处理日志：

```text
群消息已加入 AI 批次，progress=1/5
群消息批次已满，开始调用 AI，count=5
```

日志会记录收到的消息原文、事件 ID、群 `openid`、发送者 `openid`、批次进度、AI 调用错误和 QQ 消息发送错误。请妥善保管日志，避免泄露群聊内容。

## 回复分片

- 回复不超过 1900 字符时直接发送一条。
- 超过 1900 字符时最多拆分为 5 片。
- 第一片在来源群中使用 `msg_id` 被动回复。
- 后续分片使用主动消息，每片间隔 0.8 秒。
- 分片时尽量在换行处切分，避免截断代码块中的单行内容。
- 主动消息发送失败时停止发送剩余分片。

## 常见问题

### 根路径返回 404

服务接口是 `POST /webhook`，不是 `/`。请确认 QQ 回调地址包含 `/webhook`。

### 日志显示“忽略不在接收列表中的群消息”

将日志中的 `group_openid` 填入 `QQ_RECEIVE_GROUP_OPENIDS`，不要填写普通 QQ 群号，然后重启服务。

### 收到“暂不能查看该消息内容”

这是 QQ 平台放在 `d.content` 中的占位文字，常见于图片、语音、文件、卡片或其他暂不支持读取的消息类型。若纯文本也出现该内容，请让群主重新检查机器人的全量消息权限。

### AI 请求出现 `ReadTimeout`

AI 服务未在 60 秒内返回响应。请检查模型负载、API 服务状态和网络质量。

### 出现 `ConnectError`

说明服务器无法连接 AI API 或 QQ API。当前程序设置了 `trust_env=False`，不会读取系统代理环境变量。请确认当前网络能够直接访问对应 API 地址。

### 其他群无法收到回复

向非来源群发送消息属于主动推送。请确认机器人拥有主动消息权限，并且目标 `group_openid` 正确。

## 安全提示

- 不要公开或提交 `.env`、AppSecret、AI API Key。
- 不要在日志中输出 Token 或密钥。
- 建议通过 HTTPS 暴露 Webhook。
- 建议限制日志文件的访问权限并定期清理。
