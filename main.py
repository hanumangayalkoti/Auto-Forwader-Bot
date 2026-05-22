import asyncio
import io
import os
import re
import json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MAX_GROUPS, AFFILIATE_TAG
from storage import save_to_telegram, load_from_telegram

# ---- ALIASES (config.py se aa rahe hain) ----
api_id   = API_ID
api_hash = API_HASH

LINK_REGEX = re.compile(r"https?://\S+", re.IGNORECASE)

# ---- STATE ----
groups      = {}   # { gid(int): { name, incoming(set), outgoing(set), active } }
all_dialogs = []   # [ (chat_id, name), ... ]
user_state  = {}   # { uid: { action, group_id } }

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(bot)
client = TelegramClient("session", api_id, api_hash)

login_state = {
    "phone":      None,
    "phone_hash": None,
    "step":       None,   # None | "phone" | "otp" | "2fa"
}


# ====================================================================
# PERSISTENCE  —  groups ko JSON file mein save/load karo
# ====================================================================

def save_groups():
    """Settings ko Telegram Saved Messages mein save karo (restart-proof)."""
    asyncio.get_event_loop().create_task(save_to_telegram(client, groups))


async def async_load_groups():
    """Telegram Saved Messages se settings load karo."""
    global groups
    groups = await load_from_telegram(client)


# ====================================================================
# HELPERS
# ====================================================================

def is_owner(uid):
    return uid == OWNER_ID


def next_gid():
    for i in range(1, MAX_GROUPS + 1):
        if i not in groups:
            return i
    return None


def has_link(text: str) -> bool:
    """Check karo ki string mein koi URL hai ya nahi."""
    if not text:
        return False
    return bool(LINK_REGEX.search(text))


def clean_amazon_url(url: str) -> str:
    """
    Amazon URL ko clean karo:
    - Search URL (/s?): sirf k, rh, tag rakho — baaki sab hata do
    - Product URL (/dp/ASIN): sirf clean dp URL + ek tag rakho
    - Duplicate tag=... bhi remove ho jaayega
    - Affiliate tag ensure karo (AFFILIATE_TAG)
    """
    try:
        parsed = urlparse(url)
        path   = parsed.path

        # --- Search URL: amazon.in/s?k=... ---
        if re.search(r"^/s\b", path):
            params = parse_qs(parsed.query, keep_blank_values=False)
            clean_params = {}
            if "k" in params:
                clean_params["k"] = params["k"][0]
            if "rh" in params:
                clean_params["rh"] = params["rh"][0]
            clean_params["tag"] = AFFILIATE_TAG
            clean_query = urlencode(clean_params)
            return urlunparse((parsed.scheme, parsed.netloc, path, "", clean_query, ""))

        # --- Product URL: amazon.in/dp/ASIN or amazon.in/.../dp/ASIN/... ---
        dp_match = re.search(r"(/dp/[A-Z0-9]{10})", path, re.IGNORECASE)
        if dp_match:
            clean_path  = dp_match.group(1)
            clean_query = urlencode({"tag": AFFILIATE_TAG})
            return urlunparse((parsed.scheme, parsed.netloc, clean_path, "", clean_query, ""))

        # --- Koi aur Amazon URL: sirf duplicate tag fix karo ---
        params = parse_qs(parsed.query, keep_blank_values=False)
        params["tag"] = [AFFILIATE_TAG]
        clean_query = urlencode({k: v[0] for k, v in params.items()})
        return urlunparse((parsed.scheme, parsed.netloc, path, "", clean_query, ""))

    except Exception:
        return url


def clean_text_urls(text: str) -> str:
    """
    Text mein jo bhi Amazon URLs hain unhe clean_amazon_url se replace karo.
    Non-Amazon URLs untouched rahenge.
    """
    if not text:
        return text

    def replace_url(match):
        url = match.group(0)
        parsed = urlparse(url)
        if "amazon.in" in parsed.netloc.lower():
            return clean_amazon_url(url)
        return url

    return LINK_REGEX.sub(replace_url, text)


async def load_dialogs():
    global all_dialogs
    if not client.is_connected() or not await client.is_user_authorized():
        return
    try:
        result = await client.get_dialogs()
        all_dialogs = []
        for d in result:
            name = d.name or getattr(d, "title", None) or "Unnamed"
            all_dialogs.append((d.id, name))
    except Exception as err:
        print("Dialog load error:", err)


def get_dname(did):
    for d_id, d_name in all_dialogs:
        if d_id == did:
            return d_name
    return str(did)


def get_dialog(idx):
    if 0 <= idx < len(all_dialogs):
        return all_dialogs[idx]
    return None


async def is_logged_in():
    try:
        return client.is_connected() and await client.is_user_authorized()
    except Exception:
        return False


# ====================================================================
# KEYBOARDS
# ====================================================================

def kb_login():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("Login karo", callback_data="do_login"))
    return kb


def kb_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Incoming Channel", callback_data="menu_inc"),
        InlineKeyboardButton("Outgoing Channel", callback_data="menu_out"),
    )
    kb.add(
        InlineKeyboardButton("Start Forwarding", callback_data="quick_start"),
        InlineKeyboardButton("Stop Forwarding",  callback_data="quick_stop"),
    )
    kb.add(
        InlineKeyboardButton("Status",       callback_data="st"),
        InlineKeyboardButton("Help",         callback_data="hl"),
    )
    kb.add(InlineKeyboardButton("Manage Groups", callback_data="grp_list"))
    return kb


def kb_groups():
    kb = InlineKeyboardMarkup(row_width=1)
    for gid, g in groups.items():
        icon  = "ON" if g["active"] else "OFF"
        label = f"[{icon}] {g['name']}"
        kb.add(InlineKeyboardButton(label, callback_data=f"grp:{gid}"))
    if next_gid():
        kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
    kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
    return kb


def kb_group(gid):
    g = groups.get(gid)
    if not g:
        return kb_main()
    toggle_btn = (
        InlineKeyboardButton("Stop Group",  callback_data=f"gx:{gid}")
        if g["active"] else
        InlineKeyboardButton("Start Group", callback_data=f"gs:{gid}")
    )
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Incoming", callback_data=f"gi:{gid}"),
        InlineKeyboardButton("Outgoing", callback_data=f"go:{gid}"),
    )
    kb.add(toggle_btn, InlineKeyboardButton("Rename", callback_data=f"gr:{gid}"))
    kb.add(InlineKeyboardButton("Delete Group", callback_data=f"gd:{gid}"))
    kb.add(
        InlineKeyboardButton("Back to Groups", callback_data="grp_list"),
        InlineKeyboardButton("Main Menu",      callback_data="mm"),
    )
    return kb


def text_channel_list(gid, mode):
    g = groups.get(gid)
    if not g:
        return "Group nahi mila."
    selected = g["incoming"] if mode == "in" else g["outgoing"]
    if not all_dialogs:
        return "Koi channel/bot nahi mila."
    lines = []
    for i, (did, dn) in enumerate(all_dialogs):
        marker = " ✅" if did in selected else ""
        lines.append(f"{i + 1} - {dn}{marker}")
    return "\n".join(lines)


def kb_channels(gid, mode):
    g = groups.get(gid)
    if not g:
        return kb_main()
    if mode == "in":
        selected = g["incoming"]
        pfx      = "si"
        all_cb   = f"sia:{gid}"
        clear_cb = f"sic:{gid}"
        confirm  = f"gc:{gid}"
    else:
        selected = g["outgoing"]
        pfx      = "to"
        all_cb   = f"toa:{gid}"
        clear_cb = f"toc:{gid}"
        confirm  = f"gco:{gid}"
    kb = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for i, (did, dn) in enumerate(all_dialogs):
        num   = str(i + 1)
        label = f"[{num}]" if did in selected else num
        buttons.append(InlineKeyboardButton(label, callback_data=f"{pfx}:{i}:{gid}"))
    if buttons:
        kb.add(*buttons)
    kb.row(
        InlineKeyboardButton("Select All", callback_data=all_cb),
        InlineKeyboardButton("Clear All",  callback_data=clear_cb),
    )
    kb.row(
        InlineKeyboardButton("Back",    callback_data=f"grp:{gid}"),
        InlineKeyboardButton("Confirm", callback_data=confirm),
    )
    return kb


def kb_after_incoming(gid):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Set Outgoing Channel", callback_data=f"go:{gid}"))
    kb.add(InlineKeyboardButton("Group Settings",       callback_data=f"grp:{gid}"))
    kb.add(InlineKeyboardButton("Main Menu",            callback_data="mm"))
    kb.add(InlineKeyboardButton("Dismiss",              callback_data="dm"))
    return kb


def kb_after_outgoing(gid):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Start Forwarding", callback_data=f"gs:{gid}"))
    kb.add(InlineKeyboardButton("Group Settings",   callback_data=f"grp:{gid}"))
    kb.add(InlineKeyboardButton("Main Menu",        callback_data="mm"))
    kb.add(InlineKeyboardButton("Dismiss",          callback_data="dm"))
    return kb


def kb_after_start(gid):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Status",     callback_data="st"),
        InlineKeyboardButton("Stop Group", callback_data=f"gx:{gid}"),
    )
    kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
    return kb


def kb_delete_confirm(gid):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Yes, Delete", callback_data=f"gdf:{gid}"),
        InlineKeyboardButton("No, Cancel",  callback_data=f"grp:{gid}"),
    )
    return kb


def kb_status():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Start All", callback_data="sa"),
        InlineKeyboardButton("Stop All",  callback_data="xa"),
    )
    kb.add(InlineKeyboardButton("Manage Groups", callback_data="grp_list"))
    kb.add(InlineKeyboardButton("Main Menu",     callback_data="mm"))
    return kb


# ====================================================================
# TEXT BUILDERS
# ====================================================================

def text_status():
    if not groups:
        return "*Status*\n\nKoi group nahi hai abhi.\nManage Groups se naya group banao!"
    lines = ["*Status - All Groups*\n"]
    for gid, g in groups.items():
        status    = "🟢 Running" if g["active"] else "🔴 Stopped"
        in_names  = ", ".join(get_dname(d) for d in g["incoming"])  or "-"
        out_names = ", ".join(get_dname(d) for d in g["outgoing"]) or "-"
        lines.append(f"*{g['name']}* — {status}")
        lines.append(f"  IN:  {in_names}")
        lines.append(f"  OUT: {out_names}\n")
    return "\n".join(lines)


def text_group(gid):
    g = groups.get(gid)
    if not g:
        return "Group nahi mila!"
    status   = "🟢 Running" if g["active"] else "🔴 Stopped"
    in_list  = "\n  ".join(f"- {get_dname(d)}" for d in g["incoming"])  or "  -"
    out_list = "\n  ".join(f"- {get_dname(d)}" for d in g["outgoing"]) or "  -"
    return (
        f"*{g['name']}*\n\n"
        f"Status: {status}\n\n"
        f"Incoming ({len(g['incoming'])}):\n  {in_list}\n\n"
        f"Outgoing ({len(g['outgoing'])}):\n  {out_list}"
    )


# ====================================================================
# COMMANDS
# ====================================================================

@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    logged_in = await is_logged_in()
    if not logged_in:
        await msg.answer(
            "*DealsKoti Forward Bot*\n\n"
            "Pehle apne Telegram account se login karo.\n"
            "Login button dabao:",
            parse_mode="Markdown",
            reply_markup=kb_login(),
        )
        return
    text = (
        "*Welcome to DealsKoti Bot!*\n\n"
        "Messages automatically forward karo — bina Forwarded tag ke.\n"
        "Private/restricted channels bhi supported.\n\n"
        "*Quick Guide:*\n"
        "1. Manage Groups > New Group\n"
        "2. Incoming Channel set karo\n"
        "3. Outgoing Channel set karo\n"
        "4. Start karo!\n\n"
        "Neeche se option choose karo:"
    )
    await msg.answer(text, parse_mode="Markdown", reply_markup=kb_main())


@dp.message_handler(commands=["login"])
async def cmd_login(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    if await is_logged_in():
        await msg.answer("Pehle se logged in ho! /start karo.")
        return
    login_state["step"] = "phone"
    await msg.answer(
        "Apna Telegram phone number dalo (country code ke saath):\n"
        "Example: +919876543210"
    )


@dp.message_handler(commands=["logout"])
async def cmd_logout(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    try:
        await client.log_out()
    except Exception:
        pass
    login_state.update(step=None, phone=None, phone_hash=None)
    await msg.answer("Logout ho gaye. /login karke dobara login karo.")


@dp.message_handler(commands=["help"])
async def cmd_help(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    text = (
        "*Help — DealsKoti Forward Bot*\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Login (Pehli baar):*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "1. /login bhejo\n"
        "2. Phone number dalo (+91...)\n"
        "3. OTP dalo\n"
        "4. 2FA password (agar laga ho)\n"
        "5. Done!\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Forwarding Setup:*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "1. Manage Groups > New Group banao\n"
        "2. Incoming channel select > Confirm\n"
        "3. Outgoing channel select > Confirm\n"
        "4. Start karo!\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Commands:*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "/start — Main menu\n"
        "/login — Login karo\n"
        "/logout — Logout karo\n"
        "/groups — Groups manage karo\n"
        "/status — Status dekho\n"
        "/startall — Sab groups start\n"
        "/stopall — Sab groups stop\n"
        "/help — Yahi message\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "*Features:*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"- Max {MAX_GROUPS} groups\n"
        "- Multiple incoming/outgoing per group\n"
        "- Bina 'Forwarded' tag ke forward\n"
        "- Private & restricted channels support\n"
        "- Link wale messages zaroor forward hote hain\n"
        "- Groups/settings restart ke baad bhi save rehte hain"
    )
    await msg.answer(text, parse_mode="Markdown")


@dp.message_handler(commands=["groups"])
async def cmd_groups(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    if not groups:
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
        await msg.answer("Koi group nahi hai.", reply_markup=kb)
        return
    await msg.answer("*Saare Groups:*", parse_mode="Markdown", reply_markup=kb_groups())


@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    await msg.answer(text_status(), parse_mode="Markdown", reply_markup=kb_status())


@dp.message_handler(commands=["startall"])
async def cmd_startall(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    count = 0
    for g in groups.values():
        if g["incoming"] and g["outgoing"]:
            g["active"] = True
            count += 1
    save_groups()
    await msg.answer(f"{count} group(s) start ho gaye!")


@dp.message_handler(commands=["stopall"])
async def cmd_stopall(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    for g in groups.values():
        g["active"] = False
    save_groups()
    await msg.answer("Sab groups band ho gaye!")


# ====================================================================
# TEXT HANDLER  (login flow + rename)
# ====================================================================

@dp.message_handler()
async def text_handler(msg: types.Message):
    if not is_owner(msg.from_user.id):
        return

    uid  = msg.from_user.id
    text = msg.text.strip() if msg.text else ""

    # ---- LOGIN: phone ----
    if login_state["step"] == "phone":
        try:
            result = await client.send_code_request(text)
            login_state["phone"]      = text
            login_state["phone_hash"] = result.phone_code_hash
            login_state["step"]       = "otp"
            await msg.answer(
                "OTP Telegram pe bhej diya!\n\n"
                "Ab OTP enter karo (sirf numbers):\n"
                "Example: 12345"
            )
        except Exception as e:
            login_state["step"] = None
            await msg.answer(f"Error: {e}\n\nDobara /login karo.")
        return

    # ---- LOGIN: OTP ----
    if login_state["step"] == "otp":
        otp = text.replace(" ", "").replace("-", "")
        try:
            await client.sign_in(
                phone=login_state["phone"],
                code=otp,
                phone_code_hash=login_state["phone_hash"],
            )
            login_state["step"] = None
            await load_dialogs()
            await msg.answer("Login ho gaye! Ab /start karo.")
        except SessionPasswordNeededError:
            login_state["step"] = "2fa"
            await msg.answer("2-Step Verification ON hai.\nApna password dalo:")
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            login_state["step"] = None
            await msg.answer(f"OTP galat/expire: {e}\n\nDobara /login karo.")
        except Exception as e:
            login_state["step"] = None
            await msg.answer(f"Error: {e}\n\nDobara /login karo.")
        return

    # ---- LOGIN: 2FA ----
    if login_state["step"] == "2fa":
        try:
            await client.sign_in(password=text)
            login_state["step"] = None
            await load_dialogs()
            await msg.answer("Login ho gaye! Ab /start karo.")
        except PasswordHashInvalidError:
            await msg.answer("Password galat hai. Dobara dalo:")
        except Exception as e:
            login_state["step"] = None
            await msg.answer(f"Error: {e}\n\nDobara /login karo.")
        return

    # ---- RENAME ----
    state = user_state.get(uid)
    if state and state.get("action") == "rename":
        gid = state["group_id"]
        if gid in groups:
            new_name = text[:30]
            groups[gid]["name"] = new_name
            save_groups()
            del user_state[uid]
            await msg.answer(
                f"Naam badal diya: *{new_name}*",
                parse_mode="Markdown",
                reply_markup=kb_group(gid),
            )
        else:
            del user_state[uid]
            await msg.answer("Group nahi mila.")


# ====================================================================
# CALLBACK HANDLER
# ====================================================================

@dp.callback_query_handler()
async def on_callback(cb: types.CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer("Access denied!", show_alert=True)
        return

    data = cb.data
    uid  = cb.from_user.id

    # ---- Login ----
    if data == "do_login":
        if await is_logged_in():
            await cb.message.edit_text("Pehle se logged in ho! /start karo.")
            return
        login_state["step"] = "phone"
        await cb.message.edit_text(
            "Apna phone number dalo (country code ke saath):\n"
            "Example: +919876543210"
        )
        await cb.answer()
        return

    # ---- Sab actions ke liye login check ----
    if not await is_logged_in():
        await cb.answer("Pehle /login karo!", show_alert=True)
        return

    # ---- Main Menu ----
    if data == "mm":
        await cb.message.edit_text(
            "*DealsKoti Forward Bot*\n\nOption choose karo:",
            parse_mode="Markdown",
            reply_markup=kb_main(),
        )

    elif data == "dm":
        await cb.message.delete()

    elif data == "st":
        await cb.message.answer(text_status(), parse_mode="Markdown", reply_markup=kb_status())

    elif data == "hl":
        kb = InlineKeyboardMarkup()
        kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
        await cb.message.answer(
            "*Help*\n\n"
            "1. Manage Groups > New Group\n"
            "2. Incoming select > Confirm\n"
            "3. Outgoing select > Confirm\n"
            "4. Start karo!\n\n"
            f"Max {MAX_GROUPS} groups.\n"
            "Bina Forwarded tag ke forward.\n"
            "Link wale messages zaroor forward.\n"
            "/startall /stopall se sab control.",
            parse_mode="Markdown",
            reply_markup=kb,
        )

    # ---- Groups list ----
    elif data == "grp_list":
        if not groups:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
            kb.add(InlineKeyboardButton("Main Menu",   callback_data="mm"))
            await cb.message.edit_text(
                "*Groups*\n\nKoi group nahi hai. Naya banao!",
                parse_mode="Markdown",
                reply_markup=kb,
            )
        else:
            await cb.message.edit_text(
                "*Saare Groups*\n\nGroup select karo ya naya banao:",
                parse_mode="Markdown",
                reply_markup=kb_groups(),
            )

    # ---- New Group ----
    elif data == "ng":
        nid = next_gid()
        if not nid:
            await cb.answer(f"Max {MAX_GROUPS} groups bana sakte ho!", show_alert=True)
            return
        groups[nid] = {
            "name":     f"Group {nid}",
            "incoming": set(),
            "outgoing": set(),
            "active":   False,
        }
        save_groups()
        await cb.message.edit_text(
            f"*{groups[nid]['name']}* bana diya!\n\nAb incoming aur outgoing channels set karo.",
            parse_mode="Markdown",
            reply_markup=kb_group(nid),
        )

    # ---- Group detail ----
    elif data.startswith("grp:"):
        gid = int(data[4:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    # ---- Incoming channel list ----
    elif data.startswith("gi:"):
        gid = int(data[3:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await load_dialogs()
        if not all_dialogs:
            await cb.answer("Koi channel nahi mila! Pehle /login karo.", show_alert=True)
            return
        await cb.message.edit_text(
            f"{groups[gid]['name']} — Incoming\nNumber dabao to select/deselect:\n\n"
            + text_channel_list(gid, "in"),
            reply_markup=kb_channels(gid, "in"),
        )

    # ---- Outgoing channel list ----
    elif data.startswith("go:"):
        gid = int(data[3:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await load_dialogs()
        if not all_dialogs:
            await cb.answer("Koi channel nahi mila! Pehle /login karo.", show_alert=True)
            return
        await cb.message.edit_text(
            f"{groups[gid]['name']} — Outgoing\nNumber dabao to select/deselect:\n\n"
            + text_channel_list(gid, "out"),
            reply_markup=kb_channels(gid, "out"),
        )

    # ---- Toggle incoming channel ----
    elif data.startswith("si:"):
        parts = data.split(":")
        idx, gid = int(parts[1]), int(parts[2])
        d = get_dialog(idx)
        if d and gid in groups:
            did = d[0]
            s   = groups[gid]["incoming"]
            s.discard(did) if did in s else s.add(did)
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Incoming\nNumber dabao to select/deselect:\n\n"
                + text_channel_list(gid, "in"),
                reply_markup=kb_channels(gid, "in"),
            )

    # ---- Toggle outgoing channel ----
    elif data.startswith("to:"):
        parts = data.split(":")
        idx, gid = int(parts[1]), int(parts[2])
        d = get_dialog(idx)
        if d and gid in groups:
            did = d[0]
            s   = groups[gid]["outgoing"]
            s.discard(did) if did in s else s.add(did)
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Outgoing\nNumber dabao to select/deselect:\n\n"
                + text_channel_list(gid, "out"),
                reply_markup=kb_channels(gid, "out"),
            )

    # ---- Select All / Clear All incoming ----
    elif data.startswith("sia:"):
        gid = int(data[4:])
        if gid in groups:
            for did, _ in all_dialogs:
                groups[gid]["incoming"].add(did)
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Incoming\n\n" + text_channel_list(gid, "in"),
                reply_markup=kb_channels(gid, "in"),
            )

    elif data.startswith("sic:"):
        gid = int(data[4:])
        if gid in groups:
            groups[gid]["incoming"].clear()
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Incoming\n\n" + text_channel_list(gid, "in"),
                reply_markup=kb_channels(gid, "in"),
            )

    # ---- Select All / Clear All outgoing ----
    elif data.startswith("toa:"):
        gid = int(data[4:])
        if gid in groups:
            for did, _ in all_dialogs:
                groups[gid]["outgoing"].add(did)
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Outgoing\n\n" + text_channel_list(gid, "out"),
                reply_markup=kb_channels(gid, "out"),
            )

    elif data.startswith("toc:"):
        gid = int(data[4:])
        if gid in groups:
            groups[gid]["outgoing"].clear()
            save_groups()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Outgoing\n\n" + text_channel_list(gid, "out"),
                reply_markup=kb_channels(gid, "out"),
            )

    # ---- Confirm incoming ----
    elif data.startswith("gc:"):
        gid = int(data[3:])
        if gid in groups:
            count = len(groups[gid]["incoming"])
            if count == 0:
                await cb.answer("Koi channel select nahi kiya!", show_alert=True)
                return
            names = "\n".join(f"- {get_dname(d)}" for d in groups[gid]["incoming"])
            await cb.message.edit_text(
                f"*Incoming Confirmed!*\n\n{count} channel(s) set:\n{names}\n\n"
                "Ab outgoing channel set karo.",
                parse_mode="Markdown",
                reply_markup=kb_after_incoming(gid),
            )

    # ---- Confirm outgoing ----
    elif data.startswith("gco:"):
        gid = int(data[4:])
        if gid in groups:
            count = len(groups[gid]["outgoing"])
            if count == 0:
                await cb.answer("Koi channel select nahi kiya!", show_alert=True)
                return
            names = "\n".join(f"- {get_dname(d)}" for d in groups[gid]["outgoing"])
            await cb.message.edit_text(
                f"*Outgoing Confirmed!*\n\n{count} channel(s) set:\n{names}\n\n"
                "Ab forwarding start karo!",
                parse_mode="Markdown",
                reply_markup=kb_after_outgoing(gid),
            )

    # ---- Start group ----
    elif data.startswith("gs:"):
        gid = int(data[3:])
        if gid in groups:
            g = groups[gid]
            if not g["incoming"]:
                await cb.answer("Pehle incoming channel set karo!", show_alert=True)
                return
            if not g["outgoing"]:
                await cb.answer("Pehle outgoing channel set karo!", show_alert=True)
                return
            g["active"] = True
            save_groups()
            in_names  = ", ".join(get_dname(d) for d in g["incoming"])
            out_names = ", ".join(get_dname(d) for d in g["outgoing"])
            await cb.message.edit_text(
                f"*Forwarding Started!*\n\n{g['name']}\n"
                f"From: {in_names}\nTo: {out_names}\n\n"
                "Messages forward ho rahe hain!",
                parse_mode="Markdown",
                reply_markup=kb_after_start(gid),
            )

    # ---- Stop group ----
    elif data.startswith("gx:"):
        gid = int(data[3:])
        if gid in groups:
            groups[gid]["active"] = False
            save_groups()
            await cb.message.edit_text(
                f"*{groups[gid]['name']}* band ho gaya!",
                parse_mode="Markdown",
                reply_markup=kb_group(gid),
            )

    # ---- Rename group ----
    elif data.startswith("gr:"):
        gid = int(data[3:])
        if gid in groups:
            user_state[uid] = {"action": "rename", "group_id": gid}
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("Cancel", callback_data=f"grp:{gid}"))
            await cb.message.edit_text(
                f"*{groups[gid]['name']}* ka naya naam type karo (max 30 chars):",
                parse_mode="Markdown",
                reply_markup=kb,
            )

    # ---- Delete group (confirm) ----
    elif data.startswith("gd:"):
        gid = int(data[3:])
        if gid in groups:
            await cb.message.edit_text(
                f"*'{groups[gid]['name']}'* delete karna chahte ho?\n\nYe undo nahi hogi!",
                parse_mode="Markdown",
                reply_markup=kb_delete_confirm(gid),
            )

    # ---- Delete group (confirmed) ----
    elif data.startswith("gdf:"):
        gid = int(data[4:])
        if gid in groups:
            name = groups[gid]["name"]
            del groups[gid]
            save_groups()
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("Groups",    callback_data="grp_list"),
                InlineKeyboardButton("Main Menu", callback_data="mm"),
            )
            await cb.message.edit_text(
                f"*'{name}'* delete ho gaya!",
                parse_mode="Markdown",
                reply_markup=kb,
            )

    # ---- Start All / Stop All ----
    elif data == "sa":
        count = sum(1 for g in groups.values() if g["incoming"] and g["outgoing"] and not g["active"])
        for g in groups.values():
            if g["incoming"] and g["outgoing"]:
                g["active"] = True
        save_groups()
        await cb.answer(f"Sab groups start ho gaye!", show_alert=True)

    elif data == "xa":
        for g in groups.values():
            g["active"] = False
        save_groups()
        await cb.answer("Sab groups band ho gaye!", show_alert=True)

    # ---- Quick Start / Stop ----
    elif data == "quick_start":
        count = 0
        for g in groups.values():
            if g["incoming"] and g["outgoing"]:
                g["active"] = True
                count += 1
        if count == 0:
            await cb.answer("Koi configured group nahi! Pehle setup karo.", show_alert=True)
        else:
            save_groups()
            await cb.answer(f"{count} group(s) start ho gaye!", show_alert=True)

    elif data == "quick_stop":
        for g in groups.values():
            g["active"] = False
        save_groups()
        await cb.answer("Sab forwarding band ho gaya!", show_alert=True)

    # ---- Incoming shortcut from main menu ----
    elif data == "menu_inc":
        if not groups:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
            kb.add(InlineKeyboardButton("Main Menu",   callback_data="mm"))
            await cb.message.edit_text("Pehle ek group banao:", reply_markup=kb)
        elif len(groups) == 1:
            gid = list(groups.keys())[0]
            await load_dialogs()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Incoming\nSelect karo:",
                reply_markup=kb_channels(gid, "in"),
            )
        else:
            kb = InlineKeyboardMarkup(row_width=1)
            for gid, g in groups.items():
                kb.add(InlineKeyboardButton(g["name"], callback_data=f"gi:{gid}"))
            kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
            await cb.message.edit_text("Kaun se group ka incoming set karna hai?", reply_markup=kb)

    # ---- Outgoing shortcut from main menu ----
    elif data == "menu_out":
        if not groups:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
            kb.add(InlineKeyboardButton("Main Menu",   callback_data="mm"))
            await cb.message.edit_text("Pehle ek group banao:", reply_markup=kb)
        elif len(groups) == 1:
            gid = list(groups.keys())[0]
            await load_dialogs()
            await cb.message.edit_text(
                f"{groups[gid]['name']} — Outgoing\nSelect karo:",
                reply_markup=kb_channels(gid, "out"),
            )
        else:
            kb = InlineKeyboardMarkup(row_width=1)
            for gid, g in groups.items():
                kb.add(InlineKeyboardButton(g["name"], callback_data=f"go:{gid}"))
            kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
            await cb.message.edit_text("Kaun se group ka outgoing set karna hai?", reply_markup=kb)

    await cb.answer()


# ====================================================================
# FORWARDER  —  ye main logic hai jahan messages forward hote hain
# ====================================================================

async def _copy_with_download(target_id, message):
    """Restricted content ke liye: download karke fresh upload."""
    buf = io.BytesIO()
    await message.download_media(file=buf)
    buf.seek(0)
    fname = "file"
    if message.file and message.file.name:
        fname = message.file.name
    buf.name = fname
    await client.send_file(
        target_id,
        file=buf,
        caption=message.message or "",
        force_document=False,
    )


def _extract_text(message) -> str:
    """Message se pure text ko nikalo (caption bhi)."""
    parts = []
    if message.message:
        parts.append(message.message)
    return " ".join(parts)


@client.on(events.NewMessage)
async def forwarder(event):
    for gid, g in groups.items():
        if not g["active"]:
            continue
        if event.chat_id not in g["incoming"]:
            continue

        m = event.message

        # ---- Decide karo kya forward karna hai ----
        # 1) Webpage preview (link ke saath preview card)
        #    => Sirf text/link send karo (send_file nahi chalega webpage pe)
        if isinstance(m.media, MessageMediaWebPage):
            text_content = clean_text_urls(_extract_text(m))
            if not has_link(text_content):
                # Agar link hi nahi toh skip
                continue
            for tgt_id in g["outgoing"]:
                try:
                    await client.send_message(tgt_id, text_content, link_preview=True)
                except Exception as err:
                    print(f"Forward error [{g['name']}] webpage -> {tgt_id}: {err}")

        # 2) Doosra media (photo, video, document, etc.)
        elif m.media:
            clean_caption = clean_text_urls(m.message or "")
            for tgt_id in g["outgoing"]:
                try:
                    await client.send_file(
                        tgt_id,
                        file=m.media,
                        caption=clean_caption,
                    )
                except Exception as fast_err:
                    print(f"[{g['name']}] Fast fail, downloading: {fast_err}")
                    try:
                        await _copy_with_download(tgt_id, m)
                    except Exception as dl_err:
                        print(f"Download also failed [{g['name']}]: {dl_err}")

        # 3) Pure text message
        elif m.message:
            text_content = clean_text_urls(m.message)
            for tgt_id in g["outgoing"]:
                try:
                    await client.send_message(tgt_id, text_content)
                except Exception as err:
                    print(f"Forward error [{g['name']}] text -> {tgt_id}: {err}")


# ====================================================================
# STARTUP
# ====================================================================

async def on_startup(_dispatcher):
    await client.connect()
    if await client.is_user_authorized():
        print("Already logged in — dialogs load ho rahe hain...")
        await load_dialogs()
        # Settings Telegram Saved Messages se load karo
        await async_load_groups()
        print(f"DealsKoti Forward Bot ready! {len(all_dialogs)} dialogs loaded.")
        print(f"{len(groups)} group(s) restored from Telegram.")
    else:
        print("Not logged in. Bot se /login karo.")
    print("Bot is running!")


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
    )
