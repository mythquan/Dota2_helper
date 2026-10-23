"""Read Dota 2's own Simplified Chinese hero/item names from its VPK."""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path


LOCALIZATION_PATH = "resource/localization/abilities_schinese.txt"
TOKEN_RE = re.compile(r'^\s*"([^"\\]+)"\s*"((?:\\.|[^"\\])*)"', re.MULTILINE)


def locate_dota_dir() -> Path | None:
    override = os.environ.get("DOTA2_GAME_DIR")
    if override:
        path = Path(override)
        return path if (path / "pak01_dir.vpk").is_file() else None
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
                steam = Path(winreg.QueryValueEx(key, "SteamPath")[0])
            path = steam / "steamapps" / "common" / "dota 2 beta" / "game" / "dota"
            if (path / "pak01_dir.vpk").is_file():
                return path
        except (OSError, FileNotFoundError):
            pass
    return None


def read_vpk_file(dota_dir: Path, target: str) -> bytes:
    """Read one uncompressed VPK entry; no game files are changed."""
    directory = dota_dir / "pak01_dir.vpk"
    data = directory.read_bytes()
    signature, version, tree_size = struct.unpack_from("<III", data)
    if signature != 0x55AA1234 or version not in (1, 2):
        raise ValueError("Unsupported Dota VPK format")
    header_size = 28 if version == 2 else 12
    cursor = header_size
    end = cursor + tree_size

    def string() -> str:
        nonlocal cursor
        stop = data.index(0, cursor, end)
        value = data[cursor:stop].decode("utf-8")
        cursor = stop + 1
        return value

    while cursor < end:
        extension = string()
        if not extension:
            break
        while True:
            folder = string()
            if not folder:
                break
            while True:
                name = string()
                if not name:
                    break
                _, preload_size, archive, offset, length, terminator = struct.unpack_from("<IHHIIH", data, cursor)
                cursor += 18
                if terminator != 0xFFFF:
                    raise ValueError("Invalid VPK directory entry")
                preload = data[cursor:cursor + preload_size]
                cursor += preload_size
                path = f"{folder}/{name}.{extension}"
                if path == target:
                    if archive == 0x7FFF:
                        body = data[header_size + tree_size + offset:header_size + tree_size + offset + length]
                    else:
                        with (dota_dir / f"pak01_{archive:03d}.vpk").open("rb") as handle:
                            handle.seek(offset)
                            body = handle.read(length)
                    if len(body) != length:
                        raise ValueError("Incomplete VPK entry")
                    return preload + body
    raise FileNotFoundError(target)


class Localizer:
    def __init__(self, tokens: dict[str, str] | None = None, source: str = "unavailable"):
        self.tokens = tokens or {}
        self.source = source

    @classmethod
    def from_game(cls) -> "Localizer":
        dota_dir = locate_dota_dir()
        if dota_dir is None:
            return cls()
        try:
            content = read_vpk_file(dota_dir, LOCALIZATION_PATH).decode("utf-8-sig")
            tokens = {key.lower(): value.replace(r'\"', '"') for key, value in TOKEN_RE.findall(content)}
            return cls(tokens, str(dota_dir / "pak01_dir.vpk") + ":" + LOCALIZATION_PATH)
        except (OSError, ValueError, UnicodeError):
            return cls()

    def hero(self, internal_name: str) -> str:
        value = self.tokens.get(internal_name.lower() + ":n")
        return value if value else internal_name.replace("npc_dota_hero_", "")

    def item(self, internal_name: str) -> str:
        value = self.tokens.get("dota_tooltip_ability_" + internal_name.lower())
        return value if value else internal_name.replace("item_", "")
