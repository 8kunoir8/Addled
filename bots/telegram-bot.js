// Addled — Telegram Bot Bridge
// Forwards messages between Telegram and the Addled Python backend via grammY.

const { Bot } = require('grammy');
// './shared' — these files live in bots/, so '../shared' resolved to
// <root>/shared/ws-client and every bot died on require with MODULE_NOT_FOUND.
const { AddledWSClient } = require('./shared/ws-client');

const TELEGRAM_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';
const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const ALLOWED_USERS = (process.env.TELEGRAM_ALLOWED_USERS || '').split(',').map(Number).filter(Boolean);

// How a pending approval is described in the chat. Mirrors the wording the
// dashboard card uses, so the same question reads the same in both places.
function approvalPrompt(req) {
  const what = req?.kind === 'tool' ? 'tool' : req?.kind === 'skill' ? 'skill' : 'action';
  const lines = [`🔐 Permission needed`,
                 `The ${what} "${req?.name || req?.action_type || 'action'}" needs your approval.`];
  if (req?.command) lines.push('', `\`${String(req.command).slice(0, 300)}\``);
  if (!req?.grantable) {
    lines.push('', 'This one always asks first, so it cannot be remembered.');
  }
  return lines.join('\n');
}

// One row of buttons for the first pending request. A second request is left
// for the next turn: two keyboards in one chat is a way to approve the wrong
// thing.
function approvalKeyboard(pending) {
  const first = Array.isArray(pending) ? pending[0] : null;
  if (!first?.approval_id) return null;
  // `callback_data` is capped at 64 bytes, so only the id and kind travel —
  // both short — and the name is recovered from the reply the bot already
  // holds. `appr_N` keeps this comfortably inside the limit.
  const id = String(first.approval_id).slice(0, 30);
  const kind = first.kind === 'tool' ? 'tool' : 'skill';
  const rows = [[{ text: '✅ Allow once', callback_data: `ap:${id}` }]];
  if (first.grantable) {
    rows[0].push({ text: '♾️ Always allow', callback_data: `al:${kind}:${id}` });
  }
  rows.push([{ text: '⛔ Deny', callback_data: `dn:${id}` }]);
  return { inline_keyboard: rows };
}

async function main() {
  if (!TELEGRAM_TOKEN) {
    console.error('[Telegram] TELEGRAM_BOT_TOKEN environment variable is required.');
    console.error('[Telegram] Create a bot at @BotFather and set the token.');
    process.exit(1);
  }

  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  try {
    await ws.connect();
  } catch (e) {
    console.error('[Telegram] Failed to connect to Addled backend:', e.message);
    console.error('[Telegram] Make sure the Python backend is running (python backend/main.py)');
  }

  // Create bot
  const bot = new Bot(TELEGRAM_TOKEN);

  // Auth middleware — restrict to allowed users if configured
  bot.use(async (ctx, next) => {
    const userId = ctx.from?.id;
    if (ALLOWED_USERS.length > 0 && userId && !ALLOWED_USERS.includes(userId)) {
      await ctx.reply('⛔ You are not authorized to use this bot.');
      return;
    }
    await next();
  });

  // /start command
  bot.command('start', async (ctx) => {
    await ctx.reply(
      '👋 Hello! I\'m **Addled** — your AI desktop companion.\n\n' +
      '• Send me a message to chat\n' +
      '• /goal <description> — Create a background goal\n' +
      '• /status — Check Addled status\n' +
      '• /screenshot — Request a screen capture\n' +
      '• /sleep — Put Addled to sleep\n' +
      '• /wake — Wake Addled up\n\n' +
      'I\'m connected to your Addled desktop agent.',
      { parse_mode: 'Markdown' }
    );
  });

  // /goal command
  bot.command('goal', async (ctx) => {
    const text = ctx.match;
    if (!text || !text.trim()) {
      return ctx.reply('Usage: /goal <description>\nExample: /goal organize my downloads folder');
    }
    await ctx.reply('🎯 Creating goal...');
    try {
      const r = await ws.send('goal.create', { title: text.trim(), description: '', priority: 'normal' });
      await ctx.reply(`✅ Goal created!\n📋 *${text.trim()}*\n🆔 \`${r?.goalId || 'unknown'}\``, { parse_mode: 'Markdown' });
    } catch (e) {
      await ctx.reply(`❌ Failed to create goal: ${e.message}`);
    }
  });

  // /status command
  bot.command('status', async (ctx) => {
    try {
      const r = await ws.send('system.status', {});
      await ctx.reply(
        `🟢 **Addled Status**\n` +
        `• Engine: ${r?.engineState || 'unknown'}\n` +
        `• Provider: ${r?.provider || 'none'}\n` +
        `• Character: ${r?.characterState || 'unknown'}\n` +
        `• Agent: ${r?.agentName || 'Addled'}`,
        { parse_mode: 'Markdown' }
      );
    } catch (e) {
      await ctx.reply(`⚠️ Addled is not reachable: ${e.message}`);
    }
  });

  // /screenshot command
  bot.command('screenshot', async (ctx) => {
    await ctx.reply('📸 Requesting screenshot...');
    try {
      const r = await ws.send('browser.screenshot', {});
      if (r?.screenshotUrl) {
        await ctx.replyWithPhoto(r.screenshotUrl, { caption: '🖥️ Current screen' });
      } else {
        await ctx.reply('⚠️ Screenshot not available. Browser control may not be configured.');
      }
    } catch (e) {
      await ctx.reply(`❌ Screenshot failed: ${e.message}`);
    }
  });

  // /sleep command
  bot.command('sleep', async (ctx) => {
    try {
      await ws.send('character.setState', { state: 'sleeping' });
      await ctx.reply('😴 Addled is now sleeping.');
    } catch (e) {
      await ctx.reply(`❌ Failed: ${e.message}`);
    }
  });

  // /wake command
  bot.command('wake', async (ctx) => {
    try {
      await ws.send('character.setState', { state: 'idle' });
      await ctx.reply('👋 Addled is awake!');
    } catch (e) {
      await ctx.reply(`❌ Failed: ${e.message}`);
    }
  });

  // Handle text messages → forward to Addled
  bot.on('message:text', async (ctx) => {
    const text = ctx.message.text;
    if (text.startsWith('/')) return; // Commands handled above

    const sent = await ctx.reply('🤔 Thinking...');
    // A reply can take minutes on a local model, so show that it is still
    // working rather than a frozen "Thinking...".
    let elapsed = 0;
    const ticker = setInterval(() => {
      elapsed += 15;
      ctx.api.editMessageText(sent.chat.id, sent.message_id,
        `🤔 Thinking... (${elapsed}s)`).catch(() => {});
    }, 15000);
    try {
      // `source` and `conversation` are what let this bridge both label the
      // turn on the chat page and answer an approval the *user* asked for
      // here. Without the conversation the answer could not be tied to this
      // chat, and a "yes" typed in one Telegram chat could release a command
      // another chat asked for.
      const r = await ws.send('chat.send', {
        message: text,
        source: 'telegram',
        conversation: String(sent.chat.id),
      });
      const response = r?.response || 'No response';
      clearInterval(ticker);
      const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
      const keyboard = approvalKeyboard(pending);
      // Split long messages for Telegram's 4096 char limit
      if (response.length <= 4000) {
        await ctx.api.editMessageText(sent.chat.id, sent.message_id, response,
          keyboard ? { reply_markup: keyboard } : {});
      } else {
        await ctx.api.deleteMessage(sent.chat.id, sent.message_id);
        for (let i = 0; i < response.length; i += 4000) {
          await ctx.reply(response.slice(i, i + 4000));
        }
      }
      // The keyboard goes with the question, so the words and the buttons are
      // in one place the user can read together.
      if (keyboard) {
        await ctx.reply(approvalPrompt(pending[0]), { reply_markup: keyboard });
      }
    } catch (e) {
      clearInterval(ticker);
      await ctx.api.editMessageText(sent.chat.id, sent.message_id, `❌ Error: ${e.message}`);
    }
  });

  // A tap on Allow / Always / Deny. The answer goes over the same socket this
  // bridge already holds, so it works with the dashboard closed — and the
  // request it releases is the one *this chat* was shown the buttons for.
  bot.on('callback_query:data', async (ctx) => {
    const data = String(ctx.callbackQuery.data || '');
    const [verb, approvalId] = data.split(':');
    const chatId = String(ctx.callbackQuery.message?.chat?.id ?? '');
    if (!approvalId || !['ap', 'al', 'dn'].includes(verb)) {
      await ctx.answerCallbackQuery({ text: 'Unrecognised button.' });
      return;
    }
    // Only the chat the request was raised in may answer it, even though the
    // button could in principle be forwarded.
    if (chatId !== String(ctx.chat?.id ?? '')) {
      await ctx.answerCallbackQuery({ text: 'This is not the chat that asked.' });
      return;
    }
    try {
      let outcome;
      if (verb === 'al') {
        // The name is not in the button — it would not fit in 64 bytes — so
        // the backend resolves it from its own record of this approval. That
        // also means the name cannot be tampered with from the client.
        const r = await ws.send('approvals.alwaysAllow', { approvalId });
        outcome = r?.success
          ? '♾️ Always allowed — it will not ask again.'
          : `⚠️ ${r?.error || 'Could not save that.'}`;
      } else if (verb === 'ap') {
        const r = await ws.send('action.approve', { approvalId });
        outcome = r?.success
          ? '✅ Allowed — running it now.'
          : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
      } else {
        const r = await ws.send('action.deny', { approvalId });
        outcome = r?.success
          ? '⛔ Denied — it was not run.'
          : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
      }
      // Replace the buttons with the outcome, so the same decision cannot be
      // sent twice by an impatient second tap.
      await ctx.editMessageText(outcome).catch(() => {});
      await ctx.answerCallbackQuery();
    } catch (e) {
      await ctx.answerCallbackQuery({ text: `Failed: ${e.message}` });
    }
  });

  // Handle photo messages → forward for vision analysis
  bot.on('message:photo', async (ctx) => {
    await ctx.reply('🖼️ Image received. Vision analysis coming in Phase 3.');
  });

  // Handle voice messages
  bot.on('message:voice', async (ctx) => {
    await ctx.reply('🎤 Voice message received. STT coming in Phase 3.');
  });

  // Error handler
  bot.catch((err) => {
    console.error('[Telegram] Bot error:', err.message);
  });

  // Start bot
  console.log('[Telegram] Starting bot...');
  await bot.start({
    onStart: (botInfo) => {
      console.log(`[Telegram] Bot @${botInfo.username} is running`);
    },
  });
}

main().catch((err) => {
  console.error('[Telegram] Fatal error:', err);
  process.exit(1);
});
