// Addled — Discord Bot Bridge
// Forwards messages between Discord and the Addled Python backend via discord.js.

const { Client, GatewayIntentBits, REST, Routes, SlashCommandBuilder } = require('discord.js');
const { AddledWSClient } = require('./shared/ws-client');

const DISCORD_TOKEN = process.env.DISCORD_BOT_TOKEN || '';
const WS_URL = process.env.ADDLED_WS_URL || 'ws://127.0.0.1:9876';
const ALLOWED_CHANNELS = (process.env.DISCORD_ALLOWED_CHANNELS || '').split(',').filter(Boolean);

const { ActionRowBuilder, ButtonBuilder, ButtonStyle } = require('discord.js');

// How a pending approval reads in the channel. Mirrors the dashboard card.
function approvalPrompt(req) {
  const what = req?.kind === 'tool' ? 'tool' : req?.kind === 'skill' ? 'skill' : 'action';
  const lines = [`🔐 **Permission needed**`,
                 `The ${what} \`${req?.name || req?.action_type || 'action'}\` needs your approval.`];
  if (req?.command) lines.push('', `\`\`\`\n${String(req.command).slice(0, 300)}\n\`\`\``);
  if (!req?.grantable) {
    lines.push('', '_This one always asks first, so it cannot be remembered._');
  }
  return lines.join('\n');
}

// One row of buttons for the first pending request. A second request waits for
// the next turn: two rows in one channel invites approving the wrong thing.
function approvalRow(pending) {
  const first = Array.isArray(pending) ? pending[0] : null;
  if (!first?.approval_id) return null;
  const id = String(first.approval_id).slice(0, 90);
  const row = new ActionRowBuilder().addComponents(
    new ButtonBuilder().setCustomId(`ap:${id}`).setLabel('Allow once')
      .setStyle(ButtonStyle.Primary),
  );
  if (first.grantable) {
    row.addComponents(
      new ButtonBuilder().setCustomId(`al:${id}`).setLabel('Always allow')
        .setStyle(ButtonStyle.Secondary));
  }
  row.addComponents(
    new ButtonBuilder().setCustomId(`dn:${id}`).setLabel('Deny')
      .setStyle(ButtonStyle.Danger));
  return row;
}

// The shared answer path, used by the button handler. Kept in one function so
// the three verbs cannot drift apart.
async function answerApproval(ws, verb, approvalId) {
  if (verb === 'al') {
    // The name is not in the button; the backend resolves it from its record
    // of this approval, so it cannot be altered in transit.
    const r = await ws.send('approvals.alwaysAllow', { approvalId });
    return r?.success
      ? '♾️ Always allowed — it will not ask again.'
      : `⚠️ ${r?.error || 'Could not save that.'}`;
  }
  if (verb === 'ap') {
    const r = await ws.send('action.approve', { approvalId });
    return r?.success ? '✅ Allowed — running it now.'
      : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
  }
  const r = await ws.send('action.deny', { approvalId });
  return r?.success ? '⛔ Denied — it was not run.'
    : `⚠️ ${r?.error || 'It is no longer waiting.'}`;
}

async function main() {
  if (!DISCORD_TOKEN) {
    console.error('[Discord] DISCORD_BOT_TOKEN environment variable is required.');
    console.error('[Discord] Create an app at https://discord.com/developers/applications');
    process.exit(1);
  }

  // Connect to Addled backend
  const ws = new AddledWSClient(WS_URL);
  try {
    await ws.connect();
  } catch (e) {
    console.error('[Discord] Backend not reachable:', e.message);
  }

  const client = new Client({
    intents: [
      GatewayIntentBits.Guilds,
      GatewayIntentBits.GuildMessages,
      GatewayIntentBits.MessageContent,
      GatewayIntentBits.DirectMessages,
    ],
  });

  // Register slash commands on ready
  client.once('ready', async () => {
    console.log(`[Discord] Logged in as ${client.user.tag}`);

    const commands = [
      new SlashCommandBuilder().setName('chat').setDescription('Chat with Addled')
        .addStringOption(o => o.setName('message').setDescription('Your message').setRequired(true)),
      new SlashCommandBuilder().setName('goal').setDescription('Create a background goal')
        .addStringOption(o => o.setName('description').setDescription('Goal description').setRequired(true)),
      new SlashCommandBuilder().setName('status').setDescription('Check Addled status'),
      new SlashCommandBuilder().setName('sleep').setDescription('Put Addled to sleep'),
      new SlashCommandBuilder().setName('wake').setDescription('Wake Addled up'),
    ].map(cmd => cmd.toJSON());

    try {
      const rest = new REST({ version: '10' }).setToken(DISCORD_TOKEN);
      await rest.put(Routes.applicationCommands(client.user.id), { body: commands });
      console.log('[Discord] Slash commands registered');
    } catch (e) {
      console.error('[Discord] Failed to register commands:', e.message);
    }

    client.user.setActivity('your desktop');
  });

  // Handle slash commands
  client.on('interactionCreate', async (interaction) => {
    // A button before anything else. This handler used to return immediately
    // for anything that was not a chat-input command, which would have made an
    // approval button do nothing at all.
    if (interaction.isButton()) {
      const [verb, approvalId] = String(interaction.customId || '').split(':');
      if (!approvalId || !['ap', 'al', 'dn'].includes(verb)) return;
      // Reply inside Discord's 3-second window, then edit with the outcome.
      await interaction.deferUpdate().catch(() => {});
      try {
        const outcome = await answerApproval(ws, verb, approvalId);
        await interaction.editReply({ content: outcome, components: [] });
      } catch (e) {
        await interaction.editReply({
          content: `⚠️ Failed: ${e.message}`, components: [],
        }).catch(() => {});
      }
      return;
    }
    if (!interaction.isChatInputCommand()) return;

    const { commandName } = interaction;

    switch (commandName) {
      case 'chat': {
        await interaction.deferReply();
        const message = interaction.options.getString('message', true);
        // A reply can take minutes on a local model, so show that it is still
        // working rather than a frozen deferred reply.
        let elapsed = 0;
        const ticker = setInterval(() => {
          elapsed += 15;
          interaction.editReply(`\uD83E\uDD14 Thinking... (${elapsed}s)`).catch(() => {});
        }, 15000);
        try {
          // `source` is what the chat page labels the bubble with, and
          // `conversation` is what lets an approval be answered from *this*
          // channel rather than any other. The backend announces the turn to
          // every other surface, so a message asked here appears in the app.
          const r = await ws.send('chat.send', {
            message,
            source: 'discord',
            conversation: String(interaction.channelId || ''),
          });
          const response = r?.response || 'No response';
          clearInterval(ticker);
          const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
          const row = approvalRow(pending);
          if (response.length <= 2000) {
            await interaction.editReply(row
              ? { content: response, components: [row] }
              : { content: response });
          } else {
            await interaction.editReply(response.slice(0, 1997) + '...');
            // Send rest as follow-up
            for (let i = 1997; i < response.length; i += 2000) {
              await interaction.followUp(response.slice(i, i + 2000));
            }
          }
          if (row) {
            await interaction.followUp({
              content: approvalPrompt(pending[0]), components: [row],
            });
          }
        } catch (e) {
          clearInterval(ticker);
          await interaction.editReply(`\u2757 Error: ${e.message}`);
        }
        break;
      }
      case 'goal': {
        await interaction.deferReply();
        const desc = interaction.options.getString('description', true);
        try {
          const r = await ws.send('goal.create', { title: desc, priority: 'normal' });
          await interaction.editReply(`\u2705 Goal created: **${desc}**\n\uD83C\uDD94 \`${r?.goalId || '?'}\``);
        } catch (e) {
          await interaction.editReply(`\u2757 Failed: ${e.message}`);
        }
        break;
      }
      case 'status': {
        await interaction.deferReply();
        try {
          const r = await ws.send('system.status', {});
          await interaction.editReply(
            `\uD83D\uDFE2 **Addled Status**\n` +
            `Engine: ${r?.engineState || '?'}\n` +
            `Provider: ${r?.provider || '?'}\n` +
            `Character: ${r?.characterState || '?'}`
          );
        } catch (e) {
          await interaction.editReply(`\u26A0\uFE0F Addled unreachable: ${e.message}`);
        }
        break;
      }
      case 'sleep': {
        await interaction.deferReply();
        try { await ws.send('character.setState', { state: 'sleeping' }); await interaction.editReply('\uD83D\uDE34 Addled sleeping'); }
        catch (e) { await interaction.editReply(`\u2757 ${e.message}`); }
        break;
      }
      case 'wake': {
        await interaction.deferReply();
        try { await ws.send('character.setState', { state: 'idle' }); await interaction.editReply('\uD83D\uDC4B Addled awake'); }
        catch (e) { await interaction.editReply(`\u2757 ${e.message}`); }
        break;
      }
    }
  });

  // Handle @mentions in guild messages
  client.on('messageCreate', async (msg) => {
    if (msg.author.bot) return;
    if (ALLOWED_CHANNELS.length > 0 && !ALLOWED_CHANNELS.includes(msg.channelId)) return;

    const isMentioned = msg.mentions.has(client.user);
    const isDM = !msg.guild;

    if (!isMentioned && !isDM) return;

    // Remove the @mention from the text
    const text = msg.content.replace(new RegExp(`<@!?${client.user.id}>`, 'g'), '').trim();
    if (!text) {
      await msg.reply('\uD83D\uDC4B Hi! Use `/chat <message>` or just mention me with your question.');
      return;
    }

    await msg.channel.sendTyping();
    try {
      const r = await ws.send('chat.send', {
        message: text,
        source: 'discord',
        conversation: String(msg.channelId || ''),
      });
      const response = r?.response || 'No response';
      const pending = Array.isArray(r?.pendingApprovals) ? r.pendingApprovals : [];
      const row = approvalRow(pending);
      if (response.length <= 2000) {
        await msg.reply(row
          ? { content: response, components: [row] }
          : { content: response });
      } else {
        await msg.reply(response.slice(0, 1997) + '...');
        const channel = msg.channel;
        for (let i = 1997; i < response.length; i += 2000) {
          await channel.send(response.slice(i, i + 2000));
        }
      }
      if (row) {
        await msg.channel.send({
          content: approvalPrompt(pending[0]), components: [row],
        });
      }
    } catch (e) {
      await msg.reply(`\u2757 Addled: ${e.message}`);
    }
  });

  // Login
  await client.login(DISCORD_TOKEN);

  process.on('SIGINT', () => {
    console.log('[Discord] Shutting down...');
    ws.disconnect();
    client.destroy();
    process.exit(0);
  });
}

main().catch(err => { console.error('[Discord] Fatal:', err); process.exit(1); });
