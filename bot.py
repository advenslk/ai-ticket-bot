import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from groq import AsyncGroq

from emoji import e

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
DATA_PATH = BASE / "tickets.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("arvex-ticket")


def load_json(path: Path, default: Any):
    if not path.exists():
        path.write_text(json.dumps(default, indent=2), encoding="utf-8")
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        log.exception("Failed to read %s", path)
        return default


CONFIG = load_json(CONFIG_PATH, {})
TICKETS = load_json(DATA_PATH, {})

TOKEN = os.getenv("DISCORD_TOKEN", CONFIG.get("token", ""))
GROQ_API_KEY = os.getenv("GROQ_API_KEY", CONFIG.get("groq_api_key", ""))
OWNER_ID = int(CONFIG.get("owner_id", 0) or 0)
GUILD_IDS = [int(x) for x in CONFIG.get("guild_ids", [])]
BRAND = CONFIG.get("brand_name", "ArveX Hosting")
BRAND_URL = CONFIG.get("brand_url", "https://www.arvex.host")
COLOR = int(CONFIG.get("embed_color", 10494192))
TICKET_CATEGORY_ID = int(CONFIG.get("ticket_category_id", 0) or 0)
LOG_CHANNEL_ID = int(CONFIG.get("ticket_log_channel_id", 0) or 0)
STAFF_ROLE_IDS = {int(x) for x in CONFIG.get("staff_role_ids", [])}
AI_MODEL = CONFIG.get("ai_model", "llama-3.3-70b-versatile")
AI_ENABLED_DEFAULT = bool(CONFIG.get("ai_enabled_by_default", True))
MAX_HISTORY = int(CONFIG.get("ai_max_history", 16))
COOLDOWN = float(CONFIG.get("ai_cooldown_seconds", 2.0))


def save_tickets():
    DATA_PATH.write_text(json.dumps(TICKETS, indent=2), encoding="utf-8")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime | None = None) -> int:
    return int((dt or utc_now()).timestamp())


def safe_name(text: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9-]+", "-", text.lower()).strip("-")
    return text[:35] or "user"


def is_staff(member: discord.Member | discord.User) -> bool:
    if member.id == OWNER_ID:
        return True
    if isinstance(member, discord.Member):
        return any(role.id in STAFF_ROLE_IDS for role in member.roles) or member.guild_permissions.manage_channels
    return False


def footer(embed: discord.Embed):
    embed.set_footer(text=f"{BRAND} • AI Support")
    return embed


def ticket_record(channel_id: int) -> dict[str, Any] | None:
    return TICKETS.get(str(channel_id))


def category_config(key: str) -> dict[str, Any]:
    for item in CONFIG.get("categories", []):
        if item.get("id") == key:
            return item
    return {"id": key, "name": key.title(), "emoji": e("ticket"), "description": "General support"}


class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.groq = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
        self.ai_locks: dict[int, asyncio.Lock] = {}
        self.last_ai: dict[int, float] = {}

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        for guild_id in GUILD_IDS:
            try:
                await self.tree.sync(guild=discord.Object(id=guild_id))
                log.info("Synced commands to guild %s", guild_id)
            except Exception:
                log.exception("Failed to sync guild %s", guild_id)

    async def on_ready(self):
        log.info("%s online as %s", BRAND, self.user)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ArveX Support"))

    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        record = ticket_record(message.channel.id)
        if record and message.guild:
            record.setdefault("messages", []).append({
                "author_id": message.author.id,
                "author": str(message.author),
                "content": message.content[:4000],
                "timestamp": ts(),
            })
            record["messages"] = record["messages"][-1000:]
            save_tickets()
            if record.get("ai_enabled", AI_ENABLED_DEFAULT) and not is_staff(message.author):
                await self.ai_reply(message, record)
        await self.process_commands(message)

    async def ai_reply(self, message: discord.Message, record: dict[str, Any]):
        if not self.groq:
            return
        now = time.monotonic()
        if now - self.last_ai.get(message.channel.id, 0) < COOLDOWN:
            return
        lock = self.ai_locks.setdefault(message.channel.id, asyncio.Lock())
        if lock.locked():
            return
        async with lock:
            self.last_ai[message.channel.id] = time.monotonic()
            if any(x in message.content.lower() for x in CONFIG.get("escalation_keywords", [])):
                record["priority"] = "high"
                record["escalated"] = True
                save_tickets()
                await self.safe_send_staff_alert(message.channel, record)
            history = record.get("messages", [])[-MAX_HISTORY:]
            context = "\n".join(f"{m['author']}: {m['content']}" for m in history)
            knowledge = CONFIG.get("knowledge_base", "")
            system = CONFIG.get("ai_system_prompt", "")
            system += f"\n\nCOMPANY KNOWLEDGE:\n{knowledge}\n\nRULES:\n" + "\n".join(f"- {x}" for x in CONFIG.get("ai_rules", []))
            try:
                async with message.channel.typing():
                    response = await self.groq.chat.completions.create(
                        model=AI_MODEL,
                        temperature=float(CONFIG.get("ai_temperature", 0.35)),
                        max_tokens=int(CONFIG.get("ai_max_tokens", 700)),
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"Ticket category: {record.get('category', 'general')}\nConversation:\n{context}\n\nRespond to the latest customer message. Keep it helpful and concise."},
                        ],
                    )
                text = response.choices[0].message.content.strip()
                if not text:
                    return
                if len(text) > 3900:
                    text = text[:3890] + "…"
                emb = discord.Embed(description=text, color=COLOR, timestamp=utc_now())
                emb.set_author(name=f"{BRAND} AI Support", icon_url=self.user.display_avatar.url if self.user else None)
                footer(emb)
                await message.channel.send(embed=emb)
                record.setdefault("messages", []).append({"author_id": self.user.id, "author": f"{BRAND} AI", "content": text, "timestamp": ts()})
                save_tickets()
            except Exception:
                log.exception("Groq request failed")
                await message.channel.send(f"{e('cross')} I’m having trouble processing that right now. A support member has been notified.")
                await self.safe_send_staff_alert(message.channel, record)

    async def safe_send_staff_alert(self, channel: discord.TextChannel, record: dict[str, Any]):
        mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS)
        if mentions and not record.get("alerted"):
            record["alerted"] = True
            save_tickets()
            await channel.send(f"{e('bell')} {mentions} **Staff attention requested.** This ticket has been escalated.", allowed_mentions=discord.AllowedMentions(roles=True))

    async def create_ticket(self, interaction: discord.Interaction, category: str, subject: str = ""):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Tickets can only be opened inside a server.", ephemeral=True)
        existing = [r for r in TICKETS.values() if r.get("guild_id") == guild.id and r.get("user_id") == interaction.user.id and not r.get("closed")]
        if len(existing) >= int(CONFIG.get("max_open_tickets_per_user", 2)):
            return await interaction.response.send_message(f"{e('cross')} You already have the maximum number of open tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cat = category_config(category)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True),
        }
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True)
        category_obj = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        channel_name = f"ticket-{safe_name(interaction.user.display_name)}-{str(interaction.user.id)[-4:]}"
        channel = await guild.create_text_channel(channel_name, category=category_obj if isinstance(category_obj, discord.CategoryChannel) else None, overwrites=overwrites, reason="AI ticket opened")
        record = {
            "channel_id": channel.id,
            "guild_id": guild.id,
            "user_id": interaction.user.id,
            "user_name": str(interaction.user),
            "category": category,
            "subject": subject,
            "created_at": ts(),
            "ai_enabled": AI_ENABLED_DEFAULT,
            "priority": "normal",
            "claimed_by": None,
            "closed": False,
            "escalated": False,
            "messages": [],
        }
        TICKETS[str(channel.id)] = record
        save_tickets()
        emb = discord.Embed(title=f"{cat.get('emoji', e('ticket'))} {cat.get('name', category.title())} Support", description=f"Welcome {interaction.user.mention}!\n\n{cat.get('description', 'A support member will assist you shortly.')}", color=COLOR, timestamp=utc_now())
        emb.add_field(name=f"{e('users')} Customer", value=interaction.user.mention, inline=True)
        emb.add_field(name="Category", value=cat.get("name", category.title()), inline=True)
        emb.add_field(name="AI Support", value=f"{e('check')} Enabled" if AI_ENABLED_DEFAULT else f"{e('cross')} Disabled", inline=True)
        if subject:
            emb.add_field(name="Subject", value=subject[:1024], inline=False)
        emb.set_thumbnail(url=interaction.user.display_avatar.url)
        footer(emb)
        await channel.send(content=interaction.user.mention, embed=emb, view=TicketControlView())
        await interaction.followup.send(f"{e('check')} Your ticket is ready: {channel.mention}", ephemeral=True)
        await self.log_ticket("opened", record, channel)

    async def log_ticket(self, action: str, record: dict[str, Any], channel: discord.TextChannel | None = None):
        if not LOG_CHANNEL_ID:
            return
        log_channel = self.get_channel(LOG_CHANNEL_ID)
        if not isinstance(log_channel, discord.TextChannel):
            return
        emb = discord.Embed(title=f"{e('ticket')} Ticket {action.title()}", color=COLOR, timestamp=utc_now())
        emb.add_field(name="User", value=f"<@{record.get('user_id')}>", inline=True)
        emb.add_field(name="Category", value=record.get("category", "general"), inline=True)
        emb.add_field(name="Channel", value=channel.mention if channel else str(record.get("channel_id")), inline=True)
        emb.add_field(name="Priority", value=record.get("priority", "normal"), inline=True)
        footer(emb)
        await log_channel.send(embed=emb)

    async def close_ticket(self, interaction: discord.Interaction):
        record = ticket_record(interaction.channel.id)
        if not record:
            return await interaction.response.send_message(f"{e('cross')} This is not an ArveX ticket.", ephemeral=True)
        if not is_staff(interaction.user) and interaction.user.id != record.get("user_id"):
            return await interaction.response.send_message(f"{e('cross')} You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        record["closed"] = True
        record["closed_at"] = ts()
        save_tickets()
        await self.send_transcript(record, interaction.channel)
        await self.log_ticket("closed", record, interaction.channel)
        await interaction.channel.send(f"{e('lock')} Ticket closed by {interaction.user.mention}. This channel will be deleted shortly.")
        await asyncio.sleep(4)
        try:
            await interaction.channel.delete(reason="Ticket closed")
        except discord.HTTPException:
            pass

    async def send_transcript(self, record: dict[str, Any], channel: discord.TextChannel):
        lines = [f"{BRAND} Ticket Transcript", f"Channel: #{channel.name}", f"User: {record.get('user_name')}", f"Category: {record.get('category')}", "", "--- Conversation ---"]
        for m in record.get("messages", []):
            when = datetime.fromtimestamp(m["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"[{when}] {m['author']}: {m['content']}")
        data = "\n".join(lines).encode("utf-8")
        if LOG_CHANNEL_ID:
            target = self.get_channel(LOG_CHANNEL_ID)
            if isinstance(target, discord.TextChannel):
                await target.send(content=f"{e('ticket')} Transcript — `{channel.name}`", file=discord.File(io.BytesIO(data), filename=f"{channel.name}.txt"))


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        options = []
        for c in CONFIG.get("categories", []):
            options.append(discord.SelectOption(label=c.get("name", c.get("id", "General"))[:100], value=c.get("id", "general"), description=c.get("description", "Support")[:100], emoji=c.get("emoji", "🎫")))
        if not options:
            options = [discord.SelectOption(label="General Support", value="general", description="General assistance", emoji="🎫")]
        select = discord.ui.Select(placeholder="Select a support category…", custom_id="arvex:ticket_category", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        value = interaction.data.get("values", ["general"])[0] if interaction.data else "general"
        await interaction.response.send_modal(TicketModal(value))


class TicketModal(discord.ui.Modal):
    def __init__(self, category: str):
        super().__init__(title="Open Support Ticket")
        self.category = category
        self.subject = discord.ui.TextInput(label="Subject", placeholder="Briefly describe what you need help with", max_length=120, required=False)
        self.add_item(self.subject)

    async def on_submit(self, interaction: discord.Interaction):
        await bot.create_ticket(interaction, self.category, str(self.subject.value).strip())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🛡️", style=discord.ButtonStyle.secondary, custom_id="arvex:claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = ticket_record(interaction.channel.id)
        if not record:
            return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        record["claimed_by"] = interaction.user.id
        save_tickets()
        await interaction.response.send_message(f"{e('check')} Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="AI Support", emoji="🤖", style=discord.ButtonStyle.primary, custom_id="arvex:ai_toggle")
    async def ai_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = ticket_record(interaction.channel.id)
        if not record:
            return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        record["ai_enabled"] = not record.get("ai_enabled", True)
        save_tickets()
        state = "enabled" if record["ai_enabled"] else "disabled"
        await interaction.response.send_message(f"{e('check')} AI support **{state}**.", ephemeral=True)

    @discord.ui.button(label="Summary", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="arvex:summary")
    async def summary(self, interaction: discord.Interaction, button: discord.ui.Button):
        record = ticket_record(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        if not bot.groq:
            return await interaction.followup.send("Groq is not configured.", ephemeral=True)
        convo = "\n".join(f"{m['author']}: {m['content']}" for m in record.get("messages", [])[-30:])
        try:
            r = await bot.groq.chat.completions.create(model=AI_MODEL, temperature=0.2, max_tokens=500, messages=[{"role": "system", "content": "Summarize this support ticket for a human agent. Include issue, requested outcome, important facts, and next action."}, {"role": "user", "content": convo}])
            await interaction.followup.send(r.choices[0].message.content[:1900], ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"AI summary failed: `{type(exc).__name__}`", ephemeral=True)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="arvex:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        await bot.close_ticket(interaction)


bot = TicketBot()


def owner_or_staff(interaction: discord.Interaction) -> bool:
    return interaction.user.id == OWNER_ID or is_staff(interaction.user)


@bot.tree.command(name="ticketpanel", description="Send the premium ArveX ticket panel")
async def ticketpanel(interaction: discord.Interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    emb = discord.Embed(title=f"{e('ticket')} ArveX Support Center", description=CONFIG.get("panel_description", "Need help? Select a category below to open a private support ticket.\n\nOur AI assistant can help with common questions while our staff handles account-specific issues."), color=COLOR)
    if CONFIG.get("panel_image"):
        emb.set_image(url=CONFIG["panel_image"])
    emb.add_field(name=f"{e('sparkles')} Fast AI Assistance", value="Instant answers for common questions", inline=True)
    emb.add_field(name=f"{e('host')} Human Support", value="Escalation to the ArveX team", inline=True)
    emb.add_field(name=f"{e('lock')} Private Tickets", value="Only you and support staff can see your ticket", inline=True)
    footer(emb)
    await interaction.channel.send(embed=emb, view=TicketPanelView())
    await interaction.response.send_message(f"{e('check')} Ticket panel sent.", ephemeral=True)


@bot.tree.command(name="ticket", description="Open a support ticket")
@app_commands.describe(category="Ticket category", subject="Optional short subject")
async def ticket(interaction: discord.Interaction, category: str = "general", subject: str = ""):
    await bot.create_ticket(interaction, category, subject)


@bot.tree.command(name="close", description="Close the current ticket")
async def close(interaction: discord.Interaction):
    await bot.close_ticket(interaction)


@bot.tree.command(name="claim", description="Claim the current ticket")
async def claim(interaction: discord.Interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = ticket_record(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    record["claimed_by"] = interaction.user.id
    save_tickets()
    await interaction.response.send_message(f"{e('check')} Claimed by {interaction.user.mention}.")


@bot.tree.command(name="ai", description="Enable or disable AI in the current ticket")
@app_commands.describe(state="on or off")
async def ai(interaction: discord.Interaction, state: str):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = ticket_record(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    state = state.lower().strip()
    if state not in {"on", "off"}:
        return await interaction.response.send_message("Use `on` or `off`.", ephemeral=True)
    record["ai_enabled"] = state == "on"
    save_tickets()
    await interaction.response.send_message(f"{e('check')} AI is now **{state}**.", ephemeral=True)


@bot.tree.command(name="summary", description="Generate an AI summary of the current ticket")
async def summary(interaction: discord.Interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = ticket_record(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    if not bot.groq:
        return await interaction.followup.send("Groq is not configured.", ephemeral=True)
    convo = "\n".join(f"{m['author']}: {m['content']}" for m in record.get("messages", [])[-30:])
    r = await bot.groq.chat.completions.create(model=AI_MODEL, temperature=0.2, max_tokens=500, messages=[{"role": "system", "content": "Summarize this support ticket for a human agent. Include issue, requested outcome, important facts, and next action."}, {"role": "user", "content": convo}])
    await interaction.followup.send(r.choices[0].message.content[:1900], ephemeral=True)


@bot.tree.command(name="tickets", description="List open ArveX tickets")
async def tickets(interaction: discord.Interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    rows = [r for r in TICKETS.values() if r.get("guild_id") == interaction.guild_id and not r.get("closed")]
    if not rows:
        return await interaction.response.send_message("No open tickets.", ephemeral=True)
    lines = [f"<#{r['channel_id']}> — `{r['category']}` — `{r['priority']}` — <@{r['user_id']}>" for r in rows[:30]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Put it in the environment or config.json.")
    bot.run(TOKEN)
