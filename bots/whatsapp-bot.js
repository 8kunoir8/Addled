// Addled — WhatsApp Bot Bridge
// Forwards messages between WhatsApp and the Addled Python backend via Baileys.

const { makeWASocket, useMultiFileAuthState, DisconnectReason, downloadMediaMessage } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const { AddledWSClient } = require('./shared/ws-client');
const { toAttachment, describe } = require('./shared/media');
const path = require('path');
const fs = require('fs');

const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth', 'whatsapp');
// Where the last chat that messaged us is remembered, so a proactive
// notification (a scheduled reminder) has somewhere to go. Beside the auth
// folder, so it survives a restart and is not shipped with the app.
const LAST_CHAT_FILE = path.join(AUTH_DIR, 'last-chat.txt');

// WhatsApp has no interactive buttons — Baileys cannot send the tappable
// replies Telegram and Discord have. So the answer is a *word*: the request is
// sent as text, and the next message in that chat is read as the decision.
//
// Only these exact words count. "yes but also delete the other one" must not
// be mistaken for consent, and anything unrecognised falls through to being a
// normal message to Addled.
const YES = new Set(['yes', 'y', 'allow', 'ok', 'okay', 'sure', 'approve']);
const ALWAYS = new Set(['always', 'always allow', 'always-allow', 'allow always']);
const NO = new Set(['no', 'n', 'deny', 'denied', 'cancel', 'stop', 'reject']);

// What a chat was last asked to decide, so the reply finds the request it
// belongs to. Keyed by chat, because the answer belongs to the conversation
// that asked — never to whichever request happens to be first in the queue.
const waiting = new Map();

function classifyReply(text) {
  const clean = String(text || '').trim().toLowerCase().replace(/[.!]+$/, '');
  if (ALWAYS.has(clean)) return 'always';
  if (YES.has(clean)) return 'allow';
  if (NO.has(clean)) return 'deny';
  return null;
}

function approvalPrompt(req) {
  const what = req?.kind === 'tool' ? 'tool' : req?.kind === 'skill' ? 'skill' : 'action';
  const lines = ['🔐 Permission needed',
                 `The ${what} "${req?.name || req?.action_type || 'action'}" needs your approval.`];
  if (req?.command) lines.push('', String(req.command).slice(0, 300));
  const answers = req?.grantable
    ? 'Reply "yes" to allow once, "always" to stop it asking, or "no" to deny.'
    : 'Reply "yes" to allow once, or "no" to deny.';
  lines.push('', answers);
  return lines.join('\n');
}

// A question the model asked. Without buttons the choices have to be numbered,
// and the number is the answer — see `classifyQuestionReply`.
function questionPrompt(q) {
  const lines = ['❓ Addled needs to know', '', String(q?.question || '').slice(0, 900)];
  if (q?.context) lines.push('', String(q.context).slice(0, 200));
  const options = Array.isArray(q?.options) ? q.options.filter((o) => String(o).trim()) : [];
  if (options.length) {
    lines.push('');
    options.slice(0, 6).forEach((label, i) => lines.push(`${i + 1}. ${label}`));
    lines.push('', 'Reply with the number, or type your own answer.');
  } else {
    lines.push('', 'Reply with your answer.');
  }
  if (q?.ttl) lines.push('', `Waiting for ${q.ttl}. It carries on when you answer.`);
  return lines.join('\n');
}

// Which question this chat was last shown, and what its choices were.
//
// Separate from `waiting`, which holds approvals: an approval is answered with
// one of three fixed words while a question is answered with a number or with
// free text, and sharing one map meant a "yes" could be read as an answer to a
// question that was never about yes or no.
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

// Turn a reply into an answer: the number of a choice, or the text itself.
//
// A bare number is only read as a choice when the question actually offered
// that many — otherwise "2" in reply to an open question would be swallowed as
// an option that does not exist. Anything else is the answer verbatim, which is
// what makes an open-ended question answerable on a platform with no buttons.
function classifyQuestionReply(held, text) {
  const clean = String(text || '').trim();
  if (!clean) return { answer: '', ok: false };
  const options = (held?.options || []).filter((o) => String(o).trim());
  const asNumber = /^(\d{1,2})$/.exec(clean);
  if (asNumber && options.length) {
    const index = Number(asNumber[1]) - 1;
    if (index >= 0 && index < options.length) {
      return { answer: options[index], ok: true, chosen: true };
    }
    // A number too large to be a choice is left as text rather than refused:
    // it may be the answer itself ("how many retries?" -> "3").
  }
  return { answer: clean, ok: true, chosen: false };
}

// Answering resumes the work the question was blocking, exactly as the
// dashboard does: settle it, then send the answer as the next turn.
//
// The `chat.send` belongs HERE and not in the caller. Sending only
// `question.answer` settles the question without the model ever learning the
// answer — it asked, the user answered, and the work stayed stopped. Keeping
// both calls in one function is what makes that impossible to half-do.
async function answerQuestion(ws, chatId, held, answer, send) {
  const settled = await ws.send('question.answer',
    { question_id: held.question_id, answer,
      source: 'whatsapp', conversation: String(chatId) });
  if (!settled?.success) {
    // Retry only when retrying could work.
    //
    // `expired` is terminal, and so is an origin refusal — the backend is
    // saying the id is gone for good, or that this chat is not the one it
    // belongs to. Re-remembering in either case made the channel UNUSABLE:
    // every later message was read as an answer to a question that can never be
    // accepted, consumed, refused, and re-remembered, so the user could only
    // type into a void. Guarding on `expired` alone missed the refusal the
    // origin check introduced.
    //
    // A transient failure (a dropped socket) is different: the question is
    // still queued, so putting it back lets the user simply type again.
    const terminal = settled?.expired
      || /different conversation|must say which conversation/i
        .test(String(settled?.error || ''));
    if (!terminal) {
      rememberQuestion(chatId, held);
    }
    return settled;
  }
  const asked = held.question ? `"${held.question}"` : 'a question';
  const resumed = await ws.send('chat.send', {
    message:
      `Answering your question ${asked}: ${answer}\n\n` +
      `This is my answer to the question you asked. Continue the task you were ` +
      `working on when you asked it, using this.`,
    source: 'whatsapp',
    conversation: String(chatId),
  });
  const text = resumed?.response || 'No response';
  const max = 4000;
  for (let i = 0; i < text.length; i += max) {
    await send(text.slice(i, i + max));
  }
  // `settled` is what callers branch on, and success means the QUESTION was
  // answered — a failure of the resume must not be reported as a refused
  // answer, or the question would be re-armed with an id the backend has
  // already closed.
  return settled;
}

async function main() {
  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  try {
    await ws.connect();
    console.log('[WhatsApp] Connected to Addled backend');
  } catch (e) {
    console.error('[WhatsApp] Backend not reachable:', e.message);
  }

  // Ensure auth directory exists
  if (!fs.existsSync(AUTH_DIR)) {
    fs.mkdirSync(AUTH_DIR, { recursive: true });
  }

  const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

  const sock = makeWASocket({
    auth: state,
    // No `printQRInTerminal`. Baileys removed it in 6.x and says so at
    // runtime: "You will no longer receive QR codes in the terminal
    // automatically. Please listen to the connection.update event yourself."
    // Passing it produced a deprecation warning and NO QR AT ALL, which is why
    // pairing looked broken. The `qr` from `connection.update` below is the
    // supported path, and it is what we emit.
    browser: ['Addled Desktop', 'Chrome', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  // ---- proactive delivery -------------------------------------------------
  //
  // Everything above this line only ever speaks when spoken to. The scheduler
  // fires reminders and broadcasts `bot.notify`, and until now nothing listened
  // — the broadcast went out and was dropped, so a scheduled reminder reached
  // the dashboard, the character bubble and TTS but never the phone.
  //
  // The chat to deliver to comes from config, because a notification is not a
  // reply: there is no incoming message to take the sender from. Two sources,
  // in order:
  //   1. WHATSAPP_NOTIFY_TO — an explicit destination, set in Settings.
  //   2. the last chat that messaged the bot, remembered in lastChatFile.
  // The fallback is a convenience for a single-user setup: you message the bot
  // once to introduce yourself, and reminders have somewhere to go. A quiet
  // reminder with nowhere to send is logged rather than thrown, so the fault is
  // visible instead of silent.
  const NOTIFY_TO = (process.env.WHATSAPP_NOTIFY_TO || '').trim();

  function rememberChat(jid) {
    try {
      if (!jid) return;
      fs.writeFileSync(LAST_CHAT_FILE, String(jid), 'utf8');
    } catch (e) {
      console.error('[WhatsApp] could not remember the chat:', e.message);
    }
  }

  function lastChat() {
    try {
      if (fs.existsSync(LAST_CHAT_FILE)) {
        return fs.readFileSync(LAST_CHAT_FILE, 'utf8').trim();
      }
    } catch (e) {
      console.error('[WhatsApp] could not read the last chat:', e.message);
    }
    return '';
  }

  async function deliver(text) {
    const body = String(text || '').trim();
    if (!body) return;
    const to = NOTIFY_TO || lastChat();
    if (!to) {
      console.log('[WhatsApp] A notification had nowhere to go — message the '
                  + 'bot once, or set a destination in Settings.');
      return;
    }
    try {
      await sock.sendMessage(to, { text: body });
      console.log(`[WhatsApp] Delivered a notification to ${to}`);
    } catch (e) {
      console.error('[WhatsApp] Could not deliver the notification:', e.message);
    }
  }

  ws.onNotification('bot.notify', (params) => {
    // The broadcast reaches every connected bot, so each one decides whether it
    // is a named destination. An empty list means "no preference" — the case a
    // single-bot setup is in, and the only way the feature works before anyone
    // has been to Settings.
    const wanted = Array.isArray(params?.platforms) ? params.platforms : [];
    if (wanted.length && !wanted.includes('whatsapp')) return;
    // Fire-and-forget: this runs on the socket's message handler, and awaiting
    // here would stall every other notification behind a slow send.
    deliver(params?.text).catch((e) =>
      console.error('[WhatsApp] notify failed:', e.message));
  });

  // ---- outbound send ------------------------------------------------------
  //
  // `bots.send` is the backend asking this process to send a message it did not
  // receive. Two things make this different from the reply path:
  //
  //   * the backend cannot call `sock.sendMessage` itself — the socket lives in
  //     THIS process, and the WebSocket is the only bridge between them;
  //   * `to` is a real phone number supplied by the caller, so it is validated
  //     rather than trusted. A number that is not a WhatsApp JID is refused with
  //     a message naming the expected shape, because a malformed JID fails
  //     inside Baileys with an error that does not say what was wrong.
  function normaliseJid(raw) {
    const text = String(raw || '').trim();
    if (!text) return '';
    // Already a JID.
    if (/@(s\.whatsapp\.net|g\.us)$/.test(text)) return text;
    // A bare number, with or without punctuation, in international form.
    const digits = text.replace(/[^\d]/g, '');
    if (digits.length < 8 || digits.length > 15) return '';
    return `${digits}@s.whatsapp.net`;
  }

  ws.onNotification('bots.send', async (params) => {
    const requestId = params?.requestId;
    const to = normaliseJid(params?.to);
    const text = String(params?.text || '').trim();
    const reply = (payload) => {
      // The reply travels back as a notification because the backend sent this
      // as a notification: it does not hold a request id to resolve, and
      // blocking its handler on a round trip would tie it to this process.
      if (!requestId) return;
      ws.send('bots.sendResult', { requestId, ...payload }).catch(() => {});
    };
    if (!to) {
      reply({ success: false, error:
        'Give a phone number in international form, e.g. 6281234567890.' });
      return;
    }
    if (!text) {
      reply({ success: false, error: 'The message text is empty.' });
      return;
    }
    try {
      await sock.sendMessage(to, { text });
      console.log(`[WhatsApp] Sent to ${to}`);
      reply({ success: true, platform: 'whatsapp', to, length: text.length });
    } catch (e) {
      console.error('[WhatsApp] Send failed:', e.message);
      reply({ success: false, error: e.message });
    }
  });

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      // Emitted on its own line with a marker the dashboard looks for. The
      // dashboard renders it as a QR image; the raw string is long, so nothing
      // else should try to read it as a log line.
      console.log('[WhatsApp] Scan this QR code with WhatsApp (Linked Devices).');
      console.log(`QR_DATA:${qr}`);
    }
    if (connection === 'close') {
      const reconnect = (lastDisconnect?.error instanceof Boom) &&
        lastDisconnect.error.output.statusCode !== DisconnectReason.loggedOut;
      console.log('[WhatsApp] Disconnected. Reconnecting:', reconnect);
      if (reconnect) setTimeout(main, 3000);
      else { console.log('[WhatsApp] Logged out. Delete auth folder.'); process.exit(1); }
    } else if (connection === 'open') {
      console.log('[WhatsApp] Connected to WhatsApp');
    }
  });

  sock.ev.on('messages.upsert', async (m) => {
    const msg = m.messages[0];
    if (!msg.message || msg.key.fromMe) return;
    const sender = msg.key.remoteJid;
    const type = Object.keys(msg.message)[0];
    const content = msg.message[type];
    if (sender === 'status@broadcast') return;
    const isGroup = sender.endsWith('@g.us');
    // Remembered so a scheduled reminder has a destination even when nothing
    // is pinned in Settings — see `deliver`.
    rememberChat(sender);

    try {
      if (type === 'conversation' || type === 'extendedTextMessage') {
        const text = type === 'conversation' ? content : content?.text || '';
        if (!text) return;
        if (isGroup) {
          const mentioned = content?.contextInfo?.mentionedJid || [];
          const botId = sock.user?.id?.split(':')[0] + '@s.whatsapp.net';
          if (!mentioned.includes(botId)) return;
        }
        await sock.sendPresenceUpdate('composing', sender);
        // Two different decisions can be open at once, and they do not share an
        // answer vocabulary: a question is settled by a number or by free text,
        // an approval by one of three fixed words.
        //
        // The ORDER is therefore load-bearing, and it is the approval that is
        // checked first *when the reply is one of its words*. A question accepts
        // any text, so checking it first swallowed the "yes" a pending approval
        // was waiting for — the approval was left hanging and the question was
        // answered with the word "yes". A number ("1") cannot be an approval
        // answer, so a choice still reaches the question.
        const openQ = openQuestions.get(sender);
        const approvalWord = classifyReply(text);
        const pendingApproval = waiting.get(sender);
        if (openQ && !(pendingApproval && approvalWord)) {
          const parsed = classifyQuestionReply(openQ, text);
          if (parsed.ok) {
            takeQuestion(sender);
            try {
              const settled = await answerQuestion(ws, sender, openQ, parsed.answer,
                (m) => sock.sendMessage(sender, { text: m }, { quoted: msg }));
              if (!settled?.success) {
                // Refused. The text must not be dropped — forward it as an
                // ordinary request so the user is not typing into a void.
                await sock.sendMessage(sender,
                  { text: `⚠️ ${settled?.error || 'That question is no longer open.'}` },
                  { quoted: msg });
              }
            } catch (e) {
              await sock.sendMessage(sender,
                { text: '⚠️ Failed: ' + e.message }, { quoted: msg });
            }
            return;
          }
        }
        // A decision on something this chat was asked about takes priority
        // over starting a new turn — otherwise the word "yes" becomes a fresh
        // question to the model and the request it was answering is abandoned.
        const held = waiting.get(sender);
        if (held) {
          const verdict = classifyReply(text);
          if (verdict) {
            waiting.delete(sender);
            try {
              let outcome;
              if (verdict === 'always') {
                // The name is not sent; the backend resolves it from its own
                // record of this approval, so it cannot be altered in transit.
                const r = await ws.send('approvals.alwaysAllow',
                  { approvalId: held.approval_id });
                outcome = r?.success
                  ? '♾️ Always allowed — it will not ask again.'
                  : `⚠️ ${r?.error || 'Could not save that.'}`;
              } else if (verdict === 'allow') {
                const r = await ws.send('action.approve',
                  { approvalId: held.approval_id });
                outcome = r?.success
                  ? '✅ Allowed — running it now.'
                  : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
              } else {
                const r = await ws.send('action.deny',
                  { approvalId: held.approval_id });
                outcome = r?.success
                  ? '⛔ Denied — it was not run.'
                  : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
              }
              await sock.sendMessage(sender, { text: outcome }, { quoted: msg });
            } catch (e) {
              await sock.sendMessage(sender,
                { text: '⚠️ Failed: ' + e.message }, { quoted: msg });
            }
            return;
          }
          // Anything else is a new question. Drop the held request so a stale
          // one cannot be answered by a word typed minutes later.
          waiting.delete(sender);
        }
        try {
          // `source` labels the turn on the chat page, and `conversation` is
          // what lets an approval be answered from this chat — by this word or
          // by another.
          const r = await ws.send('chat.send', {
            message: text, source: 'whatsapp', conversation: String(sender),
          });
          const response = r?.response || 'No response';
          const max = 4000;
          if (response.length <= max) {
            await sock.sendMessage(sender, { text: response }, { quoted: msg });
          } else {
            for (let i = 0; i < response.length; i += max)
              await sock.sendMessage(sender, { text: response.slice(i, i + max) });
          }
          const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
          const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
          if (pending[0]?.approval_id) {
            waiting.set(sender, pending[0]);
            await sock.sendMessage(sender,
              { text: approvalPrompt(pending[0]) }, { quoted: msg });
          }
          // A question is shown even when an approval is also waiting: they are
          // separate decisions and the user needs to see both. Only the first
          // is sent, for the same reason the other bridges send one card — two
          // prompts at once is how the wrong one gets answered.
          if (questions[0]?.question_id) {
            rememberQuestion(sender, questions[0]);
            await sock.sendMessage(sender,
              { text: questionPrompt(questions[0]) }, { quoted: msg });
          }
        } catch (e) {
          await sock.sendMessage(sender, { text: '\u2757 Addled: ' + e.message }, { quoted: msg });
        }
      }
      if (type === 'imageMessage' || type === 'audioMessage') {
        // A photo or a voice note. Same route as the text path above — one
        // `chat.send` carrying an attachment — so the backend does the reading
        // and the model answers about what is IN the media.
        //
        // `downloadMediaMessage` is Baileys' own fetcher: it handles the
        // encrypted-media round trip, which is why the bytes are not pulled
        // from `content` directly.
        const isAudio = type === 'audioMessage';
        const notice = await sock.sendMessage(sender,
          { text: isAudio ? '\uD83C\uDFA4 Transcribing\u2026'
                          : '\uD83D\uDDBC Looking at it\u2026' },
          { quoted: msg }).catch(() => null);
        try {
          const buffer = await downloadMediaMessage(msg, 'buffer', {});
          const mime = content?.mimetype || '';
          const fallback = isAudio ? 'voice.ogg' : 'image.jpg';
          const built = toAttachment(buffer, fallback, mime);
          if (built.error) {
            await sock.sendMessage(sender, { text: '\u26A0\uFE0F ' + built.error },
                                   { quoted: msg });
            return;
          }
          // A caption on the image is the user's actual question, so it is used
          // when present; otherwise a neutral prompt that makes the model
          // describe what it was given.
          const caption = String(content?.caption || '').trim();
          const r = await ws.send('chat.send', {
            message: caption
              || (isAudio ? 'What did I say in this voice note?'
                          : 'What is in this image?'),
            source: 'whatsapp',
            conversation: String(sender),
            attachments: [built.attachment],
          });
          const response = r?.response || 'No response';
          const max = 4000;
          if (response.length <= max) {
            await sock.sendMessage(sender, { text: response }, { quoted: msg });
          } else {
            for (let i = 0; i < response.length; i += max)
              await sock.sendMessage(sender, { text: response.slice(i, i + max) });
          }
          const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
          const questions = Array.isArray(r?.pendingQuestions) ? r.pendingQuestions : [];
          if (pending[0]?.approval_id) {
            // Same as the text path: a decision asked for here must be
            // answerable here.
            waiting.set(sender, pending[0]);
            await sock.sendMessage(sender,
              { text: approvalPrompt(pending[0]) }, { quoted: msg });
          }
          if (questions[0]?.question_id) {
            // A voice note can raise a question too ("did you mean the January
            // or the February report?"), and it has to be answerable here for
            // the same reason.
            rememberQuestion(sender, questions[0]);
            await sock.sendMessage(sender,
              { text: questionPrompt(questions[0]) }, { quoted: msg });
          }
          console.log(`[WhatsApp] handled ${describe(built.attachment)}`);
        } catch (e) {
          console.error('[WhatsApp] media failed:', e.message);
          await sock.sendMessage(sender,
            { text: '\u2757 Could not handle that: ' + e.message }, { quoted: msg });
        } finally {
          // The placeholder is transient; leaving it makes the chat read as if
          // nothing happened after it.
          if (notice?.key) {
            await sock.sendMessage(sender, { delete: notice.key }).catch(() => {});
          }
        }
      }
    } catch (err) {
      console.error('[WhatsApp] Error:', err);
    }
  });

  process.on('SIGINT', () => { ws.disconnect(); process.exit(0); });
}

main().catch(err => { console.error('[WhatsApp] Fatal:', err); process.exit(1); });
