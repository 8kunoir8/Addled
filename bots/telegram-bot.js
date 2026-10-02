// Addled — Telegram Bot Bridge
// Forwards messages between Telegram and the Addled Python backend via grammY.

const { Bot } = require('grammy');
// './shared' — these files live in bots/, so '../shared' resolved to
// <root>/shared/ws-client and every bot died on require with MODULE_NOT_FOUND.
const { AddledWSClient } = require('./shared/ws-client');
const { toAttachment, describe } = require('./shared/media');

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

// A question the model asked, described for the chat.
//
// Shares its wording with the dashboard card ("Addled needs to know") so the
// same prompt reads the same wherever it lands, and so a user who has seen one
// recognises the other.
function questionPrompt(q) {
  const lines = ['❓ Addled needs to know', '', String(q?.question || '').slice(0, 900)];
  if (q?.context) lines.push('', `_${String(q.context).slice(0, 200)}_`);
  if (q?.ttl) lines.push('', `Waiting for ${q.ttl}. It carries on when you answer.`);
  return lines.join('\n');
}

// Buttons for a question's choices, or null when it is open-ended.
//
// `callback_data` is capped at 64 bytes, so the option *index* travels rather
// than its text — an option like "the production database" would not fit, and a
// truncated label would ask the user to choose between two identical stubs. The
// bridge already holds the wording from the same reply, so the index is enough
// to recover it.
function questionKeyboard(q) {
  const options = Array.isArray(q?.options) ? q.options.filter((o) => String(o).trim()) : [];
  if (!q?.question_id || options.length === 0) return null;
  const id = String(q.question_id).slice(0, 40);
  const rows = [];
  options.slice(0, 6).forEach((label, index) => {
    rows.push([{
      text: String(label).slice(0, 60),
      callback_data: `qa:${id}:${index}`,
    }]);
  });
  rows.push([{ text: '⏭️ Skip', callback_data: `qs:${id}` }]);
  return { inline_keyboard: rows };
}

// Which question this chat was last shown buttons for, and what they said.
//
// An open-ended question has no buttons, so the answer arrives as ordinary
// text — and without this the bot could not tell "bluefalcon" from a new
// instruction. Keyed by chat id, because each chat has its own conversation.
const openQuestions = new Map();

function rememberQuestion(chatId, q) {
  if (!q?.question_id) return;
  openQuestions.set(String(chatId), {
    question_id: q.question_id,
    question: q.question || '',
    options: Array.isArray(q.options) ? q.options : [],
  });
}

function takeQuestion(chatId) {
  const key = String(chatId);
  const held = openQuestions.get(key) || null;
  openQuestions.delete(key);
  return held;
}

// Answering resumes the work the question was blocking, exactly as the
// dashboard does: settle it, then send the answer as the next turn.
//
// The `chat.send` belongs HERE and not in the caller. Sending only
// `question.answer` settles the question without the model ever learning the
// answer — it asked, the user answered, and the work stayed stopped. Keeping
// both calls in one function is what makes that impossible to half-do.
async function answerQuestion(ws, chatId, question, answer, reply) {
  const settled = await ws.send('question.answer',
    { question_id: question.question_id, answer,
      source: 'telegram', conversation: String(chatId) });
  if (!settled?.success) return settled;
  const asked = question.question ? `"${question.question}"` : 'a question';
  const resumed = await ws.send('chat.send', {
    message:
      `Answering your question ${asked}: ${answer}\n\n` +
      `This is my answer to the question you asked. Continue the task you were ` +
      `working on when you asked it, using this.`,
    source: 'telegram',
    // The same conversation the question was raised in, so the resumed turn
    // continues that thread rather than starting a new one.
    conversation: String(chatId),
  });
  const text = resumed?.response || 'No response';
  if (typeof reply === 'function') await reply(text);
  // `settled` is what callers branch on, and success means the QUESTION was
  // answered — the resume is a follow-up whose failure must not be reported as
  // a refused answer, or the question would be re-armed with a dead id.
  return settled;
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

    // An open question with no choices takes its answer as ordinary text, so
    // this is checked BEFORE the turn is sent: otherwise "bluefalcon" would be
    // handled as a fresh instruction and the question would sit unanswered
    // while the model started something unrelated.
    const open = openQuestions.get(String(ctx.chat.id));
    if (open) {
      takeQuestion(ctx.chat.id);
      const sentAnswer = await ctx.reply('✍️ Sending your answer...');
      try {
        const settled = await answerQuestion(ws, ctx.chat.id, open, text,
          (msg) => ctx.reply(msg));
        if (!settled?.success) {
          // The answer was refused — expired, or another surface got there
          // first. It must NOT be dropped: the message the user typed is a
          // turn they meant to send, so it is forwarded as one rather than
          // swallowed. Silently doing nothing is the worst outcome, because
          // nothing on screen says anything went wrong.
          await ctx.api.editMessageText(sentAnswer.chat.id,
            sentAnswer.message_id,
            `⚠️ ${settled?.error || 'That question is no longer open.'}\n` +
            'Sending your message as a normal request instead…').catch(() => {});
          await ctx.reply(text);
          return;
        }
        await ctx.api.deleteMessage(sentAnswer.chat.id, sentAnswer.message_id)
          .catch(() => {});
      } catch (e) {
        // A thrown failure here is the WS call itself — the answer never
        // reached the backend — so the question is still queued and putting it
        // back lets the user just type again. A failure of the RESUME would not
        // reach this block: `answerQuestion` returns rather than throwing once
        // the question is settled, precisely so a dead id is never re-armed.
        rememberQuestion(ctx.chat.id, open);
        await ctx.api.editMessageText(sentAnswer.chat.id, sentAnswer.message_id,
          `❌ Could not send that: ${e.message}`).catch(() => {});
      }
      return;
    }

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
      const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
      // A question is shown as its own message below rather than as the
      // keyboard on the reply: the reply already says the same thing ("Which
      // file should I edit?"), and hanging buttons off it would put the choices
      // under a paragraph rather than under the question.
      const keyboard = questions.length ? null : approvalKeyboard(pending);
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
      // A question, with its own buttons when it offered choices. Only the
      // first: two cards in one chat is a way to answer the wrong one, and the
      // rest are recoverable — the dashboard lists them, and the next turn
      // reports them again.
      if (questions.length) {
        const q = questions[0];
        rememberQuestion(sent.chat.id, q);
        await ctx.reply(questionPrompt(q), questionKeyboard(q) || {});
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
    const [verb, refA, refB] = data.split(':');
    const chatId = String(ctx.callbackQuery.message?.chat?.id ?? '');
    // Only the chat the prompt was raised in may answer it, even though the
    // button could in principle be forwarded. Checked for questions as well as
    // approvals: an answer is consent to act on what that chat asked.
    if (chatId !== String(ctx.chat?.id ?? '')) {
      await ctx.answerCallbackQuery({ text: 'This is not the chat that asked.' });
      return;
    }
    // A tap on a question's choice, or its Skip. Handled before the approval
    // verbs below, which would otherwise reject these as unrecognised.
    if (verb === 'qa' || verb === 'qs') {
      const questionId = refA;
      const held = openQuestions.get(chatId);
      // The wording lives in this bridge's own memory of what it sent, not in
      // the button: `callback_data` is capped at 64 bytes and an option like
      // "the production database" would not fit.
      if (!held || held.question_id !== questionId) {
        await ctx.answerCallbackQuery({
          text: 'That question is no longer open here.' });
        await ctx.editMessageReplyMarkup({}).catch(() => {});
        return;
      }
      const label = verb === 'qs'
        ? null
        : (held.options[Number(refB)] ?? '');
      if (verb === 'qa' && !label) {
        await ctx.answerCallbackQuery({ text: 'That choice is not available.' });
        return;
      }
      try {
        if (verb === 'qs') {
          const r = await ws.send('question.dismiss',
            { question_id: questionId, source: 'telegram',
              conversation: String(chatId) });
          takeQuestion(chatId);
          await ctx.editMessageText(r?.success
            ? '⏭️ Skipped.'
            : `⚠️ ${r?.error || 'It is no longer open.'}`).catch(() => {});
        } else {
          await ctx.editMessageText(`✍️ ${label}`).catch(() => {});
          const settled = await answerQuestion(ws, chatId, held, label,
            (msg) => ctx.reply(msg));
          if (!settled?.success) {
            // Only claim success when the backend said so. Reporting
            // "✅ Answered" for a refused answer is the worst kind of wrong:
            // the user believes the work resumed and it did not. The question
            // is put back so the tap can be retried.
            rememberQuestion(chatId, held);
            await ctx.editMessageText(
              `⚠️ ${settled?.error || 'That question is no longer open.'}`)
              .catch(() => {});
          } else {
            takeQuestion(chatId);
            await ctx.editMessageText(`✅ Answered: ${label}`).catch(() => {});
          }
        }
        await ctx.answerCallbackQuery();
      } catch (e) {
        await ctx.answerCallbackQuery({ text: `Failed: ${e.message}` });
      }
      return;
    }

    const approvalId = refA;
    if (!approvalId || !['ap', 'al', 'dn'].includes(verb)) {
      await ctx.answerCallbackQuery({ text: 'Unrecognised button.' });
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

  /**
   * A photo or voice note, forwarded to Addled as an attachment.
   *
   * Both kinds take the same route, so this is one handler rather than two that
   * would drift: fetch the bytes from Telegram, hand them to the shared media
   * helper to decide what they are and enforce the size cap, then send them to
   * the backend through the same `chat.send` the text path uses.
   *
   * The backend does the reading — provider vision or local Florence-2 for an
   * image, local whisper for audio — and injects the result as context, so the
   * model answers about what is IN the picture rather than being told a file
   * exists.
   */
  async function handleMedia(ctx, fileId, fallbackName, mime) {
    const place = await ctx.reply('📥 Downloading…');
    try {
      const file = await ctx.api.getFile(fileId);
      const url = `https://api.telegram.org/file/bot${TELEGRAM_TOKEN}/${file.file_path}`;
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Telegram returned ${res.status}`);
      const buffer = Buffer.from(await res.arrayBuffer());

      const name = file.file_path
        ? file.file_path.split('/').pop()
        : fallbackName;
      const built = toAttachment(buffer, name, mime);
      if (built.error) {
        await ctx.api.editMessageText(ctx.chat.id, place.message_id,
          `⚠️ ${built.error}`);
        return;
      }

      const r = await ws.send('chat.send', {
        message: ctx.message.caption
          || (built.kind === 'audio' ? 'What did I say in this voice note?'
                                     : 'What is in this image?'),
        source: 'telegram',
        conversation: String(ctx.chat.id),
        attachments: [built.attachment],
      });
      const response = r?.response || 'No response';
      await ctx.api.deleteMessage(ctx.chat.id, place.message_id).catch(() => {});
      if (response.length <= 4000) {
        await ctx.reply(response);
      } else {
        for (let i = 0; i < response.length; i += 4000) {
          await ctx.reply(response.slice(i, i + 4000));
        }
      }
      // A photo or a voice note can raise a decision like any other turn — a
      // gated tool, or a question about what is in the picture. This path used
      // to read only `response`, so the user who sent the media was never shown
      // the prompt it produced and it expired unanswered. The text path
      // surfaces both; this is the same treatment, not a second mechanism.
      const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
      const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
      if (pending[0]?.approval_id) {
        await ctx.reply(approvalPrompt(pending[0]),
          { reply_markup: approvalKeyboard(pending) || {} });
      }
      if (questions[0]?.question_id) {
        const q = questions[0];
        rememberQuestion(ctx.chat.id, q);
        await ctx.reply(questionPrompt(q), questionKeyboard(q) || {});
      }
      console.log(`[Telegram] handled ${describe(built.attachment)}`);
    } catch (e) {
      // The placeholder is reused for the failure so the chat does not collect
      // two messages for one attempt.
      await ctx.api.editMessageText(ctx.chat.id, place.message_id,
        `❌ Could not handle that: ${e.message}`).catch(() => {});
    }
  }

  // A photo. Telegram sends several sizes; the LARGEST is taken deliberately —
  // the default middle one blurs text in a screenshot, which is usually the
  // whole point of sending it.
  bot.on('message:photo', async (ctx) => {
    const sizes = ctx.message.photo || [];
    const largest = sizes[sizes.length - 1];
    if (!largest) return;
    await handleMedia(ctx, largest.file_id, 'photo.jpg', 'image/jpeg');
  });

  // A voice note, and also an audio file or a video note — all three carry
  // speech, and the local whisper model reads any of them.
  bot.on('message:voice', async (ctx) => {
    await handleMedia(ctx, ctx.message.voice.file_id, 'voice.ogg', 'audio/ogg');
  });

  bot.on('message:audio', async (ctx) => {
    const a = ctx.message.audio;
    await handleMedia(ctx, a.file_id, a.file_name || 'audio.mp3',
                      a.mime_type || 'audio/mpeg');
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
