import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import random
import string
import sys
from datetime import datetime, timedelta
from getpass import getpass
from typing import Optional

from dotenv import load_dotenv, set_key

# ==================== CONFIG ====================
load_dotenv()
ALLOWED_CHANNEL_ID = 1549326367089229844  # Only this channel can use commands
ADMIN_ROLE_ID = 792434859099684875       # Role that can use admin commands
OWNER_IDS = [792434859099684875]          # Your Discord user ID(s) — will be tagged when stock is empty

SERVER_FILE = "server.txt"                # One invite/link per line
DATA_FILE = "bot_data.json"               # Stores user slots, used links, claim data
INVITE_REQUIREMENT = 4                    # How many invites needed for /claim vps
SLOT_COOLDOWN_DAYS = 15                   # Days before user can reclaim after deploy ends
DEFAULT_DAYS = 30
INVITE_TIMEOUT_HOURS = 24  # Timeout applied when a user tries !server deploy after only 2 invite joins

# VPS Specs shown to user
VPS_SPECS = {
    "ram": "4 GB",
    "storage": "100 GB",
    "network": "6 Gbps",
}

# ==================== DATA HELPERS ====================

def load_data() -> dict:
    if not os.path.exists(DATA_FILE):
        return {
            "users": {},           # user_id: {last_deploy, cooldown_until, active, os, days, link}
            "used_links": [],      # already given links
            "claims": {},          # user_id: {invite_code, invite_url, uses, completed, code, invited_members}
            "codes": {}            # code: {user_id, created_at, claimer_id}
        }
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # Ensure new keys exist for older data files
    data.setdefault("users", {})
    data.setdefault("used_links", [])
    data.setdefault("claims", {})
    data.setdefault("codes", {})
    return data


def save_data(data: dict):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, default=str)


def load_server_links() -> list[str]:
    if not os.path.exists(SERVER_FILE):
        open(SERVER_FILE, "w").close()
        return []
    with open(SERVER_FILE, "r", encoding="utf-8") as f:
        links = [line.strip() for line in f if line.strip()]
    return links


def save_server_links(links: list[str]):
    with open(SERVER_FILE, "w", encoding="utf-8") as f:
        for link in links:
            f.write(link + "\n")


def get_available_link(data: dict) -> Optional[str]:
    """Return first unused link and remove it from server.txt"""
    links = load_server_links()
    used = set(data.get("used_links", []))
    for link in links:
        if link not in used:
            links.remove(link)
            save_server_links(links)
            data.setdefault("used_links", []).append(link)
            save_data(data)
            return link
    return None


def generate_code() -> str:
    """Generate unique VASTx + random alphanumeric code"""
    chars = string.ascii_uppercase + string.digits
    suffix = "".join(random.choices(chars, k=8))
    return f"VASTx{suffix}"


def is_admin(interaction: discord.Interaction) -> bool:
    if interaction.user.id in OWNER_IDS:
        return True
    if ADMIN_ROLE_ID and interaction.guild:
        role = interaction.guild.get_role(ADMIN_ROLE_ID)
        if role and role in interaction.user.roles:
            return True
    return interaction.user.guild_permissions.administrator


def channel_check():
    """Commands only work in ALLOWED_CHANNEL_ID"""
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.channel_id != ALLOWED_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ This command can only be used in the designated channel.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


def owner_mentions() -> str:
    """Return mention string for all owners"""
    return " ".join(f"<@{oid}>" for oid in OWNER_IDS)


# ==================== BOT SETUP ====================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.invites = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Cache for invite tracking: guild_id -> {code: uses}
invite_cache: dict[int, dict[str, int]] = {}


# ==================== EVENTS ====================

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    for guild in bot.guilds:
        try:
            invites = await guild.invites()
            invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}
        except Exception as e:
            print(f"Could not cache invites for {guild.name}: {e}")

    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.event
async def on_member_join(member: discord.Member):
    """
    Track which invite was used.
    Anti-abuse: the same Discord user (by ID) can only count once for a claimer.
    Join → leave → join again will NOT increase the count.
    """
    guild = member.guild
    data = load_data()

    try:
        invites = await guild.invites()
    except Exception:
        return

    old = invite_cache.get(guild.id, {})
    used_invite = None

    for inv in invites:
        old_uses = old.get(inv.code, 0)
        if inv.uses > old_uses:
            used_invite = inv
            break

    # Update cache always
    invite_cache[guild.id] = {inv.code: inv.uses for inv in invites}

    if not used_invite:
        return

    # Find the claim that owns this invite code
    for claimer_id, claim in data.get("claims", {}).items():
        if claim.get("invite_code") != used_invite.code:
            continue
        if claim.get("completed"):
            continue

        # --- Anti-abuse: check if this member was already counted ---
        invited_list = claim.setdefault("invited_members", [])  # list of user IDs (str)

        member_id_str = str(member.id)

        if member_id_str in invited_list:
            # Already counted before (join/leave farm) → ignore
            try:
                claimer = await bot.fetch_user(int(claimer_id))
                embed = discord.Embed(
                    title="⚠️ Invite Not Counted",
                    description=(
                        f"**{member.display_name}** (`{member.name}`) joined again using your link,\n"
                        f"but this user was **already counted** before.\n\n"
                        f"Join / Leave / Join farming is not allowed.\n"
                        f"Current progress stays: `{len(invited_list)}/{INVITE_REQUIREMENT}`"
                    ),
                    color=0xe67e22
                )
                await claimer.send(embed=embed)
            except Exception:
                pass
            return

        # New unique member → count them
        invited_list.append(member_id_str)
        claim["uses"] = len(invited_list)
        save_data(data)

        remaining = max(0, INVITE_REQUIREMENT - claim["uses"])

        try:
            claimer = await bot.fetch_user(int(claimer_id))

            progress_embed = discord.Embed(
                title="🎉 Invite Progress!",
                description=(
                    f"**{member.display_name}** (`{member.name}`) joined using your invite!\n\n"
                    f"**Progress:** `{claim['uses']}/{INVITE_REQUIREMENT}`\n"
                    f"**Remaining:** `{remaining}`\n\n"
                    f"This user is now locked to your claim (cannot be re-counted)."
                ),
                color=0x00ff99
            )

            if claim["uses"] >= INVITE_REQUIREMENT:
                # Generate unique code
                code = generate_code()
                while code in data.get("codes", {}):
                    code = generate_code()

                now_iso = datetime.utcnow().isoformat()
                claim["completed"] = True
                claim["code"] = code
                claim["completed_at"] = now_iso

                data["codes"][code] = {
                    "user_id": claimer_id,
                    "created_at": now_iso,
                    "claimer_id": claimer_id,
                    "invited_members": list(invited_list)
                }
                save_data(data)

                progress_embed.title = "✅ Claim Completed!"
                progress_embed.description = (
                    f"You successfully invited **{INVITE_REQUIREMENT}** unique members!\n\n"
                    f"Your redeem code has been generated.\n"
                    f"Check your DMs for the full details."
                )
                progress_embed.color = 0x00ff00

                # Full DM with code + instructions
                dm_embed = discord.Embed(
                    title="🚀 VPS Claim Successful!",
                    description=(
                        f"Congratulations **{claimer.display_name}**!\n\n"
                        f"You completed **{INVITE_REQUIREMENT}** unique invites.\n\n"
                        f"**Your Redeem Code:**\n```\n{code}\n```\n"
                        f"**How to receive your VPS:**\n"
                        f"1. Open a support ticket\n"
                        f"2. Paste this code: `{code}`\n"
                        f"3. Tell us which **country** or **Indian state** you want\n"
                        f"4. Our team will provision your VPS shortly\n\n"
                        f"**VPS Specifications:**\n"
                        f"• RAM: **{VPS_SPECS['ram']}**\n"
                        f"• Storage: **{VPS_SPECS['storage']}**\n"
                        f"• Network: **{VPS_SPECS['network']}**\n"
                    ),
                    color=0x00ff00,
                    timestamp=datetime.utcnow()
                )
                dm_embed.set_footer(text="Keep this code private • Do not share it")
                await claimer.send(embed=dm_embed)

            await claimer.send(embed=progress_embed)
        except Exception:
            pass
        break


@bot.event
async def on_member_remove(member: discord.Member):
    """
    Optional: we do NOT decrease the count when someone leaves.
    The invited_members list keeps the ID forever so they cannot be re-counted.
    This is intentional anti-farm protection.
    """
    pass



# ==================== EASY PREFIX COMMAND ====================

@bot.group(name="server", invoke_without_command=True)
async def server_group(ctx):
    """Easy entry point: !server deploy"""
    await ctx.send("Usage: `!server deploy [os] [days]` — defaults to Ubuntu for 30 days.")


@server_group.command(name="deploy")
async def easy_server_deploy(ctx, os_name: str = "ubuntu", days: int = 30):
    """Easy deploy command: !server deploy (Ubuntu, 30 days by default)."""
    if ctx.channel.id != ALLOWED_CHANNEL_ID:
        await ctx.reply("❌ This command can only be used in the designated channel.", mention_author=False)
        return

    if days < 1 or days > 365:
        await ctx.reply("❌ Days must be between 1 and 365.", mention_author=False)
        return

    user_id = str(ctx.author.id)
    data = load_data()
    user_data = data.setdefault("users", {}).setdefault(user_id, {})
    now = datetime.utcnow()

    # Invite gate: only trigger this rule when claim progress is exactly 2 unique joins.
    claim = data.get("claims", {}).get(user_id, {})
    if claim and not claim.get("completed") and int(claim.get("uses", 0)) == 2:
        member = ctx.guild.get_member(ctx.author.id) if ctx.guild else None
        if member:
            try:
                until = discord.utils.utcnow() + timedelta(hours=INVITE_TIMEOUT_HOURS)
                await member.timeout(until, reason="VPS deploy attempted with only 2/4 required unique invites")
                timeout_embed = discord.Embed(
                    title="🛡️・DEPLOYMENT RESTRICTED",
                    description=(
                        f"Hey {ctx.author.mention}, your deployment request was stopped.\\n\\n"
                        f"📨 **Unique invites:** `2/{INVITE_REQUIREMENT}`\\n"
                        f"🔒 **Action:** `{INVITE_TIMEOUT_HOURS}-hour timeout`\\n\\n"
                        "Invite requirements must be completed before using this deployment route."
                    ),
                    color=0xE74C3C,
                    timestamp=discord.utils.utcnow(),
                )
                timeout_embed.set_footer(text="VASTx VPS • Anti-abuse protection")
                await ctx.reply(embed=timeout_embed, mention_author=False)
            except discord.Forbidden:
                await ctx.reply(
                    f"⛔ {ctx.author.mention}, you have only **2/{INVITE_REQUIREMENT}** unique invites. "
                    "Deployment is blocked, but I lack permission to apply a timeout.",
                    mention_author=False,
                )
        else:
            await ctx.reply("❌ I couldn't find your server member record.", mention_author=False)
        return

    cooldown_until = user_data.get("cooldown_until")
    if cooldown_until:
        try:
            cooldown_dt = datetime.fromisoformat(cooldown_until)
            if now < cooldown_dt:
                remaining = cooldown_dt - now
                await ctx.reply(
                    f"⏳ Your slot is on cooldown. Try again in **{remaining.days}d {remaining.seconds // 3600}h**.",
                    mention_author=False,
                )
                return
        except (TypeError, ValueError):
            pass

    if user_data.get("active"):
        await ctx.reply("❌ You already have an active server deployed.", mention_author=False)
        return

    link = get_available_link(data)
    if not link:
        stock_embed = discord.Embed(
            title="📦・VPS STOCK UNAVAILABLE",
            description=(
                "All server slots have been claimed for now.\\n\\n"
                f"👑 **Contact an administrator:** {owner_mentions()}\\n"
                "🔔 Please try again after new stock is added."
            ),
            color=0xF1C40F,
            timestamp=discord.utils.utcnow(),
        )
        stock_embed.set_footer(text="VASTx VPS • Stock updates coming soon")
        await ctx.reply(embed=stock_embed, mention_author=False)
        return

    expire_date = now + timedelta(days=days)
    cooldown_date = expire_date + timedelta(days=SLOT_COOLDOWN_DAYS)
    user_data.update({
        "active": True,
        "os": os_name,
        "days": days,
        "link": link,
        "deployed_at": now.isoformat(),
        "expires_at": expire_date.isoformat(),
        "cooldown_until": cooldown_date.isoformat(),
        "last_deploy": now.isoformat(),
    })
    save_data(data)

    announcement = discord.Embed(
        title="✅ Server Deployed Successfully!",
        description=(
            f"**Deployed for:** {ctx.author.mention}\n"
            f"**OS:** `{os_name}`\n"
            f"**Duration:** `{days} days`\n\n"
            "📩 **Your server details have been sent to your DMs.**\n"
            "Kindly check your private messages and give us feedback!\n\n"
            f"⏳ **Server is valid for {days} days.**\n"
            f"🔄 Slot used. You can reclaim a new server after **{SLOT_COOLDOWN_DAYS} days** from expiry."
        ),
        color=0x00FF99,
        timestamp=discord.utils.utcnow(),
    )
    announcement.set_thumbnail(url=ctx.author.display_avatar.url)
    announcement.set_footer(text="Thank you for using our service • Feedback appreciated")
    await ctx.reply(embed=announcement, mention_author=False)

    dm_embed = discord.Embed(
        title="🖥️ Your VPS Is Ready!",
        description=(
            f"Hey {ctx.author.mention}! 🎉\n\n"
            "Your server has been deployed successfully. Your private access details are below."
        ),
        color=0x5865F2,
        timestamp=discord.utils.utcnow(),
    )
    dm_embed.add_field(name="💿 Operating System", value=f"`{os_name.upper()}`", inline=True)
    dm_embed.add_field(name="⏳ Server Duration", value=f"`{days} days`", inline=True)
    dm_embed.add_field(
        name="📅 Expires (UTC)",
        value=f"`{expire_date.strftime('%d %b %Y • %H:%M')}`",
        inline=False,
    )
    dm_embed.add_field(name="🔗 Private Server Access Link", value=f"{link}", inline=False)
    dm_embed.add_field(
        name="🛡️ Important",
        value=(
            "Keep your access link private and do not share it publicly.\n"
            f"After expiry, your next claim is available after the **{SLOT_COOLDOWN_DAYS}-day cooldown**."
        ),
        inline=False,
    )
    dm_embed.set_thumbnail(url=ctx.author.display_avatar.url)
    dm_embed.set_footer(text="VASTx VPS • Need help? Contact the server admins")
    try:
        await ctx.author.send(embed=dm_embed)
    except discord.Forbidden:
        await ctx.send(f"{ctx.author.mention}, I couldn't DM you. Enable DMs or contact an admin.")



# ==================== SLASH COMMANDS ====================

@tree.command(name="deploy", description="Deploy a server (e.g. /deploy server ubuntu days 30)")
@channel_check()
@app_commands.describe(
    server="Must be the word 'server'",
    os_name="Operating system (e.g. ubuntu, debian, centos)",
    days_keyword="Must be the word 'days'",
    days="Number of days the server is valid for"
)
async def deploy(
    interaction: discord.Interaction,
    server: str,
    os_name: str,
    days_keyword: str,
    days: int
):
    # Validate format
    if server.lower() != "server":
        await interaction.response.send_message(
            "❌ Incorrect format.\n"
            "Correct usage: `/deploy server <os> days <number>`\n"
            "Example: `/deploy server ubuntu days 30`",
            ephemeral=True
        )
        return

    if days_keyword.lower() != "days":
        await interaction.response.send_message(
            "❌ Incorrect format.\n"
            "Correct usage: `/deploy server <os> days <number>`\n"
            "Example: `/deploy server ubuntu days 30`",
            ephemeral=True
        )
        return

    if days < 1 or days > 365:
        await interaction.response.send_message(
            "❌ Days must be between 1 and 365.",
            ephemeral=True
        )
        return

    user_id = str(interaction.user.id)
    data = load_data()
    user_data = data.setdefault("users", {}).setdefault(user_id, {})

    now = datetime.utcnow()

    # Cooldown check
    cooldown_until = user_data.get("cooldown_until")
    if cooldown_until:
        try:
            cooldown_dt = datetime.fromisoformat(cooldown_until)
            if now < cooldown_dt:
                remaining = cooldown_dt - now
                await interaction.response.send_message(
                    f"⏳ Your slot is on cooldown.\n"
                    f"You can reclaim a new server in **{remaining.days}d {remaining.seconds // 3600}h**.",
                    ephemeral=True
                )
                return
        except Exception:
            pass

    # Already has active deploy
    if user_data.get("active"):
        await interaction.response.send_message(
            "❌ You already have an active server deployed.\n"
            "Wait until it expires or contact an admin.",
            ephemeral=True
        )
        return

    # Get available link
    link = get_available_link(data)

    if not link:
        # === STOCK OVER ===
        owners = owner_mentions()
        embed = discord.Embed(
            title="📭 Current Stock is Over",
            description=(
                f"Sorry {interaction.user.mention}, there are **no server links left** right now.\n\n"
                f"Please tag the owner so they can add more stock:\n"
                f"{owners}\n\n"
                f"Once new links are added you can run `/deploy` again."
            ),
            color=0xe74c3c,
            timestamp=now
        )
        embed.set_footer(text="Owners have been notified in this message")
        await interaction.response.send_message(embed=embed)
        return

    # Mark as active
    expire_date = now + timedelta(days=days)
    cooldown_date = expire_date + timedelta(days=SLOT_COOLDOWN_DAYS)

    user_data.update({
        "active": True,
        "os": os_name,
        "days": days,
        "link": link,
        "deployed_at": now.isoformat(),
        "expires_at": expire_date.isoformat(),
        "cooldown_until": cooldown_date.isoformat(),
        "last_deploy": now.isoformat()
    })
    save_data(data)

    # Public message
    public_embed = discord.Embed(
        title="✅ Server Deployed Successfully!",
        description=(
            f"**Deployed for:** {interaction.user.mention}\n"
            f"**OS:** `{os_name}`\n"
            f"**Duration:** `{days} days`\n\n"
            f"📩 **Your server details have been sent to your DMs.**\n"
            f"Kindly check your private messages and give us feedback!\n\n"
            f"⏳ **Server is valid for {days} days.**\n"
            f"🔄 Slot used. You can reclaim a new server after **{SLOT_COOLDOWN_DAYS} days** from expiry."
        ),
        color=0x00ff99,
        timestamp=now
    )
    public_embed.set_footer(text="Thank you for using our service • Feedback appreciated")
    public_embed.set_thumbnail(url=interaction.user.display_avatar.url)

    await interaction.response.send_message(embed=public_embed)

    # DM details
    dm_embed = discord.Embed(
        title="🖥️ Your Server Details",
        description=(
            f"Hello **{interaction.user.display_name}**!\n\n"
            f"Your server has been successfully deployed.\n\n"
            f"**Operating System:** `{os_name}`\n"
            f"**Valid for:** `{days} days`\n"
            f"**Expires on:** `{expire_date.strftime('%Y-%m-%d %H:%M UTC')}`\n\n"
            f"**🔗 Server Access Link:**\n{link}\n\n"
            f"🛡️ Keep this link private. Do not share it publicly.\n"
            f"After expiry, your next claim is available after the **{SLOT_COOLDOWN_DAYS}-day cooldown**."
        ),
        color=0x5865F2,
        timestamp=now
    )
    dm_embed.set_thumbnail(url=interaction.user.display_avatar.url)
    dm_embed.set_footer(text="VASTx VPS • Need help? Contact the server admins")

    try:
        await interaction.user.send(embed=dm_embed)
    except discord.Forbidden:
        await interaction.followup.send(
            f"{interaction.user.mention} I couldn't DM you. Please open your DMs "
            f"or contact an admin for your link.",
            ephemeral=True
        )


@tree.command(name="add", description="[Admin] Add a new server link to the pool")
@channel_check()
@app_commands.describe(link="The server invite / access link to add")
async def add_link(interaction: discord.Interaction, link: str):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ You do not have permission to use this command.",
            ephemeral=True
        )
        return

    link = link.strip()
    if not link.startswith(("http://", "https://")):
        await interaction.response.send_message(
            "❌ Please provide a valid link starting with http:// or https://",
            ephemeral=True
        )
        return

    links = load_server_links()
    if link in links:
        await interaction.response.send_message(
            "⚠️ This link already exists in the pool.",
            ephemeral=True
        )
        return

    data = load_data()
    if link in data.get("used_links", []):
        await interaction.response.send_message(
            "⚠️ This link was already used before and cannot be re-added.",
            ephemeral=True
        )
        return

    links.append(link)
    save_server_links(links)

    embed = discord.Embed(
        title="✅ Link Added",
        description=(
            f"Successfully added new server link to the pool.\n\n"
            f"**Total available links now:** `{len(links)}`"
        ),
        color=0x00ff00
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="claim", description="Claim a free VPS by inviting 4 unique members")
@channel_check()
@app_commands.describe(vps="Must be the word 'vps'")
async def claim_vps(interaction: discord.Interaction, vps: str):
    if vps.lower() != "vps":
        await interaction.response.send_message(
            "❌ Incorrect format.\nCorrect usage: `/claim vps`",
            ephemeral=True
        )
        return

    user_id = str(interaction.user.id)
    data = load_data()
    claims = data.setdefault("claims", {})

    # Already completed
    if user_id in claims and claims[user_id].get("completed"):
        code = claims[user_id].get("code", "N/A")
        await interaction.response.send_message(
            f"✅ You already completed the claim!\n"
            f"Your code: `{code}`\n"
            f"Paste it in a support ticket to receive your VPS.",
            ephemeral=True
        )
        return

    # Already has active invite
    if user_id in claims and not claims[user_id].get("completed"):
        inv_url = claims[user_id].get("invite_url", "N/A")
        uses = claims[user_id].get("uses", 0)
        await interaction.response.send_message(
            f"📌 You already have an active claim invite.\n\n"
            f"**Your invite:** {inv_url}\n"
            f"**Progress:** `{uses}/{INVITE_REQUIREMENT}`\n\n"
            f"Invite more **unique** people using this link!",
            ephemeral=True
        )
        return

    # Create unique invite
    try:
        invite = await interaction.channel.create_invite(
            max_age=0,
            max_uses=0,
            unique=True,
            reason=f"VPS claim invite for {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ I don't have permission to create invites in this channel.\n"
            "Please contact an administrator.",
            ephemeral=True
        )
        return

    claims[user_id] = {
        "invite_code": invite.code,
        "invite_url": str(invite),
        "uses": 0,
        "completed": False,
        "created_at": datetime.utcnow().isoformat(),
        "invited_members": []          # list of unique user IDs (str)
    }
    save_data(data)

    if interaction.guild.id not in invite_cache:
        invite_cache[interaction.guild.id] = {}
    invite_cache[interaction.guild.id][invite.code] = 0

    # Public message
    public_embed = discord.Embed(
        title="🎯 VPS Claim Started!",
        description=(
            f"{interaction.user.mention} started a VPS claim!\n\n"
            f"**Requirement:** Invite **{INVITE_REQUIREMENT} unique members** "
            f"(same person joining multiple times does **not** count).\n\n"
            f"📩 Details & your personal invite link have been sent to your DMs."
        ),
        color=0xF1C40F,
        timestamp=datetime.utcnow()
    )
    public_embed.set_footer(text="Complete the invites to unlock your free VPS")

    await interaction.response.send_message(embed=public_embed)

    # DM
    dm_embed = discord.Embed(
        title="🚀 Claim Your Free VPS",
        description=(
            f"Hey **{interaction.user.display_name}**!\n\n"
            f"To receive your free VPS you must invite **{INVITE_REQUIREMENT} unique members** "
            f"using the link below.\n\n"
            f"**Your Personal Invite Link:**\n```\n{invite.url}\n```\n\n"
            f"**Progress:** `0/{INVITE_REQUIREMENT}`\n\n"
            f"⚠️ **Anti-abuse rule:** The same Discord account can only be counted **once**. "
            f"Join → Leave → Join again will **not** increase your count.\n\n"
            f"Once you reach {INVITE_REQUIREMENT} invites you will receive a unique "
            f"**VASTx** redeem code in this DM.\n\n"
            f"**After getting the code:**\n"
            f"1. Open a support ticket\n"
            f"2. Paste the code\n"
            f"3. Tell us which **country** or **Indian state** you want the VPS located in\n"
            f"4. Our team will deliver your VPS\n\n"
            f"**VPS Specifications:**\n"
            f"• **RAM:** {VPS_SPECS['ram']}\n"
            f"• **Storage:** {VPS_SPECS['storage']}\n"
            f"• **Network Speed:** {VPS_SPECS['network']}\n"
            f"• **Location:** Your choice (tell in ticket)\n"
        ),
        color=0x5865F2,
        timestamp=datetime.utcnow()
    )
    dm_embed.set_footer(text="Share only your invite link • Never share your redeem code")

    try:
        await interaction.user.send(embed=dm_embed)
    except discord.Forbidden:
        await interaction.followup.send(
            f"{interaction.user.mention} Please enable DMs from server members "
            f"so I can send you the invite link!",
            ephemeral=True
        )


@tree.command(name="keycheck", description="[Admin] Check details of a VASTx redeem code")
@channel_check()
@app_commands.describe(code="The VASTx redeem code to check")
async def keycheck(interaction: discord.Interaction, code: str):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "❌ This command is only for admins.",
            ephemeral=True
        )
        return

    code = code.strip()
    data = load_data()
    code_info = data.get("codes", {}).get(code)

    if not code_info:
        # Also check inside claims for older data
        found = None
        for uid, claim in data.get("claims", {}).items():
            if claim.get("code") == code:
                found = {
                    "user_id": uid,
                    "created_at": claim.get("completed_at") or claim.get("created_at"),
                    "invited_members": claim.get("invited_members", [])
                }
                break
        if not found:
            await interaction.response.send_message(
                f"❌ Code `{code}` not found.",
                ephemeral=True
            )
            return
        code_info = found

    user_id = code_info.get("user_id") or code_info.get("claimer_id")
    created_at = code_info.get("created_at", "Unknown")
    invited_ids = code_info.get("invited_members", [])

    # Resolve claimer
    claimer_mention = f"<@{user_id}>"
    claimer_name = "Unknown"
    try:
        claimer = await bot.fetch_user(int(user_id))
        claimer_name = f"{claimer} (`{claimer.id}`)"
        claimer_mention = claimer.mention
    except Exception:
        pass

    # Check how many invited members are still in the server
    still_in_server = 0
    left_server = 0
    member_lines = []

    guild = interaction.guild
    for mid in invited_ids:
        try:
            member = guild.get_member(int(mid))
            if member is None:
                # Try fetch (in case of cache miss)
                try:
                    member = await guild.fetch_member(int(mid))
                except discord.NotFound:
                    member = None

            if member:
                still_in_server += 1
                status = "✅ Still in server"
                name = f"{member.display_name} (`{member.name}`)"
            else:
                left_server += 1
                status = "❌ Left the server"
                name = f"ID `{mid}`"
            member_lines.append(f"• {name} — {status}")
        except Exception:
            left_server += 1
            member_lines.append(f"• ID `{mid}` — ❌ Left / Unknown")

    total_invited = len(invited_ids)

    embed = discord.Embed(
        title="🔑 Key Check Result",
        color=0x9b59b6,
        timestamp=datetime.utcnow()
    )
    embed.add_field(name="Code", value=f"`{code}`", inline=False)
    embed.add_field(name="Created On", value=f"`{created_at}`", inline=False)
    embed.add_field(name="Claimed By", value=f"{claimer_mention}\n{claimer_name}", inline=False)
    embed.add_field(
        name="Invited Members Summary",
        value=(
            f"**Total unique invites counted:** `{total_invited}`\n"
            f"**Still in server:** `{still_in_server}`\n"
            f"**Left the server:** `{left_server}`"
        ),
        inline=False
    )

    if member_lines:
        # Discord field value limit is 1024 chars
        details = "\n".join(member_lines)
        if len(details) > 1000:
            details = "\n".join(member_lines[:15]) + "\n… (truncated)"
        embed.add_field(name="Member Details", value=details, inline=False)

    embed.set_footer(text="Only admins can see this message")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="status", description="Check your deploy / claim status")
@channel_check()
async def status(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    data = load_data()
    user_data = data.get("users", {}).get(user_id, {})
    claim = data.get("claims", {}).get(user_id, {})

    embed = discord.Embed(
        title=f"📊 Status for {interaction.user.display_name}",
        color=0x3498db,
        timestamp=datetime.utcnow()
    )

    if user_data.get("active"):
        expires = user_data.get("expires_at", "Unknown")
        embed.add_field(
            name="🖥️ Active Deploy",
            value=(
                f"**OS:** `{user_data.get('os', 'N/A')}`\n"
                f"**Days:** `{user_data.get('days', 'N/A')}`\n"
                f"**Expires:** `{expires}`"
            ),
            inline=False
        )
    else:
        cooldown = user_data.get("cooldown_until")
        if cooldown:
            try:
                cd = datetime.fromisoformat(cooldown)
                if datetime.utcnow() < cd:
                    remaining = cd - datetime.utcnow()
                    embed.add_field(
                        name="⏳ Cooldown",
                        value=f"You can deploy again in **{remaining.days}d {remaining.seconds // 3600}h**",
                        inline=False
                    )
                else:
                    embed.add_field(name="🖥️ Deploy", value="Ready to deploy!", inline=False)
            except Exception:
                embed.add_field(name="🖥️ Deploy", value="Ready to deploy!", inline=False)
        else:
            embed.add_field(name="🖥️ Deploy", value="No active deploy. You can use `/deploy`", inline=False)

    if user_data:
        embed.add_field(
            name="📦 What You Claimed",
            value=(
                f"**Server active:** `{'Yes' if user_data.get('active') else 'No'}`\\n"
                f"**OS:** `{user_data.get('os', 'N/A')}`\\n"
                f"**Duration:** `{user_data.get('days', 'N/A')} days`\\n"
                f"**Expires:** `{user_data.get('expires_at', 'N/A')}`"
            ),
            inline=False
        )

    if claim:
        if claim.get("completed"):
            embed.add_field(
                name="🎁 VPS Claim",
                value=f"✅ Completed\nCode: `{claim.get('code', 'N/A')}`",
                inline=False
            )
        else:
            uses = claim.get("uses", 0)
            embed.add_field(
                name="🎁 VPS Claim",
                value=(
                    f"Progress: `{uses}/{INVITE_REQUIREMENT}`\n"
                    f"Invite: {claim.get('invite_url', 'N/A')}\n"
                    f"(Only unique users count)"
                ),
                inline=False
            )
    else:
        embed.add_field(name="🎁 VPS Claim", value="Not started. Use `/claim vps`", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="links", description="[Admin] Show how many server links are available")
@channel_check()
async def links_count(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return

    available = len(load_server_links())
    data = load_data()
    used = len(data.get("used_links", []))

    embed = discord.Embed(
        title="📦 Server Link Pool",
        description=f"**Available:** `{available}`\n**Already used:** `{used}`",
        color=0x9b59b6
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="resettimer", description="[Admin] Reset a user's deploy cooldown timer")
@channel_check()
@app_commands.describe(user="The user whose timer should be reset")
async def resettimer(interaction: discord.Interaction, user: discord.Member):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        return
    data = load_data()
    user_data = data.get("users", {}).get(str(user.id))
    if not user_data:
        await interaction.response.send_message(f"ℹ️ No deployment timer found for {user.mention}.", ephemeral=True)
        return
    user_data.pop("cooldown_until", None)
    save_data(data)
    await interaction.response.send_message(f"✅ Reset the deploy cooldown timer for {user.mention}.", ephemeral=False)


# ==================== RUN ====================

def get_bot_token() -> Optional[str]:
    """Use the configured token, or prompt and save one for local interactive runs."""
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token

    if not sys.stdin.isatty():
        print(
            "Missing DISCORD_BOT_TOKEN. Add it in your hosting service's Variables "
            "page (Railway: Service → Variables), then redeploy."
        )
        return None

    token = getpass("Enter your Discord bot token (input hidden): ").strip()
    if not token:
        print("No token entered; the bot was not started.")
        return None

    try:
        set_key(".env", "DISCORD_BOT_TOKEN", token)
    except OSError as error:
        print(f"Could not save the token to .env: {error}")
        return None
    print("Token saved to .env. Keep this file private; it is excluded from Git.")
    return token


if __name__ == "__main__":
    token = get_bot_token()
    if token:
        try:
            bot.run(token)
        except discord.LoginFailure:
            print(
                "Discord rejected the bot token. Check DISCORD_BOT_TOKEN or .env, "
                "and reset the token in the Discord Developer Portal if needed."
            )
        except discord.PrivilegedIntentsRequired:
            print(
                "Discord requires privileged intents. Enable the required intents "
                "in the Discord Developer Portal and try again."
            )
