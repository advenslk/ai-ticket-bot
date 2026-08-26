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
AI_MODEL = CONFIG.get("ai_model", "openai/gpt-oss-120b")
AI_ENABLED_DEFAULT = bool(CONFIG.get("ai_enabled_by_default", True))
MAX_HISTORY = int(CONFIG.get("ai_max_history", 16))
COOLDOWN = float(CONFIG.get("ai_cooldown_seconds", 2.0))


def save_config():
    CONFIG_PATH.write_text(json.dumps(CONFIG, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def save_tickets():
    DATA_PATH.write_text(json.dumps(TICKETS, indent=2, ensure_ascii=False), encoding="utf-8")


def utc_now():
    return datetime.now(timezone.utc)


def ts(dt=None):
    return int((dt or utc_now()).timestamp())


def safe_name(text: str):
    return re.sub(r"[^a-zA-Z0-9-]+", "-", text.lower()).strip("-")[:35] or "user"


def is_staff(member):
    if member.id == OWNER_ID:
        return True
    return isinstance(member, discord.Member) and (
        any(r.id in STAFF_ROLE_IDS for r in member.roles) or member.guild_permissions.manage_channels
    )


def record_for(channel_id):
    return TICKETS.get(str(channel_id))


def category_config(key):
    for item in CONFIG.get("categories", []):
        if item.get("id") == key:
            return item
    return {"id": key, "name": key.title(), "emoji": e(key) or e("ticket"), "description": "General support."}


def resolve_emoji(value):
    # Category configs can use an emoji.py key such as "billing" or a literal Discord emoji.
    if not value:
        return e("ticket")
    if isinstance(value, str) and value.startswith("emoji:"):
        return e(value[6:]) or e("ticket")
    if value in getattr(__import__("emoji"), "EMOJIS", {}):
        return e(value)
    return value


def base_embed(title, description="", *, color=None):
    emb = discord.Embed(title=title, description=description, color=color or COLOR, timestamp=utc_now())
    footer_text = CONFIG.get("footer_text", f"{BRAND} • Support")
    footer_icon = CONFIG.get("footer_icon", "")
    emb.set_footer(text=footer_text, icon_url=footer_icon or discord.Embed.Empty)
    if BRAND_URL:
        emb.url = BRAND_URL
    return emb


class TicketBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.groq = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
        self.ai_locks = {}
        self.last_ai = {}
        self.panel_views = {}

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        for guild_id in GUILD_IDS:
            try:
                guild = discord.Object(id=guild_id)
                # Global commands are copied into each configured guild for instant availability.
                global_commands = self.tree.get_commands()
                self.tree.clear_commands(guild=guild)
                for command in global_commands:
                    self.tree.add_command(command, guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d commands to guild %s: %s", len(synced), guild_id, ", ".join('/'+x.name for x in synced) or 'NONE')
            except Exception:
                log.exception("Failed to sync guild %s", guild_id)

    async def on_ready(self):
        log.info("%s online as %s", BRAND, self.user)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="ArveX Support"))

    async def on_message(self, message):
        if message.author.bot:
            return
        record = record_for(message.channel.id)
        if record and message.guild and not record.get("closed"):
            record.setdefault("messages", []).append({
                "author_id": message.author.id,
                "author": str(message.author),
                "content": message.content[:4000],
                "timestamp": ts(),
            })
            record["messages"] = record["messages"][-1000:]
            save_tickets()
            # AI replies only to customer messages, never staff messages.
            if record.get("ai_enabled", AI_ENABLED_DEFAULT) and not is_staff(message.author):
                await self.ai_reply(message, record)
        await self.process_commands(message)

    async def ai_reply(self, message, record):
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
            lowered = message.content.lower()
            if any(x.lower() in lowered for x in CONFIG.get("escalation_keywords", [])):
                record["priority"] = "high"
                record["escalated"] = True
                save_tickets()
                await self.safe_staff_alert(message.channel, record)
            history = record.get("messages", [])[-MAX_HISTORY:]
            context = "\n".join(f"{m['author']}: {m['content']}" for m in history)
            knowledge = CONFIG.get("knowledge_base", "")
            system = CONFIG.get("ai_system_prompt", "")
            system += "\n\nLANGUAGE RULE: Reply in the same language/style as the latest customer message. If the customer uses Sinhala, reply in Sinhala. If they use Singlish, understand it and reply naturally in Sinhala/Singlish as appropriate. Do not switch to English unless needed."
            system += f"\n\nCOMPANY KNOWLEDGE:\n{knowledge}\n\nRULES:\n" + "\n".join(f"- {x}" for x in CONFIG.get("ai_rules", []))
            try:
                async with message.channel.typing():
                    response = await self.groq.chat.completions.create(
                        model=AI_MODEL,
                        temperature=float(CONFIG.get("ai_temperature", 0.35)),
                        max_tokens=int(CONFIG.get("ai_max_tokens", 700)),
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"Ticket category: {record.get('category', 'general')}\nConversation:\n{context}\n\nReply naturally to the latest customer message. Do not use an embed, headings, or a formal report format. Respond like a friendly ArveX support agent."},
                        ],
                    )
                text = (response.choices[0].message.content or "").strip()
                if not text:
                    return
                if len(text) > 3900:
                    text = text[:3890] + "…"
                # Deliberately a normal message, not an embed.
                await message.channel.send(text, allowed_mentions=discord.AllowedMentions.none())
                record.setdefault("messages", []).append({"author_id": self.user.id, "author": f"{BRAND} AI", "content": text, "timestamp": ts()})
                save_tickets()
            except Exception:
                log.exception("Groq request failed")
                await message.channel.send(f"{e('cross')} I’m having trouble processing that right now. A support member has been notified.")
                await self.safe_staff_alert(message.channel, record)

    async def safe_staff_alert(self, channel, record):
        mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS)
        if mentions and not record.get("alerted"):
            record["alerted"] = True
            save_tickets()
            await channel.send(f"{e('bell')} {mentions} **Staff attention requested.**", allowed_mentions=discord.AllowedMentions(roles=True))

    async def create_ticket(self, interaction, category="general", subject=""):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message(f"{e('cross')} Tickets can only be opened inside a server.", ephemeral=True)
        existing = [r for r in TICKETS.values() if r.get("guild_id") == guild.id and r.get("user_id") == interaction.user.id and not r.get("closed")]
        if len(existing) >= int(CONFIG.get("max_open_tickets_per_user", 2)):
            return await interaction.response.send_message(f"{e('cross')} You already have the maximum number of open tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cat = category_config(category)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True),
        }
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True)
        category_obj = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        channel_name = f"ticket-{safe_name(interaction.user.display_name)}-{str(interaction.user.id)[-4:]}"
        channel = await guild.create_text_channel(channel_name, category=category_obj if isinstance(category_obj, discord.CategoryChannel) else None, overwrites=overwrites, reason="ArveX ticket opened")
        record = {
            "channel_id": channel.id, "guild_id": guild.id, "user_id": interaction.user.id, "user_name": str(interaction.user),
            "category": category, "subject": subject, "created_at": ts(), "ai_enabled": AI_ENABLED_DEFAULT,
            "priority": "normal", "claimed_by": None, "closed": False, "escalated": False, "messages": [],
        }
        TICKETS[str(channel.id)] = record
        save_tickets()
        welcome = self.ticket_welcome_embed(interaction.user, cat, subject)
        await channel.send(content=interaction.user.mention, embed=welcome, view=TicketControlView(), allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"{e('check')} Your ticket is ready: {channel.mention}", ephemeral=True)
        await self.log_ticket("opened", record, channel)

    def ticket_welcome_embed(self, user, cat, subject=""):
        emb = base_embed(f"{resolve_emoji(cat.get('emoji'))} {cat.get('name', 'Support')}", CONFIG.get("ticket_welcome_description", "Welcome! Please describe your issue below. Our AI assistant can help with common questions and staff can take over when needed."))
        emb.add_field(name=f"{e('users')} Customer", value=user.mention, inline=True)
        emb.add_field(name=f"{e('ticket')} Category", value=cat.get("name", "General Support"), inline=True)
        emb.add_field(name=f"{e('ai')} AI Support", value=f"{e('check')} Enabled" if AI_ENABLED_DEFAULT else f"{e('cross')} Disabled", inline=True)
        if subject:
            emb.add_field(name="Subject", value=subject[:1024], inline=False)
        image = CONFIG.get("ticket_welcome_image", "")
        thumb = CONFIG.get("ticket_welcome_thumbnail", "")
        if image:
            emb.set_image(url=image)
        if thumb:
            emb.set_thumbnail(url=thumb)
        return emb

    async def log_ticket(self, action, record, channel=None):
        if not LOG_CHANNEL_ID:
            return
        target = self.get_channel(LOG_CHANNEL_ID)
        if not isinstance(target, discord.TextChannel):
            return
        emb = base_embed(f"{e('ticket')} Ticket {action.title()}")
        emb.add_field(name="User", value=f"<@{record.get('user_id')}>", inline=True)
        emb.add_field(name="Category", value=record.get("category", "general"), inline=True)
        emb.add_field(name="Channel", value=channel.mention if channel else str(record.get("channel_id")), inline=True)
        emb.add_field(name="Priority", value=record.get("priority", "normal"), inline=True)
        await target.send(embed=emb)

    async def close_ticket(self, interaction, delete=True):
        record = record_for(interaction.channel.id)
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
        await interaction.channel.send(f"{e('lock')} Ticket closed by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"{e('check')} Ticket closed.", ephemeral=True)
        if delete:
            await asyncio.sleep(3)
            try:
                await interaction.channel.delete(reason="ArveX ticket closed")
            except discord.HTTPException:
                pass

    async def rename_ticket(self, interaction, name):
        record = record_for(interaction.channel.id)
        if not record:
            return await interaction.response.send_message(f"{e('cross')} This is not an ArveX ticket.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        new_name = safe_name(name)
        if not new_name.startswith("ticket-"):
            new_name = "ticket-" + new_name
        await interaction.channel.edit(name=new_name, reason="ArveX ticket renamed")
        record["channel_name"] = new_name
        save_tickets()
        await interaction.response.send_message(f"{e('check')} Ticket renamed to `{new_name}`.", ephemeral=True)

    async def send_transcript(self, record, channel):
        lines = [f"{BRAND} Ticket Transcript", f"Channel: #{channel.name}", f"User: {record.get('user_name')}", f"Category: {record.get('category')}", "", "--- Conversation ---"]
        for m in record.get("messages", []):
            when = datetime.fromtimestamp(m["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"[{when}] {m['author']}: {m['content']}")
        data = "\n".join(lines).encode("utf-8")
        if LOG_CHANNEL_ID:
            target = self.get_channel(LOG_CHANNEL_ID)
            if isinstance(target, discord.TextChannel):
                await target.send(content=f"{e('ticket')} Transcript — `{channel.name}`", file=discord.File(io.BytesIO(data), filename=f"{channel.name}.txt"))

    async def ai_summary(self, record):
        if not self.groq:
            return "Groq is not configured."
        convo = "\n".join(f"{m['author']}: {m['content']}" for m in record.get("messages", [])[-40:])
        r = await self.groq.chat.completions.create(
            model=AI_MODEL, temperature=0.2, max_tokens=600,
            messages=[
                {"role": "system", "content": "Summarize this support ticket for a human support agent. Include issue, requested outcome, important facts, and next action."},
                {"role": "user", "content": convo},
            ],
        )
        return (r.choices[0].message.content or "No summary generated.").strip()


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        options = []
        for c in CONFIG.get("categories", []):
            options.append(discord.SelectOption(label=c.get("name", c.get("id", "General"))[:100], value=c.get("id", "general"), description=c.get("description", "Support")[:100], emoji=resolve_emoji(c.get("emoji"))))
        if not options:
            options = [discord.SelectOption(label="General Support", value="general", description="General assistance", emoji=e("ticket"))]
        select = discord.ui.Select(placeholder=CONFIG.get("panel_select_placeholder", "Select a support category…"), custom_id="arvex:ticket_category", options=options)
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction):
        value = interaction.data.get("values", ["general"])[0] if interaction.data else "general"
        await interaction.response.send_modal(TicketModal(value))


class TicketModal(discord.ui.Modal):
    def __init__(self, category):
        super().__init__(title=CONFIG.get("ticket_modal_title", "Open Support Ticket"))
        self.category = category
        self.subject = discord.ui.TextInput(label="Subject", placeholder=CONFIG.get("ticket_subject_placeholder", "Briefly describe what you need help with"), max_length=120, required=False)
        self.add_item(self.subject)

    async def on_submit(self, interaction):
        await bot.create_ticket(interaction, self.category, str(self.subject.value).strip())


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", emoji="🛡️", style=discord.ButtonStyle.secondary, custom_id="arvex:claim")
    async def claim(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record:
            return await interaction.response.send_message(f"{e('cross')} This is not a ticket.", ephemeral=True)
        if not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        record["claimed_by"] = interaction.user.id
        save_tickets()
        await interaction.response.send_message(f"{e('check')} Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="AI", emoji="🤖", style=discord.ButtonStyle.primary, custom_id="arvex:ai_toggle")
    async def ai_toggle(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        record["ai_enabled"] = not record.get("ai_enabled", True)
        save_tickets()
        await interaction.response.send_message(f"{e('check')} AI support **{'enabled' if record['ai_enabled'] else 'disabled'}**.", ephemeral=True)

    @discord.ui.button(label="Rename", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="arvex:rename")
    async def rename(self, interaction, button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="Summary", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="arvex:summary")
    async def summary(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            text = await bot.ai_summary(record)
            await interaction.followup.send(text[:4000], ephemeral=True)
        except Exception as exc:
            log.exception("Summary failed")
            await interaction.followup.send(f"{e('cross')} AI summary failed: `{type(exc).__name__}`", ephemeral=True)

    @discord.ui.button(label="Close", emoji="🔒", style=discord.ButtonStyle.danger, custom_id="arvex:close")
    async def close(self, interaction, button):
        await bot.close_ticket(interaction)


class RenameModal(discord.ui.Modal, title="Rename Ticket"):
    name = discord.ui.TextInput(label="New ticket name", placeholder="e.g. billing-refund", max_length=80, required=True)

    async def on_submit(self, interaction):
        await bot.rename_ticket(interaction, str(self.name.value))


class PanelStyleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Premium Embed", value="embed", description="Classic premium Discord embed panel", emoji="✨"),
            discord.SelectOption(label="Components V2", value="v2", description="Modern Discord Components V2 layout", emoji="🧩"),
        ]
        super().__init__(placeholder="Choose panel style…", options=options, custom_id="arvex:panel_style")

    async def callback(self, interaction):
        await interaction.response.send_modal(PanelDesignerModal(self.values[0]))


class PanelSetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(PanelStyleSelect())


class PanelDesignerModal(discord.ui.Modal):
    def __init__(self, style):
        super().__init__(title="ArveX Panel Designer")
        self.style_name = style
        self.title_input = discord.ui.TextInput(label="Panel title", default=CONFIG.get("panel_title", f"{e('ticket')} ArveX Support Center")[:45], max_length=45)
        self.description = discord.ui.TextInput(label="Panel description", default=CONFIG.get("panel_description", "Select a category below to open a private support ticket.")[:4000], style=discord.TextStyle.paragraph, max_length=4000)
        self.image = discord.ui.TextInput(label="Image URL", default=CONFIG.get("panel_image", "")[:4000], required=False, max_length=4000)
        self.thumbnail = discord.ui.TextInput(label="Thumbnail URL", default=CONFIG.get("panel_thumbnail", "")[:4000], required=False, max_length=4000)
        self.button_label = discord.ui.TextInput(label="Button / selector text", default=CONFIG.get("panel_select_placeholder", "Select a support category…")[:100], max_length=100, required=False)
        for item in (self.title_input, self.description, self.image, self.thumbnail, self.button_label):
            self.add_item(item)

    async def on_submit(self, interaction):
        CONFIG["panel_style"] = self.style_name
        CONFIG["panel_title"] = str(self.title_input.value)
        CONFIG["panel_description"] = str(self.description.value)
        CONFIG["panel_image"] = str(self.image.value).strip()
        CONFIG["panel_thumbnail"] = str(self.thumbnail.value).strip()
        CONFIG["panel_select_placeholder"] = str(self.button_label.value).strip() or "Select a support category…"
        save_config()
        await interaction.response.send_message(f"{e('check')} Panel design saved as **{self.style_name}**. Use `/ticket panelsetup` again to send the configured panel.", ephemeral=True)


class PanelSendView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        send = discord.ui.Button(label="Send Configured Panel", emoji="📨", style=discord.ButtonStyle.success, custom_id="arvex:send_panel")
        send.callback = self.send_panel
        self.add_item(send)

    async def send_panel(self, interaction):
        await send_ticket_panel(interaction)


async def send_ticket_panel(interaction):
    style = CONFIG.get("panel_style", "embed")
    title = CONFIG.get("panel_title", f"{e('ticket')} ArveX Support Center")
    desc = CONFIG.get("panel_description", "Need help? Select a category below to open a private support ticket.")
    if style == "v2" and hasattr(discord.ui, "Container") and hasattr(discord.ui, "TextDisplay"):
        # Components V2 is used when supported by the installed discord.py version.
        view = discord.ui.LayoutView(timeout=None)
        container = discord.ui.Container(accent_color=COLOR)
        container.add_item(discord.ui.TextDisplay(f"# {title}\n\n{desc}"))
        if CONFIG.get("panel_image") and hasattr(discord.ui, "MediaGallery"):
            try:
                container.add_item(discord.ui.MediaGallery(media=[discord.MediaGalleryItem(CONFIG["panel_image"])]))
            except Exception:
                pass
        container.add_item(TicketPanelView().children[0])
        view.add_item(container)
        await interaction.channel.send(view=view)
    else:
        emb = base_embed(title, desc)
        if CONFIG.get("panel_image"):
            emb.set_image(url=CONFIG["panel_image"])
        if CONFIG.get("panel_thumbnail"):
            emb.set_thumbnail(url=CONFIG["panel_thumbnail"])
        emb.add_field(name=f"{e('ai')} AI Assistance", value=CONFIG.get("panel_ai_text", "Instant help for common questions."), inline=True)
        emb.add_field(name=f"{e('host')} Human Support", value=CONFIG.get("panel_staff_text", "Escalation to ArveX staff when needed."), inline=True)
        await interaction.channel.send(embed=emb, view=TicketPanelView())


class TicketGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="ticket", description="ArveX Hosting ticket system")


bot = TicketBot()
ticket_group = TicketGroup()
bot.tree.add_command(ticket_group)


def owner_or_staff(interaction):
    return interaction.user.id == OWNER_ID or is_staff(interaction.user)


@ticket_group.command(name="panelsetup", description="Configure and send the ArveX ticket panel")
async def ticket_panelsetup(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    emb = base_embed(f"{e('settings')} ArveX Ticket Panel Designer", "Choose a panel style below. You can then customize the title, text, images, thumbnail and selector label.\n\nAll saved values are stored in `config.json` and categories are managed separately in the same configuration.")
    await interaction.response.send_message(embed=emb, view=PanelSetupView(), ephemeral=True)
    await interaction.followup.send(f"{e('sparkles')} After saving, press the button below to send the configured panel.", view=PanelSendView(), ephemeral=True)


@ticket_group.command(name="open", description="Open a support ticket")
@app_commands.describe(category="Ticket category", subject="Optional subject")
async def ticket_open(interaction, category: str = "general", subject: str = ""):
    await bot.create_ticket(interaction, category, subject)


@ticket_group.command(name="close", description="Close the current ticket")
async def ticket_close(interaction):
    await bot.close_ticket(interaction)


@ticket_group.command(name="claim", description="Claim the current ticket")
async def ticket_claim(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message(f"{e('cross')} This is not a ticket.", ephemeral=True)
    record["claimed_by"] = interaction.user.id
    save_tickets()
    await interaction.response.send_message(f"{e('check')} Claimed by {interaction.user.mention}.")


@ticket_group.command(name="rename", description="Rename the current ticket")
async def ticket_rename(interaction, name: str):
    await bot.rename_ticket(interaction, name)


@ticket_group.command(name="ai", description="Enable or disable AI in the current ticket")
@app_commands.describe(state="on or off")
async def ticket_ai(interaction, state: str):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message(f"{e('cross')} This is not a ticket.", ephemeral=True)
    state = state.lower().strip()
    if state not in {"on", "off"}:
        return await interaction.response.send_message("Use `on` or `off`.", ephemeral=True)
    record["ai_enabled"] = state == "on"
    save_tickets()
    await interaction.response.send_message(f"{e('check')} AI is now **{state}**.", ephemeral=True)


@ticket_group.command(name="summary", description="Generate an AI summary")
async def ticket_summary(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message(f"{e('cross')} This is not a ticket.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        text = await bot.ai_summary(record)
        await interaction.followup.send(text[:4000], ephemeral=True)
    except Exception as exc:
        log.exception("Summary failed")
        await interaction.followup.send(f"{e('cross')} Summary failed: `{type(exc).__name__}`", ephemeral=True)


@ticket_group.command(name="list", description="List open tickets")
async def ticket_list(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    rows = [r for r in TICKETS.values() if r.get("guild_id") == interaction.guild_id and not r.get("closed")]
    if not rows:
        return await interaction.response.send_message("No open tickets.", ephemeral=True)
    lines = [f"<#{r['channel_id']}> — `{r['category']}` — `{r['priority']}` — <@{r['user_id']}>" for r in rows[:30]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# Compatibility commands for existing installations.
@bot.tree.command(name="ticketpanel", description="Send the configured ArveX ticket panel")
async def legacy_ticketpanel(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message(f"{e('cross')} Staff only.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    await send_ticket_panel(interaction)
    await interaction.followup.send(f"{e('check')} Ticket panel sent.", ephemeral=True)


@bot.tree.command(name="close", description="Close the current ticket")
async def legacy_close(interaction):
    await bot.close_ticket(interaction)


@bot.tree.command(name="claim", description="Claim the current ticket")
async def legacy_claim(interaction):
    await ticket_claim(interaction)


@bot.tree.command(name="summary", description="Generate an AI summary of the current ticket")
async def legacy_summary(interaction):
    await ticket_summary(interaction)


@bot.tree.command(name="tickets", description="List open ArveX tickets")
async def legacy_tickets(interaction):
    await ticket_list(interaction)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN is missing. Put it in the environment or config.json.")
    bot.run(TOKEN)
