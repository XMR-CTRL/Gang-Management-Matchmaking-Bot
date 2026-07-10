import asyncio
import json
import os
import discord
from discord import app_commands
from discord.ext import commands
from config import ROLE_ID, ADMIN_ROLE_ID, LOG_CHANNEL_ID
from storage import load as load_gangs, save, user_gang, member_gang, name_exists, _load, _save


BLURPLE = 0x5865f2
GREEN = 0x57f287
RED = 0xed4245
GOLD = 0xffd700
MATCH_TIMEOUT = 60
CHALLENGE_TIMEOUT = 300
TICKET_CATEGORY_NAME = "Match Tickets"

MATCH_SIZES = ["4v4", "5v5", "6v6", "7v7"]


def _load_results():
    return _load("results.json")


def _save_results(data):
    return _save("results.json", data)


async def _get_ticket_category(guild: discord.Guild):
    for channel in guild.channels:
        if (
            isinstance(channel, discord.CategoryChannel)
            and channel.name == TICKET_CATEGORY_NAME
        ):
            return channel
    try:
        return await guild.create_category(
            TICKET_CATEGORY_NAME,
            reason="Auto-created for gang matchmaking tickets",
        )
    except discord.HTTPException as exc:
        for channel in guild.channels:
            if (
                isinstance(channel, discord.CategoryChannel)
                and channel.name == TICKET_CATEGORY_NAME
            ):
                return channel
        raise exc


class MemberSelectView(discord.ui.View):
    def __init__(self, cog, gid: str, guild: discord.Guild, user_id: int, callback, required: int = 0):
        super().__init__(timeout=180)
        self.cog = cog
        self.gid = gid
        self.user_id = user_id
        self.callback = callback
        select = MemberSelect(cog, gid, guild, user_id, callback, required=required)
        if select.options:
            self.add_item(select)
        elif required == 0:
            self.add_item(SoloButton(callback))


class SoloButton(discord.ui.Button):
    def __init__(self, callback):
        super().__init__(
            label="Solo (no members)",
            style=discord.ButtonStyle.primary,
            emoji="\U0001F64B",
        )
        self._callback = callback

    async def callback(self, interaction: discord.Interaction):
        await self._callback(interaction, [])


class SizeSelectView(discord.ui.View):
    def __init__(self, callback):
        super().__init__(timeout=180)
        self.add_item(SizeSelect(callback))


class SizeSelect(discord.ui.Select):
    def __init__(self, callback):
        self._callback = callback
        options = [
            discord.SelectOption(label=size, value=size)
            for size in MATCH_SIZES
        ]
        super().__init__(
            placeholder="Pick a match size...",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        await self._callback(interaction, self.values[0])


class MemberSelect(discord.ui.Select):
    def __init__(self, cog, gid: str, guild: discord.Guild, user_id: int, callback, required: int = 0):
        self.cog = cog
        self.gid = gid
        self.user_id = user_id
        self._callback = callback
        uid = str(user_id)
        options = []
        gang = load_gangs().get(gid, {})
        for mid in gang.get("members", []):
            if mid == uid:
                continue
            member = guild.get_member(int(mid))
            name = member.display_name if member else f"User {mid}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=mid,
                    description="Bring them to the match",
                )
            )
        if len(options) > 25:
            options = options[:25]
        if required > 0:
            min_values = required
            max_values = required
            placeholder = f"Pick {required} member(s)..."
        else:
            min_values = 0
            max_values = max(1, len(options))
            placeholder = "Pick your match members (or none)..."
        if not options and required > 0:
            options = [discord.SelectOption(label="Not enough members", value="none", default=True)]
        super().__init__(
            placeholder=placeholder,
            min_values=min_values,
            max_values=max_values,
            options=options if options else [discord.SelectOption(label="Solo", value="solo")],
        )

    async def callback(self, interaction: discord.Interaction):
        values = [v for v in self.values if v != "solo"]
        await interaction.response.defer(ephemeral=True)
        await self._callback(interaction, values)


class TeamSelectView(discord.ui.View):
    def __init__(self, cog, interaction: discord.Interaction):
        super().__init__(timeout=180)
        select = TeamSelect(cog, interaction)
        if select.options:
            self.add_item(select)
        else:
            self.add_item(NoTeamsButton())


class NoTeamsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(
            label="No gangs available",
            style=discord.ButtonStyle.secondary,
            disabled=True,
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "No other gangs are available to challenge.", ephemeral=True
        )


class TeamSelect(discord.ui.Select):
    def __init__(self, cog, interaction: discord.Interaction):
        self.cog = cog
        self.interaction = interaction
        options = []
        data = load_gangs()
        uid = str(interaction.user.id)
        for gid, gang in data.items():
            if gang.get("leader") == uid:
                continue
            if not gang.get("leader"):
                continue
            leader = interaction.guild.get_member(int(gang.get("leader", 0)))
            leader_name = leader.display_name if leader else "Unknown"
            options.append(
                discord.SelectOption(
                    label=gang.get("name", "Unknown")[:100],
                    value=gid,
                    description=f"Leader: {leader_name}",
                )
            )
        if len(options) > 25:
            options = options[:25]
        super().__init__(
            placeholder="Pick a gang to challenge...",
            min_values=1,
            max_values=1,
            options=options if options else [discord.SelectOption(label="None", value="none")],
        )

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            await interaction.response.send_message(
                "No other gangs are available to challenge.", ephemeral=True
            )
            return
        await self.cog._on_team_chosen(self.interaction, interaction, self.values[0])


class AcceptChallengeView(discord.ui.View):
    def __init__(self, cog, challenge_id: str):
        super().__init__(timeout=CHALLENGE_TIMEOUT)
        self.cog = cog
        self.challenge_id = challenge_id

    async def on_timeout(self):
        challenge = self.cog.challenges.get(self.challenge_id)
        if challenge:
            await self.cog._cleanup_challenge(
                challenge, "Challenge timed out. You can challenge again."
            )

    async def _record(self, interaction: discord.Interaction, accepted: bool):
        challenge = self.cog.challenges.get(self.challenge_id)
        if not challenge:
            await interaction.response.send_message(
                "This challenge has expired.", ephemeral=True
            )
            return False
        if str(interaction.user.id) != challenge["defender_id"]:
            await interaction.response.send_message(
                "This challenge is not for you.", ephemeral=True
            )
            return False
        challenge["accepted"] = accepted
        await interaction.response.defer(ephemeral=False)
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._record(interaction, True):
            await self.cog._on_challenge_accepted(interaction, self.challenge_id)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u274C")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._record(interaction, False):
            challenge = self.cog.challenges.get(self.challenge_id)
            if challenge:
                await self.cog._cleanup_challenge(
                    challenge, "Challenge declined."
                )
            await interaction.followup.send(
                "Challenge declined.", ephemeral=False
            )


class AcceptMatchView(discord.ui.View):
    def __init__(self, cog, leader_id: str, opponent_name: str):
        super().__init__(timeout=MATCH_TIMEOUT)
        self.cog = cog
        self.leader_id = leader_id
        self.opponent_name = opponent_name

    async def on_timeout(self):
        pending = self.cog.pending.get(self.leader_id)
        if pending is not None and pending.get("accepted") is None:
            pending["accepted"] = False

    async def _record(self, interaction: discord.Interaction, accepted: bool):
        if str(interaction.user.id) != self.leader_id:
            await interaction.response.send_message(
                "This is not your match request.", ephemeral=True
            )
            return False
        pending = self.cog.pending.get(self.leader_id)
        if not pending:
            await interaction.response.send_message(
                "This match request has expired.", ephemeral=True
            )
            return False
        pending["accepted"] = accepted
        dm = interaction.guild_id is None
        await interaction.response.send_message(
            "Match accepted." if accepted else "Match declined.",
            ephemeral=not dm,
        )
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="\u2705")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, True)

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="\u274C")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record(interaction, False)


class JoinQueueView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def _can_use(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Use this in a server.", ephemeral=True
            )
            return False
        role = interaction.guild.get_role(ROLE_ID)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "You need the gang role for matchmaking.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="Join Queue", style=discord.ButtonStyle.success, emoji="\u2694\uFE0F"
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_use(interaction):
            return
        uid = str(interaction.user.id)
        gid, gang = self.cog._find_gang(interaction.user.id)
        if not gid:
            await interaction.response.send_message(
                "You must lead a gang to join matchmaking.", ephemeral=True
            )
            return
        if uid in self.cog.queue or uid in self.cog.pending or uid in self.cog.challenge_owners:
            await interaction.response.send_message(
                "You are already in the queue or waiting on a match.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Pick a match size:",
            view=SizeSelectView(
                lambda i, size: self.cog._on_queue_size_selected(i, gid, size)
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Challenge", style=discord.ButtonStyle.primary, emoji="\U0001F3AF"
    )
    async def challenge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._can_use(interaction):
            return
        uid = str(interaction.user.id)
        gid, gang = self.cog._find_gang(interaction.user.id)
        if not gid:
            await interaction.response.send_message(
                "You must lead a gang to challenge another gang.", ephemeral=True
            )
            return
        if uid in self.cog.queue or uid in self.cog.pending or uid in self.cog.challenge_owners:
            await interaction.response.send_message(
                "You are already in the queue or waiting on a match.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Pick a gang to challenge:",
            view=TeamSelectView(self.cog, interaction),
            ephemeral=True,
        )


class MatchmakingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: dict = {}
        self.pending: dict = {}
        self.challenges: dict = {}
        self.challenge_owners: set = set()
        self._lock = asyncio.Lock()

    def _find_gang(self, user_id: int):
        data = load_gangs()
        uid = str(user_id)
        for gid, gang in data.items():
            if gang.get("leader") == uid:
                return gid, gang
        return None, None

    def _find_gang_by_name(self, name: str):
        data = load_gangs()
        name = name.strip().lower()
        for gid, gang in data.items():
            if gang.get("name", "").strip().lower() == name:
                return gid, gang
        return None, None

    def _find_gang_by_member(self, user_id: int):
        data = load_gangs()
        uid = str(user_id)
        for gid, gang in data.items():
            if uid in gang.get("members", []):
                return gid, gang
        return None, None

    async def _has_role(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True
            )
            return False
        role = interaction.guild.get_role(ROLE_ID)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "You need the gang role.", ephemeral=True
            )
            return False
        return True

    @app_commands.command(name="matchmaking", description="Open the matchmaking panel")
    async def matchmaking(self, interaction: discord.Interaction):
        if not await self._has_role(interaction):
            return
        embed = discord.Embed(
            title="\u2694\uFE0F Matchmaking",
            description="Queue against a random gang or challenge one directly.",
            color=BLURPLE,
        )
        embed.set_footer(text="Gang leaders only.")
        await interaction.response.send_message(
            embed=embed, view=JoinQueueView(self)
        )

    @app_commands.command(name="win", description="Log a match result")
    @app_commands.describe(
        score="Final score, e.g. 5-3",
        size="Match size, e.g. 3v3",
        opponent="Opposing gang member or leader",
        mvp="MVP of the match",
        region="Region, e.g. NA",
    )
    async def win(
        self,
        interaction: discord.Interaction,
        score: str,
        size: str,
        opponent: discord.Member,
        mvp: discord.Member,
        region: str,
    ):
        if not await self._has_role(interaction):
            return
        uid = str(interaction.user.id)
        gid, gang = self._find_gang(interaction.user.id)
        if not gid:
            await interaction.response.send_message(
                "Only gang leaders can log match results.", ephemeral=True
            )
            return

        if opponent.id == interaction.user.id:
            await interaction.response.send_message(
                "You cannot log a match against yourself.", ephemeral=True
            )
            return

        opp_gid, opp_gang = self._find_gang(opponent.id)
        if not opp_gid:
            opp_gid, opp_gang = self._find_gang_by_member(opponent.id)
        if not opp_gid:
            await interaction.response.send_message(
                "The opponent is not in any gang.", ephemeral=True
            )
            return

        if opp_gid == gid:
            await interaction.response.send_message(
                "You cannot log a match against your own gang.", ephemeral=True
            )
            return

        data = load_gangs()
        gang_name = gang.get("name", "Unknown Gang")
        opp_name = opp_gang.get("name", "Unknown Gang")

        results = _load_results()
        results.setdefault(gang_name, {"wins": 0, "losses": 0, "matches": []})
        results.setdefault(opp_name, {"wins": 0, "losses": 0, "matches": []})
        results[gang_name]["wins"] += 1
        results[opp_name]["losses"] += 1
        results[gang_name]["matches"].append({
            "score": score,
            "size": size,
            "opponent": opp_name,
            "mvp": mvp.display_name,
            "region": region.upper(),
            "result": "win",
            "timestamp": discord.utils.utcnow().isoformat(),
        })
        results[opp_name]["matches"].append({
            "score": score,
            "size": size,
            "opponent": gang_name,
            "mvp": mvp.display_name,
            "region": region.upper(),
            "result": "loss",
            "timestamp": discord.utils.utcnow().isoformat(),
        })
        _save_results(results)
        if interaction.guild_id:
            await self._refresh_leaderboard(interaction.guild_id)

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        embed = discord.Embed(
            title="\U0001F3C6 Match Result Logged",
            description=f"**{gang_name}** defeats **{opp_name}**",
            color=GOLD,
        )
        embed.add_field(name="Score", value=score, inline=True)
        embed.add_field(name="Size", value=size, inline=True)
        embed.add_field(name="Region", value=region.upper(), inline=True)
        embed.add_field(name="MVP", value=mvp.mention, inline=True)
        embed.add_field(
            name=f"{gang_name} Record",
            value=f"{results[gang_name]['wins']}-{results[gang_name]['losses']}",
            inline=True,
        )
        embed.add_field(
            name=f"{opp_name} Record",
            value=f"{results[opp_name]['wins']}-{results[opp_name]['losses']}",
            inline=True,
        )
        embed.set_footer(text=f"Logged by {interaction.user.display_name}")
        embed.timestamp = discord.utils.utcnow()

        if log_channel:
            try:
                await log_channel.send(embed=embed)
            except discord.Forbidden:
                pass

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(name="leaderboard", description="Show the gang results leaderboard")
    @app_commands.describe(
        limit="How many gangs to show (default 20, max 50)",
        update="Post a leaderboard that auto-updates on win/loss changes",
    )
    async def leaderboard(
        self,
        interaction: discord.Interaction,
        limit: app_commands.Range[int, 1, 50] = 20,
        update: bool = False,
    ):
        results = _load_results()
        embed = self._build_leaderboard_embed(results, limit)

        if not update:
            await interaction.response.send_message(embed=embed)
            return

        await interaction.response.send_message(embed=embed)
        message = await interaction.original_response()

        tracked = self._load_leaderboard_tracked()
        tracked[str(interaction.guild_id)] = {
            "channel_id": interaction.channel_id,
            "message_id": message.id,
            "limit": limit,
        }
        self._save_leaderboard_tracked(tracked)

    def _load_leaderboard_tracked(self):
        return _load("leaderboard.json")

    def _save_leaderboard_tracked(self, data):
        return _save("leaderboard.json", data)

    def _build_leaderboard_embed(self, results: dict, limit: int):
        if not results:
            return discord.Embed(
                title="\U0001F3C6 Leaderboard",
                description="No matches logged yet.",
                color=GOLD,
            )

        rows = []
        for gang_name, stats in results.items():
            wins = stats.get("wins", 0)
            losses = stats.get("losses", 0)
            total = wins + losses
            win_rate = (wins / total * 100) if total > 0 else 0
            rows.append((gang_name, wins, losses, win_rate))

        rows.sort(key=lambda r: (-r[1], -r[3], r[2]))

        embed = discord.Embed(
            title="\U0001F3C6 Leaderboard",
            color=GOLD,
        )
        for idx, (gang_name, wins, losses, win_rate) in enumerate(rows[:limit], start=1):
            embed.add_field(
                name=f"#{idx} {gang_name}",
                value=f"{wins}W - {losses}L ({win_rate:.1f}%)",
                inline=False,
            )

        return embed

    async def _refresh_leaderboard(self, guild_id: int):
        if not guild_id:
            return
        tracked = self._load_leaderboard_tracked()
        entry = tracked.get(str(guild_id))
        if not entry:
            return

        guild = self.bot.get_guild(guild_id)
        if not guild:
            return
        channel = guild.get_channel(entry["channel_id"])
        if not channel:
            return
        try:
            message = await channel.fetch_message(entry["message_id"])
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

        results = _load_results()
        embed = self._build_leaderboard_embed(results, entry.get("limit", 20))
        try:
            await message.edit(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _on_queue_members_selected(self, interaction: discord.Interaction, gid: str, size: str, member_ids: list):
        uid = str(interaction.user.id)
        gang = load_gangs().get(gid, {})
        if not gang or gang.get("leader") != uid:
            await interaction.followup.send(
                "Your gang status changed before you could join. Please try again.",
                ephemeral=True,
            )
            return
        required = int(size.split("v")[0]) - 1
        if len(member_ids) != required:
            await interaction.followup.send(
                f"You must select exactly {required} member(s) for {size}.",
                ephemeral=True,
            )
            return
        async with self._lock:
            if uid in self.queue or uid in self.pending or uid in self.challenge_owners:
                await interaction.followup.send(
                    "You are already in the queue or waiting on a match.",
                    ephemeral=True,
                )
                return
            self.queue[uid] = {
                "gang_id": gid,
                "size": size,
                "member_ids": member_ids,
                "channel_id": interaction.channel_id,
                "guild_id": interaction.guild_id,
            }
        await interaction.followup.send(
            "You joined the matchmaking queue. Waiting for an opponent...",
            ephemeral=True,
        )
        await asyncio.sleep(0)
        await self._try_match(uid)

    async def _on_queue_size_selected(self, interaction: discord.Interaction, gid: str, size: str):
        uid = str(interaction.user.id)
        gang = load_gangs().get(gid, {})
        if not gang or gang.get("leader") != uid:
            await interaction.followup.send(
                "Your gang status changed before you could join. Please try again.",
                ephemeral=True,
            )
            return
        team_size = int(size.split("v")[0])
        required = team_size - 1
        available = [m for m in gang.get("members", []) if m != uid]
        if len(available) < required:
            await interaction.followup.send(
                f"You need {required} member(s) for {size}, but your gang only has {len(available)} available.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Select {required} member(s) for the {size}:",
            view=MemberSelectView(
                self, gid, interaction.guild, interaction.user.id,
                lambda i, mids: self._on_queue_members_selected(i, gid, size, mids),
                required=required,
            ),
            ephemeral=True,
        )

    async def _on_team_chosen(self, original_interaction: discord.Interaction, select_interaction: discord.Interaction, target_gid: str):
        uid = str(original_interaction.user.id)
        gid, gang = self._find_gang(original_interaction.user.id)
        if not gid:
            await select_interaction.response.send_message(
                "You no longer lead a gang.", ephemeral=True
            )
            return
        data = load_gangs()
        target_gang = data.get(target_gid)
        if not target_gang:
            await select_interaction.response.send_message(
                "That gang no longer exists.", ephemeral=True
            )
            return
        defender_id = target_gang.get("leader")
        if not defender_id:
            await select_interaction.response.send_message(
                "That gang has no leader.", ephemeral=True
            )
            return

        await select_interaction.response.send_message(
            "Pick a match size:",
            view=SizeSelectView(
                lambda i, size: self._on_challenge_size_selected(
                    i, original_interaction, gid, target_gid, defender_id, size
                )
            ),
            ephemeral=True,
        )

    async def _on_challenge_members_selected(
        self,
        interaction: discord.Interaction,
        original_interaction: discord.Interaction,
        challenger_gid: str,
        defender_gid: str,
        defender_id: str,
        size: str,
        member_ids: list,
    ):
        uid = str(original_interaction.user.id)
        data = load_gangs()
        challenger_gang = data.get(challenger_gid, {})
        defender_gang = data.get(defender_gid, {})
        if not challenger_gang or challenger_gang.get("leader") != uid:
            await interaction.followup.send(
                "Your gang status changed before you could challenge. Please try again.",
                ephemeral=True,
            )
            return
        if not defender_gang or defender_gang.get("leader") != defender_id:
            await interaction.followup.send(
                "The defending gang or its leader changed before the challenge could be sent.",
                ephemeral=True,
            )
            return
        required = int(size.split("v")[0]) - 1
        if len(member_ids) != required:
            await interaction.followup.send(
                f"You must select exactly {required} member(s) for {size}.",
                ephemeral=True,
            )
            return

        async with self._lock:
            if uid in self.challenge_owners or defender_id in self.challenge_owners:
                await interaction.followup.send(
                    "One of the players is already in a challenge or queue.",
                    ephemeral=True,
                )
                return
            challenge_id = f"{uid}-{defender_id}-{interaction.id}"
            self.challenges[challenge_id] = {
                "id": challenge_id,
                "challenger_id": uid,
                "challenger_gid": challenger_gid,
                "challenger_members": member_ids,
                "defender_id": defender_id,
                "defender_gid": defender_gid,
                "defender_members": [],
                "size": size,
                "accepted": None,
                "guild_id": original_interaction.guild_id,
                "channel_id": interaction.channel_id,
            }
            self.challenge_owners.add(uid)
            self.challenge_owners.add(defender_id)

        defender_user = self.bot.get_user(int(defender_id))
        if not defender_user:
            await interaction.followup.send(
                "Could not DM that gang leader.", ephemeral=True
            )
            self._cancel_challenge(challenge_id)
            return

        embed = discord.Embed(
            title="\U0001F3AF Gang Challenge",
            description=(
                f"**{challenger_gang.get('name', 'Unknown Gang')}** has challenged your gang "
                f"**{defender_gang.get('name', 'Unknown Gang')}** to a {size} match!"
            ),
            color=BLURPLE,
        )
        embed.set_footer(text="Click Accept to choose your members.")
        try:
            await defender_user.send(
                embed=embed,
                view=AcceptChallengeView(self, challenge_id),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I cannot DM that user. They may have DMs disabled.", ephemeral=True
            )
            self._cancel_challenge(challenge_id)
            return

        await interaction.followup.send(
            f"Challenge sent to {defender_user.mention}. Waiting for them to accept...",
            ephemeral=True,
        )

    async def _on_challenge_size_selected(
        self,
        interaction: discord.Interaction,
        original_interaction: discord.Interaction,
        challenger_gid: str,
        defender_gid: str,
        defender_id: str,
        size: str,
    ):
        uid = str(original_interaction.user.id)
        data = load_gangs()
        challenger_gang = data.get(challenger_gid, {})
        if not challenger_gang or challenger_gang.get("leader") != uid:
            await interaction.followup.send(
                "Your gang status changed before you could challenge. Please try again.",
                ephemeral=True,
            )
            return
        team_size = int(size.split("v")[0])
        required = team_size - 1
        available = [m for m in challenger_gang.get("members", []) if m != uid]
        if len(available) < required:
            await interaction.followup.send(
                f"You need {required} member(s) for {size}, but your gang only has {len(available)} available.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Select {required} member(s) for the {size}:",
            view=MemberSelectView(
                self, challenger_gid, original_interaction.guild, original_interaction.user.id,
                lambda i, mids: self._on_challenge_members_selected(
                    i, original_interaction, challenger_gid, defender_gid, defender_id, size, mids
                ),
                required=required,
            ),
            ephemeral=True,
        )

    async def _on_challenge_accepted(self, interaction: discord.Interaction, challenge_id: str):
        dm = interaction.guild_id is None
        async with self._lock:
            challenge = self.challenges.get(challenge_id)
        if not challenge:
            await interaction.followup.send(
                "This challenge has expired.", ephemeral=True
            )
            return
        defender_id = challenge["defender_id"]
        defender_gid = challenge["defender_gid"]
        guild = self.bot.get_guild(challenge["guild_id"])
        defender_user = self.bot.get_user(int(defender_id))

        if not guild or not defender_user:
            self._cancel_challenge(challenge_id)
            await interaction.followup.send(
                "Could not find the guild or defender.", ephemeral=True
            )
            return

        size = challenge.get("size", "4v4")
        team_size = int(size.split("v")[0])
        required = team_size - 1
        data = load_gangs()
        defender_gang = data.get(defender_gid, {})
        available = [m for m in defender_gang.get("members", []) if m != defender_id]
        if len(available) < required:
            await interaction.followup.send(
                f"You need {required} member(s) for {size}, but your gang only has {len(available)} available.",
                ephemeral=not dm,
            )
            self._cancel_challenge(challenge_id)
            return

        await interaction.followup.send(
            f"Select {required} member(s) for the {size}:",
            view=MemberSelectView(
                self, defender_gid, guild, defender_user.id,
                lambda i, mids: self._on_defender_members_selected(i, challenge_id, mids),
                required=required,
            ),
            ephemeral=not dm,
        )

    async def _on_defender_members_selected(self, interaction: discord.Interaction, challenge_id: str, member_ids: list):
        dm = interaction.guild_id is None
        async with self._lock:
            challenge = self.challenges.get(challenge_id)
            if not challenge:
                await interaction.response.send_message(
                    "This challenge has expired.", ephemeral=not dm
                )
                return
            challenge["defender_members"] = member_ids
        await interaction.response.defer(ephemeral=not dm)
        try:
            await self._create_challenge_ticket(challenge)
        except Exception as exc:
            await interaction.followup.send(
                "Ticket creation failed. The challenge has been cancelled.",
                ephemeral=not dm,
            )
            self._cancel_challenge(challenge_id)
            print(f"[matchmaking] _create_challenge_ticket error: {exc}")
            return
        await interaction.followup.send(
            "Members selected. Match ticket created.",
            ephemeral=not dm,
        )

    async def _create_challenge_ticket(self, challenge: dict):
        data = load_gangs()
        gang1 = data.get(challenge["challenger_gid"], {})
        gang2 = data.get(challenge["defender_gid"], {})
        guild = self.bot.get_guild(challenge["guild_id"])
        if not guild:
            await self._cleanup_challenge(challenge, "Guild not found.")
            return

        category = await _get_ticket_category(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
            ),
        }
        all_ids = {
            challenge["challenger_id"],
            challenge["defender_id"],
        } | set(challenge["challenger_members"]) | set(challenge["defender_members"])
        for mid in all_ids:
            member = guild.get_member(int(mid))
            if member:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        name = f"match-{gang1.get('name', 'gang1')}-vs-{gang2.get('name', 'gang2')}"
        name = name.replace(" ", "-")[:100]
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                topic=f"Match between {gang1.get('name', 'Unknown')} and {gang2.get('name', 'Unknown')}",
            )
        except Exception as exc:
            await self._cleanup_challenge(
                challenge, "Could not create the match ticket channel."
            )
            raise RuntimeError(f"Could not create challenge ticket channel: {exc}")

        size = challenge.get("size", "4v4")
        embed = discord.Embed(
            title="Match Ticket",
            description=f"**{gang1.get('name', 'Unknown')}** vs **{gang2.get('name', 'Unknown')}** ({size})",
            color=GREEN,
        )
        mentions = []
        for mid in all_ids:
            m = guild.get_member(int(mid))
            if m:
                mentions.append(m.mention)
        await channel.send(
            content=" ".join(mentions) if mentions else None,
            embed=embed,
        )
        await self._cleanup_challenge(challenge, f"Match ticket created: {channel.mention}")

    def _cancel_challenge(self, challenge_id: str):
        challenge = self.challenges.pop(challenge_id, None)
        if challenge:
            self.challenge_owners.discard(challenge["challenger_id"])
            self.challenge_owners.discard(challenge["defender_id"])

    async def _cleanup_challenge(self, challenge: dict, message: str):
        self.challenges.pop(challenge.get("id"), None)
        challenger_id = challenge["challenger_id"]
        defender_id = challenge["defender_id"]
        self.challenge_owners.discard(challenger_id)
        self.challenge_owners.discard(defender_id)
        channel_id = challenge.get("channel_id")
        await self._notify_user(challenger_id, message, channel_id)
        await self._notify_user(defender_id, message, channel_id)

    async def _notify_user(self, user_id: str, message: str, channel_id: int = None):
        user = self.bot.get_user(int(user_id))
        if not user:
            return
        try:
            await user.send(message)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _try_match(self, leader_id: str):
        async with self._lock:
            q1 = self.queue.get(leader_id)
            if not q1:
                return
            size = q1.get("size")
            l2 = None
            q2 = None
            for lid, entry in self.queue.items():
                if lid != leader_id and entry.get("size") == size:
                    l2 = lid
                    q2 = entry
                    break
            if not l2:
                return
            l1 = leader_id
            del self.queue[l1]
            del self.queue[l2]

            data = load_gangs()
            gang1 = data.get(q1["gang_id"], {})
            gang2 = data.get(q2["gang_id"], {})

            self.pending[l1] = {
                "opponent_id": l2,
                "accepted": None,
                "gang_id": q1["gang_id"],
                "size": q1["size"],
                "member_ids": q1["member_ids"],
                "channel_id": q1["channel_id"],
                "guild_id": q1["guild_id"],
            }
            self.pending[l2] = {
                "opponent_id": l1,
                "accepted": None,
                "gang_id": q2["gang_id"],
                "size": q2["size"],
                "member_ids": q2["member_ids"],
                "channel_id": q2["channel_id"],
                "guild_id": q2["guild_id"],
            }

        try:
            await self._send_match_request(l1, gang2.get("name", "Unknown Gang"))
            await self._send_match_request(l2, gang1.get("name", "Unknown Gang"))
        except Exception:
            await self._cleanup_pair(l1, l2, "Match setup failed. You can queue again.")
            return
        asyncio.create_task(self._wait_for_acceptance(l1, l2))

    async def _send_match_request(self, leader_id: str, opponent_name: str):
        pending = self.pending.get(leader_id)
        if not pending:
            return
        user = self.bot.get_user(int(leader_id))
        if not user:
            pending["accepted"] = False
            return
        size = pending.get("size", "4v4")
        embed = discord.Embed(
            title="Match Found",
            description=f"You have been matched against **{opponent_name}** for a **{size}**.\n\nDo you accept?",
            color=BLURPLE,
        )
        embed.set_footer(text=f"You have {MATCH_TIMEOUT} seconds to respond.")
        try:
            await user.send(
                embed=embed,
                view=AcceptMatchView(self, leader_id, opponent_name),
            )
        except Exception:
            pending["accepted"] = False

    async def _wait_for_acceptance(self, l1: str, l2: str):
        try:
            for _ in range(MATCH_TIMEOUT):
                a1 = self.pending.get(l1, {}).get("accepted")
                a2 = self.pending.get(l2, {}).get("accepted")
                if a1 is not None and a2 is not None:
                    break
                await asyncio.sleep(1)

            p1 = self.pending.get(l1)
            p2 = self.pending.get(l2)

            if not p1 or not p2:
                await self._cleanup_pair(l1, l2, "Match was cancelled.")
                return

            if p1["accepted"] and p2["accepted"]:
                await self._create_ticket(l1, l2)
            else:
                await self._cleanup_pair(
                    l1, l2, "Match declined or timed out."
                )
        except Exception as exc:
            await self._cleanup_pair(l1, l2, "Match failed due to an error. You can queue again.")
            print(f"[matchmaking] _wait_for_acceptance error: {exc}")

    async def _create_ticket(self, l1: str, l2: str):
        p1 = self.pending.get(l1)
        p2 = self.pending.get(l2)
        if not p1 or not p2:
            return

        data = load_gangs()
        gang1 = data.get(p1["gang_id"], {})
        gang2 = data.get(p2["gang_id"], {})
        guild = self.bot.get_guild(p1["guild_id"])

        if not guild:
            await self._cleanup_pair(l1, l2, "Guild not found.")
            return

        category = await _get_ticket_category(guild)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True,
            ),
        }
        all_ids = {l1, l2} | set(p1["member_ids"]) | set(p2["member_ids"])
        for mid in all_ids:
            member = guild.get_member(int(mid))
            if member:
                overwrites[member] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                )

        name = f"match-{gang1.get('name', 'gang1')}-vs-{gang2.get('name', 'gang2')}"
        name = name.replace(" ", "-")[:100]
        try:
            channel = await guild.create_text_channel(
                name=name,
                category=category,
                overwrites=overwrites,
                topic=f"Match between {gang1.get('name', 'Unknown')} and {gang2.get('name', 'Unknown')}",
            )
        except Exception as exc:
            await self._cleanup_pair(
                l1, l2, "Could not create the match ticket channel."
            )
            raise RuntimeError(f"Could not create match ticket channel: {exc}")

        size = p1.get("size", "4v4")
        embed = discord.Embed(
            title="Match Ticket",
            description=f"**{gang1.get('name', 'Unknown')}** vs **{gang2.get('name', 'Unknown')}** ({size})",
            color=GREEN,
        )
        mentions = []
        for mid in all_ids:
            m = guild.get_member(int(mid))
            if m:
                mentions.append(m.mention)
        await channel.send(
            content=" ".join(mentions) if mentions else None,
            embed=embed,
        )
        await self._cleanup_pair(l1, l2, f"Match ticket created: {channel.mention}")

    admin = app_commands.Group(name="admin", description="Admin match tools")

    async def _has_admin_role(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "Use this command in a server.", ephemeral=True
            )
            return False
        role = interaction.guild.get_role(ADMIN_ROLE_ID)
        if not role or role not in interaction.user.roles:
            await interaction.response.send_message(
                "You need the admin role to use this.", ephemeral=True
            )
            return False
        return True

    @admin.command(name="creategang", description="Create a gang and set its owner")
    @app_commands.describe(user="User to set as owner", name="Gang name", icon="Icon URL")
    async def creategang(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        name: str,
        icon: str = "",
    ):
        if not await self._has_admin_role(interaction):
            return
        uid = str(user.id)
        data = load_gangs()
        if user_gang(uid, data)[0] or member_gang(uid, data)[0]:
            await interaction.response.send_message(
                f"{user.mention} is already in a gang.", ephemeral=True
            )
            return
        chosen_name = name.strip()
        if name_exists(chosen_name, data):
            await interaction.response.send_message(
                "A gang with that name already exists.", ephemeral=True
            )
            return
        existing_ids = [int(k) for k in data.keys() if k.isdigit()]
        gid = str(max(existing_ids, default=0) + 1)
        raw_icon = icon.strip()
        data[gid] = {
            "name": chosen_name,
            "icon": raw_icon if raw_icon else None,
            "leader": uid,
            "members": [uid],
        }
        save(data)
        await interaction.response.send_message(
            f"Created **{name}** with {user.mention} as owner.", ephemeral=True
        )

    @admin.command(name="setowner", description="Transfer gang ownership to a user")
    @app_commands.describe(user="New owner", gang_name="Name of the gang")
    async def setowner(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        gang_name: str,
    ):
        if not await self._has_admin_role(interaction):
            return
        data = load_gangs()
        gid, gang = self._find_gang_by_name(gang_name)
        if not gid:
            await interaction.response.send_message(
                "Gang not found.", ephemeral=True
            )
            return
        uid = str(user.id)
        if user_gang(uid, data)[0] or member_gang(uid, data)[0]:
            await interaction.response.send_message(
                f"{user.mention} is already in a gang. Remove them first.", ephemeral=True
            )
            return
        old_leader = gang.get("leader")
        gang["leader"] = uid
        if old_leader in gang.get("members", []):
            gang["members"].remove(old_leader)
        if uid not in gang["members"]:
            gang["members"].append(uid)
        save(data)
        await interaction.response.send_message(
            f"Set {user.mention} as owner of **{gang['name']}**.", ephemeral=True
        )

    @admin.command(name="addwin", description="Add a win to a gang")
    @app_commands.describe(gang_name="Name of the gang")
    async def addwin(self, interaction: discord.Interaction, gang_name: str):
        if not await self._has_admin_role(interaction):
            return
        data = load_gangs()
        gid, gang = self._find_gang_by_name(gang_name)
        if not gid:
            await interaction.response.send_message(
                "Gang not found.", ephemeral=True
            )
            return
        results = _load_results()
        name = gang["name"]
        results.setdefault(name, {"wins": 0, "losses": 0, "matches": []})
        results[name]["wins"] += 1
        _save_results(results)
        if interaction.guild_id:
            await self._refresh_leaderboard(interaction.guild_id)
        await interaction.response.send_message(
            f"Added a win to **{name}**. Record: {results[name]['wins']}-{results[name]['losses']}",
            ephemeral=True,
        )

    @admin.command(name="removewin", description="Remove a win from a gang")
    @app_commands.describe(gang_name="Name of the gang")
    async def removewin(self, interaction: discord.Interaction, gang_name: str):
        if not await self._has_admin_role(interaction):
            return
        data = load_gangs()
        gid, gang = self._find_gang_by_name(gang_name)
        if not gid:
            await interaction.response.send_message(
                "Gang not found.", ephemeral=True
            )
            return
        results = _load_results()
        name = gang["name"]
        results.setdefault(name, {"wins": 0, "losses": 0, "matches": []})
        if results[name]["wins"] > 0:
            results[name]["wins"] -= 1
        _save_results(results)
        if interaction.guild_id:
            await self._refresh_leaderboard(interaction.guild_id)
        await interaction.response.send_message(
            f"Removed a win from **{name}**. Record: {results[name]['wins']}-{results[name]['losses']}",
            ephemeral=True,
        )

    @admin.command(name="addloss", description="Add a loss to a gang")
    @app_commands.describe(gang_name="Name of the gang")
    async def addloss(self, interaction: discord.Interaction, gang_name: str):
        if not await self._has_admin_role(interaction):
            return
        data = load_gangs()
        gid, gang = self._find_gang_by_name(gang_name)
        if not gid:
            await interaction.response.send_message(
                "Gang not found.", ephemeral=True
            )
            return
        results = _load_results()
        name = gang["name"]
        results.setdefault(name, {"wins": 0, "losses": 0, "matches": []})
        results[name]["losses"] += 1
        _save_results(results)
        if interaction.guild_id:
            await self._refresh_leaderboard(interaction.guild_id)
        await interaction.response.send_message(
            f"Added a loss to **{name}**. Record: {results[name]['wins']}-{results[name]['losses']}",
            ephemeral=True,
        )

    @admin.command(name="removeloss", description="Remove a loss from a gang")
    @app_commands.describe(gang_name="Name of the gang")
    async def removeloss(self, interaction: discord.Interaction, gang_name: str):
        if not await self._has_admin_role(interaction):
            return
        data = load_gangs()
        gid, gang = self._find_gang_by_name(gang_name)
        if not gid:
            await interaction.response.send_message(
                "Gang not found.", ephemeral=True
            )
            return
        results = _load_results()
        name = gang["name"]
        results.setdefault(name, {"wins": 0, "losses": 0, "matches": []})
        if results[name]["losses"] > 0:
            results[name]["losses"] -= 1
        _save_results(results)
        if interaction.guild_id:
            await self._refresh_leaderboard(interaction.guild_id)
        await interaction.response.send_message(
            f"Removed a loss from **{name}**. Record: {results[name]['wins']}-{results[name]['losses']}",
            ephemeral=True,
        )

    @admin.command(name="ticketadd", description="Add a user to this match ticket")
    @app_commands.describe(user="User to add")
    async def ticketadd(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._has_admin_role(interaction):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this inside a match ticket.", ephemeral=True
            )
            return
        if not channel.category or channel.category.name != TICKET_CATEGORY_NAME:
            await interaction.response.send_message(
                "Use this inside a match ticket.", ephemeral=True
            )
            return
        try:
            overwrites = channel.overwrites_for(user)
            overwrites.view_channel = True
            overwrites.send_messages = True
            overwrites.read_message_history = True
            await channel.set_permissions(user, overwrite=overwrites)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to update this channel.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Added {user.mention} to the ticket.", ephemeral=True
        )

    @admin.command(name="ticketremove", description="Remove a user from this match ticket")
    @app_commands.describe(user="User to remove")
    async def ticketremove(self, interaction: discord.Interaction, user: discord.Member):
        if not await self._has_admin_role(interaction):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "Use this inside a match ticket.", ephemeral=True
            )
            return
        if not channel.category or channel.category.name != TICKET_CATEGORY_NAME:
            await interaction.response.send_message(
                "Use this inside a match ticket.", ephemeral=True
            )
            return
        try:
            await channel.set_permissions(user, overwrite=None)
        except discord.Forbidden:
            await interaction.response.send_message(
                "I do not have permission to update this channel.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Removed {user.mention} from the ticket.", ephemeral=True
        )

    async def _cleanup_pair(self, l1: str, l2: str, message: str):
        for lid in (l1, l2):
            pending = self.pending.pop(lid, None)
            if not pending:
                continue
            user = self.bot.get_user(int(lid))
            if user:
                try:
                    await user.send(message)
                    continue
                except (discord.Forbidden, discord.HTTPException):
                    pass
            channel = self.bot.get_channel(pending.get("channel_id"))
            if channel:
                try:
                    await channel.send(message)
                except (discord.Forbidden, discord.HTTPException):
                    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(MatchmakingCog(bot))