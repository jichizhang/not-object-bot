import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from utils.database import init_database, can_earn_daily_message_reward, process_daily_message_reward, remove_pending_songs

# Load environment variables
load_dotenv()

# Bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True

class NotObjectBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        """Called when the bot is starting up"""
        # Load cogs
        await self.load_extension('cogs.coins')
        await self.load_extension('cogs.shooting_star')
        await self.load_extension('cogs.photos')
        await self.load_extension('cogs.llm')
        await self.load_extension('cogs.custom_role')
        await self.load_extension('cogs.sotd')
        await self.load_extension('cogs.snap')
        await self.load_extension('cogs.birthday')
        await self.load_extension('cogs.uwulock')
        await self.load_extension('cogs.msgmover')
        await self.load_extension('cogs.homeassistant')
        
        # Sync commands
        # await self.tree.sync()

    async def on_ready(self):
        print(f'{self.user} has connected to Discord!')
        init_database()
        
        # Start the shooting star task
        shooting_star_cog = self.get_cog('ShootingStarCog')
        if shooting_star_cog:
            shooting_star_cog.shooting_star_task.start()

bot = NotObjectBot()

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        await bot.process_commands(message)
        return

    # Check if this is the user's first message of the day (UTC) for coin reward
    user_id = message.author.id
    username = message.author.display_name
    
    if can_earn_daily_message_reward(user_id):
        # Check for Twitch subscriber multipliers
        multiplier = 1.0
        
        # Server owner always gets 1x multiplier
        owner_user_id = os.getenv('OWNER_USER_ID')
        if owner_user_id and str(user_id) == str(owner_user_id):
            multiplier = 1.0
        else:
            # Check for Twitch subscriber roles
            member = message.guild.get_member(user_id)
            if member:
                twitch_tier_1_role_id = os.getenv('TWITCH_TIER_1_ROLE_ID')
                twitch_tier_2_role_id = os.getenv('TWITCH_TIER_2_ROLE_ID')
                twitch_tier_3_role_id = os.getenv('TWITCH_TIER_3_ROLE_ID')

                roles = member.roles
                tier_1_role = discord.utils.get(message.guild.roles, id=int(twitch_tier_1_role_id)) if twitch_tier_1_role_id else None
                tier_2_role = discord.utils.get(message.guild.roles, id=int(twitch_tier_2_role_id)) if twitch_tier_2_role_id else None
                tier_3_role = discord.utils.get(message.guild.roles, id=int(twitch_tier_3_role_id)) if twitch_tier_3_role_id else None
                
                if tier_3_role and tier_3_role in roles:
                    multiplier = 2.0
                elif tier_2_role and tier_2_role in roles:
                    multiplier = 1.4
                elif tier_1_role and tier_1_role in roles:
                    multiplier = 1.2
        
        # Calculate coin amount with multiplier
        base_coins = 200
        total_coins = int(base_coins * multiplier)
        
        # Award coins for first message of the day with multiplier
        process_daily_message_reward(user_id, username, total_coins)
    
    # Process commands
    await bot.process_commands(message)

@bot.event
async def on_voice_state_update(member, before, after):
    if not before.channel and after.channel:
        username = member.display_name

        vc_role_id = os.getenv('VC_ROLE_ID')
        if vc_role_id:
            await bot.get_channel(after.channel.id).send(f"<@&{vc_role_id}> {username} has joined {after.channel.name}!")

@bot.event
async def on_member_remove(member):
    remove_pending_songs(member.id)

# Run the bot
if __name__ == "__main__":
    bot.run(os.getenv('DISCORD_TOKEN'))
