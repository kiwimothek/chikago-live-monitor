
import asyncio
import json
import os
import time
import requests

from pathlib import Path
from TikTokLive import TikTokLiveClient


# ==========================================
# CONFIGURATION
# ==========================================

# ENTER YOUR TWO TIKTOK USERNAMES HERE
USERNAME_1 = "user4525838653694"
USERNAME_2 = "ventsislav.angelo06"

# TELEGRAM CREDENTIALS (GITHUB SECRETS)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# SETTINGS
CHECK_INTERVAL = 5       # GitHub runs every 5 minutes
HEARTBEAT_INTERVAL = 15 * 60  # 15 minutes in seconds

STATE_FILE = Path("live_state.json")


# ==========================================
# TELEGRAM
# ==========================================

def send_telegram(message, silent=False):

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_notification": silent
        },
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError(f"Telegram error: {result}")


# ==========================================
# TIKTOK LIVE CHECKER
# ==========================================

async def check_live(username):

    client = TikTokLiveClient(unique_id=username)

    return await client.is_live()


# ==========================================
# LOAD / SAVE STATUS
# ==========================================

def load_state():

    if not STATE_FILE.exists():
        return {
            "accounts": {},
            "last_offline_heartbeat": 0
        }

    with open(STATE_FILE, "r") as file:
        data = json.load(file)

    # Support the old state-file format too
    if "accounts" not in data:
        data = {
            "accounts": {
                key: value
                for key, value in data.items()
                if isinstance(value, bool)
            },
            "last_offline_heartbeat": 0
        }

    return {
        "accounts": data.get("accounts", {}),
        "last_offline_heartbeat": data.get(
            "last_offline_heartbeat", 0
        )
    }


def save_state(state):

    with open(STATE_FILE, "w") as file:
        json.dump(state, file, indent=4)


# ==========================================
# MAIN MONITOR
# ==========================================

async def main():

    usernames = [
        USERNAME_1.strip().lstrip("@"),
        USERNAME_2.strip().lstrip("@")
    ]

    usernames = list(dict.fromkeys(
        username for username in usernames if username
    ))

    state = load_state()
    previous_status = state["accounts"]

    updated_status = previous_status.copy()
    current_status = {}

    print("=" * 40)
    print("TIKTOK LIVE MONITOR")
    print("=" * 40)

    # Check both accounts
    for username in usernames:

        try:

            is_live = await check_live(username)

            current_status[username] = is_live

            was_live = previous_status.get(username)

            print(
                f"@{username}: "
                f"{'LIVE' if is_live else 'OFFLINE'}"
            )

            # ----------------------------------
            # ACCOUNT STARTED LIVE
            # ----------------------------------

            if is_live and was_live is not True:

                message = (
                    f"🔴 @{username} IS NOW LIVE!\n\n"
                    f"https://www.tiktok.com/@{username}/live"
                )

                try:
                    send_telegram(message)
                    print(f"LIVE notification sent: @{username}")
                    updated_status[username] = True

                except Exception as error:
                    print(f"Telegram notification failed: {error}")

            # ----------------------------------
            # ACCOUNT STOPPED LIVE
            # ----------------------------------

            elif not is_live and was_live is True:

                message = (
                    f"⚫ @{username} HAS STOPPED LIVE!\n\n"
                    f"https://www.tiktok.com/@{username}"
                )

                try:
                    send_telegram(message)
                    print(f"OFFLINE notification sent: @{username}")
                    updated_status[username] = False

                except Exception as error:
                    print(f"Telegram notification failed: {error}")

            else:

                updated_status[username] = is_live

        except Exception as error:

            print(f"ERROR checking @{username}: {error}")

            # Do not change the saved status on API errors.
            # This prevents false OFFLINE notifications.

    # ==========================================
    # SILENT OFFLINE HEARTBEAT
    # ==========================================

    now = time.time()

    offline_accounts = [
        username
        for username, status in current_status.items()
        if status is False
    ]

    if (
        offline_accounts
        and now - state["last_offline_heartbeat"]
        >= HEARTBEAT_INTERVAL
    ):

        status_lines = []

        for username in usernames:

            if username in current_status:
                status = (
                    "🔴 LIVE"
                    if current_status[username]
                    else "⚫ OFFLINE"
                )
            else:
                status = "⚠️ CHECK FAILED"

            status_lines.append(f"@{username}: {status}")

        message = (
            "🟢 TIKTOK BOT IS ALIVE\n\n"
            + "\n".join(status_lines)
            + "\n\nNext heartbeat in approximately 15 minutes."
        )

        try:

            send_telegram(message, silent=True)

            print("Silent heartbeat sent.")

            state["last_offline_heartbeat"] = now

        except Exception as error:

            print(f"Heartbeat failed: {error}")

    # ==========================================
    # SAVE EVERYTHING
    # ==========================================

    state["accounts"] = updated_status

    save_state(state)

    print("=" * 40)
    print("MONITORING CHECK COMPLETED")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())
