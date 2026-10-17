"""Reviewed riddles: a small model should not invent contradictory clues."""
from .safety import normalized

RIDDLES = (
    {"question": "长长的耳朵，短短的尾巴，蹦蹦跳跳。你猜是什么动物？", "answer": "兔子", "aliases": ("兔", "rabbit"), "hint": "它很喜欢吃青草，有两只长耳朵。"},
    {"question": "下雨时撑开，帮我们挡雨。你猜是什么？", "answer": "雨伞", "aliases": ("伞", "umbrella"), "hint": "出门遇到下雨，可以把它撑在头顶。"},
    {"question": "有长长的鼻子，大大的耳朵。你猜是什么动物？", "answer": "大象", "aliases": ("象", "elephant"), "hint": "它可以用鼻子卷起食物。"},
    {"question": "圆圆的，可以拍，也可以滚。你猜是什么玩具？", "answer": "球", "aliases": ("球", "ball"), "hint": "篮球、足球都是它的家人。"},
    {"question": "水里游来游去，用鳃呼吸。你猜是什么动物？", "answer": "鱼", "aliases": ("鱼", "fish"), "hint": "它有鱼鳍，可以在水里摆动尾巴。"},
)


def riddle_reply(user_text: str, state: dict | None) -> tuple[str, dict]:
    current = dict(state or {"index": 0, "asked": False, "solved": False})
    value = normalized(user_text)
    if current["solved"] or any(word in value for word in ("下一个", "换一个", "换个", "不会", "放弃")):
        current = {"index": (current["index"] + 1) % len(RIDDLES), "asked": False, "solved": False}
    riddle = RIDDLES[current["index"]]
    if not current["asked"]:
        current["asked"] = True
        return riddle["question"], current
    if any(alias in value and not any(prefix + alias in value for prefix in ("不是", "不猜", "不选")) for alias in riddle["aliases"]):
        current["solved"] = True
        return f"你猜到了，是{riddle['answer']}！想继续或休息都可以。", current
    if any(word in value for word in ("再听", "重复", "再说")):
        return riddle["question"], current
    if any(word in value for word in ("答案", "告诉我")):
        current["solved"] = True
        return f"答案是{riddle['answer']}。想换一个或休息都可以。", current
    return riddle["hint"] + "你可以再猜，也可以说换一个。", current
