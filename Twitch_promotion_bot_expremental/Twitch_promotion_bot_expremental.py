import os
import discord
from discord.ext import commands
from discord import app_commands
from twitchAPI.twitch import Twitch
import asyncio
from dotenv import load_dotenv
import json
import logging
from datetime import datetime, timedelta
import sys
import glob

# Load environment variables from .env file
load_dotenv()

# Get sensitive information from .env
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
TWITCH_CLIENT_ID = os.getenv('TWITCH_CLIENT_ID')
TWITCH_CLIENT_SECRET = os.getenv('TWITCH_CLIENT_SECRET')
DISCORD_CHANNEL_ID = int(os.getenv('DISCORD_CHANNEL_ID'))  
ALLOWED_ROLE_IDS = [int(role_id) for role_id in os.getenv('ALLOWED_ROLE_IDS').split(',')]  
ALLOWED_CHANNEL_ID = int(os.getenv('ALLOWED_CHANNEL_ID'))  
LOG_CHANNEL_ID = int(os.getenv('LOG_CHANNEL_ID'))  
GUILD_ID = int(os.getenv('GUILD_ID'))
MAX_LOG_FILES = 7  # Keep logs for 7 days
NOTIFICATION_COOLDOWN = 300  # 5 minutes in seconds between notifications for the same user

# File to store the list of Twitch usernames
TWITCH_USERNAMES_FILE = "twitch_usernames.json"

# Load Twitch usernames from file (if it exists)
def load_twitch_usernames():
    if os.path.exists(TWITCH_USERNAMES_FILE):
        with open(TWITCH_USERNAMES_FILE, "r") as file:
            return json.load(file)
    return []

# Save Twitch usernames to file
def save_twitch_usernames(usernames):
    with open(TWITCH_USERNAMES_FILE, "w") as file:
        json.dump(usernames, file)

# List of Twitch usernames to monitor (loaded from file)
TWITCH_USERNAMES = load_twitch_usernames()

# Initialize logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_logs.txt"),
        logging.StreamHandler()
    ]
)

# Initialize Discord bot with sharding
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents, shard_count=2)  # Adjust shard_count as needed

# Initialize Twitch API
twitch = None

# Global variable to control log upload
log_upload_enabled = True

# Dictionary to track last notification times and retry counts
last_notification_times = {}
connection_retry_count = 0
last_stream_info = {}  # Store last stream info to avoid duplicate messages

async def init_twitch():
    global twitch
    try:
        twitch = await Twitch(TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET)
        logging.info("Successfully initialized Twitch API")
        return True
    except Exception as e:
        logging.error(f"Error initializing Twitch API: {e}")
        return False

async def is_user_live(username):
    try:
        async for user in twitch.get_users(logins=[username]):
            user_id = user.id
            async for stream in twitch.get_streams(user_id=[user_id]):
                # Get game/category information
                game_id = stream.game_id
                game_name = "Unknown Game"
                if game_id:
                    async for game in twitch.get_games(game_ids=[game_id]):
                        game_name = game.name
                
                return {
                    'is_live': True,
                    'title': stream.title,
                    'game': game_name,
                    'viewers': stream.viewer_count,
                    'thumbnail': stream.thumbnail_url
                }
        return {'is_live': False}
    except Exception as e:
        logging.error(f"Error checking if user is live: {e}")
        return {'is_live': False, 'error': str(e)}

async def check_live_status():
    await bot.wait_until_ready()
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    last_statuses = {username: False for username in TWITCH_USERNAMES if username}

    while not bot.is_closed():
        try:
            for username in TWITCH_USERNAMES:
                if not username:
                    continue
                
                # Check if we should skip due to cooldown
                current_time = datetime.now().timestamp()
                last_notified = last_notification_times.get(username, 0)
                if current_time - last_notified < NOTIFICATION_COOLDOWN:
                    continue
                
                stream_info = await is_user_live(username)
                
                if stream_info.get('error'):
                    logging.error(f"Error checking {username}: {stream_info['error']}")
                    continue
                
                if stream_info['is_live'] and not last_statuses[username]:
                    # Check if stream info has changed significantly
                    current_stream_key = f"{username}_{stream_info['title']}_{stream_info['game']}"
                    if current_stream_key != last_stream_info.get(username):
                        # Create rich embed for the notification
                        embed = discord.Embed(
                            title=stream_info['title'],
                            description=f"{username} is now live on Twitch!",
                            color=discord.Color.purple(),
                            url=f"https://twitch.tv/{username}"
                        )
                        embed.add_field(name="Game", value=stream_info['game'], inline=True)
                        embed.add_field(name="Viewers", value=stream_info['viewers'], inline=True)
                        if stream_info['thumbnail']:
                            thumbnail_url = stream_info['thumbnail'].format(width=320, height=180)
                            embed.set_thumbnail(url=thumbnail_url)
                        
                        await channel.send(f"@everyone {username} is live!", embed=embed)
                        last_notification_times[username] = current_time
                        last_stream_info[username] = current_stream_key
                        logging.info(f"Announced live stream for {username} playing {stream_info['game']}")
                    
                    last_statuses[username] = True
                elif not stream_info['is_live']:
                    last_statuses[username] = False
                    last_stream_info.pop(username, None)  # Remove from last stream info when offline
            
            await asyncio.sleep(60)  # Check every 60 seconds
        
        except Exception as e:
            logging.error(f"Error in check_live_status loop: {e}")
            await handle_connection_error()

async def handle_connection_error():
    global connection_retry_count
    
    connection_retry_count += 1
    if connection_retry_count > 10:
        logging.error("Max retries reached. Waiting 10 minutes before restarting...")
        await asyncio.sleep(600)  # Wait 10 minutes
        await restart_bot()
    else:
        wait_time = min(connection_retry_count * 60, 600)  # Max 10 minutes
        logging.error(f"Connection error detected. Waiting {wait_time} seconds before retry...")
        await asyncio.sleep(wait_time)
        
        # Reinitialize Twitch connection
        if not await init_twitch():
            await handle_connection_error()

async def restart_bot():
    logging.info("Attempting to restart bot...")
    python = sys.executable
    os.execl(python, python, *sys.argv)

@bot.event
async def on_ready():
    logging.info(f'Logged in as {bot.user.name}')
    if not await init_twitch():
        await handle_connection_error()
    bot.loop.create_task(check_live_status())

    # Sync commands to the specific guild
    try:
        guild = discord.Object(id=GUILD_ID)
        synced = await bot.tree.sync(guild=guild)
        logging.info(f"Synced {len(synced)} commands to guild {GUILD_ID}.")
    except Exception as e:
        logging.error(f"Error syncing commands: {e}")
        await handle_connection_error()

# Clean up old log files
def clean_up_logs():
    try:
        log_files = glob.glob("bot_logs_*.txt")
        log_files.sort()
        
        # Keep only the most recent MAX_LOG_FILES files
        if len(log_files) > MAX_LOG_FILES:
            for old_log in log_files[:-MAX_LOG_FILES]:
                os.remove(old_log)
                logging.info(f"Removed old log file: {old_log}")
    except Exception as e:
        logging.error(f"Error cleaning up log files: {e}")

# Function to upload logs to a specific channel
async def upload_logs():
    global log_upload_enabled

    if not log_upload_enabled:
        logging.info("Log upload is currently disabled.")
        return

    clean_up_logs()  # Clean up old logs before uploading new one

    # Get the current date for the log file name
    current_date = datetime.now().strftime("%Y-%m-%d")
    log_file_name = f"bot_logs_{current_date}.txt"

    # Close the logging handler to release the file
    for handler in logging.root.handlers[:]:
        if isinstance(handler, logging.FileHandler):
            handler.close()
            logging.root.removeHandler(handler)

    # Rename the log file with the current date
    if os.path.exists("bot_logs.txt"):
        os.rename("bot_logs.txt", log_file_name)

        # Reinitialize the logging handler
        file_handler = logging.FileHandler("bot_logs.txt")
        file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logging.root.addHandler(file_handler)

        # Upload the log file to the specified channel
        log_channel = bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            with open(log_file_name, "rb") as log_file:
                await log_channel.send(file=discord.File(log_file, log_file_name))
            logging.info(f"Uploaded log file {log_file_name} to channel {LOG_CHANNEL_ID}.")
        else:
            logging.error(f"Log channel with ID {LOG_CHANNEL_ID} not found.")
    else:
        logging.warning("No log file found to upload.")

# Schedule log upload every 24 hours
async def schedule_log_upload():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await upload_logs()
        await asyncio.sleep(86400)  # 24 hours in seconds

# Check if the user has any of the allowed roles
def has_allowed_role(interaction: discord.Interaction) -> bool:
    return any(role.id in ALLOWED_ROLE_IDS for role in interaction.user.roles)

# Check if the command is used in the allowed channel
def is_allowed_channel(interaction: discord.Interaction) -> bool:
    return interaction.channel_id == ALLOWED_CHANNEL_ID

# Slash command to toggle log upload
@bot.tree.command(name="toggle_log_upload", description="Turn on or off the log upload feature", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def toggle_log_upload(interaction: discord.Interaction):
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        "Are you sure you want to toggle log upload?", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Toggle cancelled (timed out).", ephemeral=True)
    elif view.value:
        global log_upload_enabled
        log_upload_enabled = not log_upload_enabled
        status = "enabled" if log_upload_enabled else "disabled"
        await interaction.followup.send(f"Log upload is now {status}.", ephemeral=True)
        logging.info(f"{interaction.user} toggled log upload to {status}.")
    else:
        await interaction.followup.send("Toggle cancelled.", ephemeral=True)

# Slash command to add a Twitch user
@bot.tree.command(name="add_twitch_user", description="Add a Twitch user to the monitoring list", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to add")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def adduser(interaction: discord.Interaction, username: str):
    if username in TWITCH_USERNAMES:
        await interaction.response.send_message(
            f"{username} is already in the list: https://twitch.tv/{username}", 
            ephemeral=True
        )
        logging.info(f"{interaction.user} tried to add an already monitored user: {username}")
        return
    
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Add", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        f"Are you sure you want to add {username} to monitoring? Profile: https://twitch.tv/{username}", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Add user cancelled (timed out).", ephemeral=True)
    elif view.value:
        TWITCH_USERNAMES.append(username)
        save_twitch_usernames(TWITCH_USERNAMES)
        await interaction.followup.send(
            f"Added {username} to the monitoring list: https://twitch.tv/{username}", 
            ephemeral=True
        )
        logging.info(f"{interaction.user} added {username} to the monitoring list.")
    else:
        await interaction.followup.send("Add user cancelled.", ephemeral=True)

# Slash command to remove a Twitch user
@bot.tree.command(name="remove_twitch_user", description="Remove a Twitch user from the monitoring list", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(username="The Twitch username to remove")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def removeuser(interaction: discord.Interaction, username: str):
    if username not in TWITCH_USERNAMES:
        await interaction.response.send_message(
            f"{username} is not in the list: https://twitch.tv/{username}", 
            ephemeral=True
        )
        logging.info(f"{interaction.user} tried to remove a non-monitored user: {username}")
        return
    
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Remove", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        f"Are you sure you want to remove {username} from monitoring? Profile: https://twitch.tv/{username}", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Remove user cancelled (timed out).", ephemeral=True)
    elif view.value:
        TWITCH_USERNAMES.remove(username)
        save_twitch_usernames(TWITCH_USERNAMES)
        await interaction.followup.send(
            f"Removed {username} from the monitoring list: https://twitch.tv/{username}", 
            ephemeral=True
        )
        logging.info(f"{interaction.user} removed {username} from the monitoring list.")
    else:
        await interaction.followup.send("Remove user cancelled.", ephemeral=True)

# Slash command to list monitored Twitch users (in embed)
@bot.tree.command(name="list_twitch_users", description="List all monitored Twitch users", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def listusers(interaction: discord.Interaction):
    if not TWITCH_USERNAMES:
        await interaction.response.send_message("No users are currently being monitored.", ephemeral=True)
        logging.info(f"{interaction.user} listed monitored users: No users being monitored.")
    else:
        # Create an embed to display the list of monitored users
        embed = discord.Embed(
            title="Monitored Twitch Channels",
            description="Here are the Twitch users currently being monitored:",
            color=discord.Color.purple()
        )
        for username in TWITCH_USERNAMES:
            embed.add_field(name="", value=f"[{username}](https://twitch.tv/{username})", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        logging.info(f"{interaction.user} listed monitored users: {TWITCH_USERNAMES}")

# Slash command to change bot status
@bot.tree.command(name="set_twitch_bot_status", description="Change the bot's status", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(status="The status to set (online, idle, dnd, invisible)")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def setstatus(interaction: discord.Interaction, status: str):
    status = status.lower()
    valid_statuses = ["online", "idle", "dnd", "invisible"]
    if status not in valid_statuses:
        await interaction.response.send_message(f"Invalid status. Valid options are: {', '.join(valid_statuses)}", ephemeral=True)
        logging.info(f"{interaction.user} tried to set an invalid status: {status}")
        return
    
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        f"Are you sure you want to change bot status to {status}?", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Status change cancelled (timed out).", ephemeral=True)
    elif view.value:
        await bot.change_presence(status=discord.Status[status])
        await interaction.followup.send(f"Bot status changed to {status}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot status to {status}.")
    else:
        await interaction.followup.send("Status change cancelled.", ephemeral=True)

# Slash command to change bot activity
@bot.tree.command(name="set_twitch_bot_activity", description="Change the bot's activity", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(activity_type="The type of activity (playing, streaming, listening, watching)")
@app_commands.describe(activity_name="The name of the activity")
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def setactivity(interaction: discord.Interaction, activity_type: str, activity_name: str):
    activity_type = activity_type.lower()
    valid_activities = ["playing", "streaming", "listening", "watching"]
    if activity_type not in valid_activities:
        await interaction.response.send_message(f"Invalid activity type. Valid options are: {', '.join(valid_activities)}", ephemeral=True)
        logging.info(f"{interaction.user} tried to set an invalid activity type: {activity_type}")
        return
    
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        f"Are you sure you want to change bot activity to {activity_type} {activity_name}?", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Activity change cancelled (timed out).", ephemeral=True)
    elif view.value:
        if activity_type == "playing":
            activity = discord.Game(name=activity_name)
        elif activity_type == "streaming":
            activity = discord.Streaming(name=activity_name, url="https://twitch.tv/example")
        elif activity_type == "listening":
            activity = discord.Activity(type=discord.ActivityType.listening, name=activity_name)
        elif activity_type == "watching":
            activity = discord.Activity(type=discord.ActivityType.watching, name=activity_name)
        await bot.change_presence(activity=activity)
        await interaction.followup.send(f"Bot activity changed to {activity_type} {activity_name}.", ephemeral=True)
        logging.info(f"{interaction.user} changed bot activity to {activity_type} {activity_name}.")
    else:
        await interaction.followup.send("Activity change cancelled.", ephemeral=True)

# Slash command to clear bot activity
@bot.tree.command(name="clear_twitch_bot_activity", description="Clear the bot's current activity", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def clearactivity(interaction: discord.Interaction):
    # Create confirmation view
    class ConfirmView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=30)
            self.value = None

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.green)
        async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = True
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
        async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
            if button_interaction.user != interaction.user:
                await button_interaction.response.send_message("You didn't initiate this command.", ephemeral=True)
                return
            self.value = False
            self.stop()

    view = ConfirmView()
    await interaction.response.send_message(
        "Are you sure you want to clear the bot's activity?", 
        view=view, 
        ephemeral=True
    )
    
    await view.wait()
    if view.value is None:
        await interaction.followup.send("Clear activity cancelled (timed out).", ephemeral=True)
    elif view.value:
        await bot.change_presence(activity=None)
        await interaction.followup.send("Bot activity cleared.", ephemeral=True)
        logging.info(f"{interaction.user} cleared bot activity.")
    else:
        await interaction.followup.send("Clear activity cancelled.", ephemeral=True)

# Slash command to manually check for live streams
@bot.tree.command(name="check_new_twitch_live_stream", description="Manually check for live streams", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def checklive(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)  # Acknowledge the interaction immediately
    live_users = []

    for username in TWITCH_USERNAMES:
        stream_info = await is_user_live(username)
        if stream_info.get('is_live'):
            live_users.append((username, stream_info))

    if live_users:
        # Create a message with the live users
        message = "The following users are now live on Twitch:\n"
        for user, info in live_users:
            message += f"- {user}: https://twitch.tv/{user}\n"
            message += f"  Title: {info['title']}\n"
            message += f"  Game: {info['game']}\n"
            message += f"  Viewers: {info['viewers']}\n\n"
        
        # Create a view with buttons for confirmation
        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=30)  # Timeout after 30 seconds
                self.value = None

            @discord.ui.button(label="Post", style=discord.ButtonStyle.green)
            async def confirm(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = True
                self.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.red)
            async def cancel(self, button_interaction: discord.Interaction, button: discord.ui.Button):
                if button_interaction.user != interaction.user:
                    await button_interaction.response.send_message("You are not the user who initiated this command.", ephemeral=True)
                    return
                self.value = False
                self.stop()

        # Send the message with buttons
        view = ConfirmView()
        await interaction.followup.send(message, view=view, ephemeral=True)

        # Wait for the user to respond
        await view.wait()

        if view.value is None:
            await interaction.followup.send("Confirmation timed out.", ephemeral=True)
            logging.info(f"{interaction.user} timed out while checking live streams.")
        elif view.value:
            # Post the results to the channel with rich embeds
            channel = bot.get_channel(DISCORD_CHANNEL_ID)
            for user, info in live_users:
                embed = discord.Embed(
                    title=info['title'],
                    description=f"{user} is now live on Twitch!",
                    color=discord.Color.purple(),
                    url=f"https://twitch.tv/{user}"
                )
                embed.add_field(name="Game", value=info['game'], inline=True)
                embed.add_field(name="Viewers", value=info['viewers'], inline=True)
                if info['thumbnail']:
                    thumbnail_url = info['thumbnail'].format(width=320, height=180)
                    embed.set_thumbnail(url=thumbnail_url)
                await channel.send(f"@everyone {user} is live!", embed=embed)
            
            await interaction.followup.send("Live stream results posted.", ephemeral=True)
            logging.info(f"{interaction.user} posted live stream results: {[user[0] for user in live_users]}")
        else:
            await interaction.followup.send("Live stream results not posted.", ephemeral=True)
            logging.info(f"{interaction.user} canceled posting live stream results.")
    else:
        await interaction.followup.send("No users are currently live.", ephemeral=True)
        logging.info(f"{interaction.user} checked for live streams: No users live.")

# Slash command to display help (in embed)
@bot.tree.command(name="twitch_bot_help", description="Display all commands and how to use them", guild=discord.Object(id=GUILD_ID))
@app_commands.check(has_allowed_role)
@app_commands.check(is_allowed_channel)
async def help(interaction: discord.Interaction):
    # Create an embed to display the help message
    embed = discord.Embed(
        title="Bot Commands",
        description="Here are all the available commands and how to use them:",
        color=discord.Color.purple()
    )
    embed.add_field(
        name="1. **/add_twitch_user**",
        value="Add a Twitch user to the monitoring list.\n**Usage:** `/adduser username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="2. **/remove_twitch_user**",
        value="Remove a Twitch user from the monitoring list.\n**Usage:** `/removeuser username:<Twitch username>`",
        inline=False
    )
    embed.add_field(
        name="3. **/list_twitch_users**",
        value="List all monitored Twitch users.\n**Usage:** `/listusers`",
        inline=False
    )
    embed.add_field(
        name="4. **/set_twitch_bot_status**",
        value="Change the bot's status (online, idle, dnd, invisible).\n**Usage:** `/setstatus status:<status>`",
        inline=False
    )
    embed.add_field(
        name="5. **/set_twitch_bot_activity**",
        value="Change the bot's activity (playing, streaming, listening, watching).\n**Usage:** `/setactivity activity_type:<type> activity_name:<name>`",
        inline=False
    )
    embed.add_field(
        name="6. **/clear_twitch_bot_activity**",
        value="Clear the bot's current activity.\n**Usage:** `/clearactivity`",
        inline=False
    )
    embed.add_field(
        name="7. **/check_new_twitch_live_stream**",
        value="Manually check for live streams of monitored users.\n**Usage:** `/checklive`",
        inline=False
    )
    embed.add_field(
        name="8. **/twitch_bot_help**",
        value="Display all commands and how to use them.\n**Usage:** `/help`",
        inline=False
    )
    embed.add_field(
        name="9. **/toggle_log_upload**",
        value="Turn on or off the log upload feature.\n**Usage:** `/toggle_log_upload`",
        inline=False
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)
    logging.info(f"{interaction.user} requested help.")

# Error handler for role and channel checks
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if not has_allowed_role(interaction):
            await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use a command without permission.")
        elif not is_allowed_channel(interaction):
            await interaction.response.send_message("This command can only be used in a specific channel.", ephemeral=True)
            logging.warning(f"{interaction.user} tried to use a command in the wrong channel.")
    else:
        await interaction.response.send_message(f"An error occurred: {error}", ephemeral=True)
        logging.error(f"An error occurred: {error}")

# Windows event loop policy fix
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Use the setup_hook to schedule tasks after the bot has started
@bot.event
async def setup_hook():
    bot.loop.create_task(schedule_log_upload())

try:
    bot.run(DISCORD_TOKEN)
except Exception as e:
    logging.error(f"Bot crashed: {e}")
    # Attempt to restart after a delay
    asyncio.run(asyncio.sleep(60))
    python = sys.executable
    os.execl(python, python, *sys.argv)