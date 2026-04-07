import discord
from discord import app_commands
from discord.ext import commands
import os

LAMP_COST = 100

COLOURS: list[tuple[str, tuple[int, int, int]]] = [
    ("Red",        (255, 0, 0)),
    ("Orange",     (255, 165, 0)),
    ("Yellow",     (255, 255, 0)),
    ("Green",      (0, 255, 0)),
    ("Cyan",       (0, 255, 255)),
    ("Blue",       (0, 0, 255)),
    ("Purple",     (128, 0, 128)),
    ("Magenta",    (255, 0, 255)),
    ("Pink",       (255, 105, 180)),
    ("White",      (255, 255, 255)),
    ("Warm White", (255, 200, 120)),
]


class HomeAssistantCog(commands.Cog):
    """Cog for Home Assistant integration"""

    def __init__(self, bot):
        self.bot = bot
        self.ha_server = os.getenv('HA_SERVER')
        self.ha_token = os.getenv('HA_TOKEN')
        self.ha_entity_id = os.getenv('HA_ENTITY_ID')

    @app_commands.command(name='lamp', description=f'Spend {LAMP_COST} coins to change the lamp colour')
    @app_commands.describe(colour='The colour to set the lamp to')
    @app_commands.choices(colour=[
        app_commands.Choice(name=name, value=name) for name, _ in COLOURS
    ])
    async def lamp(self, interaction: discord.Interaction, colour: str):
        """Change the lamp colour for 100 coins"""
        from utils.database import spend_coins, get_user_coins

        user = interaction.user
        rgb = dict(COLOURS)[colour]

        # Check balance
        current_coins = get_user_coins(user.id)
        if current_coins < LAMP_COST:
            embed = discord.Embed(
                title="❌ Insufficient Coins",
                description=f"You need **{LAMP_COST} coins** to change the lamp colour.\nYour balance: **{current_coins} coins**",
                color=0xff6b6b
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        await interaction.response.defer()

        # Check lamp is on
        lamp_on = await self._is_lamp_on()
        if lamp_on is None:
            embed = discord.Embed(
                title="❌ Home Assistant Error",
                description="Failed to communicate with Home Assistant. No coins were deducted.",
                color=0xff6b6b
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not lamp_on:
            embed = discord.Embed(
                title="💡 Lamp is Off",
                description="The lamp is currently off. Ask Object to turn it on first!",
                color=0xff6b6b
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        # Call Home Assistant
        success = await self._set_light_colour(rgb)
        if not success:
            embed = discord.Embed(
                title="❌ Home Assistant Error",
                description="Failed to communicate with Home Assistant. No coins were deducted.",
                color=0xff6b6b
            )
            await interaction.followup.send(embed=embed)
            return

        # Deduct coins only after successful HA call
        spent = spend_coins(user.id, user.display_name, LAMP_COST)
        if not spent:
            embed = discord.Embed(
                title="❌ Insufficient Coins",
                description=f"You need **{LAMP_COST} coins** to change the lamp colour.",
                color=0xff6b6b
            )
            await interaction.followup.send(embed=embed)
            return

        new_balance = get_user_coins(user.id)
        r, g, b = rgb
        colour_int = (r << 16) | (g << 8) | b

        embed = discord.Embed(
            title="💡 Lamp Colour Changed!",
            description=f"{user.mention} changed the lamp to **{colour}**!\n\n"
                        f"Spent: **{LAMP_COST} coins** | Remaining: **{new_balance} coins**",
            color=colour_int if colour_int > 0 else 0xffffff
        )
        await interaction.followup.send(embed=embed)

    async def _is_lamp_on(self) -> bool | None:
        """Returns True if the lamp is on, False if off, None on error."""
        from homeassistant_api import Client
        url = f"{self.ha_server.rstrip('/')}/api"
        try:
            async with Client(url, self.ha_token, use_async=True) as client:
                state = await client.async_get_state(entity_id=self.ha_entity_id)
                return state.state == "on"
        except Exception:
            return None

    async def _set_light_colour(self, rgb: tuple[int, int, int]) -> bool:
        """Send a turn_on service call to Home Assistant with the given RGB colour."""
        from homeassistant_api import Client
        url = f"{self.ha_server.rstrip('/')}/api"
        try:
            async with Client(url, self.ha_token, use_async=True) as client:
                await client.async_trigger_service(
                    "light", "turn_on",
                    entity_id=self.ha_entity_id,
                    rgb_color=list(rgb),
                )
            return True
        except Exception:
            return False


async def setup(bot):
    await bot.add_cog(HomeAssistantCog(bot))
