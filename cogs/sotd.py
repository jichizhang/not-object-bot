import discord
from discord.ext import commands, tasks
from discord import app_commands
import httpx
import os
import asyncio
from datetime import datetime, timezone, timedelta
from utils.database import (
    add_sotd_song,
    get_random_unused_song,
    mark_song_as_used,
    can_add_song,
    get_queue_counts,
)

SQUIGLY_API_BASE = "https://squigly.link/api"


class SotdCog(commands.Cog):
    """Cog for Song of the Day functionality"""

    def __init__(self, bot):
        self.bot = bot
        self.sotd_channel_id = int(os.getenv('SOTD_CHANNEL_ID')) if os.getenv('SOTD_CHANNEL_ID') else None

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.daily_sotd_task.is_running():
            self.daily_sotd_task.start()
            print("SOTD daily task started")

    async def resolve_squigly(self, url: str) -> dict | None:
        """Resolve a music URL via Squigly.

        Returns a track info dict on success, or None if the URL is not an individual song.
        Raises httpx.HTTPError on API failure.
        """
        async with httpx.AsyncClient() as client:
            create_resp = await client.post(
                f"{SQUIGLY_API_BASE}/create",
                json={"url": url},
                headers={"content-type": "application/json"},
                timeout=15.0,
            )
            create_resp.raise_for_status()
            create_data = create_resp.json()

            pretty_url = create_data.get("pretty_url", "")
            if not pretty_url.startswith("/song/"):
                return None

            pretty_path = pretty_url[len("/song/"):]

            resolve_resp = await client.get(
                f"{SQUIGLY_API_BASE}/resolve",
                params={"pretty": pretty_path},
                timeout=15.0,
            )
            resolve_resp.raise_for_status()
            resolve_data = resolve_resp.json()

            services = resolve_data.get("services", {})

            return {
                "track_name": resolve_data.get("title"),
                "artist_name": resolve_data.get("artist"),
                "artwork_url": resolve_data.get("artwork_url"),
                "spotify_url": (services.get("spotify") or {}).get("url"),
                "apple_music_url": (services.get("apple") or {}).get("url"),
                "tidal_url": (services.get("tidal") or {}).get("url"),
                "deezer_url": (services.get("deezer") or {}).get("url"),
            }

    @app_commands.command(name="sotd", description="Add a song to the Song of the Day library")
    async def add_song(self, interaction: discord.Interaction, url: str):
        """Add a song to the SOTD database"""
        await interaction.response.defer(ephemeral=True)

        try:
            track_info = await self.resolve_squigly(url)
        except Exception as e:
            print(f"Squigly API error: {e}")
            await interaction.followup.send("❌ Failed to resolve the URL. Please check that it's a valid music link.")
            return

        if track_info is None:
            embed = discord.Embed(
                title="❌ Invalid URL",
                description="Please provide a link to an individual song, not an album or playlist.",
                color=0xE74C3C,
            )
            await interaction.followup.send(embed=embed)
            return

        track_name = track_info["track_name"]
        artist_name = track_info["artist_name"]

        if not track_name or not artist_name:
            await interaction.followup.send("❌ Could not retrieve track information.")
            return

        can_add, _ = can_add_song(track_name, artist_name)
        if not can_add:
            embed = discord.Embed(
                title=f"❌ {track_name} by {artist_name}",
                description=(
                    "This song is already in the library and hasn't been featured yet!\n"
                    "You can add it again after it's been featured."
                ),
                color=0xE74C3C,
            )
            await interaction.followup.send(embed=embed)
            return

        add_sotd_song(
            interaction.user.id,
            track_name,
            artist_name,
            track_info["artwork_url"],
            track_info["spotify_url"],
            track_info["apple_music_url"],
            track_info["tidal_url"],
            track_info["deezer_url"],
        )

        embed = discord.Embed(
            title=f"✅ {track_name} by {artist_name}",
            description="Added to the library",
            color=0x1DB954,
        )
        if track_info["artwork_url"]:
            embed.set_thumbnail(url=track_info["artwork_url"])
        embed.set_footer(text="Powered by Squigly")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="queue", description="Show the number of songs in the SOTD queue per user")
    async def show_queue(self, interaction: discord.Interaction):
        counts = get_queue_counts()

        if not counts:
            await interaction.response.send_message("The queue is empty.", ephemeral=True)
            return

        total = sum(count for _, count in counts)
        lines = []
        for user_id, count in counts:
            user = self.bot.get_user(user_id)
            name = user.display_name if user else f"<@{user_id}>"
            lines.append(f"{name} — {count}")

        embed = discord.Embed(
            title="SOTD Queue",
            description="\n".join(lines),
            color=0x1DB954,
        )
        embed.set_footer(text=f"{total} song{'s' if total != 1 else ''} total")
        await interaction.response.send_message(embed=embed)

    @tasks.loop(hours=24)
    async def daily_sotd_task(self):
        """Send the song of the day at midnight UTC"""
        if not self.sotd_channel_id:
            print("SOTD channel not configured. Skipping daily SOTD.")
            return

        channel = self.bot.get_channel(self.sotd_channel_id)
        if not channel:
            print(f"Could not find SOTD channel with ID {self.sotd_channel_id}")
            return

        song = get_random_unused_song()
        if not song:
            print("No unused songs in the database.")
            return

        mark_song_as_used(song['id'])

        user = self.bot.get_user(song['user_id'])
        user_mention = user.mention if user else f"<@{song['user_id']}>"

        embed = discord.Embed(
            title=song['track_name'],
            description=f"by {song['artist_name']}",
            color=0x1DB954,
        )
        if song.get('album_cover_url'):
            embed.set_image(url=song['album_cover_url'])
        embed.add_field(name="Added by", value=user_mention, inline=False)

        listen_links = []
        if song.get('spotify_url'):
            listen_links.append(f"[Spotify]({song['spotify_url']})")
        if song.get('apple_music_url'):
            listen_links.append(f"[Apple Music]({song['apple_music_url']})")
        if song.get('tidal_url'):
            listen_links.append(f"[Tidal]({song['tidal_url']})")
        if song.get('deezer_url'):
            listen_links.append(f"[Deezer]({song['deezer_url']})")

        if listen_links:
            embed.add_field(name="Listen", value=" | ".join(listen_links), inline=False)

        embed.set_footer(text="Powered by Squigly")
        await channel.send(embed=embed)
        print(f"Sent SOTD: {song['track_name']} by {song['artist_name']}")

    @daily_sotd_task.before_loop
    async def before_daily_sotd_task(self):
        """Wait until the bot is ready and sleep until midnight UTC"""
        await self.bot.wait_until_ready()

        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()

        print(f"SOTD task will start in {sleep_seconds:.0f}s (at {next_midnight.strftime('%Y-%m-%d %H:%M:%S')} UTC)")
        await asyncio.sleep(sleep_seconds)

    def cog_unload(self):
        self.daily_sotd_task.cancel()


async def setup(bot):
    await bot.add_cog(SotdCog(bot))
