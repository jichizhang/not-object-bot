import re
import discord
from discord import app_commands
from discord.ext import commands
import uwuify

_URL_RE = re.compile(r'(https?://\S+)')


class UwuLockCog(commands.Cog):
    """Cog for uwu-locking users so their messages get uwuified"""

    def __init__(self, bot):
        self.bot = bot
        # Set of user IDs that are currently uwu-locked
        self.uwulocked_users: set[int] = set()

    @app_commands.command(name='uwulock', description='Toggle uwu lock on a user — their messages will be uwuified')
    @app_commands.describe(user='The user to uwu lock or unlock')
    @app_commands.default_permissions(manage_messages=True)
    async def uwulock(self, interaction: discord.Interaction, user: discord.Member):
        if user.id in self.uwulocked_users:
            self.uwulocked_users.discard(user.id)
            await interaction.response.send_message(
                f"{user.mention} has been released from uwu lock."
            )
        else:
            self.uwulocked_users.add(user.id)
            await interaction.response.send_message(
                f"{user.mention} is now uwu locked! UwU"
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.author.id not in self.uwulocked_users:
            return
        # Only handle text channels that support webhooks
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return
        # Need message content to uwuify
        if not message.content:
            return
        # Do not uwuify messages with attachments, as attachments will be deleted when message gets deleted
        if message.attachments:
            return

        flags = uwuify.SMILEY | uwuify.YU | uwuify.STUTTER
        parts = _URL_RE.split(message.content)
        uwuified = ''.join(
            part if _URL_RE.fullmatch(part) else uwuify.uwu(part, flags=flags)
            for part in parts
        )

        try:
            # Get the base channel for webhook creation (threads share parent channel's webhooks)
            if isinstance(message.channel, discord.Thread):
                webhook_channel = message.channel.parent
            else:
                webhook_channel = message.channel

            # Reuse an existing webhook created by this bot, or create one
            webhooks = await webhook_channel.webhooks()
            webhook = next((w for w in webhooks if w.user == self.bot.user), None)
            if webhook is None:
                webhook = await webhook_channel.create_webhook(name="NotObject")

            await message.delete()

            send_kwargs = dict(
                content=uwuified,
                username=message.author.display_name,
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            if isinstance(message.channel, discord.Thread):
                send_kwargs['thread'] = message.channel

            await webhook.send(**send_kwargs)

        except discord.Forbidden:
            pass
        except Exception as e:
            print(f"UwuLock error: {e}")


async def setup(bot):
    await bot.add_cog(UwuLockCog(bot))
