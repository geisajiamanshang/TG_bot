import express from "express";
import OpenAI from "openai";

const app = express();
app.use(express.json({ limit: "1mb" }));

const {
  TELEGRAM_BOT_TOKEN,
  OPENAI_API_KEY,
  WEBHOOK_SECRET,
  PORT = 3000
} = process.env;

if (!TELEGRAM_BOT_TOKEN || !OPENAI_API_KEY || !WEBHOOK_SECRET) {
  throw new Error("缺少必要的环境变量");
}

const openai = new OpenAI({
  apiKey: OPENAI_API_KEY
});

const telegramApi =
  `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}`;

/**
 * 保存已经处理过的更新，防止 Telegram 重试时重复回复。
 * 正式环境建议换成 Redis 或数据库。
 */
const processedUpdates = new Set();

async function sendBusinessMessage({
  businessConnectionId,
  chatId,
  text,
  replyToMessageId
}) {
  const response = await fetch(`${telegramApi}/sendMessage`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      business_connection_id: businessConnectionId,
      chat_id: chatId,
      text,
      reply_parameters: replyToMessageId
        ? { message_id: replyToMessageId }
        : undefined
    })
  });

  const result = await response.json();

  if (!result.ok) {
    throw new Error(
      `Telegram 发送失败：${JSON.stringify(result)}`
    );
  }

  return result.result;
}

async function generateReply(customerMessage) {
  const response = await openai.responses.create({
    model: "gpt-5.4-mini",
    instructions: `
你是“小杰工作室”的 Telegram 客服。

规则：
1. 使用简体中文。
2. 回复友好、简洁，不超过150字。
3. 不编造价格、库存、营业时间或承诺。
4. 不确定的信息请说明需要人工客服确认。
5. 不要求客户提供密码、验证码或银行卡密码。
6. 如果客户要求人工服务，回复：“好的，我已经记录，稍后由人工客服回复您。”
7. 营业时间为周一至周五 09:00–18:00。
8. 禁止发送营销骚扰内容。
    `.trim(),
    input: customerMessage,
    max_output_tokens: 300
  });

  return response.output_text?.trim() ||
    "您好，我们已收到您的消息，稍后由人工客服回复您。";
}

app.get("/", (req, res) => {
  res.status(200).send("Telegram GPT service is running");
});

app.post("/telegram/webhook", async (req, res) => {
  const receivedSecret =
    req.headers["x-telegram-bot-api-secret-token"];

  if (receivedSecret !== WEBHOOK_SECRET) {
    return res.status(401).send("Unauthorized");
  }

  const update = req.body;

  // 尽快告诉 Telegram 已收到，避免超时重试
  res.sendStatus(200);

  if (processedUpdates.has(update.update_id)) return;
  processedUpdates.add(update.update_id);

  // 控制内存占用；正式环境应使用 Redis
  if (processedUpdates.size > 5000) {
    processedUpdates.clear();
  }

  try {
    const message = update.business_message;

    if (!message?.text) return;
    if (!message.business_connection_id) return;

    // 忽略机器人或业务账号自己发出的消息，防止循环回复
    if (message.from?.is_bot || message.sender_business_bot) {
      return;
    }

    const reply = await generateReply(message.text);

    await sendBusinessMessage({
      businessConnectionId: message.business_connection_id,
      chatId: message.chat.id,
      text: reply,
      replyToMessageId: message.message_id
    });
  } catch (error) {
    console.error("处理消息失败：", error);
  }
});

app.listen(PORT, () => {
  console.log(`Service running on port ${PORT}`);
});