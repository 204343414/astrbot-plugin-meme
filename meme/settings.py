"""网页调参：哪些配置能在网页改、每个值的合法范围。

白名单是有意为之 —— 网页能把配置写回文件，如果允许任意键，一个手滑的
前端 bug 就能往插件配置里塞垃圾字段，而 AstrBot 对多出来的键不会报错，
只会静静地让插件行为变得没人说得清。所以这里只开放真正值得边看图边调的项，
Provider 这类「选错就整条链挂掉」的，网页只读展示，改还是去配置页改。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # "int" | "float" | "bool" | "choice" | "flags" | "text"
    minimum: float = 0
    maximum: float = 0
    choices: tuple[str, ...] = ()
    hint: str = ""


FIELDS: tuple[Field, ...] = (
    Field("nsfw_max_level", "色情等级上限", "int", 0, 4,
          hint="0 安全 … 4 露骨；调到 2 会明显宽松"),
    Field("require_is_meme", "必须判定为梗图才收", "bool",
          hint="关掉之后聊天截图、公告也会入库"),
    Field("moderation_fail_closed", "审核失败时拒收", "bool",
          hint="关掉 = 审核超时也放行"),
    Field("caption_zh_mode", "英文梗图中文化", "choice", choices=("off", "append", "redraw"),
          hint="append 只翻字幕；redraw 真重画中文版（慢且贵）"),
    Field("redraw_size", "重绘尺寸", "choice",
          choices=("auto", "1024x1024", "1536x1024", "1024x1536"),
          hint="auto = 按原图横竖自动挑画布，画完裁回原比例"),
    Field("reject_flags", "直接拒收的类别", "flags",
          hint="钩上的类别命中就丢；色情走左边那个等级阈值"),
    Field("weight_submitted", "投稿图权重", "float", 0, 100),
    Field("weight_api", "接口图权重", "float", 0, 100),
    Field("cooldown_seconds", "同一个人的冷却（秒）", "int", 0, 3600),
    Field("send_cooldown_seconds", "同一个群的冷却（秒）", "int", 0, 600),
    Field("caption_limit", "图注字数上限", "int", 0, 500),
    Field("daily_push_max_per_day", "每群每天最多推几条", "int", 0, 20),
    Field("card_enabled", "发梗图用卡片（带按钮）", "bool",
          hint="关掉就退回纯图片；只有 QQ 官方号能显示按钮"),
    Field("card_show_again", "卡片上显示「再来一张」", "bool"),
    Field("card_show_howto", "卡片上显示「我要投稿」", "bool"),
    Field("card_material_keep", "卡片图保留几张（素材额度）", "int", 5, 90,
          hint="QQ 素材库每机器人 100 张；留太多会撞上限，留 60 左右合适"),
    Field("card_material_reuse_days", "同一张图复用几天", "int", 1, 89,
          hint="QQ 引用 90 天没被用到才失效；留 80 天，失效了会自动重传"),
    Field("card_show_auto", "卡片上显示「自动订阅/取消自动」按钮", "bool",
          hint="关掉就没有第三个按钮；已经订阅的群照常自动发"),
    Field("auto_push_enabled", "自动搬石总开关", "bool",
          hint="关掉后所有群都不再自动发，各群的订阅档位留着"),
    Field("auto_push_low_pct", "低档概率（%／小时）", "int", 0, 100,
          hint="说白了：选「低」的群每小时有这么多机会冒一张，10% ≈ 平均两天多一张"),
    Field("auto_push_mid_pct", "中档概率（%／小时）", "int", 0, 100,
          hint="说白了：25% ≈ 平均一天六张"),
    Field("auto_push_high_pct", "高档概率（%／小时）", "int", 0, 100,
          hint="说白了：50% ≈ 平均一天十二张；嫌吵就调低，群里看到的按钮数字会跟着变"),
    Field("auto_push_max_per_day", "每群每天最多自动发几条", "int", 0, 48,
          hint="0 = 不限；默认 8 条，到了就停到第二天"),
    Field("auto_push_quiet", "自动推送免打扰时段", "text",
          hint="写成 23:00-08:00 这样（可跨零点）；留空 = 全天都可能发"),
    Field("ban_malicious_submitter", "恶意投稿永久禁止投稿", "bool",
          hint="只针对故意投「能让 bot 被封号」的内容；地狱笑话、擦边、微恶俗不算"),
    Field("ban_notify_group", "拉黑后在群里说一句", "bool"),
)

FLAG_CHOICES: tuple[tuple[str, str], ...] = (
    ("sexual", "色情"),
    ("minor", "未成年"),
    ("gore", "血腥"),
    ("violence", "打斗"),
    ("politics", "现实政治"),
    ("hate", "歧视"),
    ("ads_or_watermark", "广告/二维码/水印"),
    ("text_heavy", "文字太多"),
    ("other", "其他"),
)

#: 网页只读展示的项：改错了整条链会挂，去插件配置页改。
READONLY_KEYS: tuple[tuple[str, str], ...] = (
    ("moderation_provider_id", "审核 Provider"),
    ("translate_provider_id", "翻译 Provider（空=复用审核）"),
    ("redraw_provider_id", "画图 Provider"),
    ("redraw_model", "重绘模型"),
    ("custom_data_dir", "数据目录"),
)

FIELD_BY_KEY = {field.key: field for field in FIELDS}


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是")


def coerce(key: str, value) -> tuple[object, str]:
    """``(规整后的值, 错误说明)``。错误说明非空表示这一项不合法、不要写。"""
    field = FIELD_BY_KEY.get(key)
    if field is None:
        return None, f"不认识的配置项：{key}"
    if field.kind == "bool":
        return _as_bool(value), ""
    if field.kind == "choice":
        text = str(value or "").strip()
        if text not in field.choices:
            return None, f"{field.label} 只能是 {'/'.join(field.choices)}"
        return text, ""
    if field.kind in ("int", "float"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, f"{field.label} 需要一个数字"
        if number < field.minimum or number > field.maximum:
            return None, f"{field.label} 要在 {field.minimum:g}–{field.maximum:g} 之间"
        return (int(number) if field.kind == "int" else float(number)), ""
    if field.kind == "flags":
        raw = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
        allowed = {name for name, _label in FLAG_CHOICES}
        picked = [str(item).strip().lower() for item in raw if str(item).strip()]
        unknown = [item for item in picked if item not in allowed]
        if unknown:
            return None, "不认识的类别：" + "、".join(unknown)
        return picked, ""
    return str(value or "").strip(), ""


def export_settings(config) -> dict:
    """网页要的那一份（含只读项，前端用它渲染）."""
    values = {}
    for field in FIELDS:
        raw = config.get(field.key)
        values[field.key] = list(raw) if field.kind == "flags" and raw else raw
    readonly = {key: config.get(key, "") for key, _label in READONLY_KEYS}
    return {
        "values": values,
        "readonly": readonly,
        "fields": [
            {
                "key": field.key,
                "label": field.label,
                "kind": field.kind,
                "min": field.minimum,
                "max": field.maximum,
                "choices": list(field.choices),
                "hint": field.hint,
            }
            for field in FIELDS
        ],
        "flags": [{"key": name, "label": label} for name, label in FLAG_CHOICES],
        "readonlyMeta": [{"key": key, "label": label} for key, label in READONLY_KEYS],
    }


def apply_settings(config, updates: dict) -> tuple[dict, list[str]]:
    """把网页提交的值写进配置字典（不落盘）。

    返回 ``(实际写进去的键值, 错误列表)``。有错误的项整体跳过，而不是整批失败：
    调参页上十来个开关，其中一个填错就把其余的也丢掉，用户会以为页面坏了。
    """
    applied: dict = {}
    errors: list[str] = []
    for key, value in (updates or {}).items():
        if key not in FIELD_BY_KEY:
            errors.append(f"不认识的配置项：{key}")
            continue
        cleaned, error = coerce(key, value)
        if error:
            errors.append(error)
            continue
        config[key] = cleaned
        applied[key] = cleaned
    return applied, errors


def auto_prob_problem(values: dict) -> str:
    """三档概率的顺序检查。返回空串表示没问题。

    为什么要拦：低档填 60、高档填 10 这种手滑，在页面上完全看不出来（三个滑块
    长得一样），但群里的「低/中/高」就会彻底反过来，事后很难想明白。
    """
    try:
        low = int(values.get("auto_push_low_pct", 10))
        mid = int(values.get("auto_push_mid_pct", 25))
        high = int(values.get("auto_push_high_pct", 50))
    except (TypeError, ValueError):
        return "三档概率都得是数字"
    if not (low <= mid <= high):
        return f"概率要满足 低 ≤ 中 ≤ 高（现在是 {low} / {mid} / {high}）"
    return ""
