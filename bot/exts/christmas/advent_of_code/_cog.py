import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

import discord
from discord.ext import commands

from bot.bot import Bot
from bot.constants import (
    AdventOfCode as AocConfig, Channels, Colours, Emojis, Month, Roles, WHITELISTED_CHANNELS,
)
from bot.exts.christmas.advent_of_code import _helpers
from bot.utils.decorators import InChannelCheckFailure, in_month, override_in_channel, with_role

log = logging.getLogger(__name__)

AOC_REQUEST_HEADER = {"user-agent": "PythonDiscord AoC Event Bot"}

COUNTDOWN_STEP = 60 * 5

AOC_WHITELIST_RESTRICTED = WHITELISTED_CHANNELS + (Channels.advent_of_code_commands,)

# Some commands can be run in the regular advent of code channel
# They aren't spammy and foster discussion
AOC_WHITELIST = AOC_WHITELIST_RESTRICTED + (Channels.advent_of_code,)


async def countdown_status(bot: commands.Bot) -> None:
    """Set the playing status of the bot to the minutes & hours left until the next day's challenge."""
    log.info("Started `AoC Status Countdown` task")
    while _helpers.is_in_advent():
        _, time_left = _helpers.time_left_to_aoc_midnight()

        aligned_seconds = int(math.ceil(time_left.seconds / COUNTDOWN_STEP)) * COUNTDOWN_STEP
        hours, minutes = aligned_seconds // 3600, aligned_seconds // 60 % 60

        if aligned_seconds == 0:
            playing = "right now!"
        elif aligned_seconds == COUNTDOWN_STEP:
            playing = f"in less than {minutes} minutes"
        elif hours == 0:
            playing = f"in {minutes} minutes"
        elif hours == 23:
            playing = f"since {60 - minutes} minutes ago"
        else:
            playing = f"in {hours} hours and {minutes} minutes"

        # Status will look like "Playing in 5 hours and 30 minutes"
        await bot.change_presence(activity=discord.Game(playing))

        # Sleep until next aligned time or a full step if already aligned
        delay = time_left.seconds % COUNTDOWN_STEP or COUNTDOWN_STEP
        await asyncio.sleep(delay)


async def day_countdown(bot: commands.Bot) -> None:
    """
    Calculate the number of seconds left until the next day of Advent.

    Once we have calculated this we should then sleep that number and when the time is reached, ping
    the Advent of Code role notifying them that the new challenge is ready.
    """
    log.info("Started `Daily AoC Notification` task")
    while _helpers.is_in_advent():
        tomorrow, time_left = _helpers.time_left_to_aoc_midnight()

        # Prevent bot from being slightly too early in trying to announce today's puzzle
        await asyncio.sleep(time_left.seconds + 1)

        channel = bot.get_channel(Channels.advent_of_code)

        if not channel:
            log.error("Could not find the AoC channel to send notification in")
            break

        aoc_role = channel.guild.get_role(AocConfig.role_id)
        if not aoc_role:
            log.error("Could not find the AoC role to announce the daily puzzle")
            break

        puzzle_url = f"https://adventofcode.com/{AocConfig.year}/day/{tomorrow.day}"

        # Check if the puzzle is already available to prevent our members from spamming
        # the puzzle page before it's available by making a small HEAD request.
        for retry in range(1, 5):
            log.debug(f"Checking if the puzzle is already available (attempt {retry}/4)")
            async with bot.http_session.head(puzzle_url, raise_for_status=False) as resp:
                if resp.status == 200:
                    log.debug("Puzzle is available; let's send an announcement message.")
                    break
            log.debug(f"The puzzle is not yet available (status={resp.status})")
            await asyncio.sleep(10)
        else:
            log.error("The puzzle does does not appear to be available at this time, canceling announcement")
            break

        await channel.send(
            f"{aoc_role.mention} Good morning! Day {tomorrow.day} is ready to be attempted. "
            f"View it online now at {puzzle_url}. Good luck!",
            allowed_mentions=discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=[discord.Object(AocConfig.role_id)],
            )
        )

        # Wait a couple minutes so that if our sleep didn't sleep enough
        # time we don't end up announcing twice.
        await asyncio.sleep(120)


class AdventOfCode(commands.Cog):
    """Advent of Code festivities! Ho Ho Ho!"""

    def __init__(self, bot: Bot) -> None:
        self.bot = bot

        self._base_url = f"https://adventofcode.com/{AocConfig.year}"
        self.global_leaderboard_url = f"https://adventofcode.com/{AocConfig.year}/leaderboard"

        self.about_aoc_filepath = Path("./bot/resources/advent_of_code/about.json")
        self.cached_about_aoc = self._build_about_embed()

        self.countdown_task = None
        self.status_task = None

        countdown_coro = day_countdown(self.bot)
        self.countdown_task = self.bot.loop.create_task(countdown_coro)
        self.countdown_task.set_name("Daily AoC Notification")
        self.countdown_task.add_done_callback(_helpers.background_task_callback)

        status_coro = countdown_status(self.bot)
        self.status_task = self.bot.loop.create_task(status_coro)
        self.status_task.set_name("AoC Status Countdown")
        self.status_task.add_done_callback(_helpers.background_task_callback)

    @commands.group(name="adventofcode", aliases=("aoc",))
    @override_in_channel(AOC_WHITELIST)
    async def adventofcode_group(self, ctx: commands.Context) -> None:
        """All of the Advent of Code commands."""
        if not ctx.invoked_subcommand:
            await ctx.send_help(ctx.command)

    @adventofcode_group.command(
        name="subscribe",
        aliases=("sub", "notifications", "notify", "notifs"),
        brief="Notifications for new days"
    )
    @override_in_channel(AOC_WHITELIST)
    async def aoc_subscribe(self, ctx: commands.Context) -> None:
        """Assign the role for notifications about new days being ready."""
        current_year = datetime.now().year
        if current_year != AocConfig.year:
            await ctx.send(f"You can't subscribe to {current_year}'s Advent of Code announcements yet!")
            return

        role = ctx.guild.get_role(AocConfig.role_id)
        unsubscribe_command = f"{ctx.prefix}{ctx.command.root_parent} unsubscribe"

        if role not in ctx.author.roles:
            await ctx.author.add_roles(role)
            await ctx.send("Okay! You have been __subscribed__ to notifications about new Advent of Code tasks. "
                           f"You can run `{unsubscribe_command}` to disable them again for you.")
        else:
            await ctx.send("Hey, you already are receiving notifications about new Advent of Code tasks. "
                           f"If you don't want them any more, run `{unsubscribe_command}` instead.")

    @in_month(Month.DECEMBER)
    @adventofcode_group.command(name="unsubscribe", aliases=("unsub",), brief="Notifications for new days")
    @override_in_channel(AOC_WHITELIST)
    async def aoc_unsubscribe(self, ctx: commands.Context) -> None:
        """Remove the role for notifications about new days being ready."""
        role = ctx.guild.get_role(AocConfig.role_id)

        if role in ctx.author.roles:
            await ctx.author.remove_roles(role)
            await ctx.send("Okay! You have been __unsubscribed__ from notifications about new Advent of Code tasks.")
        else:
            await ctx.send("Hey, you don't even get any notifications about new Advent of Code tasks currently anyway.")

    @adventofcode_group.command(name="countdown", aliases=("count", "c"), brief="Return time left until next day")
    @override_in_channel(AOC_WHITELIST)
    async def aoc_countdown(self, ctx: commands.Context) -> None:
        """Return time left until next day."""
        if not _helpers.is_in_advent():
            datetime_now = datetime.now(_helpers.EST)

            # Calculate the delta to this & next year's December 1st to see which one is closest and not in the past
            this_year = datetime(datetime_now.year, 12, 1, tzinfo=_helpers.EST)
            next_year = datetime(datetime_now.year + 1, 12, 1, tzinfo=_helpers.EST)
            deltas = (dec_first - datetime_now for dec_first in (this_year, next_year))
            delta = min(delta for delta in deltas if delta >= timedelta())  # timedelta() gives 0 duration delta

            # Add a finer timedelta if there's less than a day left
            if delta.days == 0:
                delta_str = f"approximately {delta.seconds // 3600} hours"
            else:
                delta_str = f"{delta.days} days"

            await ctx.send(f"The Advent of Code event is not currently running. "
                           f"The next event will start in {delta_str}.")
            return

        tomorrow, time_left = _helpers.time_left_to_aoc_midnight()

        hours, minutes = time_left.seconds // 3600, time_left.seconds // 60 % 60

        await ctx.send(f"There are {hours} hours and {minutes} minutes left until day {tomorrow.day}.")

    @adventofcode_group.command(name="about", aliases=("ab", "info"), brief="Learn about Advent of Code")
    @override_in_channel(AOC_WHITELIST)
    async def about_aoc(self, ctx: commands.Context) -> None:
        """Respond with an explanation of all things Advent of Code."""
        await ctx.send("", embed=self.cached_about_aoc)

    @adventofcode_group.command(name="join", aliases=("j",), brief="Learn how to join the leaderboard (via DM)")
    @override_in_channel(AOC_WHITELIST)
    async def join_leaderboard(self, ctx: commands.Context) -> None:
        """DM the user the information for joining the Python Discord leaderboard."""
        current_year = datetime.now().year
        if current_year != AocConfig.year:
            await ctx.send(f"The Python Discord leaderboard for {current_year} is not yet available!")
            return

        author = ctx.message.author
        log.info(f"{author.name} ({author.id}) has requested a PyDis AoC leaderboard code")

        if AocConfig.staff_leaderboard_id and any(r.id == Roles.helpers for r in author.roles):
            join_code = AocConfig.leaderboards[AocConfig.staff_leaderboard_id].join_code
        else:
            try:
                join_code = await _helpers.get_public_join_code(author)
            except _helpers.FetchingLeaderboardFailed:
                await ctx.send(":x: Failed to get join code! Notified maintainers.")
                return

        if not join_code:
            log.error(f"Failed to get a join code for user {author} ({author.id})")
            error_embed = discord.Embed(
                title="Unable to get join code",
                description="Failed to get a join code to one of our boards. Please notify staff.",
                colour=discord.Colour.red(),
            )
            await ctx.send(embed=error_embed)
            return

        info_str = [
            "To join our leaderboard, follow these steps:",
            "• Log in on https://adventofcode.com",
            "• Head over to https://adventofcode.com/leaderboard/private",
            f"• Use this code `{join_code}` to join the Python Discord leaderboard!",
        ]
        try:
            await author.send("\n".join(info_str))
        except discord.errors.Forbidden:
            log.debug(f"{author.name} ({author.id}) has disabled DMs from server members")
            await ctx.send(f":x: {author.mention}, please (temporarily) enable DMs to receive the join code")
        else:
            await ctx.message.add_reaction(Emojis.envelope)

    @adventofcode_group.command(
        name="leaderboard",
        aliases=("board", "lb"),
        brief="Get a snapshot of the PyDis private AoC leaderboard",
    )
    @override_in_channel(AOC_WHITELIST_RESTRICTED)
    async def aoc_leaderboard(self, ctx: commands.Context) -> None:
        """Get the current top scorers of the Python Discord Leaderboard."""
        async with ctx.typing():
            try:
                leaderboard = await _helpers.fetch_leaderboard()
            except _helpers.FetchingLeaderboardFailed:
                await ctx.send(":x: Unable to fetch leaderboard!")
                return

            number_of_participants = leaderboard["number_of_participants"]

            top_count = min(AocConfig.leaderboard_displayed_members, number_of_participants)
            header = f"Here's our current top {top_count}! {Emojis.christmas_tree * 3}"

            table = f"```\n{leaderboard['top_leaderboard']}\n```"
            info_embed = _helpers.get_summary_embed(leaderboard)

            await ctx.send(content=f"{header}\n\n{table}", embed=info_embed)

    @adventofcode_group.command(
        name="global",
        aliases=("globalboard", "gb"),
        brief="Get a link to the global leaderboard",
    )
    @override_in_channel(AOC_WHITELIST_RESTRICTED)
    async def aoc_global_leaderboard(self, ctx: commands.Context) -> None:
        """Get a link to the global Advent of Code leaderboard."""
        url = self.global_leaderboard_url
        global_leaderboard = discord.Embed(
            title="Advent of Code — Global Leaderboard",
            description=f"You can find the global leaderboard [here]({url})."
        )
        global_leaderboard.set_thumbnail(url=_helpers.AOC_EMBED_THUMBNAIL)
        await ctx.send(embed=global_leaderboard)

    @adventofcode_group.command(
        name="stats",
        aliases=("dailystats", "ds"),
        brief="Get daily statistics for the Python Discord leaderboard"
    )
    @override_in_channel(AOC_WHITELIST_RESTRICTED)
    async def private_leaderboard_daily_stats(self, ctx: commands.Context) -> None:
        """Send an embed with daily completion statistics for the Python Discord leaderboard."""
        try:
            leaderboard = await _helpers.fetch_leaderboard()
        except _helpers.FetchingLeaderboardFailed:
            await ctx.send(":x: Can't fetch leaderboard for stats right now!")
            return

        # The daily stats are serialized as JSON as they have to be cached in Redis
        daily_stats = json.loads(leaderboard["daily_stats"])
        async with ctx.typing():
            lines = ["Day   ⭐  ⭐⭐ |   %⭐    %⭐⭐\n================================"]
            for day, stars in daily_stats.items():
                star_one = stars["star_one"]
                star_two = stars["star_two"]
                p_star_one = star_one / leaderboard["number_of_participants"]
                p_star_two = star_two / leaderboard["number_of_participants"]
                lines.append(
                    f"{day:>2}) {star_one:>4}  {star_two:>4} | {p_star_one:>7.2%} {p_star_two:>7.2%}"
                )
            table = "\n".join(lines)
            info_embed = _helpers.get_summary_embed(leaderboard)
            await ctx.send(f"```\n{table}\n```", embed=info_embed)

    @with_role(Roles.admin, Roles.events_lead)
    @adventofcode_group.command(
        name="refresh",
        aliases=("fetch",),
        brief="Force a refresh of the leaderboard cache.",
    )
    async def refresh_leaderboard(self, ctx: commands.Context) -> None:
        """
        Force a refresh of the leaderboard cache.

        Note: This should be used sparingly, as we want to prevent sending too
        many requests to the Advent of Code server.
        """
        async with ctx.typing():
            try:
                await _helpers.fetch_leaderboard(invalidate_cache=True)
            except _helpers.FetchingLeaderboardFailed:
                await ctx.send(":x: Something went wrong while trying to refresh the cache!")
            else:
                await ctx.send("\N{OK Hand Sign} Refreshed leaderboard cache!")

    def cog_unload(self) -> None:
        """Cancel season-related tasks on cog unload."""
        log.debug("Unloading the cog and canceling the background task.")
        self.countdown_task.cancel()
        self.status_task.cancel()

    def _build_about_embed(self) -> discord.Embed:
        """Build and return the informational "About AoC" embed from the resources file."""
        with self.about_aoc_filepath.open("r", encoding="utf8") as f:
            embed_fields = json.load(f)

        about_embed = discord.Embed(
            title=self._base_url,
            colour=Colours.soft_green,
            url=self._base_url,
            timestamp=datetime.utcnow()
        )
        about_embed.set_author(name="Advent of Code", url=self._base_url)
        for field in embed_fields:
            about_embed.add_field(**field)

        about_embed.set_footer(text="Last Updated")
        return about_embed

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        """Custom error handler if an advent of code command was posted in the wrong channel."""
        if isinstance(error, InChannelCheckFailure):
            await ctx.send(f":x: Please use <#{Channels.advent_of_code_commands}> for aoc commands instead.")
            error.handled = True
