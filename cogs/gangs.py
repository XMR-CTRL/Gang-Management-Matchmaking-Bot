import json
import os
import discord
from discord import app_commands
from discord.ext import commands
from config import ROLE_ID
from storage import load, save, user_gang, member_gang, name_exists, _load, _save


def _load_results():
    return _load("results.json")


def _save_results(data):
    return _save("results.json", data)


BLURPLE = 0x5865f2
GREEN = 0x57f287
RED = 0xed4245
DEFAULT_AVATAR = "https://cdn.discordapp.com/embed/avatars/0.png"


def _now():
    return discord.utils.utcnow()


def _author_embed(interaction: discord.Interaction, **kwargs):
    embed = discord.Embed(**kwargs)
    embed.set_author(
        name=interaction.user.display_name,
        icon_url=interaction.user.display_avatar.url,
    )
    embed.timestamp = _now()
    return embed


class CreateModal(discord.ui.Modal, title="Start a Gang"):
    name = discord.ui.TextInput(
        label="Gang name",
        placeholder="Gang name",
        max_length=32,
    )
    icon = discord.ui.TextInput(
        label="Icon URL",
        placeholder="https://...",
        required=False,
        max_length=200,
    )

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = load()
        if user_gang(uid, data)[0]:
            await interaction.response.send_message(
                "You already lead a gang.", ephemeral=True
            )
            return
        chosen_name = str(self.name).strip()
        if name_exists(chosen_name, data):
            await interaction.response.send_message(
                "A gang with that name already exists.", ephemeral=True
            )
            return
        existing_ids = [int(k) for k in data.keys() if k.isdigit()]
        gid = str(max(existing_ids, default=0) + 1)
        raw_icon = str(self.icon).strip() if self.icon else ""
        icon_url = raw_icon if raw_icon else None
        data[gid] = {
            "name": chosen_name,
            "icon": icon_url,
            "leader": uid,
            "members": [uid],
        }
        save(data)

        embed = _author_embed(
            interaction,
            title=data[gid]["name"],
            description="Gang created. Use **/gang panel** to manage it.",
            color=GREEN,
        )
        embed.set_thumbnail(
            url=icon_url or DEFAULT_AVATAR
        )
        embed.set_footer(text=f"Gang ID {gid}")
        await interaction.response.send_message(embed=embed)


class RenameModal(discord.ui.Modal, title="Rename Gang"):
    name = discord.ui.TextInput(
        label="New name",
        placeholder="New gang name",
        max_length=32,
    )

    def __init__(self, gid: str):
        super().__init__()
        self.gid = gid

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = load()
        if data.get(self.gid, {}).get("leader") != uid:
            await interaction.response.send_message(
                "You are no longer the gang leader.", ephemeral=True
            )
            return
        chosen_name = str(self.name).strip()
        if name_exists(chosen_name, data, exclude_gid=self.gid):
            await interaction.response.send_message(
                "A gang with that name already exists.", ephemeral=True
            )
            return
        old_name = data[self.gid].get("name")
        data[self.gid]["name"] = chosen_name
        save(data)
        if old_name and old_name != chosen_name:
            results = _load_results()
            if old_name in results:
                results[chosen_name] = results.pop(old_name)
                _save_results(results)
        embed = _author_embed(
            interaction,
            title="Name Updated",
            description=f"Renamed to **{self.name}**",
            color=BLURPLE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class IconModal(discord.ui.Modal, title="Change Gang Icon"):
    icon = discord.ui.TextInput(
        label="New icon URL",
        placeholder="https://...",
        max_length=200,
    )

    def __init__(self, gid: str):
        super().__init__()
        self.gid = gid

    async def on_submit(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        url = str(self.icon).strip()
        data = load()
        if data.get(self.gid, {}).get("leader") != uid:
            await interaction.response.send_message(
                "You are no longer the gang leader.", ephemeral=True
            )
            return
        data[self.gid]["icon"] = url
        save(data)
        embed = _author_embed(
            interaction,
            title="Icon Updated",
            color=BLURPLE,
        )
        embed.set_image(url=url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DisbandModal(discord.ui.Modal, title="Disband Gang"):
    confirm = discord.ui.TextInput(
        label="Type DISBAND to confirm",
        placeholder="DISBAND",
        max_length=10,
    )

    def __init__(self, gid: str):
        super().__init__()
        self.gid = gid

    async def on_submit(self, interaction: discord.Interaction):
        if str(self.confirm).strip().upper() != "DISBAND":
            await interaction.response.send_message(
                "Wrong confirmation.", ephemeral=True
            )
            return
        uid = str(interaction.user.id)
        data = load()
        if data.get(self.gid, {}).get("leader") != uid:
            await interaction.response.send_message(
                "You are no longer the gang leader.", ephemeral=True
            )
            return
        if self.gid in data:
            name = data[self.gid]["name"]
            del data[self.gid]
            save(data)
            results = _load_results()
            if name in results:
                del results[name]
                _save_results(results)
        else:
            name = "your gang"
        embed = _author_embed(
            interaction,
            title="Gang Disbanded",
            description=f"**{name}** has been disbanded.",
            color=RED,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ManageView(discord.ui.View):
    def __init__(self, gid: str):
        super().__init__(timeout=180)
        self.gid = gid

    def _load_gang(self):
        return load().get(self.gid, {})

    async def is_leader(self, interaction: discord.Interaction):
        return str(interaction.user.id) == self._load_gang().get("leader")

    async def _leader_only(self, interaction: discord.Interaction):
        if not await self.is_leader(interaction):
            await interaction.response.send_message(
                "Only the gang leader can do that.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(
        label="Rename", style=discord.ButtonStyle.primary, emoji="✏️", row=0
    )
    async def rename_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if await self._leader_only(interaction):
            await interaction.response.send_modal(RenameModal(self.gid))

    @discord.ui.button(
        label="Icon", style=discord.ButtonStyle.primary, emoji="🖼️", row=0
    )
    async def icon_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if await self._leader_only(interaction):
            await interaction.response.send_modal(IconModal(self.gid))

    @discord.ui.button(
        label="Invite", style=discord.ButtonStyle.success, emoji="➕", row=1
    )
    async def invite_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if await self._leader_only(interaction):
            await interaction.response.send_message(
                "Use **/gang add @user** to invite members.", ephemeral=True
            )

    @discord.ui.button(
        label="Kick", style=discord.ButtonStyle.secondary, emoji="➖", row=1
    )
    async def kick_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if await self._leader_only(interaction):
            await interaction.response.send_message(
                "Use **/gang remove @user** to kick members.", ephemeral=True
            )

    @discord.ui.button(
        label="Disband", style=discord.ButtonStyle.danger, emoji="🗑️", row=2
    )
    async def disband_btn(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        if await self._leader_only(interaction):
            await interaction.response.send_modal(DisbandModal(self.gid))


class GangsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    gang = app_commands.Group(name="gang", description="Gang management")

    async def _has_gang_role(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True
            )
            return False
        role = interaction.guild.get_role(ROLE_ID)
        if role is None or role not in interaction.user.roles:
            await interaction.response.send_message(
                "You need the gang role.", ephemeral=True
            )
            return False
        return True

    @gang.command(name="create", description="Create your own gang")
    async def create(self, interaction: discord.Interaction):
        if not await self._has_gang_role(interaction):
            return
        uid = str(interaction.user.id)
        data = load()
        if user_gang(uid, data)[0]:
            await interaction.response.send_message(
                "You already lead a gang.", ephemeral=True
            )
            return
        await interaction.response.send_modal(CreateModal())

    @gang.command(name="panel", description="Open your gang panel")
    async def panel(self, interaction: discord.Interaction):
        uid = str(interaction.user.id)
        data = load()
        gid, gang = user_gang(uid, data)
        if not gid:
            gid, gang = member_gang(uid, data)
        if not gid:
            await interaction.response.send_message(
                "You are not in a gang.", ephemeral=True
            )
            return

        guild = interaction.guild
        leader = guild.get_member(int(gang["leader"]))
        member_objs = [guild.get_member(int(m)) for m in gang["members"]]
        online = sum(
            1 for m in member_objs if m and m.status != discord.Status.offline
        )
        total = len(gang["members"])

        embed = discord.Embed(color=BLURPLE)
        embed.set_author(
            name=gang["name"],
            icon_url=gang["icon"] or DEFAULT_AVATAR,
        )
        embed.set_thumbnail(url=gang["icon"] or DEFAULT_AVATAR)
        embed.add_field(
            name="👑 Leader",
            value=leader.mention if leader else "Unknown",
            inline=True,
        )
        embed.add_field(
            name="👥 Members",
            value=f"{total}",
            inline=True,
        )
        embed.add_field(
            name="🟢 Online",
            value=f"{online}/{total}",
            inline=True,
        )

        results = _load_results()
        record = results.get(gang["name"], {"wins": 0, "losses": 0})
        wins = record.get("wins", 0)
        losses = record.get("losses", 0)
        total_matches = wins + losses
        win_rate = (wins / total_matches * 100) if total_matches > 0 else 0
        embed.add_field(
            name="\U0001F3C6 Record",
            value=f"{wins}W - {losses}L ({win_rate:.1f}%)",
            inline=True,
        )

        roster = []
        for member in member_objs:
            if not member:
                continue
            status = "🟢" if member.status != discord.Status.offline else "⚫"
            roster.append(f"{status} {member.mention}")

        roster_text = "\n".join(roster) if roster else "*No members found.*"
        if len(roster_text) > 1000:
            roster_text = roster_text[:997] + "..."

        embed.add_field(name="Roster", value=roster_text, inline=False)
        embed.set_footer(
            text=f"Gang ID {gid}",
            icon_url=guild.icon.url if guild.icon else None,
        )
        embed.timestamp = _now()

        view = ManageView(gid) if uid == gang["leader"] else None
        await interaction.response.send_message(
            embed=embed, view=view, ephemeral=True
        )

    @gang.command(name="add", description="Invite a player to your gang")
    async def add(self, interaction: discord.Interaction, user: discord.Member):
        uid = str(interaction.user.id)
        data = load()
        gid, gang = user_gang(uid, data)
        if not gid:
            await interaction.response.send_message(
                "You do not lead a gang.", ephemeral=True
            )
            return
        sid = str(user.id)
        if sid in gang["members"]:
            await interaction.response.send_message(
                "That user is already in your gang.", ephemeral=True
            )
            return
        gang["members"].append(sid)
        save(data)
        embed = _author_embed(
            interaction,
            title="Member Joined",
            description=f"Added {user.mention} to **{gang['name']}**",
            color=GREEN,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @gang.command(name="remove", description="Remove a player from your gang")
    async def remove(
        self, interaction: discord.Interaction, user: discord.Member
    ):
        uid = str(interaction.user.id)
        data = load()
        gid, gang = user_gang(uid, data)
        if not gid:
            await interaction.response.send_message(
                "You do not lead a gang.", ephemeral=True
            )
            return
        sid = str(user.id)
        if sid == gang["leader"]:
            await interaction.response.send_message(
                "You cannot remove yourself. Disband the gang instead.",
                ephemeral=True,
            )
            return
        if sid not in gang["members"]:
            await interaction.response.send_message(
                "That player is not in your gang.", ephemeral=True
            )
            return
        gang["members"].remove(sid)
        save(data)
        embed = _author_embed(
            interaction,
            title="Member Removed",
            description=f"Removed {user.mention} from **{gang['name']}**",
            color=RED,
        )
        embed.set_thumbnail(url=user.display_avatar.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @gang.command(name="name", description="Change your gang name")
    async def name_cmd(self, interaction: discord.Interaction, name: str):
        uid = str(interaction.user.id)
        data = load()
        gid, gang = user_gang(uid, data)
        if not gid:
            await interaction.response.send_message(
                "You do not lead a gang.", ephemeral=True
            )
            return
        chosen_name = name.strip()
        if name_exists(chosen_name, data, exclude_gid=gid):
            await interaction.response.send_message(
                "A gang with that name already exists.", ephemeral=True
            )
            return
        old_name = gang.get("name")
        gang["name"] = chosen_name
        save(data)
        if old_name and old_name != chosen_name:
            results = _load_results()
            if old_name in results:
                results[chosen_name] = results.pop(old_name)
                _save_results(results)
        embed = _author_embed(
            interaction,
            title="Name Updated",
            description=f"Renamed to **{name}**",
            color=BLURPLE,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @gang.command(name="icon", description="Change your gang icon")
    async def icon_cmd(self, interaction: discord.Interaction, url: str):
        uid = str(interaction.user.id)
        data = load()
        gid, gang = user_gang(uid, data)
        if not gid:
            await interaction.response.send_message(
                "You do not lead a gang.", ephemeral=True
            )
            return
        gang["icon"] = url.strip()
        save(data)
        embed = _author_embed(
            interaction,
            title="Icon Updated",
            color=BLURPLE,
        )
        embed.set_image(url=url)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GangsCog(bot))