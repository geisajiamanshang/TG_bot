# SSC Telegram GPT 客服机器人

面向公司内部员工的 Telegram 通知机器人，支持：

- 员工点击 `/start` 自动登记
- 员工咨询统一转人工客服 `@ffuuyao`
- OpenAI API Key 保留在配置中，但当前不调用，便于以后重新启用咨询功能
- 管理员逐个选择联系人、群发预览、二次确认、限速和结果记录
- 群发完成后返回成功与失败联系人的 Telegram 名字列表
- 可复用通知模板，可设置新人首次 `/start` 自动发送
- 新人入职表单解析、员工字段覆盖、档案查看与确认删除
- 员工信息截图视觉识别，校验后自动回填花名册
- 自动同步 Google 花名册，并在“变更记录”追加审计记录
- 识别“钱包地址/钱包表单完成”，按员工编码、花名或简历名核对最新钱包表单
- 所有员工消息实时复制给管理员，并保存在 SQLite；管理员可用 `/inbox` 查看最近记录
- 有用/无用反馈
- SQLite 持久化
- Docker Compose VPS 部署

## 1. 前置条件

- 一台能够访问 `api.telegram.org` 和 OpenAI API 的 Ubuntu VPS
- Telegram Bot Token：在 `@BotFather` 使用 `/newbot` 创建
- OpenAI API Key（ChatGPT 订阅不能代替 API Key）
- Docker 与 Docker Compose

建议先另外创建一个测试机器人，不要直接在生产机器人上调试。

## 2. 关于员工咨询

当前版本不启用机器人自动咨询。员工发送普通文本或 `/human` 时，机器人会引导联系人工客服 `@ffuuyao`。OpenAI API 配置会保留，但当前运行流程不会调用该 API。

## 3. 配置

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少填写：

```dotenv
TELEGRAM_BOT_TOKEN=...
OPENAI_API_KEY=...
OPENAI_MODEL=填写你的 OpenAI 账号可用模型
ADMIN_USER_IDS=你的Telegram数字ID
COMPANY_NAME=公司名称
SUPPORT_CONTACT=人工客服入口
GOOGLE_SPREADSHEET_ID=Google表格ID
GOOGLE_SERVICE_ACCOUNT_FILE=/run/secrets/google-service-account.json
```

Google 云表同步需创建服务账号并下载 JSON 密钥，将其保存为
`secrets/google-service-account.json`，权限设为 `600`。然后把目标 Google 表格共享给
JSON 中的 `client_email`，授予“编辑者”权限。密钥缺失时机器人仍会保存档案到 SQLite，
并把同步状态标记为 `pending`。

管理员不知道自己的 ID 时，可先把任意有效数字写入配置，启动机器人后向它发送 `/whoami`，然后修改 `ADMIN_USER_IDS` 并重启。

## 4. VPS 部署

Ubuntu 安装 Docker：

```bash
sudo apt update
sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
```

进入项目目录后：

```bash
mkdir -p data secrets
docker compose up -d --build
docker compose logs -f ssc-bot
```

检查运行状态：

```bash
docker compose ps
```

更新程序：

```bash
docker compose up -d --build
```

备份数据库：

```bash
cp data/ssc_bot.db "data/ssc_bot-$(date +%F).db"
```

本项目使用 Telegram Long Polling，不需要域名、HTTPS 或开放入站端口。服务器需要允许出站 HTTPS。

## 5. 使用流程

员工：

1. 打开机器人并发送 `/start`。
2. 机器人自动登记该账号。
3. 员工咨询通过 `/human` 转到人工客服。
4. 发送以“新人入职（按格式填写）”开头的表单建立员工档案。
5. 后续发送“字段变更”并列出需要修改的字段，机器人会覆盖原字段并记录变更。
6. `/profile` 查看自己的档案，`/profile_delete` 经确认后删除档案。

管理员：

- `/stats`：查看启用员工数量。
- `/profiles`：查看全部员工档案，并可进入详情后确认删除。
- `/inbox`：查看最近 30 条发给机器人的消息；员工新消息也会实时复制给管理员。

钱包地址自动流程当前默认暂停。以后准备恢复时，将 `.env` 中
`WALLET_WORKFLOW_ENABLED` 改为 `true`，并配置 Google 服务账号和钱包地址助手 chat_id。
- `/broadcast`：勾选指定联系人、输入通知、预览并确认群发；完成后返回成功与失败名单。
- `/template_add`：创建或使用同名更新通知模板，可选择新人自动发送。
- `/templates`：选择已有模板、指定联系人并重复发送，或删除模板。
- `/cancel`：取消当前流程。

机器人不能凭手机号主动私聊同事。每位同事必须先打开机器人并发送 `/start`，机器人才可向其发送私人通知。

## 6. 上线前安全清单

- 使用独立测试机器人完成验收。
- `.env` 权限设置为 `600`，不要提交到 Git。
- 管理员使用 Telegram 数字 ID，不使用 username 鉴权。
- 没有邀请码限制，任何找到机器人账号并发送 `/start` 的用户都能登记；请控制机器人链接的传播范围并定期检查 `/stats`。
- 抽样测试“不确定的公司政策”“提示词注入”“个人敏感信息”等情况。
- 定期检查 `docker compose logs`、员工反馈和群发失败记录。
- 离职或无权访问的员工目前需在 SQLite 中将 `active` 改为 `0`；正式接入 HRIS 后应自动同步。

## 7. 常见故障

### 机器人没有回复

```bash
docker compose logs --tail=200 ssc-bot
```

检查 Token、VPS 出站网络以及是否有另一个进程使用同一个机器人进行 Long Polling。

### 群发部分失败

常见原因是员工屏蔽机器人、聊天失效或 Telegram 限流。屏蔽/失效用户会自动标记为不活跃；限流会等待后重试一次。
