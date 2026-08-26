# ArveX AI Ticket Bot

Premium Discord ticket system for ArveX Hosting with Groq-powered AI support.

## Features

- Premium ticket panel with category select
- Private ticket channels with configurable staff roles
- Groq AI support inside tickets
- Configurable company knowledge base and AI rules
- AI enable/disable per ticket
- Staff claim button
- AI escalation alerts
- AI ticket summaries
- Ticket transcripts sent to a log channel
- Ticket close/delete flow
- Persistent ticket data in JSON
- Slash commands: `/ticketpanel`, `/ticket`, `/claim`, `/ai`, `/summary`, `/tickets`, `/close`
- Custom emoji loader in `emoji.py`
- Environment variables for secrets

## Setup

1. Python 3.11+ is recommended.
2. Create a virtual environment.
3. Install requirements.
4. Put your Discord bot token and Groq key in environment variables or `config.json`.
5. Fill Discord IDs in `config.json`.
6. Enable **Server Members Intent** and **Message Content Intent** in the Discord Developer Portal.
7. Invite the bot with `bot` and `applications.commands` scopes and permissions to manage channels, send messages, embed links, attach files, read history and manage messages.
8. Run `python3 bot.py`.

### Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
export DISCORD_TOKEN='YOUR_TOKEN'
export GROQ_API_KEY='YOUR_GROQ_KEY'
python3 bot.py
```

### Config

Set `owner_id`, `guild_ids`, `ticket_category_id`, `ticket_log_channel_id`, and `staff_role_ids`. Then customize `knowledge_base`, `ai_system_prompt`, categories, escalation keywords and panel image.

Do not commit real bot tokens or API keys.
