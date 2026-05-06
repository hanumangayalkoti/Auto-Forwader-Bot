import asyncio
import io
import os
from telethon import TelegramClient, events
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# ---- CONFIG ----
api_id    = int(os.getenv("API_ID",   "0"))
api_hash  = os.getenv("API_HASH",     "")
BOT_TOKEN = os.getenv("BOT_TOKEN",    "")
OWNER_ID  = int(os.getenv("OWNER_ID", "0"))
MAX_GROUPS = 5

# ---- STATE ----
groups      = {}
all_dialogs = []
user_state  = {}

bot    = Bot(token=BOT_TOKEN)
dp     = Dispatcher(bot)
client = TelegramClient("session", api_id, api_hash)


# ---- HELPER FUNCTIONS ----

def is_owner(uid):
    if uid == OWNER_ID:
        return True
    return False


def next_gid():
    for i in range(1, MAX_GROUPS + 1):
        if i not in groups:
            return i
    return None


async def load_dialogs():
    global all_dialogs
    try:
        result = await client.get_dialogs()
        all_dialogs = []
        for d in result:
            name = d.name or d.title or "Unnamed"
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


# ---- KEYBOARDS ----

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
        InlineKeyboardButton("Status",  callback_data="st"),
        InlineKeyboardButton("Help",    callback_data="hl"),
    )
    kb.add(InlineKeyboardButton("Manage Groups", callback_data="grp_list"))
    return kb


def kb_groups():
    kb = InlineKeyboardMarkup(row_width=1)
    for gid, g in groups.items():
        icon = "ON" if g["active"] else "OFF"
        label = "[" + icon + "] " + g["name"]
        kb.add(InlineKeyboardButton(label, callback_data="grp:" + str(gid)))
    if next_gid():
        kb.add(InlineKeyboardButton("+ New Group", callback_data="ng"))
    kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
    return kb


def kb_group(gid):
    g = groups.get(gid)
    if not g:
        return kb_main()
    if g["active"]:
        toggle_btn = InlineKeyboardButton("Stop Group", callback_data="gx:" + str(gid))
    else:
        toggle_btn = InlineKeyboardButton("Start Group", callback_data="gs:" + str(gid))
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Incoming", callback_data="gi:" + str(gid)),
        InlineKeyboardButton("Outgoing", callback_data="go:" + str(gid)),
    )
    kb.add(toggle_btn, InlineKeyboardButton("Rename", callback_data="gr:" + str(gid)))
    kb.add(InlineKeyboardButton("Delete Group", callback_data="gd:" + str(gid)))
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
        marker = " <-- SELECTED" if did in selected else ""
        lines.append(str(i + 1) + " - " + dn + marker)
    return "\n".join(lines)


def kb_channels(gid, mode):
    g = groups.get(gid)
    if not g:
        return kb_main()
    if mode == "in":
        selected = g["incoming"]
        pfx      = "si"
        all_cb   = "sia:" + str(gid)
        clear_cb = "sic:" + str(gid)
        confirm  = "gc:"  + str(gid)
    else:
        selected = g["outgoing"]
        pfx      = "to"
        all_cb   = "toa:" + str(gid)
        clear_cb = "toc:" + str(gid)
        confirm  = "gco:" + str(gid)
    kb = InlineKeyboardMarkup(row_width=5)
    buttons = []
    for i, (did, dn) in enumerate(all_dialogs):
        num = str(i + 1)
        label = "[" + num + "]" if did in selected else num
        buttons.append(InlineKeyboardButton(label, callback_data=pfx + ":" + str(i) + ":" + str(gid)))
    if buttons:
        kb.add(*buttons)
    kb.row(
        InlineKeyboardButton("Select All", callback_data=all_cb),
        InlineKeyboardButton("Clear All",  callback_data=clear_cb),
    )
    kb.row(
        InlineKeyboardButton("Back",    callback_data="grp:" + str(gid)),
        InlineKeyboardButton("Confirm", callback_data=confirm),
    )
    return kb


def kb_after_incoming(gid):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Set Outgoing Channel", callback_data="go:" + str(gid)))
    kb.add(InlineKeyboardButton("Group Settings",       callback_data="grp:" + str(gid)))
    kb.add(InlineKeyboardButton("Main Menu",            callback_data="mm"))
    kb.add(InlineKeyboardButton("Dismiss",              callback_data="dm"))
    return kb


def kb_after_outgoing(gid):
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("Start Forwarding", callback_data="gs:" + str(gid)))
    kb.add(InlineKeyboardButton("Group Settings",   callback_data="grp:" + str(gid)))
    kb.add(InlineKeyboardButton("Main Menu",        callback_data="mm"))
    kb.add(InlineKeyboardButton("Dismiss",          callback_data="dm"))
    return kb


def kb_after_start(gid):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Status",     callback_data="st"),
        InlineKeyboardButton("Stop Group", callback_data="gx:" + str(gid)),
    )
    kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
    return kb


def kb_delete_confirm(gid):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("Yes, Delete", callback_data="gdf:" + str(gid)),
        InlineKeyboardButton("No, Cancel",  callback_data="grp:" + str(gid)),
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


# ---- TEXT BUILDERS ----

def text_status():
    if not groups:
        return "*Status*\n\nKoi group nahi hai abhi.\nManage Groups se naya group banao!"
    lines = ["*Status - All Groups*\n"]
    for gid, g in groups.items():
        status    = "Running" if g["active"] else "Stopped"
        in_names  = ", ".join(get_dname(d) for d in g["incoming"])  or "-"
        out_names = ", ".join(get_dname(d) for d in g["outgoing"]) or "-"
        lines.append("*" + g["name"] + "* - " + status)
        lines.append("  IN:  " + in_names)
        lines.append("  OUT: " + out_names + "\n")
    return "\n".join(lines)


def text_group(gid):
    g = groups.get(gid)
    if not g:
        return "Group nahi mila!"
    status   = "Running" if g["active"] else "Stopped"
    in_list  = "\n  ".join("- " + get_dname(d) for d in g["incoming"])  or "  -"
    out_list = "\n  ".join("- " + get_dname(d) for d in g["outgoing"]) or "  -"
    return (
        "*" + g["name"] + "*\n\n"
        "Status: " + status + "\n\n"
        "Incoming (" + str(len(g["incoming"])) + "):\n  " + in_list + "\n\n"
        "Outgoing (" + str(len(g["outgoing"])) + "):\n  " + out_list
    )


# ---- COMMANDS ----

@dp.message_handler(commands=["start"])
async def cmd_start(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied. Sirf owner use kar sakta hai.")
        return
    text = (
        "*Welcome to DealsKoti Bot!*\n\n"
        "Is bot se messages automatically forward karo - bina Forwarded tag ke.\n"
        "Private/restricted channels bhi support karta hai.\n\n"
        "*Quick Guide:*\n"
        "1. Manage Groups > New Group\n"
        "2. Incoming Channel set karo\n"
        "3. Outgoing Channel set karo\n"
        "4. Start karo!\n\n"
        "Neeche se option choose karo:"
    )
    await msg.answer(text, parse_mode="Markdown", reply_markup=kb_main())


@dp.message_handler(commands=["help"])
async def cmd_help(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    text = (
        "*Help - DealsKoti Forward Bot*\n\n"
        "Bot kaise use karein:\n"
        "1. /start - Main menu\n"
        "2. Manage Groups > New Group banao\n"
        "3. Group > Incoming select karo > Confirm\n"
        "4. Group > Outgoing select karo > Confirm\n"
        "5. Start Forwarding karo!\n\n"
        "Groups:\n"
        "- Maximum " + str(MAX_GROUPS) + " groups\n"
        "- Multiple channels per group\n"
        "- Rename, start/stop, delete kar sakte ho\n"
        "- Bina Forwarded tag ke forward hota hai\n"
        "- Private/restricted channels bhi support\n\n"
        "Commands:\n"
        "/start - Main menu\n"
        "/groups - Saare groups\n"
        "/status - Current status\n"
        "/startall - Sab groups start\n"
        "/stopall - Sab groups stop\n"
        "/help - Ye message"
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
    await msg.answer(str(count) + " group(s) start ho gaye!", parse_mode="Markdown")


@dp.message_handler(commands=["stopall"])
async def cmd_stopall(msg: types.Message):
    if not is_owner(msg.from_user.id):
        await msg.answer("Access denied.")
        return
    for g in groups.values():
        g["active"] = False
    await msg.answer("Sab groups band ho gaye!")


@dp.message_handler()
async def text_handler(msg: types.Message):
    if not is_owner(msg.from_user.id):
        return
    uid   = msg.from_user.id
    state = user_state.get(uid)
    if not state:
        return
    if state.get("action") == "rename":
        gid = state["group_id"]
        if gid in groups:
            new_name = msg.text.strip()[:30]
            groups[gid]["name"] = new_name
            del user_state[uid]
            await msg.answer(
                "Naam badal diya: " + new_name,
                parse_mode="Markdown",
                reply_markup=kb_group(gid),
            )
        else:
            del user_state[uid]
            await msg.answer("Group nahi mila.")


# ---- CALLBACK HANDLER ----

@dp.callback_query_handler()
async def on_callback(cb: types.CallbackQuery):
    if not is_owner(cb.from_user.id):
        await cb.answer("Access denied!", show_alert=True)
        return

    data = cb.data
    uid  = cb.from_user.id

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
        text = (
            "*Help*\n\n"
            "1. Manage Groups > New Group\n"
            "2. Incoming select > Confirm\n"
            "3. Outgoing select > Confirm\n"
            "4. Start karo!\n\n"
            "Max " + str(MAX_GROUPS) + " groups allowed.\n"
            "Bina Forwarded tag ke forward hota hai.\n"
            "Private channels bhi supported.\n"
            "/startall /stopall se sab control karo."
        )
        await cb.message.answer(text, parse_mode="Markdown", reply_markup=kb)

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

    elif data == "ng":
        nid = next_gid()
        if not nid:
            await cb.answer("Max " + str(MAX_GROUPS) + " groups bana sakte ho!", show_alert=True)
            return
        groups[nid] = {
            "name":     "Group " + str(nid),
            "incoming": set(),
            "outgoing": set(),
            "active":   False,
        }
        await cb.message.edit_text(
            groups[nid]["name"] + " bana diya!\n\nAb incoming aur outgoing channels set karo.",
            parse_mode="Markdown",
            reply_markup=kb_group(nid),
        )

    elif data.startswith("grp:"):
        gid = int(data[4:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await cb.message.edit_text(
            text_group(gid),
            parse_mode="Markdown",
            reply_markup=kb_group(gid),
        )

    elif data.startswith("gi:"):
        gid = int(data[3:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await load_dialogs()
        if not all_dialogs:
            await cb.answer("Koi channel/bot nahi mila!", show_alert=True)
            return
        text = (
            groups[gid]["name"] + " - Incoming\n"
            "Number dabao to select/deselect karo:\n\n"
            + text_channel_list(gid, "in")
        )
        await cb.message.edit_text(text, reply_markup=kb_channels(gid, "in"))

    elif data.startswith("go:"):
        gid = int(data[3:])
        if gid not in groups:
            await cb.answer("Group nahi mila!", show_alert=True)
            return
        await load_dialogs()
        if not all_dialogs:
            await cb.answer("Koi channel/bot nahi mila!", show_alert=True)
            return
        text = (
            groups[gid]["name"] + " - Outgoing\n"
            "Number dabao to select/deselect karo:\n\n"
            + text_channel_list(gid, "out")
        )
        await cb.message.edit_text(text, reply_markup=kb_channels(gid, "out"))

    elif data.startswith("si:"):
        parts = data.split(":")
        idx   = int(parts[1])
        gid   = int(parts[2])
        d     = get_dialog(idx)
        if d and gid in groups:
            did = d[0]
            s   = groups[gid]["incoming"]
            if did in s:
                s.discard(did)
            else:
                s.add(did)
            text = (
                groups[gid]["name"] + " - Incoming\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "in")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "in"))

    elif data.startswith("to:"):
        parts = data.split(":")
        idx   = int(parts[1])
        gid   = int(parts[2])
        d     = get_dialog(idx)
        if d and gid in groups:
            did = d[0]
            s   = groups[gid]["outgoing"]
            if did in s:
                s.discard(did)
            else:
                s.add(did)
            text = (
                groups[gid]["name"] + " - Outgoing\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "out")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "out"))

    elif data.startswith("sia:"):
        gid = int(data[4:])
        if gid in groups:
            for did, _ in all_dialogs:
                groups[gid]["incoming"].add(did)
            text = (
                groups[gid]["name"] + " - Incoming\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "in")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "in"))

    elif data.startswith("sic:"):
        gid = int(data[4:])
        if gid in groups:
            groups[gid]["incoming"].clear()
            text = (
                groups[gid]["name"] + " - Incoming\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "in")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "in"))

    elif data.startswith("toa:"):
        gid = int(data[4:])
        if gid in groups:
            for did, _ in all_dialogs:
                groups[gid]["outgoing"].add(did)
            text = (
                groups[gid]["name"] + " - Outgoing\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "out")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "out"))

    elif data.startswith("toc:"):
        gid = int(data[4:])
        if gid in groups:
            groups[gid]["outgoing"].clear()
            text = (
                groups[gid]["name"] + " - Outgoing\n"
                "Number dabao to select/deselect karo:\n\n"
                + text_channel_list(gid, "out")
            )
            await cb.message.edit_text(text, reply_markup=kb_channels(gid, "out"))

    elif data.startswith("gc:"):
        gid = int(data[3:])
        if gid in groups:
            count = len(groups[gid]["incoming"])
            if count == 0:
                await cb.answer("Koi channel select nahi kiya!", show_alert=True)
                return
            names = "\n".join("- " + get_dname(d) for d in groups[gid]["incoming"])
            await cb.message.edit_text(
                "*Incoming Confirmed!*\n\n"
                + str(count) + " channel(s) set:\n" + names + "\n\n"
                "Ab outgoing channel set karo.",
                parse_mode="Markdown",
                reply_markup=kb_after_incoming(gid),
            )

    elif data.startswith("gco:"):
        gid = int(data[4:])
        if gid in groups:
            count = len(groups[gid]["outgoing"])
            if count == 0:
                await cb.answer("Koi channel select nahi kiya!", show_alert=True)
                return
            names = "\n".join("- " + get_dname(d) for d in groups[gid]["outgoing"])
            await cb.message.edit_text(
                "*Outgoing Confirmed!*\n\n"
                + str(count) + " channel(s) set:\n" + names + "\n\n"
                "Ab forwarding start karo!",
                parse_mode="Markdown",
                reply_markup=kb_after_outgoing(gid),
            )

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
            in_names  = ", ".join(get_dname(d) for d in g["incoming"])
            out_names = ", ".join(get_dname(d) for d in g["outgoing"])
            await cb.message.edit_text(
                "*Forwarding Started!*\n\n"
                + g["name"] + "\n"
                "From: " + in_names + "\n"
                "To:   " + out_names + "\n\n"
                "Messages automatically forward ho rahe hain!",
                parse_mode="Markdown",
                reply_markup=kb_after_start(gid),
            )

    elif data.startswith("gx:"):
        gid = int(data[3:])
        if gid in groups:
            groups[gid]["active"] = False
            await cb.message.edit_text(
                groups[gid]["name"] + " band ho gaya!",
                parse_mode="Markdown",
                reply_markup=kb_group(gid),
            )

    elif data.startswith("gr:"):
        gid = int(data[3:])
        if gid in groups:
            user_state[uid] = {"action": "rename", "group_id": gid}
            kb = InlineKeyboardMarkup()
            kb.add(InlineKeyboardButton("Cancel", callback_data="grp:" + str(gid)))
            await cb.message.edit_text(
                groups[gid]["name"] + " ka naya naam type karo (max 30 chars):",
                parse_mode="Markdown",
                reply_markup=kb,
            )

    elif data.startswith("gd:"):
        gid = int(data[3:])
        if gid in groups:
            await cb.message.edit_text(
                "'" + groups[gid]["name"] + "' delete karna chahte ho?\n\nYe action undo nahi hogi!",
                parse_mode="Markdown",
                reply_markup=kb_delete_confirm(gid),
            )

    elif data.startswith("gdf:"):
        gid = int(data[4:])
        if gid in groups:
            name = groups[gid]["name"]
            del groups[gid]
            kb = InlineKeyboardMarkup(row_width=2)
            kb.add(
                InlineKeyboardButton("Groups",    callback_data="grp_list"),
                InlineKeyboardButton("Main Menu", callback_data="mm"),
            )
            await cb.message.edit_text(
                "'" + name + "' delete ho gaya!",
                parse_mode="Markdown",
                reply_markup=kb,
            )

    elif data == "sa":
        count = 0
        for g in groups.values():
            if g["incoming"] and g["outgoing"]:
                g["active"] = True
                count += 1
        await cb.answer(str(count) + " group(s) start ho gaye!", show_alert=True)

    elif data == "xa":
        for g in groups.values():
            g["active"] = False
        await cb.answer("Sab groups band ho gaye!", show_alert=True)

    elif data == "quick_start":
        count = 0
        for g in groups.values():
            if g["incoming"] and g["outgoing"]:
                g["active"] = True
                count += 1
        if count == 0:
            await cb.answer("Koi configured group nahi! Pehle setup karo.", show_alert=True)
        else:
            await cb.answer(str(count) + " group(s) start ho gaye!", show_alert=True)

    elif data == "quick_stop":
        for g in groups.values():
            g["active"] = False
        await cb.answer("Sab forwarding band ho gaya!", show_alert=True)

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
                groups[gid]["name"] + " - Incoming\nSelect karo:",
                parse_mode="Markdown",
                reply_markup=kb_channels(gid, "in"),
            )
        else:
            kb = InlineKeyboardMarkup(row_width=1)
            for gid, g in groups.items():
                kb.add(InlineKeyboardButton(g["name"], callback_data="gi:" + str(gid)))
            kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
            await cb.message.edit_text("Kaun se group ka incoming set karna hai?", reply_markup=kb)

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
                groups[gid]["name"] + " - Outgoing\nSelect karo:",
                parse_mode="Markdown",
                reply_markup=kb_channels(gid, "out"),
            )
        else:
            kb = InlineKeyboardMarkup(row_width=1)
            for gid, g in groups.items():
                kb.add(InlineKeyboardButton(g["name"], callback_data="go:" + str(gid)))
            kb.add(InlineKeyboardButton("Main Menu", callback_data="mm"))
            await cb.message.edit_text("Kaun se group ka outgoing set karna hai?", reply_markup=kb)

    await cb.answer()


# ---- FORWARDER ----
#
# Private/restricted channels support:
# Pehle fast forward try karta hai. Agar channel me "Restrict saving content"
# ON ho (noforwards) to fast method fail ho jata hai - tab media ko bytes me
# download karke fresh upload karta hai. Iss tarah har tarah ke channel se
# (public, private, restricted) message copy ho jata hai.

async def _copy_with_download(target_id, message):
    """Restricted content fallback - download karke fresh upload."""
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


@client.on(events.NewMessage)
async def forwarder(event):
    for gid, g in groups.items():
        if not g["active"]:
            continue
        if event.chat_id not in g["incoming"]:
            continue
        for tgt_id in g["outgoing"]:
            try:
                m = event.message
                if m.media:
                    try:
                        # Fast path - normal channels ke liye
                        await client.send_file(tgt_id, file=m.media, caption=m.message or "")
                    except Exception as fast_err:
                        # Restricted content - download + re-upload
                        print("[" + g["name"] + "] Fast fail, downloading:", fast_err)
                        await _copy_with_download(tgt_id, m)
                elif m.message:
                    await client.send_message(tgt_id, m.message)
            except Exception as err:
                print("Forward error [" + g["name"] + "] ->", tgt_id, ":", err)


# ---- MAIN ----

async def on_startup():
    await client.start()
    print("Telethon client started")
    print("DealsKoti Forward Bot is running!")


loop = asyncio.get_event_loop()
loop.create_task(on_startup())
executor.start_polling(dp, skip_updates=True)
