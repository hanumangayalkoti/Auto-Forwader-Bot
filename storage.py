"""
Telegram Saved Messages mein groups data save/load karta hai.
Isse Railway restart ya main.py edit ke baad bhi settings safe rehti hain.
"""
import json
from config import STORAGE_TAG, LOCAL_BACKUP


async def save_to_telegram(client, groups: dict):
    """Groups data ko Telegram Saved Messages mein save karo."""
    try:
        data = {}
        for gid, g in groups.items():
            data[str(gid)] = {
                "name":     g["name"],
                "incoming": list(g["incoming"]),
                "outgoing": list(g["outgoing"]),
                "active":   g["active"],
            }
        content = f"{STORAGE_TAG}\n{json.dumps(data, indent=2)}"

        # Pehle dekho koi purana message hai kya
        existing_msg = await _find_storage_message(client)
        if existing_msg:
            await existing_msg.edit(content)
        else:
            await client.send_message("me", content)

        # Local backup bhi rakhte hain (extra safety)
        try:
            with open(LOCAL_BACKUP, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    except Exception as e:
        print(f"[Storage] Telegram save error: {e}")
        # Fallback: local file try karo
        _save_local(groups)


async def load_from_telegram(client) -> dict:
    """Telegram Saved Messages se groups data load karo."""
    try:
        msg = await _find_storage_message(client)
        if msg:
            raw = msg.text or ""
            json_part = raw.replace(STORAGE_TAG, "").strip()
            data = json.loads(json_part)
            groups = _parse_groups(data)
            print(f"[Storage] Telegram se {len(groups)} group(s) load ho gaye.")
            return groups
    except Exception as e:
        print(f"[Storage] Telegram load error: {e}")

    # Fallback: local JSON try karo
    return _load_local()


async def _find_storage_message(client):
    """Saved Messages mein STORAGE_TAG wala message dhundho."""
    try:
        async for msg in client.iter_messages("me", limit=50):
            if msg.text and STORAGE_TAG in msg.text:
                return msg
    except Exception as e:
        print(f"[Storage] Search error: {e}")
    return None


def _parse_groups(data: dict) -> dict:
    groups = {}
    for gid_str, g in data.items():
        gid = int(gid_str)
        groups[gid] = {
            "name":     g["name"],
            "incoming": set(g["incoming"]),
            "outgoing": set(g["outgoing"]),
            "active":   g.get("active", False),
        }
    return groups


def _save_local(groups: dict):
    try:
        data = {}
        for gid, g in groups.items():
            data[str(gid)] = {
                "name":     g["name"],
                "incoming": list(g["incoming"]),
                "outgoing": list(g["outgoing"]),
                "active":   g["active"],
            }
        with open(LOCAL_BACKUP, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"[Storage] Local save error: {e}")


def _load_local() -> dict:
    import os
    if not os.path.exists(LOCAL_BACKUP):
        return {}
    try:
        with open(LOCAL_BACKUP, "r") as f:
            data = json.load(f)
        groups = _parse_groups(data)
        print(f"[Storage] Local backup se {len(groups)} group(s) load ho gaye.")
        return groups
    except Exception as e:
        print(f"[Storage] Local load error: {e}")
        return {}
