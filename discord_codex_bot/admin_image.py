"""Operator commands for image generation: image.get and image.set read and change a server's
switch and spoiler setting (imagegen.ImageSettings, the same store the slash command and the
<image_gen/> tag are checked against). Nothing here generates an image or posts to a channel.
The schema format is in admin_server.py."""

from __future__ import annotations

from typing import Any

from .admin_server import Handler, Rejected
from .imagegen import SPOILER_MODES

GUILD = {"type": "id", "required": True, "help": "伺服器 id（要在 ALLOWED_GUILD_IDS 內）"}
EG = "--guild 123456789012345678"
SPOILER_TEXT = {
    "auto": "auto（服務依提示詞判定為成人內容的圖才隱藏）",
    "always": "always（每張都隱藏）",
    "off": "off（都不隱藏）",
}


def _state(client: Any, guild_id: int) -> dict:
    settings = client.image_settings
    on, service = settings.enabled(guild_id), client.images.enabled
    spoiler = settings.spoiler(guild_id)
    note = "" if service else "（IMAGE_GEN_URL 未設定，目前沒有圖片生成）"
    lines = [
        f"圖片生成：{'開' if on else '關'}{note}",
        f"暴雷隱藏：{SPOILER_TEXT[spoiler]}",
    ]
    state = {
        "enabled": on,
        "service": service,
        "spoiler": spoiler,
    }
    return {"text": "\n".join(lines), **state}


async def image_get(client: Any, args: dict) -> dict:
    return _state(client, args["guild"])


async def image_set(client: Any, args: dict) -> dict:
    state = args.get("state")
    enabled = None if state is None else state == "on"
    if enabled is None and "spoiler" not in args:
        raise Rejected("至少要給 --state 或 --spoiler 其中一個")
    client.image_settings.update(
        args["guild"],
        enabled=enabled,
        spoiler=args.get("spoiler"),
    )
    return _state(client, args["guild"])


HANDLERS: dict[str, Handler] = {
    "image.get": image_get,
    "image.set": image_set,
}

GROUPS: dict[str, str] = {
    "image": "伺服器的圖片生成開關與暴雷隱藏設定：get 查詢、set 設定",
}

SCHEMAS: dict[str, dict] = {
    "image.get": {
        "help": "查詢伺服器的圖片生成開關與暴雷隱藏設定",
        "examples": [f"image get {EG}"],
        "args": {
            "guild": GUILD,
        },
    },
    "image.set": {
        "help": "設定伺服器的圖片生成開關與暴雷隱藏設定",
        "description": (
            "改 --guild 的圖片生成設定，只給要改的選項。\n"
            "開關從下一個請求起生效，進行中的工作不受影響。\n"
            "這個指令本身不在頻道發訊息。"
        ),
        "examples": [
            f"image set {EG} --state off",
            f"image set {EG} --spoiler always",
        ],
        "args": {
            "guild": GUILD,
            "state": {
                "type": "choice",
                "choices": ["on", "off"],
                "help": "on＝開放；off＝關閉（斜線指令與對話裡的畫圖都停）",
            },
            "spoiler": {
                "type": "choice",
                "choices": list(SPOILER_MODES),
                "help": "暴雷隱藏：auto＝判定為成人內容才隱藏；always＝全部；off＝不隱藏",
            },
        },
    },
}
