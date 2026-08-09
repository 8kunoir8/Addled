// Addled — WhatsApp Bot Bridge
// Forwards messages between WhatsApp and the Addled Python backend via Baileys.

const { makeWASocket, useMultiFileAuthState, DisconnectReason } = require('@whiskeysockets/baileys');
const { Boom } = require('@hapi/boom');
const { AddledWSClient } = require('../shared/ws-client');
const path = require('path');
const fs = require('fs');

const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const AUTH_DIR = process.env.WHATSAPP_AUTH_DIR || path.join(__dirname, 'auth', 'whatsapp');

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
        try {
          const r = await ws.send('chat.send', { message: text });
          const response = r?.response || 'No response';
          const max = 4000;
          if (response.length <= max) {
            await sock.sendMessage(sender, { text: response }, { quoted: msg });
          } else {
            for (let i = 0; i < response.length; i += max)
              await sock.sendMessage(sender, { text: response.slice(i, i + max) });
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
