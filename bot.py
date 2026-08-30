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
from staff_application import StaffApplicationManager

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
STAFF_APP_CATEGORY = CONFIG.get("staff_application_category_id")
STAFF_REVIEW_CHANNEL_ID = int(CONFIG.get("staff_application_review_channel_id", 0) or 0)


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
    return isinstance(member, discord.Member) and (any(r.id in STAFF_ROLE_IDS for r in member.roles) or member.guild_permissions.manage_channels)


def record_for(channel_id):
    return TICKETS.get(str(channel_id))


def category_config(key):
    for item in CONFIG.get("categories", []):
        if item.get("id") == key:
            return item
    return {"id": key, "name": key.title(), "emoji": e(key) or e("ticket"), "description": "General support."}


def resolve_emoji(value):
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
        self.staff_apps = StaffApplicationManager(CONFIG)

    async def setup_hook(self):
        self.add_view(TicketPanelView())
        self.add_view(TicketControlView())
        for channel_id, record in TICKETS.items():
            if record.get("category") == "staff_application" and record.get("staff_application_evaluation"):
                self.add_view(StaffReviewView(record.get("user_id", 0), int(channel_id)))
        for guild_id in GUILD_IDS:
            try:
                guild = discord.Object(id=guild_id)
                global_commands = self.tree.get_commands()
                self.tree.clear_commands(guild=guild)
                for command in global_commands:
                    self.tree.add_command(command, guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d commands to guild %s", len(synced), guild_id)
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
            record.setdefault("messages", []).append({"author_id": message.author.id, "author": str(message.author), "content": message.content[:4000], "timestamp": ts()})
            record["messages"] = record["messages"][-1000:]
            save_tickets()
            if record.get("category") == "staff_application" and not is_staff(message.author):
                await self.handle_staff_application(message, record)
            elif record.get("ai_enabled", AI_ENABLED_DEFAULT) and not is_staff(message.author):
                await self.ai_reply(message, record)
        await self.process_commands(message)

    async def handle_staff_application(self, message, record):
        state = self.staff_apps.state(message.channel.id)
        if not state:
            state = self.staff_apps.start(message.channel.id, record["user_id"])
            saved = record.get("staff_application_answers", {})
            if saved:
                state["answers"] = dict(saved)
                state["question_index"] = len(saved)
            state["completed"] = record.get("staff_application_status") == "submitted"
        if state.get("completed"):
            return
        ok, next_question = self.staff_apps.accept_answer(message.channel.id, message.content)
        if not ok:
            return
        record["staff_application"] = True
        record["staff_application_answers"] = dict(state.get("answers", {}))
        record["staff_application_status"] = "in_progress"
        save_tickets()
        if next_question:
            await message.channel.send(next_question, allowed_mentions=discord.AllowedMentions.none())
            return
        await self.complete_staff_application(message, record, state)

    async def complete_staff_application(self, message, record, state):
        record["staff_application_status"] = "submitted"
        record["staff_application_answers"] = state.get("answers", {})
        state["submitted"] = True
        save_tickets()
        await message.channel.send("Your staff application has been completed and submitted for review. A human ArveX Hosting staff member will make the final decision.", allowed_mentions=discord.AllowedMentions.none())
        if not self.groq:
            await self.safe_staff_alert(message.channel, record)
            return
        try:
            prompt = self.staff_apps.build_evaluation_prompt(state)
            async with message.channel.typing():
                response = await self.groq.chat.completions.create(
                    model=AI_MODEL,
                    temperature=0.2,
                    max_tokens=900,
                    messages=[
                        {"role": "system", "content": "You are an internal recruitment assistant for ArveX Hosting. Evaluate only the supplied application evidence. Never make a final hiring decision. Never infer protected or sensitive personal attributes."},
                        {"role": "user", "content": prompt},
                    ],
                )
            evaluation = (response.choices[0].message.content or "No evaluation generated.").strip()
            record["staff_application_evaluation"] = evaluation
            save_tickets()
            target = self.get_channel(STAFF_REVIEW_CHANNEL_ID)
            if isinstance(target, discord.TextChannel):
                applicant = message.guild.get_member(record["user_id"]) or message.author
                emb = self.staff_apps.build_review_embed(applicant, evaluation)
                await target.send(embed=emb, view=StaffReviewView(record["user_id"], message.channel.id), allowed_mentions=discord.AllowedMentions(users=True))
            await self.safe_staff_alert(message.channel, record)
        except Exception:
            log.exception("Staff application evaluation failed")
            await self.safe_staff_alert(message.channel, record)
            await message.channel.send("The application was saved successfully, but the automated review could not be generated. Human staff have been notified.", allowed_mentions=discord.AllowedMentions.none())

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
            system = CONFIG.get("ai_system_prompt", "")
            system += "\n\nLANGUAGE RULE: Reply in the same language/style as the latest customer message."
            system += f"\n\nCOMPANY KNOWLEDGE:\n{CONFIG.get('knowledge_base', '')}\n\nRULES:\n" + "\n".join(f"- {x}" for x in CONFIG.get("ai_rules", []))
            try:
                async with message.channel.typing():
                    response = await self.groq.chat.completions.create(
                        model=AI_MODEL,
                        temperature=float(CONFIG.get("ai_temperature", 0.35)),
                        max_tokens=int(CONFIG.get("ai_max_tokens", 700)),
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": f"Ticket category: {record.get('category', 'general')}\nConversation:\n{context}\n\nReply naturally to the latest customer message. Do not use an embed, headings, or a formal report format."},
                        ],
                    )
                text = (response.choices[0].message.content or "").strip()
                if not text:
                    return
                await message.channel.send(text[:3900], allowed_mentions=discord.AllowedMentions.none())
                record.setdefault("messages", []).append({"author_id": self.user.id, "author": f"{BRAND} AI", "content": text[:3900], "timestamp": ts()})
                save_tickets()
            except Exception:
                log.exception("Groq request failed")
                await message.channel.send("I’m having trouble processing that right now. A support member has been notified.", allowed_mentions=discord.AllowedMentions.none())
                await self.safe_staff_alert(message.channel, record)

    async def safe_staff_alert(self, channel, record):
        mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS)
        if mentions and not record.get("alerted"):
            record["alerted"] = True
            save_tickets()
            await channel.send(f"{mentions} Staff attention requested.", allowed_mentions=discord.AllowedMentions(roles=True))

    async def create_ticket(self, interaction, category="general", subject=""):
        guild = interaction.guild
        if not guild:
            return await interaction.response.send_message("Tickets can only be opened inside a server.", ephemeral=True)
        existing = [r for r in TICKETS.values() if r.get("guild_id") == guild.id and r.get("user_id") == interaction.user.id and not r.get("closed")]
        if len(existing) >= int(CONFIG.get("max_open_tickets_per_user", 2)):
            return await interaction.response.send_message("You already have the maximum number of open tickets.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        cat = category_config(category)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False), interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)}
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True, attach_files=True)
        category_obj = guild.get_channel(TICKET_CATEGORY_ID) if TICKET_CATEGORY_ID else None
        if category == "staff_application" and STAFF_APP_CATEGORY:
            try:
                staff_cat = guild.get_channel(int(STAFF_APP_CATEGORY))
                if isinstance(staff_cat, discord.CategoryChannel):
                    category_obj = staff_cat
            except (TypeError, ValueError):
                pass
        channel_name = f"staff-apply-{safe_name(interaction.user.display_name)}-{str(interaction.user.id)[-4:]}" if category == "staff_application" else f"ticket-{safe_name(interaction.user.display_name)}-{str(interaction.user.id)[-4:]}"
        channel = await guild.create_text_channel(channel_name, category=category_obj if isinstance(category_obj, discord.CategoryChannel) else None, overwrites=overwrites, reason="ArveX ticket opened")
        record = {"channel_id": channel.id, "guild_id": guild.id, "user_id": interaction.user.id, "user_name": str(interaction.user), "category": category, "subject": subject, "created_at": ts(), "ai_enabled": AI_ENABLED_DEFAULT, "priority": "normal", "claimed_by": None, "closed": False, "escalated": False, "messages": []}
        TICKETS[str(channel.id)] = record
        save_tickets()
        await channel.send(content=interaction.user.mention, embed=self.ticket_welcome_embed(interaction.user, cat, subject), view=TicketControlView(), allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send(f"Your ticket is ready: {channel.mention}", ephemeral=True)
        await self.log_ticket("opened", record, channel)
        if category == "staff_application":
            state = self.staff_apps.start(channel.id, interaction.user.id)
            question = self.staff_apps.next_question(channel.id)
            if question:
                await channel.send("Welcome to the ArveX Hosting Staff Application. Please answer each question honestly and with as much relevant detail as possible.\n\n" + question, allowed_mentions=discord.AllowedMentions.none())

    def ticket_welcome_embed(self, user, cat, subject=""):
        emb = base_embed(f"{resolve_emoji(cat.get('emoji'))} {cat.get('name', 'Support')}", CONFIG.get("ticket_welcome_description", "Welcome! Please describe your issue below."))
        emb.add_field(name="Customer", value=user.mention, inline=True)
        emb.add_field(name="Category", value=cat.get("name", "General Support"), inline=True)
        emb.add_field(name="AI Support", value="Enabled" if AI_ENABLED_DEFAULT else "Disabled", inline=True)
        if subject:
            emb.add_field(name="Subject", value=subject[:1024], inline=False)
        if CONFIG.get("ticket_welcome_image"):
            emb.set_image(url=CONFIG["ticket_welcome_image"])
        if CONFIG.get("ticket_welcome_thumbnail"):
            emb.set_thumbnail(url=CONFIG["ticket_welcome_thumbnail"])
        return emb

    async def log_ticket(self, action, record, channel=None):
        target = self.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if not isinstance(target, discord.TextChannel):
            return
        emb = base_embed(f"Ticket {action.title()}")
        emb.add_field(name="User", value=f"<@{record.get('user_id')}>", inline=True)
        emb.add_field(name="Category", value=record.get("category", "general"), inline=True)
        emb.add_field(name="Channel", value=channel.mention if channel else str(record.get("channel_id")), inline=True)
        await target.send(embed=emb)

    async def close_ticket(self, interaction, delete=True):
        record = record_for(interaction.channel.id)
        if not record:
            return await interaction.response.send_message("This is not an ArveX ticket.", ephemeral=True)
        if not is_staff(interaction.user) and interaction.user.id != record.get("user_id"):
            return await interaction.response.send_message("You cannot close this ticket.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        record["closed"] = True
        record["closed_at"] = ts()
        save_tickets()
        await self.send_transcript(record, interaction.channel)
        await self.log_ticket("closed", record, interaction.channel)
        await interaction.channel.send(f"Ticket closed by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions(users=True))
        await interaction.followup.send("Ticket closed.", ephemeral=True)
        if delete:
            await asyncio.sleep(3)
            try:
                await interaction.channel.delete(reason="ArveX ticket closed")
            except discord.HTTPException:
                pass

    async def rename_ticket(self, interaction, name):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        new_name = safe_name(name)
        if not new_name.startswith("ticket-") and not new_name.startswith("staff-apply-"):
            new_name = "ticket-" + new_name
        await interaction.channel.edit(name=new_name, reason="ArveX ticket renamed")
        record["channel_name"] = new_name
        save_tickets()
        await interaction.response.send_message(f"Ticket renamed to `{new_name}`.", ephemeral=True)

    async def send_transcript(self, record, channel):
        lines = [f"{BRAND} Ticket Transcript", f"Channel: #{channel.name}", f"User: {record.get('user_name')}", f"Category: {record.get('category')}", "", "--- Conversation ---"]
        for m in record.get("messages", []):
            when = datetime.fromtimestamp(m["timestamp"], timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            lines.append(f"[{when}] {m['author']}: {m['content']}")
        target = self.get_channel(LOG_CHANNEL_ID) if LOG_CHANNEL_ID else None
        if isinstance(target, discord.TextChannel):
            await target.send(content=f"Transcript — `{channel.name}`", file=discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"{channel.name}.txt"))

    async def ai_summary(self, record):
        if not self.groq:
            return "Groq is not configured."
        convo = "\n".join(f"{m['author']}: {m['content']}" for m in record.get("messages", [])[-40:])
        r = await self.groq.chat.completions.create(model=AI_MODEL, temperature=0.2, max_tokens=600, messages=[{"role": "system", "content": "Summarize this support ticket for a human support agent. Include issue, requested outcome, important facts, and next action."}, {"role": "user", "content": convo}])
        return (r.choices[0].message.content or "No summary generated.").strip()


class StaffReviewView(discord.ui.View):
    def __init__(self, applicant_id: int, channel_id: int):
        super().__init__(timeout=None)
        self.applicant_id = applicant_id
        self.channel_id = channel_id

    async def decision(self, interaction, decision: str):
        if not owner_or_staff(interaction):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        record = record_for(self.channel_id)
        if not record:
            return await interaction.response.send_message("Application ticket record was not found.", ephemeral=True)
        record["staff_final_decision"] = decision
        record["staff_decided_by"] = interaction.user.id
        record["staff_decided_at"] = ts()
        save_tickets()
        await interaction.response.send_message(f"Application marked **{decision}** by {interaction.user.mention}.", allowed_mentions=discord.AllowedMentions(users=True))
        channel = bot.get_channel(self.channel_id)
        if isinstance(channel, discord.TextChannel):
            text = {"Approved": "Your ArveX Hosting staff application has been approved. A staff member will contact you with the next steps.", "Rejected": "Thank you for applying to ArveX Hosting. After review, we will not be proceeding with this application at this time.", "Hold": "Your application has been placed on hold for further human review."}[decision]
            await channel.send(text, allowed_mentions=discord.AllowedMentions.none())

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="arvex:staff_approve")
    async def approve(self, interaction, button):
        await self.decision(interaction, "Approved")

    @discord.ui.button(label="Hold", style=discord.ButtonStyle.secondary, custom_id="arvex:staff_hold")
    async def hold(self, interaction, button):
        await self.decision(interaction, "Hold")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="arvex:staff_reject")
    async def reject(self, interaction, button):
        await self.decision(interaction, "Rejected")


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        options = [discord.SelectOption(label=c.get("name", c.get("id", "General"))[:100], value=c.get("id", "general"), description=c.get("description", "Support")[:100], emoji=resolve_emoji(c.get("emoji"))) for c in CONFIG.get("categories", [])]
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

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.secondary, custom_id="arvex:claim")
    async def claim(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        record["claimed_by"] = interaction.user.id
        save_tickets()
        await interaction.response.send_message(f"Ticket claimed by {interaction.user.mention}.")

    @discord.ui.button(label="AI", style=discord.ButtonStyle.primary, custom_id="arvex:ai_toggle")
    async def ai_toggle(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        record["ai_enabled"] = not record.get("ai_enabled", True)
        save_tickets()
        await interaction.response.send_message(f"AI support {'enabled' if record['ai_enabled'] else 'disabled'}.", ephemeral=True)

    @discord.ui.button(label="Rename", style=discord.ButtonStyle.secondary, custom_id="arvex:rename")
    async def rename(self, interaction, button):
        if not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.send_modal(RenameModal())

    @discord.ui.button(label="Summary", style=discord.ButtonStyle.secondary, custom_id="arvex:summary")
    async def summary(self, interaction, button):
        record = record_for(interaction.channel.id)
        if not record or not is_staff(interaction.user):
            return await interaction.response.send_message("Staff only.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.followup.send((await bot.ai_summary(record))[:4000], ephemeral=True)
        except Exception as exc:
            log.exception("Summary failed")
            await interaction.followup.send(f"AI summary failed: `{type(exc).__name__}`", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, custom_id="arvex:close")
    async def close(self, interaction, button):
        await bot.close_ticket(interaction)


class RenameModal(discord.ui.Modal, title="Rename Ticket"):
    name = discord.ui.TextInput(label="New ticket name", placeholder="e.g. billing-refund", max_length=80, required=True)
    async def on_submit(self, interaction):
        await bot.rename_ticket(interaction, str(self.name.value))


class TicketGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="ticket", description="ArveX Hosting ticket system")


bot = TicketBot()
ticket_group = TicketGroup()
bot.tree.add_command(ticket_group)


def owner_or_staff(interaction):
    return interaction.user.id == OWNER_ID or is_staff(interaction.user)


@ticket_group.command(name="open", description="Open a support ticket")
@app_commands.describe(category="Ticket category", subject="Optional subject")
async def ticket_open(interaction, category: str = "general", subject: str = ""):
    await bot.create_ticket(interaction, category, subject)


@ticket_group.command(name="staffapply", description="Open an ArveX Hosting staff application")
async def ticket_staffapply(interaction):
    await bot.create_ticket(interaction, "staff_application", "ArveX Hosting Staff Application")


@ticket_group.command(name="close", description="Close the current ticket")
async def ticket_close(interaction):
    await bot.close_ticket(interaction)


@ticket_group.command(name="claim", description="Claim the current ticket")
async def ticket_claim(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    record["claimed_by"] = interaction.user.id
    save_tickets()
    await interaction.response.send_message(f"Claimed by {interaction.user.mention}.")


@ticket_group.command(name="rename", description="Rename the current ticket")
async def ticket_rename(interaction, name: str):
    await bot.rename_ticket(interaction, name)


@ticket_group.command(name="ai", description="Enable or disable AI in the current ticket")
@app_commands.describe(state="on or off")
async def ticket_ai(interaction, state: str):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    state = state.lower().strip()
    if state not in {"on", "off"}:
        return await interaction.response.send_message("Use `on` or `off`.", ephemeral=True)
    record["ai_enabled"] = state == "on"
    save_tickets()
    await interaction.response.send_message(f"AI is now **{state}**.", ephemeral=True)


@ticket_group.command(name="summary", description="Generate an AI summary")
async def ticket_summary(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    record = record_for(interaction.channel.id)
    if not record:
        return await interaction.response.send_message("This is not a ticket.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.followup.send((await bot.ai_summary(record))[:4000], ephemeral=True)
    except Exception as exc:
        log.exception("Summary failed")
        await interaction.followup.send(f"Summary failed: `{type(exc).__name__}`", ephemeral=True)


@ticket_group.command(name="list", description="List open tickets")
async def ticket_list(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    rows = [r for r in TICKETS.values() if r.get("guild_id") == interaction.guild_id and not r.get("closed")]
    if not rows:
        return await interaction.response.send_message("No open tickets.", ephemeral=True)
    lines = [f"<#{r['channel_id']}> — `{r['category']}` — `{r['priority']}` — <@{r['user_id']}>" for r in rows[:30]]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="ticketpanel", description="Send the configured ArveX ticket panel")
async def ticketpanel(interaction):
    if not owner_or_staff(interaction):
        return await interaction.response.send_message("Staff only.", ephemeral=True)
    emb = base_embed(CONFIG.get("panel_title", "ArveX Support Center"), CONFIG.get("panel_description", "Select a category below to open a private support ticket."))
    if CONFIG.get("panel_image"):
        emb.set_image(url=CONFIG["panel_image"])
    await interaction.response.send_message("Ticket panel sent.", ephemeral=True)
    await interaction.channel.send(embed=emb, view=TicketPanelView())


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
