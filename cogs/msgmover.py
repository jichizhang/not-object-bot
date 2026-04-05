import re
from typing import Union

import discord
from discord import app_commands
from discord.ext import commands

_MESSAGE_URL_RE = re.compile(
    r'https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(\d+)/(\d+)/(\d+)'
)


class MsgMoverCog(commands.Cog):

    def __init__(self, bot):
        self.bot = bot

    async def _get_or_create_webhook(self, channel: discord.TextChannel) -> discord.Webhook:
        webhooks = await channel.webhooks()
        webhook = next((w for w in webhooks if w.user == self.bot.user), None)
        if webhook is None:
            webhook = await channel.create_webhook(name="NotObject")
        return webhook

    async def _send_message(
        self,
        message: discord.Message,
        destination: Union[discord.TextChannel, discord.Thread],
        webhook: discord.Webhook,
    ) -> None:
        content = message.content or ""
        files = [await a.to_file() for a in message.attachments]

        send_kwargs: dict = dict(
            username=message.author.display_name,
            avatar_url=message.author.display_avatar.url,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        if content:
            send_kwargs['content'] = content
        if files:
            send_kwargs['files'] = files
        if isinstance(destination, discord.Thread):
            send_kwargs['thread'] = destination

        if not content and not files:
            return  # nothing to send

        await webhook.send(**send_kwargs)

    @app_commands.command(name='msgmove', description='Move message(s) to another channel')
    @app_commands.describe(
        destination='Channel to move message(s) to',
        message_url='URL of the specific message to move',
        count='Number of the latest messages in this channel to move (1–100)',
    )
    @app_commands.default_permissions(manage_messages=True)
    async def msgmove(
        self,
        interaction: discord.Interaction,
        destination: Union[discord.TextChannel, discord.Thread],
        message_url: str | None = None,
        count: app_commands.Range[int, 1, 100] | None = None,
    ):
        if message_url is None and count is None:
            await interaction.response.send_message(
                "Provide either a `message_url` or a `count` of messages to move.", ephemeral=True
            )
            return
        if message_url is not None and count is not None:
            await interaction.response.send_message(
                "Provide either `message_url` or `count`, not both.", ephemeral=True
            )
            return

        await interaction.response.defer()

        webhook_channel = destination.parent if isinstance(destination, discord.Thread) else destination

        try:
            webhook = await self._get_or_create_webhook(webhook_channel)

            if message_url is not None:
                match = _MESSAGE_URL_RE.match(message_url.strip())
                if not match:
                    await interaction.followup.send("Invalid message URL.", ephemeral=True)
                    return
                channel_id, message_id = int(match.group(2)), int(match.group(3))
                source_channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
                message = await source_channel.fetch_message(message_id)
                source_channel = message.channel
                await destination.send(embed=discord.Embed(
                    description=f"1 message was moved from {source_channel.mention}.",
                    color=discord.Color.blurple(),
                ))
                await self._send_message(message, destination, webhook)
                await message.delete()
                await interaction.followup.send(embed=discord.Embed(
                    description=f"1 message was moved to {destination.mention}.",
                    color=discord.Color.blurple(),
                ))
            else:
                original_response = await interaction.original_response()
                messages = [
                    msg async for msg in interaction.channel.history(limit=count + 1)
                    if msg.id != original_response.id
                ][:count]
                messages.reverse()  # oldest first so they appear in order
                n = len(messages)
                noun = "message" if n == 1 else "messages"
                await destination.send(embed=discord.Embed(
                    description=f"{n} {noun} were moved from {interaction.channel.mention}.",
                    color=discord.Color.blurple(),
                ))
                for msg in messages:
                    await self._send_message(msg, destination, webhook)
                    await msg.delete()
                await interaction.followup.send(embed=discord.Embed(
                    description=f"{n} {noun} were moved to {destination.mention}.",
                    color=discord.Color.blurple(),
                ))

        except discord.Forbidden:
            await interaction.followup.send("Missing permissions to move messages.", ephemeral=True)
        except discord.NotFound:
            await interaction.followup.send("Message not found.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


async def setup(bot):
    await bot.add_cog(MsgMoverCog(bot))
