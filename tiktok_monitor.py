import asyncio
import csv
import json
import os
import time
import requests

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
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
HEARTBEAT_INTERVAL = 12 * 60 * 60  # 12 hours in seconds

# Runs start a little earlier or later than every 5 minutes, so a run that
# is only a minute or two short of the interval still counts as "due".
# Without this, a heartbeat can slip to the next run.
HEARTBEAT_TOLERANCE = 2 * 60  # 2 minutes in seconds

# A LIVE only counts as ended after the account looks offline on this many
# checks in a row (about 4.7 minutes apart, so 3 checks is roughly 10
# minutes). This stops a short TikTok glitch from splitting one live into
# two. The recorded end time is the FIRST offline check, so the waiting
# does not inflate the duration. Starts are announced immediately.
OFFLINE_CONFIRM_CHECKS = 3

STATE_FILE = Path("live_state.json")

# One row per finished LIVE. Opens in Excel / Google Sheets, and GitHub
# shows it as a table.
HISTORY_FILE = Path("live_history.csv")
HISTORY_COLUMNS = [
    "username",
    "start_utc",
    "end_utc",
    "start_local",
    "end_local",
    "duration_seconds",
    "duration",
]

# Times in the history file and in Telegram messages use this time zone
# (the *_utc columns always stay in UTC).
try:
    LOCAL_TZ = ZoneInfo("Europe/Sofia")
except Exception:
    LOCAL_TZ = timezone.utc


# ==========================================
# TIME FORMATTING
# ==========================================

def format_time(timestamp, tz):

    return datetime.fromtimestamp(timestamp, tz).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def format_short(timestamp):

    return datetime.fromtimestamp(timestamp, LOCAL_TZ).strftime(
        "%d %b %H:%M"
    )


def format_duration(seconds):

    # Checks run every 5 minutes, so seconds would be false precision.
    minutes = max(0, int(round(seconds / 60)))

    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}h {minutes:02d}m"

    if minutes:
        return f"{minutes}m"

    return "<1m"


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
# LIVE HISTORY
# ==========================================

def ensure_history_file():

    if HISTORY_FILE.exists():
        return

    with open(HISTORY_FILE, "w", newline="") as file:
        csv.writer(file).writerow(HISTORY_COLUMNS)


def append_history(username, start, end):

    ensure_history_file()

    duration = max(0, end - start)

    with open(HISTORY_FILE, "a", newline="") as file:
        csv.writer(file).writerow([
            username,
            format_time(start, timezone.utc),
            format_time(end, timezone.utc),
            format_time(start, LOCAL_TZ),
            format_time(end, LOCAL_TZ),
            int(round(duration)),
            format_duration(duration),
        ])


# ==========================================
# LOAD / SAVE STATUS
# ==========================================

def load_state():

    if not STATE_FILE.exists():
        return {
            "accounts": {},
            "last_offline_heartbeat": 0,
            "live_since": {},
            "offline_checks": {},
            "offline_since": {}
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
        ),
        # When each currently-live account was first seen live
        "live_since": {
            key: float(value)
            for key, value in data.get("live_since", {}).items()
            if isinstance(value, (int, float))
        },
        # Consecutive offline checks for an account that was LIVE, and when
        # the first of them happened (see OFFLINE_CONFIRM_CHECKS)
        "offline_checks": {
            key: int(value)
            for key, value in data.get("offline_checks", {}).items()
            if isinstance(value, (int, float))
        },
        "offline_since": {
            key: float(value)
            for key, value in data.get("offline_since", {}).items()
            if isinstance(value, (int, float))
        }
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
    live_since = state["live_since"]
    offline_checks = state["offline_checks"]
    offline_since = state["offline_since"]

    # Accounts that look offline but are still inside the confirmation wait
    pending = set()

    updated_status = previous_status.copy()
    current_status = {}

    ensure_history_file()

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

            # Remember when this LIVE was first seen. The real start can be
            # up to one check interval (about 5 minutes) earlier.
            if is_live and username not in live_since:
                live_since[username] = time.time()

            # Back online (or never went offline): cancel any stop countdown
            if is_live:
                offline_checks.pop(username, None)
                offline_since.pop(username, None)

            # An account that was LIVE now looks offline: count it, and only
            # confirm the stop after OFFLINE_CONFIRM_CHECKS checks in a row.
            confirmed_stop = False

            if not is_live and was_live is True:

                offline_since.setdefault(username, time.time())
                offline_checks[username] = (
                    offline_checks.get(username, 0) + 1
                )

                if offline_checks[username] >= OFFLINE_CONFIRM_CHECKS:
                    confirmed_stop = True
                else:
                    pending.add(username)
                    print(
                        f"@{username}: offline check "
                        f"{offline_checks[username]}/"
                        f"{OFFLINE_CONFIRM_CHECKS}, "
                        "waiting before ending the LIVE"
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

            elif confirmed_stop:

                start = live_since.get(username)

                # The LIVE ended when it first looked offline
                end = offline_since.get(username, time.time())

                lines = [f"⚫ @{username} HAS STOPPED LIVE!", ""]

                if start:
                    lines += [
                        f"Started: {format_short(start)}",
                        f"Ended: {format_short(end)}",
                        f"Duration: {format_duration(end - start)}",
                        "",
                    ]

                lines.append(f"https://www.tiktok.com/@{username}")

                message = "\n".join(lines)

                try:
                    send_telegram(message)
                    print(f"OFFLINE notification sent: @{username}")
                    updated_status[username] = False

                    if start:
                        try:
                            append_history(username, start, end)
                            print(
                                f"Saved LIVE session: @{username}, "
                                f"{format_duration(end - start)}"
                            )
                        except Exception as error:
                            print(f"Could not save history: {error}")
                    else:
                        print(
                            f"No start time known for @{username}; "
                            "session not saved."
                        )

                    live_since.pop(username, None)
                    offline_checks.pop(username, None)
                    offline_since.pop(username, None)

                except Exception as error:
                    print(f"Telegram notification failed: {error}")

            elif username in pending:

                # Still counted as LIVE until the stop is confirmed
                pass

            else:

                updated_status[username] = is_live

                if not is_live:
                    live_since.pop(username, None)
                    offline_checks.pop(username, None)
                    offline_since.pop(username, None)

        except Exception as error:

            print(f"ERROR checking @{username}: {error}")

            # Do not change the saved status on API errors.
            # This prevents false OFFLINE notifications.

    # ==========================================
    # SILENT OFFLINE HEARTBEAT
    # ==========================================

    now = time.time()

    # An account still inside the confirmation wait counts as LIVE here too
    offline_accounts = [
        username
        for username, status in current_status.items()
        if status is False and username not in pending
    ]

    if (
        offline_accounts
        and now - state["last_offline_heartbeat"]
        >= HEARTBEAT_INTERVAL - HEARTBEAT_TOLERANCE
    ):

        status_lines = []

        for username in usernames:

            if username in current_status:
                status = (
                    "🔴 LIVE"
                    if current_status[username] or username in pending
                    else "⚫ OFFLINE"
                )
            else:
                status = "⚠️ CHECK FAILED"

            status_lines.append(f"@{username}: {status}")

        message = (
            "🟢 TIKTOK BOT IS ALIVE\n\n"
            + "\n".join(status_lines)
            + "\n\nNext heartbeat in approximately 12 hours."
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
    state["live_since"] = live_since
    state["offline_checks"] = offline_checks
    state["offline_since"] = offline_since

    save_state(state)

    print("=" * 40)
    print("MONITORING CHECK COMPLETED")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())
