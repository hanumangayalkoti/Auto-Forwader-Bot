"""
DealsKoti Auto-Forwarder Bot — Enhanced Edition
Features: Keyword Filter, Blacklist, Word Replace, Caption, Live Edit Sync
Storage:  PostgreSQL (Railway persistent)
Restart:  Auto via Procfile shell loop
"""
import asyncio
import re

from aiogram import Bot, Dispatcher, executor, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
)
from telethon.tl.types import MessageMediaWebPage

import storage
from config import API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MAX_GROUPS

# ====================================================================
# INIT
# ====================================================================

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(bot)
client = TelegramClient("session", API_ID, API_HASH)

# In-memory state (synced from DB on startup)
groups:      dict[int, dict] = {}   # gid -> group dict
all_dialogs: list[tuple[int, str]] = []  # (chat_id, name)

login_state: dict = {"step": None, "phone": None, "phone_hash": None}
user_state:  dict = {}   # uid -> {"action": ..., "group_id": ..., ...}


# ====================================================================
# HELPERS
# ====================================================================

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def get_dname(did: int) -> str:
    for cid, name in all_dialogs:
        if cid == did:
            return name
    return str(did)


async def is_logged_in() -> bool:
    try:
        return await client.is_user_authorized()
    except Exception:
        return False


async def load_dialogs():
    all_dialogs.clear()
    async for d in client.iter_dialogs():
        all_dialogs.append((d.id, d.name or str(d.id)))
    print(f"[Dialogs] {len(all_dialogs)} loaded.")


# ====================================================================
# FILTER / TRANSFORM HELPERS
# ====================================================================

def passes_filters(text: str, g: dict) -> bool:
    """Return True if message should be forwarded, False if it should be skipped."""
    lower = (text or "").lower()

    # Blacklist — skip if any blacklist word found
    for word in g.get("blacklist", set()):
        if word.lower() in lower:
            return False

    # Keywords (whitelist) — if list non-empty, at least one must match
    kws = g.get("keywords", set())
    if kws:
        if not any(kw.lower() in lower for kw in kws):
            return False

    return True


def apply_replacements(text: str, g: dict) -> str:
    """Apply all word/link replacements defined for this group."""
    if not text:
        return text
    for old, new in g.get("replacements", {}).items():
        text = text.replace(old, new)
    return text


def apply_caption(original: str, g: dict) -> str:
    """Handle caption_mode: keep / add / remove."""
    mode = g.get("caption_mode", "keep")
    extra = g.get("caption_text", "")
    if mode == "remove":
        return ""
    if mode == "add":
        if original:
            return f"{original}\n{extra}" if extra else original
        return extra
    return original  # keep


def process_text(text: str, g: dict) -> str:
    """Apply replacements then caption logic on a plain text message."""
    text = apply_replacements(text or "", g)
    return apply_caption(text, g)


def process_media_caption(caption: str, g: dict) -> str:
    """Apply replacements then caption logic on a media caption."""
    caption = apply_replacements(caption or "", g)
    return apply_caption(caption, g)


def channel_list_text(group_name: str, direction: str) -> str:
    """Numbered list of channels shown above the selection keyboard."""
    lines = [f"*{group_name} — {direction}*", "Number dabao to select/deselect:\n"]
    for i, (did, name) in enumerate(all_dialogs[:50], 1):
        lines.append(f"`{i}.` {name}")
    return "\n".join(lines)


def has_link(text: str) -> bool:
    return bool(re.search(r"https?://|t\.me/", text or ""))


def clean_text_urls(text: str) -> str:
    return text


async def _copy_with_download(tgt_id: int, m):
    path = await client.download_media(m)
    if path:
        await client.send_file(tgt_id, path, caption=m.message or "")


# ====================================================================
# KEYBOARDS
# ====================================================================

def kb_login():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🔑 Login", callback_data="do_login"))
    return kb


def kb_main():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📋 Manage Groups", callback_data="grp_list"),
        InlineKeyboardButton("📊 Status",        callback_data="st"),
    )
    kb.add(
        InlineKeyboardButton("❓ Help",   callback_data="hl"),
        InlineKeyboardButton("🗑 Dismiss", callback_data="dm"),
    )
    return kb


def kb_groups():
    kb = InlineKeyboardMarkup(row_width=3)
    buttons = [
        InlineKeyboardButton(g["name"], callback_data=f"grp:{gid}")
        for gid, g in groups.items()
    ]
    if buttons:
        kb.add(*buttons)
    kb.row(InlineKeyboardButton("➕ New Group", callback_data="ng"))
    kb.row(InlineKeyboardButton("🏠 Main Menu",  callback_data="mm"))
    return kb


def kb_group(gid: int):
    g = groups.get(gid, {})
    status_icon = "🟢" if g.get("active") else "🔴"
    toggle_cb   = f"gx:{gid}" if g.get("active") else f"gs:{gid}"
    toggle_txt  = f"{status_icon} Stop" if g.get("active") else f"{status_icon} Start"
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📥 Incoming", callback_data=f"gi:{gid}"),
        InlineKeyboardButton("📤 Outgoing", callback_data=f"go:{gid}"),
    )
    kb.add(
        InlineKeyboardButton(toggle_txt,      callback_data=toggle_cb),
        InlineKeyboardButton("⚙️ Settings",   callback_data=f"cfg:{gid}"),
    )
    kb.add(
        InlineKeyboardButton("✏️ Rename",     callback_data=f"rn:{gid}"),
        InlineKeyboardButton("🗑 Delete",     callback_data=f"gd:{gid}"),
    )
    kb.row(InlineKeyboardButton("🔙 Back", callback_data="grp_list"))
    return kb


def kb_settings(gid: int):
    g = groups.get(gid, {})
    cap_mode = g.get("caption_mode", "keep")
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔍 Keyword Filter",  callback_data=f"kw:{gid}"))
    kb.add(InlineKeyboardButton("🚫 Blacklist",        callback_data=f"bl:{gid}"))
    kb.add(InlineKeyboardButton("✏️ Word Replace",     callback_data=f"rp:{gid}"))
    kb.add(InlineKeyboardButton(f"📝 Caption ({cap_mode})", callback_data=f"cap:{gid}"))
    kb.add(InlineKeyboardButton("🔙 Back",             callback_data=f"grp:{gid}"))
    return kb


def kb_keyword(gid: int):
    g = groups.get(gid, {})
    kws = sorted(g.get("keywords", set()))
    kb = InlineKeyboardMarkup(row_width=1)
    for kw in kws:
        kb.add(InlineKeyboardButton(f"❌ {kw}", callback_data=f"kwdel:{gid}:{kw}"))
    kb.add(InlineKeyboardButton("➕ Add Keyword", callback_data=f"kwadd:{gid}"))
    kb.add(InlineKeyboardButton("🔙 Back",        callback_data=f"cfg:{gid}"))
    return kb


def kb_blacklist(gid: int):
    g = groups.get(gid, {})
    words = sorted(g.get("blacklist", set()))
    kb = InlineKeyboardMarkup(row_width=1)
    for w in words:
        kb.add(InlineKeyboardButton(f"❌ {w}", callback_data=f"bldel:{gid}:{w}"))
    kb.add(InlineKeyboardButton("➕ Add Word", callback_data=f"bladd:{gid}"))
    kb.add(InlineKeyboardButton("🔙 Back",      callback_data=f"cfg:{gid}"))
    return kb


def kb_replace(gid: int):
    g = groups.get(gid, {})
    pairs = g.get("replacements", {})
    kb = InlineKeyboardMarkup(row_width=1)
    for old, new in pairs.items():
        short = f"{old[:15]} → {new[:15]}"
        kb.add(InlineKeyboardButton(f"❌ {short}", callback_data=f"rpdel:{gid}:{old}"))
    kb.add(InlineKeyboardButton("➕ Add Rule", callback_data=f"rpadd:{gid}"))
    kb.add(InlineKeyboardButton("🔙 Back",      callback_data=f"cfg:{gid}"))
    return kb


def kb_caption(gid: int):
    g = groups.get(gid, {})
    mode = g.get("caption_mode", "keep")
    kb = InlineKeyboardMarkup(row_width=3)
    kb.add(
        InlineKeyboardButton(("✅ " if mode == "keep"   else "") + "Keep",   callback_data=f"capmode:{gid}:keep"),
        InlineKeyboardButton(("✅ " if mode == "add"    else "") + "Add",    callback_data=f"capmode:{gid}:add"),
        InlineKeyboardButton(("✅ " if mode == "remove" else "") + "Remove", callback_data=f"capmode:{gid}:remove"),
    )
    if mode == "add":
        kb.add(InlineKeyboardButton("✏️ Set Caption Text", callback_data=f"captext:{gid}"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data=f"cfg:{gid}"))
    return kb


def kb_channel_select(gid: int, direction: str):
    g = groups[gid]
    selected = g["incoming"] if direction == "incoming" else g["outgoing"]
    pfx       = "fr" if direction == "incoming" else "to"
    all_cb    = f"{'fia' if direction=='incoming' else 'toa'}:{gid}"
    clear_cb  = f"{'fic' if direction=='incoming' else 'toc'}:{gid}"
    confirm   = f"gci:{gid}" if direction == "incoming" else f"gco:{gid}"

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


def kb_status():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("▶️ Start All", callback_data="sa"),
        InlineKeyboardButton("⏹ Stop All",  callback_data="xa"),
    )
    kb.add(InlineKeyboardButton("📋 Manage Groups", callback_data="grp_list"))
    kb.add(InlineKeyboardButton("🏠 Main Menu",     callback_data="mm"))
    return kb


# ====================================================================
# TEXT BUILDERS
# ====================================================================

def text_status():
    if not groups:
        return "*Status*\n\nKoi group nahi hai abhi.\nManage Groups se naya group banao!"
    lines = ["*Status — All Groups*\n"]
    for gid, g in groups.items():
        status    = "🟢 Running" if g["active"] else "🔴 Stopped"
        in_names  = ", ".join(get_dname(d) for d in g["incoming"])  or "—"
        out_names = ", ".join(get_dname(d) for d in g["outgoing"]) or "—"
        lines.append(f"*{g['name']}* — {status}")
        lines.append(f"  IN:  {in_names}")
        lines.append(f"  OUT: {out_names}\n")
    return "\n".join(lines)


def text_group(gid: int):
    g = groups.get(gid)
    if not g:
        return "Group nahi mila!"
    status   = "🟢 Running" if g["active"] else "🔴 Stopped"
    in_list  = "\n  ".join(f"- {get_dname(d)}" for d in g["incoming"])  or "  —"
    out_list = "\n  ".join(f"- {get_dname(d)}" for d in g["outgoing"]) or "  —"
    return (
        f"*{g['name']}*\n\n"
        f"Status: {status}\n\n"
        f"Incoming ({len(g['incoming'])}):\n  {in_list}\n\n"
        f"Outgoing ({len(g['outgoing'])}):\n  {out_list}"
    )


def text_settings(gid: int):
    g = groups.get(gid, {})
    kws   = ", ".join(sorted(g.get("keywords", [])))   or "—"
    bl    = ", ".join(sorted(g.get("blacklist", [])))   or "—"
    rep   = "\n  ".join(f"{o} → {n}" for o, n in g.get("replacements", {}).items()) or "—"
    cap   = g.get("caption_mode", "keep")
    ctxt  = g.get("caption_text", "") or "—"
    return (
        f"*⚙️ Settings — {g.get('name', '')}*\n\n"
        f"🔍 *Keywords (whitelist):* {kws}\n"
        f"🚫 *Blacklist:* {bl}\n"
        f"✏️ *Replacements:*\n  {rep}\n"
        f"📝 *Caption mode:* `{cap}`"
        + (f"\n  Text: {ctxt}" if cap == "add" else "")
    )


# ====================================================================
# COMMANDS
# ====================================================================

@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    if not await is_logged_in():
        await msg.answer(
            "*DealsKoti Forward Bot*\n\nPehle login karo:",
            parse_mode="Markdown", reply_markup=kb_login(),
        )
        return
    await msg.answer(
        "*Welcome to DealsKoti Forward Bot!*\n\n"
        "Messages automatically forward karo — bina Forwarded tag ke.\n\n"
        "*Quick Guide:*\n"
        "1. Manage Groups → New Group\n"
        "2. Incoming Channel set karo\n"
        "3. Outgoing Channel set karo\n"
        "4. Start karo!",
        parse_mode="Markdown", reply_markup=kb_main(),
    )


@dp.message_handler(commands=["login"])
async def cmd_login(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    if await is_logged_in():
        await msg.answer("Pehle se logged in ho!")
        return
    login_state["step"] = "phone"
    await msg.answer("Apna phone number dalo (+919876543210):")


@dp.message_handler(commands=["logout"])
async def cmd_logout(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    try:
        await client.log_out()
    except Exception:
        pass
    login_state.update(step=None, phone=None, phone_hash=None)
    await msg.answer("Logout ho gaye. /login se dobara login karo.")


@dp.message_handler(commands=["status"])
async def cmd_status(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    await msg.answer(text_status(), parse_mode="Markdown", reply_markup=kb_status())


@dp.message_handler(commands=["startall"])
async def cmd_startall(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    count = 0
    for g in groups.values():
        if g["incoming"] and g["outgoing"]:
            g["active"] = True
            await storage.set_group_active(g["id"], True)
            count += 1
    await msg.answer(f"✅ {count} group(s) start ho gaye!")


@dp.message_handler(commands=["stopall"])
async def cmd_stopall(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    for g in groups.values():
        g["active"] = False
        await storage.set_group_active(g["id"], False)
    await msg.answer("⏹ Sab groups band ho gaye!")


@dp.message_handler(commands=["help"])
async def cmd_help(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    text = (
        "*Help — DealsKoti Forward Bot*\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*🔑 Login (pehli baar):*\n"
        "/login → phone → OTP → (2FA if needed)\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*📋 Forwarding Setup:*\n"
        "1. /start → Manage Groups → New Group\n"
        "2. Incoming channel set karo → Confirm\n"
        "3. Outgoing channel set karo → Confirm\n"
        "4. Start!\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*⚙️ Per-Group Settings:*\n"
        "Group → ⚙️ Settings button se manage karo\n\n"
        "🔍 *Keyword Filter* — `/filter`\n"
        "Sirf specific words wale messages forward honge\n\n"
        "🚫 *Blacklist* — `/blacklist`\n"
        "In words wale messages skip ho jaayenge\n\n"
        "✏️ *Word Replace* — `/replace`\n"
        "Forward se pehle text mein words badlo\n"
        "Format: `purana | naya`\n\n"
        "📝 *Caption* — `/caption`\n"
        "Keep / Add / Remove caption control karo\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*⭐ Live Edit Sync:*\n"
        "Source channel ne message edit kiya → target mein bhi auto-edit!\n\n"
        "━━━━━━━━━━━━━━━━\n"
        "*All Commands:*\n"
        "/start — 🏠 Main menu\n"
        "/login — 🔑 Login karo\n"
        "/logout — 🚪 Logout karo\n"
        "/status — 📊 Groups ka status\n"
        "/startall — ▶️ Sab groups start\n"
        "/stopall — ⏹ Sab groups stop\n"
        "/filter — 🔍 Keyword filter manage karo\n"
        "/blacklist — 🚫 Blacklist manage karo\n"
        "/replace — ✏️ Word replace rules manage karo\n"
        "/caption — 📝 Caption settings manage karo\n"
        "/help — ❓ Yahi message\n\n"
        f"Max {MAX_GROUPS} groups supported."
    )
    await msg.answer(text, parse_mode="Markdown")


def _group_selection_kb(callback_prefix: str) -> InlineKeyboardMarkup:
    """Show all groups as buttons for command-based group selection."""
    kb = InlineKeyboardMarkup(row_width=2)
    if not groups:
        kb.add(InlineKeyboardButton("➕ Pehle group banao", callback_data="grp_list"))
        return kb
    for gid, g in groups.items():
        kb.add(InlineKeyboardButton(g["name"], callback_data=f"{callback_prefix}:{gid}"))
    return kb


@dp.message_handler(commands=["filter"])
async def cmd_filter(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    if not await is_logged_in():
        await msg.answer("Pehle /login karo!"); return
    if not groups:
        await msg.answer("Koi group nahi hai. Pehle /start se group banao."); return
    await msg.answer(
        "*🔍 Keyword Filter*\nKaunse group ka filter manage karna hai?",
        parse_mode="Markdown",
        reply_markup=_group_selection_kb("kw"),
    )


@dp.message_handler(commands=["blacklist"])
async def cmd_blacklist(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    if not await is_logged_in():
        await msg.answer("Pehle /login karo!"); return
    if not groups:
        await msg.answer("Koi group nahi hai. Pehle /start se group banao."); return
    await msg.answer(
        "*🚫 Blacklist*\nKaunse group ki blacklist manage karna hai?",
        parse_mode="Markdown",
        reply_markup=_group_selection_kb("bl"),
    )


@dp.message_handler(commands=["replace"])
async def cmd_replace(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    if not await is_logged_in():
        await msg.answer("Pehle /login karo!"); return
    if not groups:
        await msg.answer("Koi group nahi hai. Pehle /start se group banao."); return
    await msg.answer(
        "*✏️ Word Replace*\nKaunse group ke replace rules manage karna hai?",
        parse_mode="Markdown",
        reply_markup=_group_selection_kb("rp"),
    )


@dp.message_handler(commands=["caption"])
async def cmd_caption(msg: types.Message):
    if not is_owner(msg.from_user.id): return
    if not await is_logged_in():
        await msg.answer("Pehle /login karo!"); return
    if not groups:
        await msg.answer("Koi group nahi hai. Pehle /start se group banao."); return
    await msg.answer(
        "*📝 Caption Settings*\nKaunse group ki caption setting manage karna hai?",
        parse_mode="Markdown",
        reply_markup=_group_selection_kb("cap"),
    )


# ====================================================================
# TEXT HANDLER  (login flow + rename + settings input)
# ====================================================================

@dp.message_handler()
async def text_handler(msg: types.Message):
    if not is_owner(msg.from_user.id): return

    uid  = msg.from_user.id
    text = msg.text.strip() if msg.text else ""

    # ---- LOGIN: phone ----
    if login_state["step"] == "phone":
        try:
            result = await client.send_code_request(text)
            login_state["phone"]      = text
            login_state["phone_hash"] = result.phone_code_hash
            login_state["step"]       = "otp"
            await msg.answer("OTP bhej diya! Ab OTP dalo (sirf numbers):")
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
            await msg.answer("✅ Login ho gaye! Ab /start karo.")
        except SessionPasswordNeededError:
            login_state["step"] = "2fa"
            await msg.answer("2FA ON hai. Password dalo:")
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            login_state["step"] = None
            await msg.answer(f"OTP galat/expired: {e}\n\nDobara /login karo.")
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
            await msg.answer("✅ Login ho gaye! Ab /start karo.")
        except PasswordHashInvalidError:
            await msg.answer("Password galat. Dobara dalo:")
        except Exception as e:
            login_state["step"] = None
            await msg.answer(f"Error: {e}\n\nDobara /login karo.")
        return

    # ---- USER STATE ACTIONS ----
    state = user_state.get(uid)
    if not state:
        return
    action = state.get("action")
    gid    = state.get("group_id")

    # Rename group
    if action == "rename" and gid in groups:
        new_name = text[:30]
        groups[gid]["name"] = new_name
        await storage.rename_group(gid, new_name)
        del user_state[uid]
        await msg.answer(f"✅ Naam badla: *{new_name}*", parse_mode="Markdown",
                         reply_markup=kb_group(gid))

    # Add keyword
    elif action == "add_keyword" and gid in groups:
        words = [w.strip() for w in text.split(",") if w.strip()]
        groups[gid]["keywords"].update(words)
        await storage.update_group_settings(gid, keywords=groups[gid]["keywords"])
        del user_state[uid]
        await msg.answer(f"✅ Keywords add hue: {', '.join(words)}",
                         reply_markup=kb_keyword(gid))

    # Add blacklist word
    elif action == "add_blacklist" and gid in groups:
        words = [w.strip() for w in text.split(",") if w.strip()]
        groups[gid]["blacklist"].update(words)
        await storage.update_group_settings(gid, blacklist=groups[gid]["blacklist"])
        del user_state[uid]
        await msg.answer(f"✅ Blacklist mein add hue: {', '.join(words)}",
                         reply_markup=kb_blacklist(gid))

    # Add replacement rule  (format: "old text | new text")
    elif action == "add_replace" and gid in groups:
        if "|" in text:
            old, new = text.split("|", 1)
            old, new = old.strip(), new.strip()
            if old:
                groups[gid]["replacements"][old] = new
                await storage.update_group_settings(gid, replacements=groups[gid]["replacements"])
                del user_state[uid]
                await msg.answer(f"✅ Rule add hua:\n`{old}` → `{new}`",
                                 parse_mode="Markdown", reply_markup=kb_replace(gid))
            else:
                await msg.answer("❌ Format galat. Example:\n`Amazon | My Store`",
                                 parse_mode="Markdown")
        else:
            await msg.answer("❌ Format: `purana text | naya text`\nExample:\n`Amazon | My Store`",
                             parse_mode="Markdown")

    # Set caption text
    elif action == "set_caption_text" and gid in groups:
        groups[gid]["caption_text"] = text
        await storage.update_group_settings(gid, caption_text=text)
        del user_state[uid]
        await msg.answer(f"✅ Caption text set hua:\n{text}", reply_markup=kb_caption(gid))


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
            await cb.message.edit_text("Pehle se logged in ho!")
            return
        login_state["step"] = "phone"
        await cb.message.edit_text("Phone number dalo (+91...):")
        await cb.answer()
        return

    if not await is_logged_in():
        await cb.answer("Pehle /login karo!", show_alert=True)
        return

    # ---- Main Menu ----
    if data == "mm":
        await cb.message.edit_text(
            "*DealsKoti Forward Bot*\n\nOption choose karo:",
            parse_mode="Markdown", reply_markup=kb_main(),
        )

    elif data == "dm":
        await cb.message.delete()

    elif data == "st":
        await cb.message.answer(text_status(), parse_mode="Markdown", reply_markup=kb_status())

    elif data == "hl":
        await cb.message.answer(
            "Dekho /help command — full guide wahan hai.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Main Menu", callback_data="mm")
            )
        )

    # ---- Start All / Stop All ----
    elif data == "sa":
        count = 0
        for g in groups.values():
            if g["incoming"] and g["outgoing"]:
                g["active"] = True
                await storage.set_group_active(g["id"], True)
                count += 1
        await cb.message.edit_text(
            f"✅ {count} group(s) start ho gaye!", reply_markup=kb_status()
        )

    elif data == "xa":
        for g in groups.values():
            g["active"] = False
            await storage.set_group_active(g["id"], False)
        await cb.message.edit_text("⏹ Sab groups band ho gaye!", reply_markup=kb_status())

    # ---- Groups List ----
    elif data == "grp_list":
        if not groups:
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("➕ New Group", callback_data="ng"))
            kb.add(InlineKeyboardButton("🏠 Main Menu",  callback_data="mm"))
            await cb.message.edit_text(
                "*Groups*\n\nKoi group nahi hai. Naya banao!",
                parse_mode="Markdown", reply_markup=kb,
            )
        else:
            await cb.message.edit_text(
                "*Saare Groups*", parse_mode="Markdown", reply_markup=kb_groups()
            )

    # ---- New Group ----
    elif data == "ng":
        if len(groups) >= MAX_GROUPS:
            await cb.answer(f"Max {MAX_GROUPS} groups!", show_alert=True)
            return
        g = await storage.create_group(f"Group {len(groups)+1}")
        g["incoming"] = set()
        g["outgoing"] = set()
        groups[g["id"]] = g
        await cb.message.edit_text(
            f"*{g['name']}* bana diya! Incoming/Outgoing set karo.",
            parse_mode="Markdown", reply_markup=kb_group(g["id"]),
        )

    # ---- Group Detail ----
    elif data.startswith("grp:"):
        gid = int(data[4:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True); return
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    # ---- Start / Stop single group ----
    elif data.startswith("gs:"):
        gid = int(data[3:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True); return
        g = groups[gid]
        if not g["incoming"] or not g["outgoing"]:
            await cb.answer("Pehle incoming aur outgoing set karo!", show_alert=True); return
        g["active"] = True
        await storage.set_group_active(gid, True)
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    elif data.startswith("gx:"):
        gid = int(data[3:])
        if gid not in groups: return
        groups[gid]["active"] = False
        await storage.set_group_active(gid, False)
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    # ---- Rename ----
    elif data.startswith("rn:"):
        gid = int(data[3:])
        user_state[uid] = {"action": "rename", "group_id": gid}
        await cb.message.edit_text("Naya naam bhejo:")

    # ---- Delete ----
    elif data.startswith("gd:"):
        gid = int(data[3:])
        kb = InlineKeyboardMarkup(row_width=2)
        kb.add(
            InlineKeyboardButton("✅ Haan, Delete", callback_data=f"gdf:{gid}"),
            InlineKeyboardButton("❌ Nahi",          callback_data=f"grp:{gid}"),
        )
        await cb.message.edit_text(
            f"*{groups.get(gid, {}).get('name', '')}* delete karna chahte ho?",
            parse_mode="Markdown", reply_markup=kb,
        )

    elif data.startswith("gdf:"):
        gid = int(data[4:])
        name = groups.get(gid, {}).get("name", "")
        await storage.delete_group(gid)
        groups.pop(gid, None)
        await cb.message.edit_text(
            f"🗑 *{name}* delete ho gaya.", parse_mode="Markdown", reply_markup=kb_groups()
        )

    # ---- Incoming Channel Selection ----
    elif data.startswith("gi:"):
        gid = int(data[3:])
        if gid not in groups: return
        await load_dialogs()
        await cb.message.edit_text(
            channel_list_text(groups[gid]['name'], "Incoming"),
            parse_mode="Markdown",
            reply_markup=kb_channel_select(gid, "incoming"),
        )

    elif data.startswith("fr:"):
        _, idx, gid = data.split(":")
        gid, idx = int(gid), int(idx)
        if gid not in groups or idx >= len(all_dialogs): return
        did, _ = all_dialogs[idx]
        if did in groups[gid]["incoming"]:
            groups[gid]["incoming"].discard(did)
        else:
            groups[gid]["incoming"].add(did)
        await cb.message.edit_reply_markup(kb_channel_select(gid, "incoming"))

    elif data.startswith("fia:"):
        gid = int(data[4:])
        groups[gid]["incoming"] = {d[0] for d in all_dialogs}
        await cb.message.edit_reply_markup(kb_channel_select(gid, "incoming"))

    elif data.startswith("fic:"):
        gid = int(data[4:])
        groups[gid]["incoming"] = set()
        await cb.message.edit_reply_markup(kb_channel_select(gid, "incoming"))

    elif data.startswith("gci:"):
        gid = int(data[4:])
        await storage.set_channels(gid, "incoming", groups[gid]["incoming"])
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    # ---- Outgoing Channel Selection ----
    elif data.startswith("go:"):
        gid = int(data[3:])
        if gid not in groups: return
        await load_dialogs()
        await cb.message.edit_text(
            channel_list_text(groups[gid]['name'], "Outgoing"),
            parse_mode="Markdown",
            reply_markup=kb_channel_select(gid, "outgoing"),
        )

    elif data.startswith("to:"):
        _, idx, gid = data.split(":")
        gid, idx = int(gid), int(idx)
        if gid not in groups or idx >= len(all_dialogs): return
        did, _ = all_dialogs[idx]
        if did in groups[gid]["outgoing"]:
            groups[gid]["outgoing"].discard(did)
        else:
            groups[gid]["outgoing"].add(did)
        await cb.message.edit_reply_markup(kb_channel_select(gid, "outgoing"))

    elif data.startswith("toa:"):
        gid = int(data[4:])
        groups[gid]["outgoing"] = {d[0] for d in all_dialogs}
        await cb.message.edit_reply_markup(kb_channel_select(gid, "outgoing"))

    elif data.startswith("toc:"):
        gid = int(data[4:])
        groups[gid]["outgoing"] = set()
        await cb.message.edit_reply_markup(kb_channel_select(gid, "outgoing"))

    elif data.startswith("gco:"):
        gid = int(data[4:])
        await storage.set_channels(gid, "outgoing", groups[gid]["outgoing"])
        await cb.message.edit_text(
            text_group(gid), parse_mode="Markdown", reply_markup=kb_group(gid)
        )

    # ====================================================================
    # SETTINGS MENU
    # ====================================================================

    elif data.startswith("cfg:"):
        gid = int(data[4:])
        if gid not in groups: return
        await cb.message.edit_text(
            text_settings(gid), parse_mode="Markdown", reply_markup=kb_settings(gid)
        )

    # ---- Keyword Filter ----
    elif data.startswith("kw:"):
        gid = int(data[3:])
        await cb.message.edit_text(
            "*🔍 Keyword Filter*\n\nSirf in words wale messages forward honge.\n"
            "Empty hai toh sab forward hoga.",
            parse_mode="Markdown", reply_markup=kb_keyword(gid),
        )

    elif data.startswith("kwadd:"):
        gid = int(data[6:])
        user_state[uid] = {"action": "add_keyword", "group_id": gid}
        await cb.message.edit_text(
            "Keywords bhejo (comma se alag karo):\nExample: `deal, offer, sale`",
            parse_mode="Markdown",
        )

    elif data.startswith("kwdel:"):
        _, gid_str, kw = data.split(":", 2)
        gid = int(gid_str)
        groups[gid]["keywords"].discard(kw)
        await storage.update_group_settings(gid, keywords=groups[gid]["keywords"])
        await cb.message.edit_reply_markup(kb_keyword(gid))

    # ---- Blacklist ----
    elif data.startswith("bl:"):
        gid = int(data[3:])
        await cb.message.edit_text(
            "*🚫 Blacklist*\n\nIn words wale messages skip ho jaayenge.",
            parse_mode="Markdown", reply_markup=kb_blacklist(gid),
        )

    elif data.startswith("bladd:"):
        gid = int(data[6:])
        user_state[uid] = {"action": "add_blacklist", "group_id": gid}
        await cb.message.edit_text(
            "Blacklist words bhejo (comma se alag karo):\nExample: `spam, casino, bet`",
            parse_mode="Markdown",
        )

    elif data.startswith("bldel:"):
        _, gid_str, word = data.split(":", 2)
        gid = int(gid_str)
        groups[gid]["blacklist"].discard(word)
        await storage.update_group_settings(gid, blacklist=groups[gid]["blacklist"])
        await cb.message.edit_reply_markup(kb_blacklist(gid))

    # ---- Word Replace ----
    elif data.startswith("rp:"):
        gid = int(data[3:])
        await cb.message.edit_text(
            "*✏️ Word Replace*\n\nForward hone se pehle text mein words badlo.",
            parse_mode="Markdown", reply_markup=kb_replace(gid),
        )

    elif data.startswith("rpadd:"):
        gid = int(data[6:])
        user_state[uid] = {"action": "add_replace", "group_id": gid}
        await cb.message.edit_text(
            "Rule bhejo is format mein:\n`purana text | naya text`\n\nExample:\n`Amazon | My Store`",
            parse_mode="Markdown",
        )

    elif data.startswith("rpdel:"):
        _, gid_str, old = data.split(":", 2)
        gid = int(gid_str)
        groups[gid]["replacements"].pop(old, None)
        await storage.update_group_settings(gid, replacements=groups[gid]["replacements"])
        await cb.message.edit_reply_markup(kb_replace(gid))

    # ---- Caption ----
    elif data.startswith("cap:"):
        gid = int(data[4:])
        await cb.message.edit_text(
            "*📝 Caption Settings*\n\n"
            "• *Keep* — original caption raho\n"
            "• *Add* — apna caption add karo\n"
            "• *Remove* — caption hata do",
            parse_mode="Markdown", reply_markup=kb_caption(gid),
        )

    elif data.startswith("capmode:"):
        _, gid_str, mode = data.split(":", 2)
        gid = int(gid_str)
        groups[gid]["caption_mode"] = mode
        await storage.update_group_settings(gid, caption_mode=mode)
        await cb.message.edit_reply_markup(kb_caption(gid))

    elif data.startswith("captext:"):
        gid = int(data[8:])
        user_state[uid] = {"action": "set_caption_text", "group_id": gid}
        await cb.message.edit_text(
            "Apna caption text bhejo (jo har forwarded message ke neeche add hoga):"
        )

    await cb.answer()


# ====================================================================
# FORWARDER  (New Messages)
# ====================================================================

@client.on(events.NewMessage())
async def on_new_message(event):
    if not groups:
        return
    m = event.message
    if not m:
        return

    src_chat = event.chat_id

    for gid, g in groups.items():
        if not g["active"]:
            continue
        if src_chat not in g["incoming"]:
            continue

        raw_text = m.message or ""

        # ---- Apply filters ----
        if not passes_filters(raw_text, g):
            continue

        # ---- Send to all outgoing channels ----
        if isinstance(m.media, MessageMediaWebPage):
            text_out = process_text(apply_replacements(raw_text, g), g)
            if not has_link(text_out):
                continue
            for tgt_id in g["outgoing"]:
                try:
                    sent = await client.send_message(tgt_id, text_out, link_preview=True)
                    await storage.save_message_map(gid, src_chat, m.id, tgt_id, sent.id)
                except Exception as err:
                    print(f"[{g['name']}] webpage forward error -> {tgt_id}: {err}")

        elif m.media:
            caption_out = process_media_caption(raw_text, g)
            for tgt_id in g["outgoing"]:
                try:
                    sent = await client.send_file(tgt_id, file=m.media, caption=caption_out)
                    await storage.save_message_map(gid, src_chat, m.id, tgt_id, sent.id)
                except Exception as fast_err:
                    print(f"[{g['name']}] fast send failed, downloading: {fast_err}")
                    try:
                        sent = await _copy_with_download(tgt_id, m)
                        if sent:
                            await storage.save_message_map(gid, src_chat, m.id, tgt_id, sent.id)
                    except Exception as dl_err:
                        print(f"[{g['name']}] download also failed: {dl_err}")

        elif raw_text:
            text_out = process_text(raw_text, g)
            for tgt_id in g["outgoing"]:
                try:
                    sent = await client.send_message(tgt_id, text_out)
                    await storage.save_message_map(gid, src_chat, m.id, tgt_id, sent.id)
                except Exception as err:
                    print(f"[{g['name']}] text forward error -> {tgt_id}: {err}")


# ====================================================================
# LIVE EDIT SYNC  ⭐
# Source channel ne message edit kiya → target mein bhi auto-edit
# ====================================================================

@client.on(events.MessageEdited())
async def on_message_edited(event):
    m = event.message
    if not m:
        return

    src_chat = event.chat_id

    # Find all forwarded copies of this message
    targets = await storage.get_mapped_targets(src_chat, m.id)
    if not targets:
        return

    # Get the group for this source to apply filters/replacements
    for row in targets:
        gid         = row["group_id"]
        tgt_chat    = row["target_chat_id"]
        tgt_msg_id  = row["target_msg_id"]
        g           = groups.get(gid)

        if not g or not g["active"]:
            continue

        raw_text = m.message or ""

        try:
            if m.media and not isinstance(m.media, MessageMediaWebPage):
                # For media messages, only edit caption
                new_caption = process_media_caption(raw_text, g)
                await client.edit_message(tgt_chat, tgt_msg_id, new_caption)
            else:
                new_text = process_text(raw_text, g)
                await client.edit_message(tgt_chat, tgt_msg_id, new_text)
        except Exception as err:
            print(f"[LiveEdit] Edit failed for group {gid} -> {tgt_chat}/{tgt_msg_id}: {err}")


# ====================================================================
# STARTUP
# ====================================================================

async def on_startup(_dispatcher):
    # Init DB
    await storage.init_db()

    # Load all groups from DB into memory
    loaded = await storage.get_all_groups()
    groups.update(loaded)
    print(f"[Groups] {len(groups)} group(s) loaded from DB.")

    # Connect Telethon
    await client.connect()
    if await client.is_user_authorized():
        print("Telethon: already logged in. Loading dialogs...")
        await load_dialogs()
    else:
        print("Telethon: not logged in. Use /login.")

    print("✅ DealsKoti Forward Bot ready!")


if __name__ == "__main__":
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
    )
