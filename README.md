# QQ 群聊 AI Webhook 机器人

基于 FastAPI 的 QQ 官方机器人 Webhook 服务。程序接收 QQ 开放平台 API v2 的全量群消息，按群累计 5 条消息后调用兼容 OpenAI Chat Completions 格式的 AI API，并将回复发送到指定群聊。

## 功能

- 监听 `POST /webhook`
- 支持 `op=13` 回调地址验证
- 使用 Ed25519 校验 `op=0` 事件签名
- 处理 `GROUP_MESSAGE_CREATE` 全量群消息
- 处理 `C2C_MESSAGE_CREATE` 私聊消息，单条即时回复
- 使用 JSON 字典配置接收群到发送群的一对一路由
- 群消息被 @ 时跳过批处理，立即回复，并按成员限制 @ 频率
- 每个接收群独立累计 5 条消息后调用一次 AI
- 缓存并提前刷新 QQ `access_token`
- AI 回复超过 1900 字符时最多拆分为 5 片
- 后台异步调用 AI，不阻塞 Webhook 响应
- 日志写入 `logs/YYYY-MM-DD.log`

## 环境要求

- Python 3.10 或更高版本
- 已在 QQ 开放平台创建机器人并取得 AppID、AppSecret
- 已订阅 `GROUP_MESSAGE_CREATE` 事件；如需私聊，还需订阅 `C2C_MESSAGE_CREATE` 事件
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

QQ_GROUP_ROUTES={"receive_group_openid_1":"send_group_openid_1","receive_group_openid_2":"send_group_openid_2"}

AI_BASE_URL=https://your-ai-api.example.com/v1
AI_API_KEY=your_ai_api_key
AI_MODEL=your_model_name
```

### 提示词文件

群聊和私聊使用不同提示词文件：

| 文件 | 用途 |
| --- | --- |
| `prompt-group.txt` | 群聊场景系统提示词 |
| `prompt-chat.txt` | 私聊场景系统提示词 |

两个文件都需要存在且不能为空。程序每次调用 AI 时读取对应文件，修改内容无需重启。

### 环境变量说明

| 变量 | 说明 |
| --- | --- |
| `QQ_APP_ID` | QQ 开放平台机器人 AppID |
| `QQ_APP_SECRET` | QQ 开放平台机器人 AppSecret，同时用于生成 Ed25519 签名密钥 |
| `QQ_GROUP_ROUTES` | 接收群 `group_openid` 到发送群 `group_openid` 的一对一 JSON 字典 |
| `AI_BASE_URL` | 兼容 OpenAI 格式的 API 基础地址，程序会自动追加 `/chat/completions` |
| `AI_API_KEY` | AI API 密钥 |
| `AI_MODEL` | AI 模型名称 |
| `AI_EXTRA_HEADERS` | 可选，AI API 额外请求头 JSON 字典 |
| `AI_MENTION_INSTRUCTION` | 可选，是否在发送给 AI 的群聊内容末尾附加 @ 成员说明，默认 `true` |
| `BOT_MENTION_REPLY_ENABLED` | 可选，被 @ 时是否立即回复，默认 `true` |
| `BOT_MENTION_RATE_LIMIT` | 可选，每个成员窗口期内最多 @ 次数，默认 `5` |
| `BOT_MENTION_RATE_WINDOW_SECONDS` | 可选，@ 频率统计窗口秒数，默认 `60` |
| `BOT_MENTION_BUSY_MESSAGE` | 可选，超出频率限制时的提示语，默认 `哎呀，不要AT我了喵` |
| `GROUP_MENTION_FORCE_ACTIVE` | 可选，回复含有效 @ 时是否改为主动消息发送，默认 `false` |
| `QQ_PRIVATE_CHAT_ENABLED` | 可选，是否启用私聊消息，默认 `true` |
| `QQ_PRIVATE_CHAT_ALLOWLIST` | 可选，允许私聊的 `user_openid` JSON 字符串列表，留空表示不限制 |
| `QQ_FETCH_MEMBER_NICKNAME` | 可选，是否调用 QQ API 获取群成员昵称，默认 `true` |
| `AI_TIMEOUT_SECONDS` | 可选，AI 请求超时秒数，默认 `60` |
| `QQ_TIMEOUT_SECONDS` | 可选，QQ API 请求超时秒数，默认 `30` |

群路由必须使用 JSON 字符串字典，字典的键是接收群，值是该接收群唯一对应的发送群。即使 ID 看起来是数字也必须加双引号：

```env
QQ_GROUP_ROUTES={"receive_group_openid":"send_group_openid"}
```

这里需要填写事件日志中的 `group_openid`，不能填写普通 QQ 群号。程序会忽略不在字典键中的群消息；某个接收群凑满 5 条后，AI 回复只发送到该键对应的目标群，不再广播到所有群。

目标群与消息来源群相同时，第一条回复会携带原消息的 `msg_id` 作为被动回复；目标群不同时使用主动消息，因此机器人必须具备主动推送权限。

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

## 私聊

私聊依赖 QQ 开放平台的单聊事件 `C2C_MESSAGE_CREATE`，需要在回调配置中勾选该事件。

私聊行为：

- 每条消息立即调用 AI，不参与 5 条批处理。
- 使用 `prompt-chat.txt` 作为系统提示词。
- 通过 `POST /v2/users/{user_openid}/messages` 被动回复。
- 不获取群成员昵称，不添加 @ 前缀。
- 可用 `QQ_PRIVATE_CHAT_ENABLED=false` 关闭，关闭后私聊事件不回调 AI。

白名单配置：

```env
QQ_PRIVATE_CHAT_ALLOWLIST=["user_openid_1","user_openid_2"]
```

行为：

- 留空表示不限制，任何人都可私聊。
- 配置后，仅列表内的 `user_openid` 会调用 AI。
- 不在列表内的消息会被忽略，并回复 `无私聊权限`。
- 列表值是私聊日志中的 `user_openid`，不是普通 QQ 号，需加双引号。

日志中会显示：

```text
开始处理私聊消息，event_id=...，user_openid=...，length=...，content=...
```

## 消息处理流程

1. QQ 平台向 `/webhook` 推送事件。
2. 程序使用原始请求体和签名请求头进行 Ed25519 验签。
3. 程序忽略机器人自身消息以及不在 `QQ_GROUP_ROUTES` 键中的群消息。
4. 每个来源群独立累计消息，前 4 条只缓存。
5. 第 5 条到达后，将 5 条消息按顺序合并并调用一次 AI。
6. AI 回复只发送到 `QQ_GROUP_ROUTES` 中该来源群对应的目标群。
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

日志会记录收到的消息原文、事件 ID、群 `openid`、发送者 `openid`、发送者昵称、批次进度、AI 调用错误和 QQ 消息发送错误。请妥善保管日志，避免泄露群聊内容。

发送者昵称通过 QQ API 拉取，受 `QQ_FETCH_MEMBER_NICKNAME` 控制。拉取失败时只记录发送者 `openid`，不影响机器人回复。发送给 AI 的批次文本中也会包含每条消息的发送者昵称和 ID。

## @ 成员

程序不再自动添加 @，而是由 AI 自行决定是否点名成员。

发送给 AI 的群聊批次文本末尾会附加 @ 说明：

```text
【@ 成员说明】
如果你想在回复中点名某位群友，请在需要的位置使用 <@成员openid> 的形式，openid 必须原样取自下方列表。
只能使用下方列表中出现过的 openid，严禁自行编造、猜测或改写 openid。
本批消息中可 @ 的成员 openid 为：<@ABC123>、<@DEF456>
是否 @ 由你根据语境自行判断……
```

AI 回复示例：

```text
<@ABC123> 你说的这个问题我知道
```

相关行为：

- QQ 没有独立的 @ 字段，`content` 中的 `<@openid>` 就是原生 @ 格式。
- AI 可以直接输出 `<@openid>`，也可以输出 `@昵称`，程序会自动把 `@昵称`、`＠昵称` 转成原生 `<@openid>`。
- 发送前会校验 AI 输出的 `@` 目标，只保留本批消息中真实存在的 `openid`。
- 无效目标会被替换为 `@群友`，避免群里出现 `<@乱码>`。
- 日志会记录：`AI 回复中的 @昵称 已转换为原生提及，converted=1`、`AI 回复中包含有效 @ 成员，count=1`、`AI 回复中存在无效 @ 目标，已替换为普通文本，invalid=1`。
- 可用 `AI_MENTION_INSTRUCTION=false` 关闭该说明。

注意：`@昵称` 转换依赖成员昵称，需保证 `QQ_FETCH_MEMBER_NICKNAME=true` 且成员昵称接口可用。

## 被 @ 时立即回复

群消息中 `d.mentions` 里出现 `is_you: true` 时，机器人跳过 5 条批处理，立即用该条消息调用 AI 并回复。

频率限制：

- 按成员 `member_openid` 独立统计。
- 默认 60 秒内最多 5 次。
- 超限时不调用 AI，回复 `<@发送者> 哎呀，不要AT我了喵`。
- 该提示使用被动回复，不占用主动消息配额。

配置：

```env
BOT_MENTION_REPLY_ENABLED=true
BOT_MENTION_RATE_LIMIT=5
BOT_MENTION_RATE_WINDOW_SECONDS=60
BOT_MENTION_BUSY_MESSAGE=哎呀，不要AT我了喵
```

说明：

- 消息文本中的 `<@xxx>` 标签会在发送给 AI 前去掉。
- 立即回复同样使用 `prompt-group.txt`，并附带可 @ 成员说明。
- 限流状态保存在内存中，重启后清空。

日志：

```text
机器人被 @，立即调用 AI，group_openid=...，member_openid=...
触发 @ 频率限制，忽略 AI 调用，group_openid=...，member_openid=...，limit=5，window=60.0
```

### @ 显示为纯文本

若群里看到 `<@openid>` 原文而不是 @ 效果，通常是以下原因：

- 目标 openid 不在本群，或属于其他群。
- AI 编造了不存在的 openid，程序已自动替换为 `@群友`。
- 被动回复（带 `msg_id`）中 @ 不被渲染。

第三种情况可用开关改为主动消息发送：

```env
GROUP_MENTION_FORCE_ACTIVE=true
```

开启后，含有效 @ 的回复会去掉 `msg_id` 改为主动发送，日志会记录：

```text
回复含 @ 成员，按配置改为主动消息发送，group_openid=...，event_id=...
```

注意：主动消息需要机器人在开放平台具备主动推送权限，且不受被动回复 5 条 / 5 分钟限制约束。

## 回复分片

- 回复不超过 1900 字符时直接发送一条。
- 超过 1900 字符时最多拆分为 5 片。
- 目标群与来源群相同时，第一片使用 `msg_id` 被动回复；目标群不同时，所有分片均为主动消息。
- 后续分片使用主动消息，每片间隔 0.8 秒。
- 分片时尽量在换行处切分，避免截断代码块中的单行内容。
- 主动消息发送失败时停止发送剩余分片。

## 常见问题

### 根路径返回 404

服务接口是 `POST /webhook`，不是 `/`。请确认 QQ 回调地址包含 `/webhook`。

### 日志显示“忽略不在接收列表中的群消息”

将日志中的来源群 `group_openid` 作为键添加到 `QQ_GROUP_ROUTES`，并将唯一目标群 `group_openid` 作为值；不要填写普通 QQ 群号，然后重启服务。

### 收到“暂不能查看该消息内容”

这是 QQ 平台放在 `d.content` 中的占位文字，常见于图片、语音、文件、卡片或其他暂不支持读取的消息类型。若纯文本也出现该内容，请让群主重新检查机器人的全量消息权限。

### AI 请求出现 403

说明密钥、模型权限或 API 来源校验未通过。请先确认 `AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL` 与可用客户端完全一致。若该 API 校验来源标识或 `User-Agent`，可在 `.env` 中补充：

```env
AI_EXTRA_HEADERS={"HTTP-Referer":"https://your-site.example","X-Title":"QQ Bot","User-Agent":"CherryStudio/1.0"}
```

日志中的 `AI API 返回错误状态` 会打印实际请求地址和响应内容，可用于确认具体原因。

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
