# VPS Discord Bot — Railway Ready

Railway automatically installs Python dependencies from `requirements.txt` and starts `bot.py` using `railway.json`. No terminal/command setup is needed after importing the project.

## Deploy

1. Upload these files to a GitHub repository (keep `bot.py`, `requirements.txt`, and `railway.json` in the repository root).
2. In Railway, create a project and deploy the GitHub repository.
3. Open the Railway service's **Variables** and add:
   - `DISCORD_BOT_TOKEN` = your Discord bot token
4. In the Discord Developer Portal, enable the **Server Members Intent** and **Message Content Intent** if the bot's features use them.
5. Add your server access links, one per line, to `server.txt` in the persistent volume. If no file exists, the bot creates an empty one; it cannot issue VPS links until you add links.

## Set the bot token locally

When you run `python bot.py` in a terminal without `DISCORD_BOT_TOKEN` set, the bot securely prompts for the token and saves it in `.env`. The token is not displayed while typing, and `.env` is excluded from Git. You can also set `DISCORD_BOT_TOKEN` directly in your operating system environment.

Railway runs the bot without an interactive terminal, so enter the token in the service's **Variables** page instead; the bot prints this instruction and exits cleanly if the variable is missing.

## Keep data across redeploys

Add a Railway Volume to the service and mount it at `/data`. Set the variable `RAILWAY_VOLUME_MOUNT_PATH=/data`. The bot stores `server.txt` and `bot_data.json` there so link inventory and claim data survive restarts/redeploys.

## Notes

- Never put your bot token in `bot.py` or commit it to GitHub. A token was previously present in the source code; reset it immediately in the Discord Developer Portal, then use the new token as `DISCORD_BOT_TOKEN` or enter it into the local `.env` prompt.
- Railway cannot know your VPS access links; add them to `server.txt` on the mounted volume.
- The bot's configured Discord channel/role/user IDs remain in `bot.py`; update them if they differ in your server.
