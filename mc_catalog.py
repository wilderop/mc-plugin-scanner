"""Offline catalog of non-vanilla survival items. Read-only. No coords in output.

Survival has been on this map since 1.14. Chunks and player .dat files are only
upgraded when something loads them, so the scanner must read every on-disk
generation: 1.14–1.20.4 `tag` items, 1.20.5+ `components`, pre-1.18 `Level`
chunks, and 1.17+ split entity regions. Fingerprints canonicalize name/lore so
the same relic is one card whether it last sat in a 1.16 inventory or 26.2.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import struct
import zlib
import gzip
from pathlib import Path

import nbtlib

COLORS = (
    "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
    "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
)

VANILLA_MAX_LEVELS = {
    "minecraft:protection": 4, "minecraft:fire_protection": 4, "minecraft:feather_falling": 4,
    "minecraft:blast_protection": 4, "minecraft:projectile_protection": 4, "minecraft:respiration": 3,
    "minecraft:aqua_affinity": 1, "minecraft:thorns": 3, "minecraft:depth_strider": 3,
    "minecraft:frost_walker": 2, "minecraft:binding_curse": 1, "minecraft:curse_of_binding": 1,
    "minecraft:soul_speed": 3, "minecraft:swift_sneak": 3, "minecraft:sharpness": 5, "minecraft:smite": 5,
    "minecraft:bane_of_arthropods": 5, "minecraft:knockback": 2, "minecraft:fire_aspect": 2,
    "minecraft:looting": 3, "minecraft:sweeping_edge": 3, "minecraft:sweeping": 3, "minecraft:impaling": 5,
    "minecraft:channeling": 1, "minecraft:flame": 1, "minecraft:infinity": 1, "minecraft:loyalty": 3,
    "minecraft:riptide": 3, "minecraft:multishot": 1, "minecraft:piercing": 4, "minecraft:power": 5,
    "minecraft:punch": 2, "minecraft:quick_charge": 3, "minecraft:efficiency": 5,
    "minecraft:silk_touch": 1, "minecraft:fortune": 3, "minecraft:luck_of_the_sea": 3,
    "minecraft:lure": 3, "minecraft:unbreaking": 3, "minecraft:mending": 1,
    "minecraft:vanishing_curse": 1, "minecraft:curse_of_vanishing": 1,
    "minecraft:wind_burst": 3, "minecraft:breach": 4, "minecraft:density": 5,
}

SKIP_UNLESS_EXTRA = {
    "minecraft:written_book", "minecraft:writable_book", "minecraft:book_and_quill",
    "minecraft:filled_map", "minecraft:map", "minecraft:empty_map",
    "minecraft:player_head", "minecraft:skeleton_skull", "minecraft:wither_skeleton_skull",
    "minecraft:zombie_head", "minecraft:creeper_head", "minecraft:dragon_head", "minecraft:piglin_head",
    "minecraft:skull",
}

# Pre-1.13 numeric enchant ids still show up in dusty tag NBT.
LEGACY_ENCHANT_IDS = {
    0: "minecraft:protection", 1: "minecraft:fire_protection", 2: "minecraft:feather_falling",
    3: "minecraft:blast_protection", 4: "minecraft:projectile_protection", 5: "minecraft:respiration",
    6: "minecraft:aqua_affinity", 7: "minecraft:thorns", 8: "minecraft:depth_strider",
    9: "minecraft:frost_walker", 10: "minecraft:binding_curse",
    16: "minecraft:sharpness", 17: "minecraft:smite", 18: "minecraft:bane_of_arthropods",
    19: "minecraft:knockback", 20: "minecraft:fire_aspect", 21: "minecraft:looting",
    22: "minecraft:sweeping",
    32: "minecraft:efficiency", 33: "minecraft:silk_touch", 34: "minecraft:unbreaking",
    35: "minecraft:fortune",
    48: "minecraft:power", 49: "minecraft:punch", 50: "minecraft:flame", 51: "minecraft:infinity",
    61: "minecraft:luck_of_the_sea", 62: "minecraft:lure",
    70: "minecraft:mending", 71: "minecraft:vanishing_curse",
}

SKULL_DAMAGE_IDS = {
    0: "minecraft:skeleton_skull",
    1: "minecraft:wither_skeleton_skull",
    2: "minecraft:zombie_head",
    3: "minecraft:player_head",
    4: "minecraft:creeper_head",
    5: "minecraft:dragon_head",
}

# Bump when fingerprint/canonicalization changes so resume does not mix formats.
SCAN_FORMAT = "3"

ITEM_LIST_KEYS = {"items", "inventory", "armoritems", "handitems", "enderitems"}
ITEM_COMPOUND_KEYS = {"item", "recorditem", "book", "saddleitem"}

# Positions that must never leave the scanner.
_POS_KEYS = {
    "x", "y", "z", "X", "Y", "Z", "pos", "Pos", "position", "Position",
    "lodestone_pos", "LodestonePos", "target", "block_pos", "BlockPos",
    "paper.origin", "Paper.Origin", "LastDeathLocation", "spawn", "Spawn",
    "world", "World", "dimension", "Dimension", "UUID", "uuid",
}

# x y z triples and x=/y=/z= groups. Leaves years and enchant levels alone.
COORD_RE = re.compile(
    r"(?:"
    r"(?:[~^]?-?\d{1,8}(?:\.\d+)?)(?:\s*[,\s/]\s*)(?:[~^]?-?\d{1,5}(?:\.\d+)?)(?:\s*[,\s/]\s*)(?:[~^]?-?\d{1,8}(?:\.\d+)?)"
    r"|(?:(?:\b[xyz]\s*[:=]\s*[~^]?-?\d+(?:\.\d+)?\s*,?\s*){2,3})"
    r")",
    re.IGNORECASE,
)

REDACT = "[coords hidden]"


def to_py(tag):
    if tag is None:
        return None
    unpack = getattr(tag, "unpack", None)
    if callable(unpack):
        try:
            return unpack()
        except Exception:
            pass
    if isinstance(tag, dict):
        return {str(k): to_py(v) for k, v in tag.items()}
    if isinstance(tag, (list, tuple)):
        return [to_py(v) for v in tag]
    if isinstance(tag, (bytes, bytearray)):
        return bytes(tag)
    if isinstance(tag, (int, float, str, bool)):
        return tag
    try:
        return int(tag)
    except Exception:
        return str(tag)


def redact_text(s: str) -> str:
    if not s or not isinstance(s, str):
        return s
    return COORD_RE.sub(REDACT, s)


def redact_walk(obj):
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, list):
        return [redact_walk(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _POS_KEYS or k.lower() in {"pos", "position", "origin"}:
                continue
            if k in {"clickEvent", "click_event", "hoverEvent", "hover_event", "insertion"}:
                continue
            out[k] = redact_walk(v)
        return out
    return obj


def parse_text_component(value):
    """Turn JSON-string lore (1.14–1.20.4), SNBT strings, and compounds into one shape."""
    if value is None:
        return None
    if isinstance(value, (int, float, bool)):
        return {"text": str(value)}
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                return parse_text_component(json.loads(s))
            except Exception:
                pass
        return {"text": value}
    if isinstance(value, list):
        extra = [parse_text_component(x) for x in value]
        extra = [x for x in extra if x]
        if not extra:
            return None
        if len(extra) == 1:
            return extra[0]
        return {"text": "", "extra": extra}
    if not isinstance(value, dict):
        return {"text": str(value)}
    out = {}
    for k in ("text", "translate", "color", "font", "insertion"):
        if k in value and value[k] not in (None, ""):
            out[k] = value[k]
    for k in ("italic", "bold", "underlined", "strikethrough", "obfuscated"):
        if k in value:
            v = value[k]
            out[k] = bool(int(v)) if isinstance(v, (int, float)) else bool(v)
    if "extra" in value:
        extra = parse_text_component(value.get("extra"))
        if extra is None:
            pass
        elif isinstance(extra, dict) and extra.get("extra") and extra.get("text") == "":
            out["extra"] = extra["extra"]
        elif isinstance(extra, dict):
            out["extra"] = [extra]
        elif isinstance(extra, list):
            out["extra"] = extra
    return out or None


def flatten_runs(node, inherited=None):
    """Identity of how text looks, ignoring extra-wrapper encoding."""
    inherited = dict(inherited or {})
    node = parse_text_component(node)
    if not node:
        return []
    style = dict(inherited)
    for k in ("color", "italic", "bold", "underlined", "strikethrough", "obfuscated"):
        if k in node:
            style[k] = node[k]
    runs = []
    bits = []
    if node.get("text"):
        bits.append(str(node["text"]))
    if node.get("translate"):
        bits.append(str(node["translate"]))
    text = " ".join("".join(bits).split())
    if text:
        run = {"text": text}
        if style.get("color"):
            run["color"] = str(style["color"]).lower()
        if style.get("bold"):
            run["bold"] = True
        runs.append(run)
    for extra in node.get("extra") or []:
        runs.extend(flatten_runs(extra, style))
    return runs


def norm_res(s: str) -> str:
    s = str(s or "").strip()
    if s.startswith("minecraft:"):
        s = s[10:]
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", s).replace("generic.", "").lower()
    return ("minecraft:" + s) if s else ""


def norm_slot(s: str) -> str:
    s = str(s or "").lower().replace("minecraft:", "")
    s = s.replace("main_hand", "mainhand").replace("off_hand", "offhand")
    if s in {"any", "armor", ""}:
        return ""
    return s


def canon_text(value):
    parsed = parse_text_component(value)
    if parsed is None:
        return None
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def is_item(obj) -> bool:
    if not isinstance(obj, dict) or "id" not in obj:
        return False
    raw = obj.get("id")
    if raw in (None, "", 0, "minecraft:air", "air"):
        return False
    return "count" in obj or "Count" in obj or "components" in obj or "tag" in obj or "Slot" in obj


def stack_count(item: dict) -> int:
    n = item.get("count", item.get("Count", 1))
    try:
        return max(1, int(n))
    except Exception:
        return 1


def item_id(item: dict) -> str:
    raw = item.get("id")
    if isinstance(raw, int):
        return f"minecraft:legacy_{raw}"
    iid = str(raw or "")
    if iid and ":" not in iid:
        iid = "minecraft:" + iid
    if iid in {"minecraft:skull", "minecraft:player_head"}:
        dmg = item.get("Damage", item.get("damage"))
        tag = legacy_tag(item)
        if dmg is None:
            dmg = tag.get("SkullType")
        try:
            dmg = int(dmg) if dmg is not None else None
        except Exception:
            dmg = None
        if iid == "minecraft:skull" and dmg in SKULL_DAMAGE_IDS:
            return SKULL_DAMAGE_IDS[dmg]
    return iid


def components(item: dict) -> dict:
    c = item.get("components")
    return c if isinstance(c, dict) else {}


def legacy_tag(item: dict) -> dict:
    t = item.get("tag")
    return t if isinstance(t, dict) else {}


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _as_list(value):
    return value if isinstance(value, list) else []


def is_special_container(iid: str) -> bool:
    name = iid.split(":")[-1]
    return (
        name == "shulker_box"
        or name.endswith("_shulker_box")
        or name == "bundle"
        or name.endswith("_bundle")
        or name in {"chest", "trapped_chest", "barrel", "hopper", "dropper", "dispenser", "crafter", "decorated_pot"}
    )


def nested_items(item: dict):
    comps = components(item)
    for slot in _as_list(comps.get("minecraft:container")):
        if isinstance(slot, dict):
            yield slot.get("item")
    for it in _as_list(comps.get("minecraft:bundle_contents")):
        yield it
    tag = legacy_tag(item)
    bet = tag.get("BlockEntityTag")
    if isinstance(bet, dict):
        for it in _as_list(bet.get("Items")):
            yield it
    for it in _as_list(tag.get("Items")):
        yield it
    if isinstance(bet, dict):
        for it in _as_list(bet.get("items")):
            yield it


def walk_item_tree(item):
    if not is_item(item):
        return
    yield item
    for child in nested_items(item):
        yield from walk_item_tree(child)


def get_lore(item: dict):
    comps = components(item)
    raw = None
    if "minecraft:lore" in comps:
        raw = comps.get("minecraft:lore")
    else:
        display = _as_dict(legacy_tag(item).get("display"))
        raw = display.get("Lore") or display.get("lore")
    if not raw:
        return []
    lines = raw if isinstance(raw, list) else [raw]
    parsed = [parse_text_component(x) for x in lines]
    return [p for p in parsed if p]


def get_custom_name(item: dict):
    comps = components(item)
    raw = None
    if "minecraft:custom_name" in comps:
        raw = comps.get("minecraft:custom_name")
    elif "minecraft:item_name" in comps:
        raw = comps.get("minecraft:item_name")
    else:
        display = _as_dict(legacy_tag(item).get("display"))
        raw = display.get("Name") or display.get("name")
    return parse_text_component(raw)


def _norm_enchant_id(eid) -> str:
    if isinstance(eid, int) or (isinstance(eid, str) and eid.isdigit()):
        mapped = LEGACY_ENCHANT_IDS.get(int(eid))
        if mapped:
            return mapped
        return f"minecraft:legacy_enchant_{eid}"
    eid = str(eid or "")
    if eid and ":" not in eid:
        eid = "minecraft:" + eid
    return eid


def _enchant_map(raw) -> dict:
    out = {}
    if isinstance(raw, dict):
        levels = raw.get("levels") if "levels" in raw and isinstance(raw.get("levels"), dict) else raw
        for k, v in levels.items():
            if k in {"levels", "show_in_tooltip", "id"}:
                continue
            try:
                lvl = int(v)
            except Exception:
                continue
            if lvl:
                out[_norm_enchant_id(k)] = lvl
        return out
    if isinstance(raw, list):
        for e in raw:
            if not isinstance(e, dict):
                continue
            eid = e.get("id")
            if eid is None:
                continue
            try:
                lvl = int(e.get("lvl", e.get("level", 1)))
            except Exception:
                lvl = 1
            if lvl:
                out[_norm_enchant_id(eid)] = lvl
    return out


def get_enchantments(item: dict, stored: bool = False) -> dict:
    comps = components(item)
    key = "minecraft:stored_enchantments" if stored else "minecraft:enchantments"
    if key in comps:
        return _enchant_map(comps.get(key))
    tag = legacy_tag(item)
    if stored:
        return _enchant_map(tag.get("StoredEnchantments"))
    return _enchant_map(tag.get("Enchantments") or tag.get("ench"))


def lore_nonempty(lore) -> bool:
    if not lore:
        return False
    if isinstance(lore, list):
        return any(lore_nonempty(x) for x in lore)
    if isinstance(lore, dict):
        if str(lore.get("text") or "").strip():
            return True
        if lore.get("translate"):
            return True
        return lore_nonempty(lore.get("extra"))
    if isinstance(lore, str):
        s = lore.strip()
        if not s or s in {"[]", "{}", '""', "''"}:
            return False
        return True
    return False


def name_nonempty(name) -> bool:
    return lore_nonempty(name) if not isinstance(name, str) else bool(name.strip())


def illegal_enchants(ench: dict) -> bool:
    for eid, lvl in ench.items():
        if ":" in eid and not eid.startswith("minecraft:"):
            return True
        max_lvl = VANILLA_MAX_LEVELS.get(eid)
        if max_lvl is not None and lvl > max_lvl:
            return True
    return False


def plugin_namespaces(item: dict) -> list:
    found = []
    comps = components(item)
    for k in comps:
        ks = str(k)
        if ":" in ks:
            ns = ks.split(":", 1)[0]
            if ns not in {"minecraft", "neoforge", "forge", "fabric", "paper", "spigot", "bukkit"}:
                found.append(ks)
    custom = comps.get("minecraft:custom_data")
    if isinstance(custom, dict):
        pbv = custom.get("PublicBukkitValues") or custom.get("public_bukkit_values")
        if isinstance(pbv, dict):
            for k in pbv:
                found.append(str(k))
        else:
            for k in custom:
                if k in {"PublicBukkitValues", "public_bukkit_values"}:
                    continue
                found.append("custom_data:" + str(k))
    tag = legacy_tag(item)
    pbv = tag.get("PublicBukkitValues")
    if isinstance(pbv, dict):
        for k in pbv:
            found.append(str(k))
    return sorted(set(found))


def get_attributes(item: dict) -> list:
    raw = components(item).get("minecraft:attribute_modifiers")
    if raw is None:
        raw = legacy_tag(item).get("AttributeModifiers")
    entries = []
    if isinstance(raw, dict):
        raw = raw.get("modifiers", raw.get("Modifier", []))
    for m in _as_list(raw):
        if not isinstance(m, dict):
            continue
        op = m.get("operation") if m.get("operation") is not None else m.get("Operation")
        if op in (0, "0", "add_value"):
            op = "add_value"
        elif op in (1, "1", "add_multiplied_base"):
            op = "add_multiplied_base"
        elif op in (2, "2", "add_multiplied_total"):
            op = "add_multiplied_total"
        else:
            op = str(op or "add_value")
        entries.append({
            "type": norm_res(m.get("type") or m.get("AttributeName") or m.get("id") or ""),
            "operation": op,
            "amount": float(m.get("amount", m.get("Amount", 0))),
            "slot": norm_slot(m.get("slot") or m.get("Slot") or ""),
        })
    return entries


def get_custom_model_data(item: dict):
    comps = components(item)
    if "minecraft:custom_model_data" in comps:
        return comps.get("minecraft:custom_model_data")
    tag = legacy_tag(item)
    if "CustomModelData" in tag:
        return tag.get("CustomModelData")
    return None


def get_profile(item: dict):
    raw = components(item).get("minecraft:profile")
    if raw is None:
        raw = _as_dict(legacy_tag(item).get("SkullOwner"))
    if isinstance(raw, str):
        return {"name": raw}
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") or raw.get("Name")
    uid = raw.get("id") or raw.get("Id")
    if isinstance(uid, list):
        uid = None
    out = {}
    if name:
        out["name"] = str(name)
    if uid:
        out["id"] = str(uid)
    return out or None


def tooltip_hidden(item: dict) -> tuple[bool, list]:
    td = components(item).get("minecraft:tooltip_display")
    hidden = []
    hide_all = False
    if isinstance(td, dict):
        hide_all = bool(td.get("hide_tooltip") or td.get("hideTooltip"))
        hidden = [str(x) for x in _as_list(td.get("hidden_components"))]
    hideflags = legacy_tag(item).get("HideFlags")
    try:
        flags = int(hideflags) if hideflags is not None else 0
    except Exception:
        flags = 0
    if flags & 1:
        hidden.append("minecraft:enchantments")
    if flags & 2:
        hidden.append("minecraft:attribute_modifiers")
    if flags & 4:
        hidden.append("minecraft:unbreakable")
    if flags & 32:
        hidden.append("minecraft:dyed_color")
    return hide_all, hidden


def qualify(item: dict) -> list[str]:
    """Return reason tags if this item belongs in the catalog."""
    if not is_item(item):
        return []
    iid = item_id(item)
    reasons = []
    lore = get_lore(item)
    name = get_custom_name(item)
    ench = get_enchantments(item)
    stored = get_enchantments(item, stored=True)
    cmd = get_custom_model_data(item)
    attrs = get_attributes(item)
    plugins = plugin_namespaces(item)
    comps = components(item)

    if lore_nonempty(lore):
        reasons.append("lore")
    if illegal_enchants(ench) or illegal_enchants(stored):
        reasons.append("illegal_enchant")
    if cmd is not None:
        reasons.append("custom_model_data")
    if plugins:
        reasons.append("plugin")
    if attrs:
        reasons.append("attributes")
    if comps.get("minecraft:unbreakable") or legacy_tag(item).get("Unbreakable"):
        reasons.append("unbreakable")
    if "minecraft:enchantment_glint_override" in comps:
        reasons.append("glint_override")

    extra = bool(reasons)
    if iid in SKIP_UNLESS_EXTRA and not extra:
        return []
    # Anvil-renamed shulkers/bundles/chests are vanilla. Only list them if they
    # also have lore, illegal enchants, plugin data, etc. Contents are still walked.
    if not extra:
        return []
    return reasons


def extract(item: dict) -> dict | None:
    reasons = qualify(item)
    if not reasons:
        return None
    iid = item_id(item)
    comps = components(item)
    hide_all, hidden = tooltip_hidden(item)
    dyed = comps.get("minecraft:dyed_color")
    if dyed is None:
        dyed = _as_dict(legacy_tag(item).get("display")).get("color")
    if isinstance(dyed, dict):
        dyed = dyed.get("rgb", dyed.get("value"))
    try:
        dyed = int(dyed) if dyed is not None else None
    except Exception:
        dyed = None
    trim = comps.get("minecraft:trim") or legacy_tag(item).get("Trim") or legacy_tag(item).get("trim")
    if isinstance(trim, dict):
        trim = {
            "pattern": norm_res(trim.get("pattern") or ""),
            "material": norm_res(trim.get("material") or ""),
        }
        if not trim["pattern"] and not trim["material"]:
            trim = None
    else:
        trim = None
    glint_override = comps.get("minecraft:enchantment_glint_override")
    ench = get_enchantments(item)
    stored = get_enchantments(item, stored=True)
    has_glint = bool(ench or stored or glint_override is True)
    if glint_override is False:
        has_glint = False
    rec = {
        "item_id": iid,
        "name": redact_walk(get_custom_name(item)),
        "lore": redact_walk(get_lore(item)),
        "enchantments": ench,
        "stored_enchantments": stored,
        "glint": has_glint,
        "unbreakable": bool(comps.get("minecraft:unbreakable") or legacy_tag(item).get("Unbreakable")),
        "dyed_color": dyed,
        "trim": trim,
        "custom_model_data": get_custom_model_data(item),
        "attributes": get_attributes(item),
        "profile": get_profile(item) if iid.endswith("player_head") or iid == "minecraft:player_head" else None,
        "hide_tooltip": hide_all,
        "hidden": hidden,
        "rarity": str(comps["minecraft:rarity"]) if "minecraft:rarity" in comps else None,
        "reasons": reasons,
        "plugin_keys": plugin_namespaces(item)[:24],
    }
    rec = json.loads(json.dumps(rec, default=str))
    return rec


def fingerprint(rec: dict) -> str:
    ench = {norm_res(k): int(v) for k, v in (rec.get("enchantments") or {}).items() if int(v or 0)}
    stored = {norm_res(k): int(v) for k, v in (rec.get("stored_enchantments") or {}).items() if int(v or 0)}
    attrs = sorted(
        (
            {
                "type": norm_res(a.get("type")),
                "operation": a.get("operation") or "add_value",
                "amount": round(float(a.get("amount") or 0), 4),
                "slot": norm_slot(a.get("slot")),
            }
            for a in (rec.get("attributes") or [])
        ),
        key=lambda a: json.dumps(a, sort_keys=True),
    )
    payload = {
        "item_id": rec.get("item_id"),
        "name": flatten_runs(rec.get("name")),
        "lore": [flatten_runs(line) for line in (rec.get("lore") or []) if flatten_runs(line)],
        "enchantments": ench,
        "stored_enchantments": stored,
        "unbreakable": bool(rec.get("unbreakable")),
        "dyed_color": rec.get("dyed_color"),
        "trim": rec.get("trim"),
        "custom_model_data": rec.get("custom_model_data"),
        "attributes": attrs,
        "profile": rec.get("profile"),
        "plugin_keys": rec.get("plugin_keys") or [],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def harvest(item: dict) -> list[tuple[str, dict, int]]:
    found = []
    for it in walk_item_tree(item):
        rec = extract(it)
        if rec is None:
            continue
        found.append((fingerprint(rec), rec, stack_count(it)))
    return found


def harvest_from_entity(ent: dict) -> list[tuple[str, dict, int]]:
    found = []
    if not isinstance(ent, dict):
        return found
    for k, v in ent.items():
        lk = str(k).lower()
        if lk in ITEM_COMPOUND_KEYS and isinstance(v, dict):
            found.extend(harvest(v))
        elif lk in ITEM_LIST_KEYS:
            for it in _as_list(v):
                found.extend(harvest(it))
        elif lk == "equipment" and isinstance(v, dict):
            for it in v.values():
                found.extend(harvest(it))
        elif lk == "passengers":
            for passenger in _as_list(v):
                found.extend(harvest_from_entity(passenger))
    return found


# --- MCA / NBT IO ----------------------------------------------------------

def load_nbt_file(path: Path):
    try:
        if path.stat().st_size < 8:
            return None
        py = to_py(nbtlib.load(path))
        return py if isinstance(py, dict) else None
    except Exception:
        return None


def _lz4_decompress(payload: bytes) -> bytes:
    import lz4.block
    try:
        return lz4.block.decompress(payload)
    except Exception:
        if len(payload) >= 4:
            size = int.from_bytes(payload[:4], "little")
            if 0 < size < 8_000_000:
                return lz4.block.decompress(payload[4:], uncompressed_size=size)
        raise


def iter_mca_nbt(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return
    if len(data) < 8192:
        return
    for i in range(1024):
        loc = struct.unpack(">I", data[i * 4 : i * 4 + 4])[0]
        offset = (loc >> 8) * 4096
        sectors = loc & 0xFF
        if offset == 0 or sectors == 0:
            continue
        if offset + 5 > len(data):
            continue
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        if length <= 1 or offset + 4 + length > len(data):
            continue
        ctype = data[offset + 4]
        payload = data[offset + 5 : offset + 4 + length]
        try:
            if ctype == 1:
                raw = gzip.decompress(payload)
            elif ctype == 2:
                raw = zlib.decompress(payload)
            elif ctype == 3:
                raw = payload
            elif ctype == 4:
                raw = _lz4_decompress(payload)
            else:
                continue
            nbt = nbtlib.File.parse(io.BytesIO(raw))
            yield to_py(nbt)
        except Exception:
            continue


def harvest_player_nbt(root: dict) -> list[tuple[str, dict, int]]:
    found = []
    if not isinstance(root, dict):
        return found
    for key in ("Inventory", "inventory", "EnderItems", "ender_items"):
        for it in _as_list(root.get(key)):
            found.extend(harvest(it))
    eq = root.get("equipment")
    if isinstance(eq, dict):
        for it in eq.values():
            found.extend(harvest(it))
    for key in ("ArmorItems", "HandItems", "SelectedItem", "selectedItem"):
        val = root.get(key)
        if isinstance(val, list):
            for it in val:
                found.extend(harvest(it))
        elif isinstance(val, dict):
            found.extend(harvest(val))
    return found


def chunk_data_version(root: dict):
    if not isinstance(root, dict):
        return None
    dv = root.get("DataVersion")
    if dv is None:
        level = root.get("Level")
        if isinstance(level, dict):
            dv = level.get("DataVersion")
    try:
        return int(dv) if dv is not None else None
    except Exception:
        return None


def harvest_chunk_nbt(root: dict) -> list[tuple[str, dict, int]]:
    """Items from any on-disk chunk generation (Level-wrapped or 1.18+ flat)."""
    found = []
    if not isinstance(root, dict):
        return found
    level = root.get("Level") if isinstance(root.get("Level"), dict) else {}
    bes = (
        root.get("block_entities")
        or root.get("BlockEntities")
        or level.get("block_entities")
        or level.get("TileEntities")
        or []
    )
    ents = (
        root.get("Entities")
        or root.get("entities")
        or level.get("Entities")
        or []
    )
    for be in _as_list(bes):
        found.extend(harvest_from_entity(be))
    for ent in _as_list(ents):
        found.extend(harvest_from_entity(ent))
    return found


# --- sqlite store ----------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    mtime REAL,
    size INTEGER,
    scanned_at TEXT,
    hits INTEGER DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS hits (
    path TEXT NOT NULL,
    fp TEXT NOT NULL,
    rec_json TEXT NOT NULL,
    count INTEGER NOT NULL,
    PRIMARY KEY (path, fp)
);
CREATE INDEX IF NOT EXISTS hits_fp ON hits(fp);
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._versions = {}
        if self.meta_get("scan_format") != SCAN_FORMAT:
            self.conn.execute("DELETE FROM hits")
            self.conn.execute("DELETE FROM files")
            self.meta_set("scan_format", SCAN_FORMAT)
            self.meta_set("data_versions", "{}")
            self.conn.commit()

    def meta_get(self, k, default=None):
        row = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else default

    def meta_set(self, k, v):
        self.conn.execute("REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def note_version(self, dv):
        if dv is None:
            return
        try:
            dv = int(dv)
        except Exception:
            return
        self._versions[dv] = self._versions.get(dv, 0) + 1

    def flush_versions(self):
        if not self._versions:
            return
        prev = {}
        raw = self.meta_get("data_versions") or "{}"
        try:
            prev = json.loads(raw)
        except Exception:
            prev = {}
        for k, v in self._versions.items():
            key = str(k)
            prev[key] = int(prev.get(key, 0)) + v
        self._versions.clear()
        self.meta_set("data_versions", json.dumps(prev, separators=(",", ":")))

    def version_histogram(self) -> dict:
        self.flush_versions()
        try:
            return json.loads(self.meta_get("data_versions") or "{}")
        except Exception:
            return {}

    def file_done(self, path: Path, mtime: float, size: int) -> bool:
        row = self.conn.execute(
            "SELECT mtime, size FROM files WHERE path=?", (str(path),)
        ).fetchone()
        return bool(row and row[0] == mtime and row[1] == size)

    def record_file(self, path: Path, kind: str, mtime: float, size: int, hits: list, error: str | None):
        p = str(path)
        self.conn.execute("DELETE FROM hits WHERE path=?", (p,))
        for fp, rec, count in hits:
            self.conn.execute(
                "INSERT INTO hits(path, fp, rec_json, count) VALUES(?,?,?,?)",
                (p, fp, json.dumps(rec, separators=(",", ":"), default=str), count),
            )
        self.conn.execute(
            "REPLACE INTO files(path, kind, mtime, size, scanned_at, hits, error) VALUES(?,?,?,?,datetime('now'),?,?)",
            (p, kind, mtime, size, len(hits), error),
        )

    def commit(self):
        self.flush_versions()
        self.conn.commit()

    def stats(self):
        files_done = self.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        unique = self.conn.execute("SELECT COUNT(DISTINCT fp) FROM hits").fetchone()[0]
        instances = self.conn.execute("SELECT COALESCE(SUM(count),0) FROM hits").fetchone()[0]
        return files_done, unique, instances

    def export_items(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT fp, rec_json, SUM(count) FROM hits GROUP BY fp"
        ).fetchall()
        items = []
        for fp, rec_json, count in rows:
            rec = json.loads(rec_json)
            rec["count"] = int(count)
            rec["fp"] = fp
            items.append(rec)
        def sort_key(r):
            name = r.get("name")
            if isinstance(name, str) and name.strip():
                label = name
            elif isinstance(name, dict):
                label = str(name.get("text") or "")
            else:
                label = ""
            return (label.lower(), r.get("item_id") or "", -r.get("count", 0))
        items.sort(key=sort_key)
        return items

    def close(self):
        self.conn.commit()
        self.conn.close()


def write_json_atomic(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)
