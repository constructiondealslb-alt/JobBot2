#!/usr/bin/env python3
"""
Telegram Command Listener
=========================
Purpose : Poll Telegram every hour for stop / activate commands, update state.json
Schedule: Every 1 hour via GitHub Actions (telegram_listener.yml)
Commands:
  stop     → sets active=false in state.json (stops 8am reports)
  activate → sets active=true  in state.json (resumes 8am reports)
"""

# ─── HERE START: IMPORTS & CONFIG ─────────────────────────────────────────────
import os
import json
import logging
import requests

# Telegram credentials — read from GitHub Secrets (same secrets as job_search.py)
TELEGRAM_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

TELE_BASE  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"
STATE_FILE = "state.json"

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger(__name__)
# ─── HERE END: IMPORTS & CONFIG ───────────────────────────────────────────────


# ─── HERE START: STATE FILE ───────────────────────────────────────────────────
def read_state() -> dict:
    """
    Read state.json.
    Output: dict with keys: active (bool), last_update_id (int)
    Failure behavior: returns default state if file missing or unreadable
    """
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"Could not read {STATE_FILE}: {e} — using default state")
        return {"active": True, "last_update_id": 0}


def write_state(state: dict):
    """
    Write updated state to state.json.
    Input : state dict with active + last_update_id
    """
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    log.info(f"state.json written: {state}")
# ─── HERE END: STATE FILE ─────────────────────────────────────────────────────


# ─── HERE START: TELEGRAM FUNCTIONS ──────────────────────────────────────────
def send_reply(text: str):
    """
    Send a message to the authorized Telegram chat.
    Input : message text (plain text, no HTML needed here)
    """
    try:
        resp = requests.post(
            f"{TELE_BASE}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
            },
            timeout=10,
        )
        if resp.status_code == 200:
            log.info("Reply sent to Telegram")
        else:
            log.warning(f"Reply failed: HTTP {resp.status_code} — {resp.text[:100]}")
    except Exception as e:
        log.error(f"Failed to send reply: {e}")


def poll_updates(offset: int) -> list[dict]:
    """
    Fetch new Telegram messages since last processed update_id.
    Input : offset = last_update_id + 1
    Output: list of update dicts from Telegram API
    Failure behavior: returns empty list on error
    """
    try:
        resp = requests.get(
            f"{TELE_BASE}/getUpdates",
            params={"offset": offset, "timeout": 0, "limit": 50},
            timeout=15,
        )
        if resp.status_code == 200:
            return resp.json().get("result", [])
        else:
            log.error(f"getUpdates failed: HTTP {resp.status_code} — {resp.text[:100]}")
            return []
    except Exception as e:
        log.error(f"getUpdates error: {e}")
        return []
# ─── HERE END: TELEGRAM FUNCTIONS ────────────────────────────────────────────


# ─── HERE START: MAIN ─────────────────────────────────────────────────────────
def main():
    """
    Flow:
    1. Read current state + last_update_id from state.json
    2. Poll Telegram for new messages since last_update_id
    3. Process each message from authorized chat only
    4. On "stop"     → set active=False, reply confirmation
    5. On "activate" → set active=True,  reply confirmation
    6. Update last_update_id and write state.json
    7. GitHub Actions workflow commits state.json after this script exits
    """
    log.info("=== Telegram Listener Starting ===")

    state     = read_state()
    offset    = state.get("last_update_id", 0) + 1
    updates   = poll_updates(offset)
    last_id   = state.get("last_update_id", 0)

    if not updates:
        log.info("No new Telegram messages")
        # Still write state to update last_update_id if needed
        write_state(state)
        return

    state_changed = False

    for update in updates:
        update_id = update.get("update_id", 0)
        last_id   = max(last_id, update_id)

        message = update.get("message", {})
        text    = message.get("text", "").strip().lower()
        chat_id = str(message.get("chat", {}).get("id", ""))

        # Security: only process messages from the authorized chat
        if chat_id != str(TELEGRAM_CHAT_ID):
            log.warning(f"Ignored message from unauthorized chat_id: {chat_id}")
            continue

        log.info(f"Received: '{text}' from authorized chat")

        if text == "stop":
            if not state.get("active", True):
                send_reply("ℹ️ Daily reports are already stopped.")
            else:
                state["active"] = False
                state_changed   = True
                send_reply(
                    "✅ <b>Daily Reports Stopped.</b>\n"
                    "No reports will be sent at 8:00 AM Beirut until reactivated.\n\n"
                    "To reactivate → send: <b>activate</b>"
                )
                log.info("Action: reports stopped")

        elif text == "activate":
            if state.get("active", True):
                send_reply("ℹ️ Daily reports are already active.")
            else:
                state["active"] = True
                state_changed   = True
                send_reply(
                    "✅ <b>Daily Reports Reactivated.</b>\n"
                    "Reports will resume at 8:00 AM Beirut time.\n\n"
                    "To stop → send: <b>stop</b>"
                )
                log.info("Action: reports reactivated")

        else:
            log.info(f"Unrecognized command: '{text}' — ignored")

    # Always update last_update_id to prevent reprocessing old messages
    state["last_update_id"] = last_id
    write_state(state)

    if state_changed:
        log.info("State changed — GitHub Actions will commit state.json")
    else:
        log.info("No state change this run")

    log.info("=== Listener Done ===")


if __name__ == "__main__":
    main()
# ─── HERE END: MAIN ───────────────────────────────────────────────────────────
