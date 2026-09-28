// Addled — WhatsApp Bot Bridge
// Forwards messages between WhatsApp and the Addled Python backend via Baileys.

const { makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const { AddledWSClient } = require('./shared/ws-client');
const path = require('path');
const fs = require('fs');

const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth', 'whatsapp');

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
    printQRInTerminal: true,
    browser: ['Addled Desktop', 'Chrome', '1.0.0'],
  });

  sock.ev.on('creds.update', saveCreds);

  sock.ev.on('connection.update', (update) => {
    const { connection, lastDisconnect, qr } = update;
    if (qr) {
      console.log('[WhatsApp] Scan QR code to pair:');
      if (typeof qr === 'string') console.log(`QR_DATA:${qr}`);
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
