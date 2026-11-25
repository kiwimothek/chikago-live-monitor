import asyncio
import base64
import csv
import hashlib
import json
import os
import time
import requests

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from TikTokLive import TikTokLiveClient

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    # Followers are switched off if the library is missing; the monitor
    # itself keeps working and only messages the owner.
    Fernet = None
    InvalidToken = Exception


# ==========================================
# CONFIGURATION
# ==========================================

# ENTER YOUR TWO TIKTOK USERNAMES HERE
USERNAME_1 = "user4525838653694"
USERNAME_2 = "ventsislav.angelo06"

# TELEGRAM CREDENTIALS (GITHUB SECRETS)
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# OPTIONAL: lets other people send /follow to get the same alerts. It is the
# password that encrypts the follower list (subscribers.enc), because this
# repository is public. Without it the bot only messages the owner.
SUBSCRIBERS_KEY = os.environ.get("SUBSCRIBERS_KEY", "").strip()

# SETTINGS
CHECK_INTERVAL = 5       # GitHub runs every 5 minutes
HEARTBEAT_INTERVAL = 7 * 24 * 60 * 60  # 7 days in seconds

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

# Followers who sent /follow (encrypted list of Telegram chat IDs)
SUBSCRIBERS_FILE = Path("subscribers.enc")
MAX_SUBSCRIBERS = 20

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

def describe(error):

    # Network errors from requests contain the full URL, and the URL
    # contains the bot token. Never let it reach the (public) logs.
    return str(error).replace(BOT_TOKEN, "***")


def send_telegram(message, silent=False, chat_id=None):

    # chat_id=None means the owner (TELEGRAM_CHAT_ID)
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID if chat_id is None else chat_id,
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
# FOLLOWERS (/follow, /unfollow)
# ==========================================
#
# Anyone can message the bot /follow. Their chat ID is read from Telegram
# (getUpdates), stored ENCRYPTED in subscribers.enc, and from then on they
# get the same LIVE / STOPPED alerts as the owner. The TikTok check still
# happens only once per run. Chat IDs are never printed: the logs of a
# public repository are public.

FOLLOW_TEXT = (
    "✅ You are now following. You will get an alert when a monitored "
    "account goes LIVE or stops (checked about every 5 minutes).\n\n"
    "Your chat ID is stored encrypted. Send /unfollow to stop."
)


def make_fernet():

    if not SUBSCRIBERS_KEY or Fernet is None:
        return None

    # Slow key derivation, because the encrypted file is public and
    # anyone could try to guess the password offline.
    raw = hashlib.pbkdf2_hmac(
        "sha256",
        SUBSCRIBERS_KEY.encode(),
        b"tiktok-live-monitor/subscribers",
        200_000
    )

    return Fernet(base64.urlsafe_b64encode(raw))


def load_subscribers(fernet):

    # Raises if the file cannot be read, so a wrong key can never lead
    # to the real list being overwritten with an empty one.

    if not SUBSCRIBERS_FILE.exists():
        return []

    try:
        data = fernet.decrypt(SUBSCRIBERS_FILE.read_bytes())
        return [int(chat_id) for chat_id in json.loads(data)]

    except (InvalidToken, ValueError, TypeError):
        raise RuntimeError(
            "could not decrypt subscribers.enc (wrong SUBSCRIBERS_KEY?)"
        ) from None


def save_subscribers(fernet, chat_ids):

    SUBSCRIBERS_FILE.write_bytes(
        fernet.encrypt(json.dumps(sorted(chat_ids)).encode())
    )


def broadcast(message, subs):

    # The owner is sent first. If that fails the error is raised, so the
    # caller keeps its old state and tries again on the next run.
    send_telegram(message)

    sent = 0
    failed = 0

    for chat_id in list(subs["ids"]):

        try:
            send_telegram(message, chat_id=chat_id)
            sent += 1

        except requests.HTTPError as error:
            failed += 1

            # 403 = the follower blocked the bot or deleted the chat
            if (
                error.response is not None
                and error.response.status_code == 403
            ):
                subs["ids"].remove(chat_id)
                subs["dirty"] = True

        except Exception:
            failed += 1

    if sent or failed:
        print(f"Followers notified: {sent}, failed: {failed}")


# ------------------------------------------
# Command handlers. Each one gets a small
# "ctx" dict and returns (reply, owner_note):
# the reply goes to whoever sent the command,
# the owner_note (or None) is sent silently
# to the owner.
# ------------------------------------------

def monitored_usernames():

    usernames = [
        USERNAME_1.strip().lstrip("@"),
        USERNAME_2.strip().lstrip("@")
    ]

    return list(dict.fromkeys(
        username for username in usernames if username
    ))


def cmd_help(ctx):

    lines = [
        "👋 This bot sends an alert when the monitored TikTok accounts "
        "go LIVE or stop being LIVE.",
        "",
        "Commands:",
    ]

    for name, description, owner_only, _ in COMMANDS:

        if owner_only and not ctx["is_owner"]:
            continue

        lines.append(f"/{name} - {description}")

    lines += ["", "Replies can take up to 5 minutes."]

    return "\n".join(lines), None


def cmd_status(ctx):

    state = ctx["state"]

    lines = ["📡 Status (checked about every 5 minutes):", ""]

    for username in monitored_usernames():

        is_live = state["accounts"].get(username)

        if is_live is True:

            since = state["live_since"].get(username)

            extra = ""

            if since:
                extra = (
                    f" since {format_short(since)} "
                    f"({format_duration(time.time() - since)})"
                )

            lines.append(f"🔴 @{username} is LIVE{extra}")

        elif is_live is False:
            lines.append(f"⚫ @{username} is offline")

        else:
            lines.append(f"❔ @{username}: not checked yet")

    return "\n".join(lines), None


def cmd_history(ctx):

    try:
        with open(HISTORY_FILE, newline="") as file:
            rows = list(csv.DictReader(file))
    except FileNotFoundError:
        rows = []

    if not rows:
        return "No finished LIVEs recorded yet.", None

    lines = [f"🗓 Last {min(5, len(rows))} LIVEs (newest first):", ""]

    for row in rows[-5:][::-1]:

        # start_local looks like "2026-11-18 21:06:15"; show "11-18 21:06"
        lines.append(
            f"@{row['username']}: "
            f"{row['start_local'][5:16]} to {row['end_local'][5:16]} "
            f"({row['duration']})"
        )

    return "\n".join(lines), None


def cmd_followers(ctx):

    count = len(ctx["subs"]["ids"])

    return f"👥 Followers: {count} of {MAX_SUBSCRIBERS}", None


def cmd_follow(ctx):

    subs = ctx["subs"]
    chat_id = ctx["chat_id"]

    if ctx["is_owner"]:
        return "You already get every alert, you own this bot. 🙂", None

    if chat_id in subs["ids"]:
        return "You are already following. Send /unfollow to stop.", None

    if len(subs["ids"]) >= MAX_SUBSCRIBERS:
        return "Sorry, the follower list is full.", None

    subs["ids"].append(chat_id)
    subs["dirty"] = True

    return (
        FOLLOW_TEXT,
        f"👤 New follower. Followers: {len(subs['ids'])}"
    )


def cmd_unfollow(ctx):

    subs = ctx["subs"]
    chat_id = ctx["chat_id"]

    if chat_id not in subs["ids"]:
        return "You were not following.", None

    subs["ids"].remove(chat_id)
    subs["dirty"] = True

    return (
        "✅ You stopped following. Send /follow to return.",
        f"👤 A follower left. Followers: {len(subs['ids'])}"
    )


# ------------------------------------------
# THE COMMAND LIST. To add a command: write a
# cmd_... function above and add one line here.
# (name, description shown in Telegram's menu,
#  owner_only, handler)
# Names: lowercase letters, digits, underscore.
# The bot copies this list into Telegram's menu
# by itself, so BotFather is not needed.
# ------------------------------------------

COMMANDS = [
    ("status",    "Who is LIVE right now",                False, cmd_status),
    ("history",   "Last 5 finished LIVEs",                False, cmd_history),
    ("follow",    "Get an alert when they go LIVE",       False, cmd_follow),
    ("unfollow",  "Stop getting alerts",                  False, cmd_unfollow),
    ("help",      "Show all commands",                    False, cmd_help),
    ("followers", "How many people follow (owner only)",  True,  cmd_followers),
]


def sync_command_menu(state):

    # Copies COMMANDS into Telegram's own command menu (the "/" list and the
    # Menu button). Only runs when the list changed. Everyone sees the normal
    # commands; the owner's chat also shows the owner-only ones.

    everyone = [
        (name, description)
        for name, description, owner_only, _ in COMMANDS
        if not owner_only
    ]

    owner = [
        (name, description)
        for name, description, _, _ in COMMANDS
    ]

    fingerprint = hashlib.sha256(
        json.dumps([everyone, owner]).encode()
    ).hexdigest()[:16]

    if state.get("commands_hash") == fingerprint:
        return

    def set_commands(commands, scope=None):

        data = {
            "commands": json.dumps([
                {"command": name, "description": description}
                for name, description in commands
            ])
        }

        if scope:
            data["scope"] = json.dumps(scope)

        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setMyCommands",
            data=data,
            timeout=20
        )

        response.raise_for_status()

        if not response.json().get("ok"):
            raise RuntimeError("Telegram setMyCommands returned an error")

    set_commands(everyone)
    set_commands(owner, {"type": "chat", "chat_id": int(CHAT_ID)})

    state["commands_hash"] = fingerprint

    print("Telegram command menu updated.")


def process_commands(state, subs):

    response = requests.get(
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates",
        params={
            "offset": state["update_offset"],
            "timeout": 0,
            "allowed_updates": json.dumps(["message"])
        },
        timeout=20
    )

    response.raise_for_status()

    result = response.json()

    if not result.get("ok"):
        raise RuntimeError("Telegram getUpdates returned an error")

    handlers = {
        name: (owner_only, handler)
        for name, _, owner_only, handler in COMMANDS
    }

    handlers["start"] = handlers["help"]

    handled = 0

    for update in result["result"]:

        # Tell Telegram this update has been handled
        state["update_offset"] = max(
            state["update_offset"], update["update_id"] + 1
        )

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = (message.get("text") or "").strip()

        # Private chats only, and only /commands
        if chat.get("type") != "private" or not text.startswith("/"):
            continue

        chat_id = chat.get("id")
        command = text.split()[0].split("@")[0].lstrip("/").lower()

        ctx = {
            "chat_id": chat_id,
            "is_owner": str(chat_id) == str(CHAT_ID),
            "state": state,
            "subs": subs,
        }

        owner_note = None

        entry = handlers.get(command)

        if entry is None:
            reply = "Unknown command. Send /help to see what I can do."

        elif entry[0] and not ctx["is_owner"]:
            reply = "That command is only for the owner of this bot."

        else:
            reply, owner_note = entry[1](ctx)

        handled += 1

        try:
            send_telegram(reply, chat_id=chat_id)
        except Exception:
            print("Could not reply to a command")

        if owner_note:
            try:
                send_telegram(owner_note, silent=True)
            except Exception:
                print("Could not notify the owner about a follower change")

    print(f"Telegram commands handled: {handled}")


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
            "offline_since": {},
            "update_offset": 0,
            "commands_hash": ""
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
        },
        # Next Telegram update to read (see process_commands)
        "update_offset": int(data.get("update_offset", 0)),
        # Fingerprint of the command list last sent to Telegram's menu
        "commands_hash": str(data.get("commands_hash", ""))
    }


def save_state(state):

    with open(STATE_FILE, "w") as file:
        json.dump(state, file, indent=4)


# ==========================================
# MAIN MONITOR
# ==========================================

async def main():

    usernames = monitored_usernames()

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

    # Followers: read new /follow and /unfollow commands first, so a new
    # follower can already get an alert from this very run.
    fernet = make_fernet()

    subs = {"ids": [], "dirty": False, "ok": False}

    if fernet is None:
        print("Followers off (no SUBSCRIBERS_KEY): alerts go to the owner only.")
    else:
        try:
            subs["ids"] = load_subscribers(fernet)
            subs["ok"] = True
            print(f"Followers: {len(subs['ids'])}")

            # Keep Telegram's command menu in sync with COMMANDS. A failure
            # here must not stop the commands themselves from being read.
            try:
                sync_command_menu(state)
            except Exception as error:
                print(f"Command menu not updated: {describe(error)}")

            process_commands(state, subs)

        except Exception as error:
            print(f"Follower commands skipped: {describe(error)}")

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
                    broadcast(message, subs)
                    print(f"LIVE notification sent: @{username}")
                    updated_status[username] = True

                except Exception as error:
                    print(f"Telegram notification failed: {describe(error)}")

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
                    broadcast(message, subs)
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
                    print(f"Telegram notification failed: {describe(error)}")

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
            + "\n\nNext heartbeat in approximately 7 days."
        )

        try:

            send_telegram(message, silent=True)

            print("Silent heartbeat sent.")

            state["last_offline_heartbeat"] = now

        except Exception as error:

            print(f"Heartbeat failed: {describe(error)}")

    # ==========================================
    # SAVE EVERYTHING
    # ==========================================

    state["accounts"] = updated_status
    state["live_since"] = live_since
    state["offline_checks"] = offline_checks
    state["offline_since"] = offline_since

    # Only rewrite the follower file when it really changed (and only if it
    # was read correctly), so the public repo shows no needless changes.
    if fernet is not None and subs["ok"] and subs["dirty"]:
        try:
            save_subscribers(fernet, subs["ids"])
            print("Follower list updated.")
        except Exception as error:
            print(f"Could not save followers: {error}")

    save_state(state)

    print("=" * 40)
    print("MONITORING CHECK COMPLETED")
    print("=" * 40)


if __name__ == "__main__":
    asyncio.run(main())
