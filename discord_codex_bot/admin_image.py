"""Operator commands for image generation: image.get and image.set read and change a server's
switch and limits (imagegen.ImageSettings, the same store the slash command and the
<image_gen/> tag are checked against). Nothing here generates an image or posts to a channel.
The schema format is in admin_server.py."""

from __future__ import annotations

from typing import Any

from .admin_server import Handler, Rejected
from .imagegen import MAX_DAILY, MAX_IN_FLIGHT, SPOILER_MODES

GUILD = {"type": "id", "required": True, "help": "伺服器 id（要在 ALLOWED_GUILD_IDS 內）"}
EG = "--guild 123456789012345678"
SPOILER_TEXT = {
    "auto": "auto（服務依提示詞判定為成人內容的圖才隱藏）",
    "always": "always（每張都隱藏）",
    "off": "off（都不隱藏）",
}


def _state(client: Any, guild_id: int, user_id: int | None = None) -> dict:
    settings = client.image_settings
    on, service = settings.enabled(guild_id), client.images.enabled
    daily, in_flight = settings.daily_limit(guild_id), settings.in_flight_limit(guild_id)
    spoiler = settings.spoiler(guild_id)
    note = "" if service else "（IMAGE_GEN_URL 未設定，目前沒有圖片生成）"
    lines = [
        f"圖片生成：{'開' if on else '關'}{note}",
        f"每人每天：{daily} 張",
        f"每人同時：{in_flight} 個工作",
        f"暴雷隱藏：{SPOILER_TEXT[spoiler]}",
    ]
    state = {
        "enabled": on,
        "service": service,
        "daily": daily,
        "in_flight": in_flight,
        "spoiler": spoiler,
    }
    if user_id is not None:
        used = settings.used(guild_id, user_id)
        lines.append(f"成員 {user_id} 今天已用：{used} 張")
        state["used"] = used
    return {"text": "\n".join(lines), **state}


async def image_get(client: Any, args: dict) -> dict:
    return _state(client, args["guild"], args.get("user"))


async def image_set(client: Any, args: dict) -> dict:
    state = args.get("state")
    enabled = None if state is None else state == "on"
    if enabled is None and not {"daily", "in_flight", "spoiler"} & args.keys():
        raise Rejected("至少要給 --state、--daily、--in-flight 或 --spoiler 其中一個")
    client.image_settings.update(
        args["guild"],
        enabled=enabled,
        daily=args.get("daily"),
        in_flight=args.get("in_flight"),
        spoiler=args.get("spoiler"),
    )
    return _state(client, args["guild"])


HANDLERS: dict[str, Handler] = {
    "image.get": image_get,
    "image.set": image_set,
}

GROUPS: dict[str, str] = {
    "image": "伺服器的圖片生成開關與每人上限：get 查詢、set 設定",
}

SCHEMAS: dict[str, dict] = {
    "image.get": {
        "help": "查詢伺服器的圖片生成開關與每人上限；給 --user 也看他今天用了幾張",
        "examples": [f"image get {EG}", f"image get {EG} --user 345678901234567890"],
        "args": {
            "guild": GUILD,
            "user": {
                "type": "id",
                "owner": True,
                "help": "成員 id：看他今天（台灣時間）已用的張數（可以已經離開伺服器）",
            },
        },
    },
    "image.set": {
        "help": "設定伺服器的圖片生成開關與每人上限",
        "description": (
            "改 --guild 的圖片生成設定，只給要改的選項。\n"
            "上限從下一個請求起生效，進行中的工作不受影響；今天已用的張數不會歸零。\n"
            "這個指令本身不在頻道發訊息。"
        ),
        "examples": [
            f"image set {EG} --state off",
            f"image set {EG} --daily 10 --in-flight 2",
            f"image set {EG} --spoiler always",
        ],
        "args": {
            "guild": GUILD,
            "state": {
                "type": "choice",
                "choices": ["on", "off"],
                "help": "on＝開放；off＝關閉（斜線指令與對話裡的畫圖都停）",
            },
            "daily": {"type": "int", "min": 1, "max": MAX_DAILY, "help": "每人每天最多幾張"},
            "in_flight": {
                "type": "int",
                "min": 1,
                "max": MAX_IN_FLIGHT,
                "help": "每人同時最多幾個工作",
            },
            "spoiler": {
                "type": "choice",
                "choices": list(SPOILER_MODES),
                "help": "暴雷隱藏：auto＝判定為成人內容才隱藏；always＝全部；off＝不隱藏",
            },
        },
    },
}
