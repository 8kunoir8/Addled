// Addled — WhatsApp Bot Bridge
// Forwards messages between WhatsApp and the Addled Python backend via Baileys.

const { makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const { AddledWSClient } = require('./shared/ws-client');
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
          if (pending[0]?.approval_id) {
            waiting.set(sender, pending[0]);
            await sock.sendMessage(sender,
              { text: approvalPrompt(pending[0]) }, { quoted: msg });
          }
        } catch (e) {
          await sock.sendMessage(sender, { text: '\u2757 Addled: ' + e.message }, { quoted: msg });
        }
      }
      if (type === 'imageMessage') {
        await sock.sendMessage(sender, { text: '\uD83D\uDDBC Image received. Vision in Phase 3.' }, { quoted: msg });
      }
      if (type === 'audioMessage') {
        await sock.sendMessage(sender, { text: '\uD83C\uDFA4 Voice note. STT in Phase 3.' }, { quoted: msg });
      }
    } catch (err) {
      console.error('[WhatsApp] Error:', err);
    }
  });

  process.on('SIGINT', () => { ws.disconnect(); process.exit(0); });
}

main().catch(err => { console.error('[WhatsApp] Fatal:', err); process.exit(1); });
