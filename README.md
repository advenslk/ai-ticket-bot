# ArveX AI Ticket Bot

Premium Discord ticket system for ArveX Hosting with Groq-powered AI support.

## Included

- Premium `/ticket panelsetup` designer
- Embed or Discord Components V2 panel style
- Configurable panel title, description, image, thumbnail, footer and selector text
- Configurable ticket welcome image and thumbnail
- Category system with emoji keys resolved through `emoji.py`
- Private ticket channels and staff roles
- AI replies as normal Discord messages (not embeds)
- Same-language AI responses: Sinhala/Singlish/English aware
- Groq model, temperature, history, cooldown, prompt and knowledge-base configuration
- Per-ticket AI toggle
- Staff claim, rename, close and transcript controls
- AI summaries and escalation alerts
- Persistent JSON ticket storage
- Legacy `/ticketpanel`, `/close`, `/claim`, `/summary`, `/tickets` compatibility commands

## Setup

1. Python 3.11+ is recommended.
2. Create and activate a virtual environment.
3. Install requirements.
4. Put the Discord token and Groq key in environment variables or `config.json`.
5. Fill `owner_id`, `guild_ids`, `ticket_category_id`, `ticket_log_channel_id` and `staff_role_ids`.
6. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal.
7. Invite the bot with `bot` and `applications.commands` scopes and permissions to manage channels, send messages, embed links, attach files, read history and manage messages.
8. Run `python3 bot.py`.

### Install

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 bot.py
```

### Secrets

Environment variables are preferred:

```bash
export DISCORD_TOKEN='YOUR_TOKEN'
export GROQ_API_KEY='YOUR_GROQ_KEY'
```

Do not commit real Discord bot tokens or Groq API keys.

## Panel Designer

Use:

```text
/ticket panelsetup
```

Choose **Premium Embed** or **Components V2**, then customize the title, description, image, thumbnail and selector text. The saved configuration is stored in `config.json`. Run the command again to send the configured panel.

## Emoji system

All category and UI emoji values can be emoji keys from `emoji.py`, for example:

```json
{"id":"billing","name":"Billing","emoji":"billing"}
```

Replace the values in `emoji.py` with Discord custom emoji syntax such as `<:name:id>` or `<a:name:id>`.

## AI

AI replies automatically inside open tickets when AI is enabled. Staff messages are ignored by the AI. The system prompt explicitly tells the model to match the customer's language; Sinhala customers receive Sinhala responses and Singlish is handled naturally. AI replies are normal messages, not embeds.

The default model is `openai/gpt-oss-120b`. Keep the model configurable because provider model availability can change.
