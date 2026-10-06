"""
Metal Drops - Staff Application Ticket Bot
Single-file, no database. Discord (channel topics) is the source of truth.
"""

import os
import io
import re
import random
import string
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# =========================================================================
# CONFIG SECTION - EDIT THESE
# =========================================================================

SERVER_NAME = "Metal Drops"

TICKET_CATEGORY_ID = 1557069934293426216        # ID of the category where ticket channels go
STAFF_ROLE_IDS = [1545782832004206592]                             # <-- PUT YOUR STAFF/MANAGER ROLE ID(S) HERE, e.g. [123456789012345678]
LOG_CHANNEL_ID = 1556724009025150996           # Channel for transcripts
ACCEPTED_ROLE_ID = 0                            # Role given on accept, 0 = none
ANSWER_TIMEOUT_MINUTES = 15
PANEL_BANNER_URL = ""                           # Optional: URL to a banner image for the panel embed

POSITIONS = {
    "ticket_manager": {
        "id": "ticket_manager",
        "name": "Ticket manager",
        "description": (
            "Manage support tickets, assists members, and ensures every issue "
            "is handled efficiently and professionally."
        ),
        "questions": [
            "What is your Discord username?",
            "How old are you?",
            "What timezone are you primarily in?",
            "How many hours can you be active per day on Metal Drops?",
            "Have you ever been a ticket helper in similar drop servers like ours?",
            "Do you have any experience being a ticket manager/helper?",
            "Are you capable of handling many tickets?",
            "What is your opinion on Metal Drops?",
            "Why do you want to become a part of the Metal Drops team?",
            "Any message for the reviewer?",
        ],
    },
}

# =========================================================================
# LOGGING
# =========================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("metaldrops")

# =========================================================================
# COLORS
# =========================================================================

COLOR_BLURPLE = 0x5865F2
COLOR_GREEN = 0x57F287
COLOR_RED = 0xED4245
COLOR_ORANGE = 0xE67E22

# =========================================================================
# IN-MEMORY STATE
# =========================================================================

user_locks: dict[int, asyncio.Lock] = {}
interview_tasks: dict[int, asyncio.Task] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        user_locks[user_id] = lock
    return lock


# =========================================================================
# HELPERS
# =========================================================================

def generate_app_id() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=4))


def build_topic(applicant_id: int, position_id: str, app_id: str, status: str) -> str:
    return f"applicant:{applicant_id} | position:{position_id} | app:{app_id} | status:{status}"


def parse_topic(topic: str):
    if not topic:
        return None
    try:
        data = {}
        for part in topic.split("|"):
            part = part.strip()
            if ":" not in part:
                continue
            key, _, value = part.partition(":")
            data[key.strip()] = value.strip()
        if not all(k in data for k in ("applicant", "position", "app", "status")):
            return None
        return {
            "applicant": int(data["applicant"]),
            "position": data["position"],
            "app": data["app"],
            "status": data["status"],
        }
    except Exception:
        return None


def sanitize_channel_name(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9-]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        name = "user"
    return name[:80]


def check_bot_permissions(guild: discord.Guild) -> list[str]:
    me = guild.me
    if me is None:
        return ["Unknown (bot member not cached)"]
    perms = me.guild_permissions
    required = {
        "Manage Channels": perms.manage_channels,
        "Manage Roles": perms.manage_roles,
        "View Channel": perms.view_channel,
        "Send Messages": perms.send_messages,
        "Embed Links": perms.embed_links,
        "Attach Files": perms.attach_files,
        "Read Message History": perms.read_message_history,
    }
    return [name for name, ok in required.items() if not ok]


def is_staff(member: discord.Member) -> bool:
    try:
        if member.guild_permissions.manage_guild:
            return True
        role_ids = {r.id for r in member.roles}
        return any(rid in role_ids for rid in STAFF_ROLE_IDS)
    except Exception:
        return False


def no_permission_embed() -> discord.Embed:
    embed = discord.Embed(
        description=(
            "🚫 **No Permission**\n"
            "Only **Metal Drops Managers/Admins** can perform this action."
        ),
        color=COLOR_RED,
    )
    embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")
    return embed


async def find_user_ticket(guild: discord.Guild, user_id: int):
    category = guild.get_channel(TICKET_CATEGORY_ID)
    if not isinstance(category, discord.CategoryChannel):
        return None
    for ch in category.channels:
        if not isinstance(ch, discord.TextChannel):
            continue
        info = parse_topic(ch.topic or "")
        if info and info["applicant"] == user_id:
            return ch
    return None


async def safe_delete_channel(channel: discord.abc.GuildChannel, reason: str = ""):
    try:
        await channel.delete(reason=reason)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        logger.exception("Failed to delete channel %s", getattr(channel, "id", "?"))
    except Exception:
        logger.exception("Unexpected error deleting channel")


async def upload_transcript_to_log(guild: discord.Guild | None, app_id: str, transcript_text: str):
    if not guild:
        return
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        logger.warning("LOG_CHANNEL_ID not found or invalid")
        return
    try:
        file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{app_id}.txt")
        await log_channel.send(content=f"📄 Transcript for application #{app_id}", file=file)
    except Exception:
        logger.exception("Failed to upload transcript to log channel")


async def send_decision_dm(member, app_id: str, position_name: str, accepted: bool,
                            reason: str | None, transcript_text: str) -> bool:
    if not member:
        return False
    try:
        if accepted:
            desc = (
                f"🎉 Your application **#{app_id}** for **{position_name}** in **{SERVER_NAME}** "
                f"was **ACCEPTED**! Welcome aboard! 🥳"
            )
            color = COLOR_GREEN
        else:
            desc = (
                f"❌ Your application **#{app_id}** for **{position_name}** in **{SERVER_NAME}** "
                f"was **DENIED**.\n**Reason:** {reason or 'No reason provided.'}"
            )
            color = COLOR_RED
        embed = discord.Embed(description=desc, color=color)
        embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")
        file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{app_id}.txt")
        await member.send(embed=embed, file=file)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
    except Exception:
        logger.exception("Unexpected error sending decision DM")
        return False


async def send_transcript_dm(member, app_id: str, transcript_text: str) -> bool:
    if not member:
        return False
    try:
        embed = discord.Embed(
            description=f"📄 Here is the transcript for your application **#{app_id}** in **{SERVER_NAME}**.",
            color=COLOR_BLURPLE,
        )
        embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")
        file = discord.File(io.BytesIO(transcript_text.encode("utf-8")), filename=f"transcript-{app_id}.txt")
        await member.send(embed=embed, file=file)
        return True
    except (discord.Forbidden, discord.HTTPException):
        return False
    except Exception:
        logger.exception("Unexpected error sending transcript DM")
        return False


# =========================================================================
# BOT
# =========================================================================

intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.members = True


class MetalDropsBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix="!",
            intents=intents,
            chunk_guild_at_startup=False,
            member_cache_flags=discord.MemberCacheFlags.none(),
            max_messages=None,
        )

    async def setup_hook(self):
        self.add_view(PanelView())
        self.add_view(ReviewView())
        self.add_view(CloseTicketView())
        try:
            await self.tree.sync()
        except Exception:
            logger.exception("Failed to sync application commands")


bot = MetalDropsBot()


# =========================================================================
# APPLY FLOW
# =========================================================================

async def handle_apply(interaction: discord.Interaction, position_id: str):
    position = POSITIONS.get(position_id)
    if not position:
        try:
            await interaction.response.send_message("This position is no longer available.", ephemeral=True)
        except Exception:
            pass
        return

    guild = interaction.guild
    user = interaction.user
    if guild is None or not isinstance(user, discord.Member):
        try:
            await interaction.response.send_message("This can only be used in the server.", ephemeral=True)
        except Exception:
            pass
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    lock = get_user_lock(user.id)
    async with lock:
        try:
            existing = await find_user_ticket(guild, user.id)
        except Exception:
            logger.exception("Error scanning for existing ticket")
            existing = None

        if existing:
            try:
                await interaction.followup.send(
                    f"You already have an open application: <#{existing.id}>", ephemeral=True
                )
            except Exception:
                pass
            return

        missing = check_bot_permissions(guild)
        if missing:
            try:
                await interaction.followup.send(
                    f"⚠️ The bot is missing required permissions: {', '.join(missing)}. "
                    "Please contact an administrator.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        category = guild.get_channel(TICKET_CATEGORY_ID)
        if not isinstance(category, discord.CategoryChannel):
            try:
                await interaction.followup.send(
                    "⚠️ The ticket category is not configured correctly. Please contact an administrator.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            user: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True, attach_files=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_messages=True,
                manage_channels=True,
            ),
        }
        for rid in STAFF_ROLE_IDS:
            role = guild.get_role(rid)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True, manage_messages=True
                )

        app_id = generate_app_id()
        topic = build_topic(user.id, position_id, app_id, "interviewing")
        channel_name = f"application-{sanitize_channel_name(user.name)}"

        try:
            channel = await guild.create_text_channel(
                name=channel_name,
                category=category,
                overwrites=overwrites,
                topic=topic,
                reason=f"Application ticket for {user} ({user.id})",
            )
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to create ticket channel")
            try:
                await interaction.followup.send(
                    "⚠️ Failed to create your application ticket. Please contact an administrator.",
                    ephemeral=True,
                )
            except Exception:
                pass
            return

        try:
            await interaction.followup.send(
                f"📋 **Interview Started.** Your private ticket is ready: <#{channel.id}>", ephemeral=True
            )
        except Exception:
            pass

        task = asyncio.create_task(run_interview(channel, user, position_id, app_id))
        interview_tasks[user.id] = task


async def run_interview(channel: discord.TextChannel, user: discord.Member, position_id: str, app_id: str):
    position = POSITIONS[position_id]
    try:
        staff_mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS)
        intro = discord.Embed(
            title=f"📝 Application Started: {SERVER_NAME}",
            description=(
                f"You are applying for **{position['name']}**.\n"
                f"There are **10 questions**.\n"
                f"Please answer each question below. You have **{ANSWER_TIMEOUT_MINUTES} minutes** "
                f"per question. Type `cancel` to abort."
            ),
            color=COLOR_BLURPLE,
        )
        intro.set_footer(text=f"{SERVER_NAME} • Staff Applications")
        try:
            await channel.send(
                content=f"{user.mention} {staff_mentions}".strip(),
                embed=intro,
                allowed_mentions=discord.AllowedMentions(users=True, roles=True),
            )
        except Exception:
            logger.exception("Failed to send intro message")

        answers = []
        total = len(position["questions"])

        for idx, question in enumerate(position["questions"], start=1):
            q_embed = discord.Embed(description=f"**Question {idx}/{total}:** {question}", color=COLOR_BLURPLE)
            q_embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")
            try:
                await channel.send(embed=q_embed)
            except Exception:
                logger.exception("Failed to send question")

            while True:
                def check(m: discord.Message):
                    return m.channel.id == channel.id and m.author.id == user.id

                try:
                    msg = await bot.wait_for("message", check=check, timeout=ANSWER_TIMEOUT_MINUTES * 60)
                except asyncio.TimeoutError:
                    try:
                        await channel.send("⏰ You took too long to respond. This application has been cancelled.")
                    except Exception:
                        pass
                    await asyncio.sleep(10)
                    await safe_delete_channel(channel, "Interview timeout")
                    return

                content = (msg.content or "").strip()

                if content.lower() == "cancel":
                    try:
                        await channel.send("🚫 Application cancelled.")
                    except Exception:
                        pass
                    await asyncio.sleep(10)
                    await safe_delete_channel(channel, "Applicant cancelled")
                    return

                if not content:
                    try:
                        await channel.send(
                            "Please provide a text answer (attachments alone are not accepted). Try again:"
                        )
                    except Exception:
                        pass
                    continue

                if len(content) > 1000:
                    content = content[:1000]

                answers.append((question, content))
                break

        await post_summary_and_pending(channel, user, position_id, app_id, answers)

    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled error during interview for %s", user.id)
    finally:
        interview_tasks.pop(user.id, None)


def build_summary_embeds(user: discord.Member, position: dict, answers: list, app_id: str):
    embeds = []
    current = discord.Embed(
        title=f"📋 Application Summary — #{app_id}",
        description=f"Applicant: {user.mention}\nPosition: **{position['name']}**",
        color=COLOR_BLURPLE,
    )
    current.set_footer(text=f"{SERVER_NAME} • Staff Applications")
    current_len = len(current.title or "") + len(current.description or "")

    for idx, (q, a) in enumerate(answers, start=1):
        name = f"Q{idx}: {q}"[:256]
        value = (a or "No answer")[:1024]
        field_len = len(name) + len(value)

        if current_len + field_len > 5500 or len(current.fields) >= 24:
            embeds.append(current)
            current = discord.Embed(
                title=f"📋 Application Summary — #{app_id} (cont.)", color=COLOR_BLURPLE
            )
            current.set_footer(text=f"{SERVER_NAME} • Staff Applications")
            current_len = len(current.title or "")

        current.add_field(name=name, value=value, inline=False)
        current_len += field_len

    embeds.append(current)
    return embeds


async def post_summary_and_pending(channel: discord.TextChannel, user: discord.Member, position_id: str,
                                    app_id: str, answers: list):
    position = POSITIONS[position_id]
    embeds = build_summary_embeds(user, position, answers, app_id)
    for e in embeds:
        try:
            await channel.send(embed=e)
        except Exception:
            logger.exception("Failed to send summary embed")

    try:
        new_topic = build_topic(user.id, position_id, app_id, "pending")
        await channel.edit(topic=new_topic)
    except Exception:
        logger.exception("Failed to update channel topic to pending")

    staff_mentions = " ".join(f"<@&{rid}>" for rid in STAFF_ROLE_IDS)

    final_embed = discord.Embed(
        title="📥 Application Ready for Review",
        description=(
            f"Your application (**#{app_id}**) has been received by **{SERVER_NAME}** staff! 🎉\n\n"
            f"📌 Our managers will review it shortly.\n"
            f"📬 You will be notified via **DM** once a decision is made.\n\n"
            f"Thank you for applying to **{SERVER_NAME}**! 🤘"
        ),
        color=COLOR_ORANGE,
    )
    final_embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")
    try:
        await channel.send(
            content=staff_mentions if staff_mentions else None,
            embed=final_embed,
            view=ReviewView(),
            allowed_mentions=discord.AllowedMentions(roles=True),
        )
    except Exception:
        logger.exception("Failed to post pending message with review buttons")


# =========================================================================
# REVIEW FLOW
# =========================================================================

async def handle_review(interaction: discord.Interaction, accepted: bool):
    member = interaction.user
    if not isinstance(member, discord.Member) or not is_staff(member):
        try:
            await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        except Exception:
            pass
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        try:
            await interaction.response.send_message("Invalid channel.", ephemeral=True)
        except Exception:
            pass
        return

    info = parse_topic(channel.topic or "")
    if not info:
        try:
            await interaction.response.send_message(
                "Couldn't read application data from this channel's topic.", ephemeral=True
            )
        except Exception:
            pass
        return

    original_message = interaction.message

    if accepted:
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await finalize_review(interaction, channel, info, True, None, original_message)
    else:
        modal = DenyReasonModal(channel, info, original_message)
        try:
            await interaction.response.send_modal(modal)
        except Exception:
            logger.exception("Failed to open deny modal")


async def finalize_review(interaction: discord.Interaction, channel: discord.TextChannel, info: dict,
                           accepted: bool, reason: str | None, original_message: discord.Message | None):
    guild = interaction.guild
    applicant_id = info["applicant"]
    position_id = info["position"]
    app_id = info["app"]
    position = POSITIONS.get(position_id, {"name": "Unknown Position", "description": ""})

    member = guild.get_member(applicant_id) if guild else None
    if member is None and guild:
        try:
            member = await guild.fetch_member(applicant_id)
        except (discord.NotFound, discord.HTTPException):
            member = None

    if original_message:
        try:
            view = discord.ui.View.from_message(original_message)
            for item in view.children:
                item.disabled = True
            await original_message.edit(view=view)
        except Exception:
            logger.exception("Failed to disable review buttons")

    try:
        new_status = "accepted" if accepted else "denied"
        new_topic = build_topic(applicant_id, position_id, app_id, new_status)
        await channel.edit(topic=new_topic)
    except Exception:
        logger.exception("Failed to update topic after decision")

    if accepted and ACCEPTED_ROLE_ID and member and guild:
        try:
            role = guild.get_role(ACCEPTED_ROLE_ID)
            if role:
                await member.add_roles(role, reason=f"Application #{app_id} accepted")
        except Exception:
            logger.exception("Failed to add accepted role")

    decision_label = "ACCEPTED" if accepted else "DENIED"
    transcript_text = await build_transcript_text(
        channel, member, applicant_id, position, app_id, decision_label, reason, interaction.user
    )

    await upload_transcript_to_log(guild, app_id, transcript_text)
    dm_sent = await send_decision_dm(member, app_id, position["name"], accepted, reason, transcript_text)

    mention = member.mention if member else f"<@{applicant_id}>"
    result_lines = [
        f"{mention}'s application has been **{decision_label}** by {interaction.user.mention}.",
        "",
    ]
    if dm_sent:
        result_lines.append("📄 A full transcript has been sent to your DMs.")
    else:
        result_lines.append("⚠️ We couldn't DM you — please contact staff for details.")
    if not accepted and reason:
        result_lines.append(f"**Reason:** {reason}")

    result_embed = discord.Embed(
        title="✅ Application Accepted" if accepted else "❌ Application Denied",
        description="\n".join(result_lines),
        color=COLOR_GREEN if accepted else COLOR_RED,
    )
    result_embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")

    try:
        await channel.send(
            content=mention,
            embed=result_embed,
            view=CloseTicketView(),
            allowed_mentions=discord.AllowedMentions(users=True),
        )
    except Exception:
        logger.exception("Failed to post decision message")

    try:
        await interaction.followup.send(f"Application #{app_id} marked as {decision_label}.", ephemeral=True)
    except Exception:
        pass


class DenyReasonModal(discord.ui.Modal, title="Deny Application"):
    reason_input = discord.ui.TextInput(
        label="Reason (optional)",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
        placeholder="Why is this application being denied?",
    )

    def __init__(self, channel: discord.TextChannel, info: dict, original_message: discord.Message | None):
        super().__init__()
        self.channel = channel
        self.info = info
        self.original_message = original_message

    async def on_submit(self, interaction: discord.Interaction):
        try:
            await interaction.response.defer()
        except Exception:
            pass
        await finalize_review(interaction, self.channel, self.info, False, self.reason_input.value, self.original_message)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        logger.exception("Deny modal error", exc_info=error)


async def build_transcript_text(channel: discord.TextChannel, member, applicant_id: int, position: dict,
                                 app_id: str, decision_label: str, reason: str | None, reviewer):
    lines = []
    lines.append(f"{SERVER_NAME} Application Transcript")
    lines.append("=" * 50)
    lines.append(f"Application ID: #{app_id}")
    lines.append(f"Position: {position.get('name', 'Unknown')}")
    if member:
        lines.append(f"Applicant: {member} ({applicant_id})")
    else:
        lines.append(f"Applicant ID: {applicant_id} (not found in server)")
    lines.append("")
    lines.append("Questions & Answers:")
    lines.append("-" * 50)

    qa_found = False
    try:
        async for msg in channel.history(limit=300, oldest_first=True):
            if msg.embeds:
                for embed in msg.embeds:
                    if embed.title and "Application Summary" in embed.title:
                        for field in embed.fields:
                            qa_found = True
                            lines.append(field.name)
                            lines.append(field.value)
                            lines.append("")
    except Exception:
        logger.exception("Failed to read channel history for transcript")

    if not qa_found:
        lines.append("(No answers recorded.)")

    lines.append("-" * 50)
    lines.append(f"Decision: {decision_label}")
    lines.append(f"Reviewer: {reviewer} ({getattr(reviewer, 'id', 'unknown')})")
    if reason:
        lines.append(f"Reason: {reason}")

    return "\n".join(lines)


# =========================================================================
# UI VIEWS
# =========================================================================

class ApplyButton(discord.ui.Button):
    def __init__(self, position_id: str, position_name: str):
        super().__init__(
            label=f"🎫 Apply: {position_name}",
            style=discord.ButtonStyle.blurple,
            custom_id=f"apply:{position_id}",
        )
        self.position_id = position_id

    async def callback(self, interaction: discord.Interaction):
        await handle_apply(interaction, self.position_id)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for pid, pdata in POSITIONS.items():
            self.add_item(ApplyButton(pid, pdata["name"]))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logger.exception("PanelView error", exc_info=error)
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("An error occurred.", ephemeral=True)
        except Exception:
            pass


class ReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.green, custom_id="review_accept")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_review(interaction, True)

    @discord.ui.button(label="❌ Deny", style=discord.ButtonStyle.red, custom_id="review_deny")
    async def deny_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_review(interaction, False)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logger.exception("ReviewView error", exc_info=error)


class CloseTicketView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.gray, custom_id="close_ticket_btn")
    async def close_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member) or not is_staff(member):
            try:
                await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
            except Exception:
                pass
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            try:
                await interaction.response.send_message("Invalid channel.", ephemeral=True)
            except Exception:
                pass
            return

        button.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:
            try:
                if not interaction.response.is_done():
                    await interaction.response.defer()
            except Exception:
                pass

        try:
            await channel.send(f"🗑️ **{member.mention} is closing this ticket in 10 seconds...**")
        except Exception:
            pass

        await asyncio.sleep(10)
        await safe_delete_channel(channel, f"Ticket closed by {member}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        logger.exception("CloseTicketView error", exc_info=error)


# =========================================================================
# SLASH COMMANDS
# =========================================================================

@bot.tree.command(name="panel", description="Post the staff application panel")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def panel(interaction: discord.Interaction):
    guild = interaction.guild

    description_parts = [
        "✨ **Join the Metal Drops Team!** ✨",
        "",
        "We're always on the lookout for dedicated, passionate members to help our "
        "community grow and thrive! 🚀",
        "",
        "📜 **How it works:**",
        "1️⃣ Choose a position below\n"
        "2️⃣ Answer 10 quick questions in your own private ticket\n"
        "3️⃣ Our managers carefully review your application\n"
        "4️⃣ Get notified via **DM** with the final result 📬",
        "",
        "⚠️ *Please answer honestly — misleading submissions will be disqualified.*",
        "",
        "**📌 Available Positions:**",
    ]
    for pdata in POSITIONS.values():
        description_parts.append("")
        description_parts.append(f"> 🎫 **Position:** {pdata['name']}")
        description_parts.append(f"> *{pdata['description']}*")
        description_parts.append(
            "> ⚠️ *Take your time and answer honestly. Misleading submissions will result in disqualification.*"
        )

    embed = discord.Embed(
        title="🤘 Metal Drops — Staff Applications 🤘",
        description="\n".join(description_parts),
        color=COLOR_BLURPLE,
    )
    if guild and guild.icon:
        try:
            embed.set_thumbnail(url=guild.icon.url)
        except Exception:
            pass
    if PANEL_BANNER_URL:
        embed.set_image(url=PANEL_BANNER_URL)
    embed.set_footer(text=f"{SERVER_NAME} • Staff Applications")

    try:
        await interaction.response.send_message(embed=embed, view=PanelView())
    except Exception:
        logger.exception("Failed to send panel")
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("Failed to post the panel.", ephemeral=True)
        except Exception:
            pass


@panel.error
async def panel_error(interaction: discord.Interaction, error: Exception):
    if isinstance(error, app_commands.MissingPermissions):
        try:
            await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        except Exception:
            pass
    else:
        logger.exception("panel command error", exc_info=error)


@bot.tree.command(name="closeticket", description="Force-close this application ticket (staff only)")
@app_commands.guild_only()
async def closeticket(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or not is_staff(member):
        try:
            await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        except Exception:
            pass
        return

    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        try:
            await interaction.response.send_message("This command must be used in a ticket channel.", ephemeral=True)
        except Exception:
            pass
        return

    info = parse_topic(channel.topic or "")
    if not info:
        try:
            await interaction.response.send_message(
                "This isn't a recognized application ticket channel.", ephemeral=True
            )
        except Exception:
            pass
        return

    try:
        await interaction.response.send_message("🔒 Closing this ticket and generating a transcript...")
    except Exception:
        pass

    guild = interaction.guild
    applicant_id = info["applicant"]
    position = POSITIONS.get(info["position"], {"name": "Unknown Position"})

    task = interview_tasks.pop(applicant_id, None)
    if task and not task.done():
        task.cancel()

    gmember = guild.get_member(applicant_id) if guild else None
    if gmember is None and guild:
        try:
            gmember = await guild.fetch_member(applicant_id)
        except Exception:
            gmember = None

    transcript_text = await build_transcript_text(
        channel, gmember, applicant_id, position, info["app"], "CLOSED (force closed by staff)",
        "Force closed by staff", member
    )
    await upload_transcript_to_log(guild, info["app"], transcript_text)
    await send_transcript_dm(gmember, info["app"], transcript_text)

    try:
        await channel.send("🗑️ Deleting this ticket in 10 seconds...")
    except Exception:
        pass
    await asyncio.sleep(10)
    await safe_delete_channel(channel, "Force closed by staff")


@bot.tree.command(name="diagnose", description="Check bot config (staff only)")
@app_commands.guild_only()
async def diagnose(interaction: discord.Interaction):
    member = interaction.user
    if not isinstance(member, discord.Member) or not is_staff(member):
        try:
            await interaction.response.send_message(embed=no_permission_embed(), ephemeral=True)
        except Exception:
            pass
        return

    guild = interaction.guild
    lines = []

    lines.append(f"**TICKET_CATEGORY_ID:** `{TICKET_CATEGORY_ID}` (type: {type(TICKET_CATEGORY_ID).__name__})")
    cat = guild.get_channel(TICKET_CATEGORY_ID)
    if cat is None:
        lines.append("❌ Category NOT FOUND in this server. Check the ID is correct and belongs to this server.")
    elif not isinstance(cat, discord.CategoryChannel):
        lines.append(f"❌ Found a channel but it's a **{type(cat).__name__}**, not a category: `{cat.name}`")
    else:
        lines.append(f"✅ Category found: **{cat.name}**")

    lines.append(f"\n**LOG_CHANNEL_ID:** `{LOG_CHANNEL_ID}`")
    log_ch = guild.get_channel(LOG_CHANNEL_ID)
    lines.append("✅ Found" if log_ch else "❌ NOT FOUND")

    lines.append(f"\n**STAFF_ROLE_IDS:** `{STAFF_ROLE_IDS}`")
    if not STAFF_ROLE_IDS:
        lines.append("⚠️ EMPTY — only users with Manage Server permission will be treated as staff!")
    for rid in STAFF_ROLE_IDS:
        role = guild.get_role(rid)
        lines.append(f"- `{rid}` → {'✅ ' + role.name if role else '❌ NOT FOUND'}")

    missing_perms = check_bot_permissions(guild)
    lines.append(f"\n**Bot permissions:** {'✅ All present' if not missing_perms else '❌ Missing: ' + ', '.join(missing_perms)}")

    embed = discord.Embed(title="🔧 Metal Drops Config Diagnostic", description="\n".join(lines), color=COLOR_BLURPLE)
    try:
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: Exception):
    logger.exception("App command error", exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send("An error occurred while processing this command.", ephemeral=True)
        else:
            await interaction.response.send_message("An error occurred while processing this command.", ephemeral=True)
    except Exception:
        pass


# =========================================================================
# EVENTS
# =========================================================================

async def cleanup_stale_ticket(channel: discord.TextChannel):
    try:
        await channel.send("Bot restarted, please apply again.")
    except Exception:
        pass
    await asyncio.sleep(10)
    await safe_delete_channel(channel, "Stale interview after restart")


@bot.event
async def on_ready():
    logger.info("Logged in as %s (%s)", bot.user, getattr(bot.user, "id", "?"))
    try:
        await bot.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="Metal Drops applications")
        )
    except Exception:
        logger.exception("Failed to set presence")

    category = bot.get_channel(TICKET_CATEGORY_ID)
    if isinstance(category, discord.CategoryChannel):
        for ch in list(category.channels):
            if not isinstance(ch, discord.TextChannel):
                continue
            info = parse_topic(ch.topic or "")
            if not info:
                continue
            if info["status"] == "interviewing":
                asyncio.create_task(cleanup_stale_ticket(ch))
    logger.info("Startup scan complete")


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel):
    if not isinstance(channel, discord.TextChannel):
        return
    info = parse_topic(channel.topic or "")
    if info:
        uid = info["applicant"]
        task = interview_tasks.pop(uid, None)
        if task and not task.done():
            task.cancel()
        user_locks.pop(uid, None)


@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    try:
        channel = await find_user_ticket(guild, member.id)
    except Exception:
        channel = None

    if channel:
        info = parse_topic(channel.topic or "")
        try:
            await channel.send(f"🚪 {member} left {SERVER_NAME}. Closing this ticket.")
        except Exception:
            pass

        log_channel = guild.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                app_id = info["app"] if info else "unknown"
                await log_channel.send(f"🚪 Application ticket for {member} (left server) closed: #{app_id}")
            except Exception:
                pass

        task = interview_tasks.pop(member.id, None)
        if task and not task.done():
            task.cancel()

        await safe_delete_channel(channel, "Applicant left the server")


# =========================================================================
# HEALTH SERVER (for Render)
# =========================================================================

async def start_health_server():
    app = web.Application()

    async def handle(request):
        return web.Response(text="OK")

    app.router.add_get("/", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server listening on port %s", port)


# =========================================================================
# ENTRYPOINT
# =========================================================================

async def main():
    await start_health_server()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        logger.error("DISCORD_TOKEN environment variable is not set. Exiting.")
        return
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down.")
