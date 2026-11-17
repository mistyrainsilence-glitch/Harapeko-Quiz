@echo off
cd /d %~dp0
set DISCORD_TOKEN=YOUR_BOT_TOKEN
set GUILD_ID=YOUR_SERVER_ID
set RESULT_CHANNEL_ID=YOUR_RESULT_CHANNEL_ID
py app\quiz_bot.py
pause
