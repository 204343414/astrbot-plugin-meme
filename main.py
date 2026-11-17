"""每日梗图：群里点一下就随机来一张，投稿越多越对味。

这个文件只做一件事——把 AstrBot 的东西接到业务层上：
``event`` / ``provider`` / ``hub`` 各自包一层，真正的审核、入库、挑图、
投递降级全在 ``meme/`` 里，那里一行 astrbot 都不 import，所以能脱离框架跑单测。

几条来自平台的硬约束决定了这里的写法：

* **被动回复优先**。群里 ``/meme`` 是一次被动回复，5 分钟窗口内可回 5 条，
  额度上比主动消息宽得多；每日定时推送才走主动消息（QQ 官方：群 1000 条/天、
  bot 维度 60 条/分钟，且要群主在群设置里打开「机器人主动在群聊内发言」）。
* **``context.send_message`` 明确不支持 QQ 官方平台**，所以主动推送走 Hub 的
  ``send_media_message``（一条 msg_type=7 的富媒体消息，不带 msg_id 即主动），
  Hub 不在时才退到 ``send_message``（其他平台可用）。
* **图片要先上传才能发**，AstrBot 原生 ``Image`` 组件会自己上传，Hub 的
  ``send_image_message`` 还能顺手把字幕挂在同一条消息里 —— 后者更好，优先用。
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import importlib
import inspect
import os
import random
import time
import urllib.parse
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

from .meme.adapters import (
    JsonApiImageSource,
    OpenAIImageGenerator,
    ProviderTranslator,
    ProviderVision,
    fetch_bytes,
    resolve_provider_credentials,
)
from .meme.cards import (
    ACTION_AGAIN,
    ACTION_AUTO_LEVELS,
    ACTION_AUTO_LOW,
    ACTION_AUTO_MID,
    ACTION_AUTO_HIGH,
    ACTION_AUTO_OFF,
    ACTION_AUTO_SUB,
    ACTION_HOWTO,
    AUTO_NOTE,
    LEVEL_LABELS,
    build_auto_done_card,
    build_auto_pick_card,
    build_howto_card,
    build_meme_card,
)
from .meme.chinese import MODE_APPEND, MODE_REDRAW, compose_caption
from .meme.delivery import Attempt, deliver
from .meme.models import MemeItem
from .meme.pipeline import MemePipeline
from .meme.review import ReviewPolicy, build_review_prompt
from .meme.select import format_stats
from .meme.store import MemeStore

PLUGIN_NAME = "astrbot_plugin_meme"
HUB_NAME = "astrbot_plugin_qqofficial_hub"
COMMAND_NAMES = ("meme", "梗图", "来张梗图")

#: 一次消息里最多收几张投稿图。3 张足够，再多会让一次审核排队几十秒。
MAX_IMAGES_PER_SUBMIT = 3
#: QQ 富媒体里的「图片」编号。
QQ_FILE_TYPE_IMAGE = 1

#: 卡片素材保留几张。QQ 每个机器人总共 100 张，超了是「新上传被拒」（40035007），
#: 不会挤掉旧的；删除也不影响已发的消息（平台发消息时已转存副本）。
#: 所以这里留多少只影响「还能不能继续发新卡片」，跟历史消息无关。
CARD_MATERIAL_KEEP = 60
CARD_MATERIAL_KEEP_MAX = 90
#: 收到「主动消息无权限」后，这个群多久之内不再试。
PUSH_BLOCK_SECONDS = 24 * 3600
#: 自动推送的默认值（配置项缺省时用）。
AUTO_PCT_DEFAULTS = {"low": 10, "mid": 25, "high": 50}
AUTO_MAX_PER_DAY_DEFAULT = 8
AUTO_QUIET_DEFAULT = "23:00-08:00"

HELP_TEXT = """梗图玩法：
/meme —— 随机来一张（也可以发「梗图」「来张梗图」）
/meme 再来 —— 再摇一张（卡片上那个按钮的同义命令）
引用一张图、@我，然后 /meme 投稿 —— 投稿进图库（所有群共用一个库）
/meme 库 —— 看看库里现在有多少张能发
/meme 自动 —— 看这个群的每小时自动推送状态
/meme 自动 关 —— 关掉本群的自动推送（管理员）
/meme 每日 —— 看每日推送状态
/meme 每日 9:00,21:00 —— 每天这两个点自动发一张（管理员）
/meme 每日 关 —— 关掉自动发（管理员）
/meme 删 <编号> —— 下架一张（管理员）
/meme 审核 <编号> —— 重新审一次（管理员）
/meme 拉 —— 立刻从第三方接口拉一张入库（管理员）
/meme 测试 —— 引用一张图跑一遍完整管线，看审核输出（管理员）
/meme 黑名单 —— 看被永久禁止投稿的人（管理员）
/meme 解禁 <用户ID> —— 恢复某人的投稿权限（管理员）"""


def _image_size(data: bytes, default: int = 720) -> tuple[int, int]:
    """图的宽高（卡片 markdown 的 alt 要带尺寸，否则 QQ 显示裂图）。

    PIL 缺失或不是 PIL 认得的格式时退回正方形：尺寸只影响显示，不影响能不能发。
    """
    try:
        from PIL import Image as _PILImage  # 延迟 import：没装 PIL 也要能跑
        from io import BytesIO

        with _PILImage.open(BytesIO(data)) as img:
            return int(img.width), int(img.height)
    except Exception:  # noqa: BLE001
        return default, default


def _looks_private_host(host: str) -> bool:
    """这地址是不是指向「本机 / 内网」。

    只挡字面量，不做 DNS 解析 —— 解析会在面板线程里卡住，而且挡不住 DNS
    rebinding，收益不对等。挡掉 127.0.0.1、10.x、172.16-31.x、192.168.x、
    169.254.x、::1 和 localhost 这些常见的，够用了。
    """
    import ipaddress

    name = (host or "").strip().strip("[]").lower().rstrip(".")
    if not name:
        return True
    if name in {"localhost", "localhost.localdomain"} or name.endswith(
        (".local", ".internal", ".localdomain")
    ):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False  # 普通域名，交给网络层
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _now_day_key(ts: float | None = None) -> str:
    moment = dt.datetime.fromtimestamp(ts or time.time())
    return moment.strftime("%Y-%m-%d")


def _parse_slots(text: str) -> list[str]:
    """``"9:00,21:00"`` → ``["09:00", "21:00"]``，非法项直接丢掉。"""
    slots: list[str] = []
    for chunk in (text or "").replace("，", ",").replace(" ", ",").split(","):
        raw = chunk.strip()
        if not raw:
            continue
        if ":" not in raw and "." in raw:
            raw = raw.replace(".", ":")
        head, _, minute = raw.partition(":")
        try:
            hour = int(head)
            mins = int(minute or 0)
        except ValueError:
            continue
        if 0 <= hour <= 23 and 0 <= mins <= 59:
            slot = f"{hour:02d}:{mins:02d}"
            if slot not in slots:
                slots.append(slot)
    return slots


@register(
    PLUGIN_NAME,
    "204343414",
    "每日梗图：随机梗图 + 投稿审核 + 英图中文化 + 每日推送",
    "0.5.0",
    "https://github.com/204343414/astrbot-plugin-meme",
)
class MemePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config or {}
        self.data_dir = self._resolve_data_dir()
        self.store = MemeStore(
            os.path.join(self.data_dir, "meme.db"),
            os.path.join(self.data_dir, "media"),
        )
        # Provider 是**插件加载之后**才实例化的（core_lifecycle：先
        # plugin_manager.reload()，再 provider_manager.initialize()）。所以在
        # initialize() 里去解析 provider 一定拿到 None，必须等第一次真正用。
        self._pipeline: MemePipeline | None = None
        self._cooldowns: dict[tuple[str, str], float] = {}
        self._group_cooldowns: dict[str, float] = {}
        self._hub = None
        self._scheduler = None
        self._web = None
        self._rng = random.Random()

    # --- 启动 / 收尾 --------------------------------------------------------

    async def initialize(self) -> None:
        self.store.init()
        self._ensure_scheduler()
        self._reschedule_daily()
        self._register_web()
        self._register_actions()
        logger.info(
            "[Meme] 就绪：数据目录 %s，中文化 %s，审核 Provider=%s（首次使用时解析："
            "AstrBot 是先加载插件、后加载 Provider 的）",
            self.data_dir,
            self.config.get("caption_zh_mode", MODE_APPEND),
            self.cfg.get("moderation_provider_id", "") or "未配（投稿会被拒收）",
        )

    def _register_web(self) -> None:
        """挂上后台页面用的接口（失败了也只是页面不可用，命令照常）。"""
        try:
            from .web import MemeWebUI
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 页面接口不可用：%s", exc)
            return
        if getattr(self, "_web", None) is not None:
            return
        self._web = MemeWebUI(self.context, self)
        self._web.register()

    async def terminate(self) -> None:
        if self._scheduler is not None:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                logger.warning("[Meme] 关闭定时器时出错", exc_info=True)
            self._scheduler = None
        self._unregister_actions()
        self._cooldowns.clear()

    def _resolve_data_dir(self) -> str:
        override = str(self.config.get("custom_data_dir", "") or "").strip()
        if override:
            path = os.path.abspath(override)
        else:
            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                base = get_astrbot_data_path()
            except Exception:  # noqa: BLE001 - 老版本没有这个工具函数
                base = os.path.expanduser("~/.astrbot/data")
            path = os.path.join(base, "plugin_data", PLUGIN_NAME)
        os.makedirs(path, exist_ok=True)
        return path

    @property
    def pipeline(self) -> MemePipeline:
        """首次使用时才真正建管线（那时 Provider 已经加载好了）。

        解析失败时**不缓存**：配置里明明写了 Provider 却没拿到，多半只是
        还没加载完（AstrBot 先插件后 Provider），下次进来再试一次就好过
        一直拿着一份「审核缺失」的管线用到重启。
        """
        if self._pipeline is None or self._needs_rebuild():
            self._build_pipeline()
        return self._pipeline

    def _needs_rebuild(self) -> bool:
        pipeline = self._pipeline
        if pipeline is None:
            return True
        if str(self.cfg.get("moderation_provider_id", "") or "").strip():
            if pipeline.moderator is None:
                return True
        if str(self.cfg.get("caption_zh_mode", "") or "") == MODE_REDRAW:
            if str(self.cfg.get("redraw_provider_id", "") or "").strip():
                if pipeline.redrawer is None:
                    return True
        return False

    @pipeline.setter
    def pipeline(self, value) -> None:
        self._pipeline = value

    # --- 配置 → 组件 --------------------------------------------------------

    @property
    def cfg(self) -> dict:
        return self.config or {}

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    def _provider(self, provider_id: str):
        provider_id = str(provider_id or "").strip()
        if not provider_id:
            return None
        try:
            return self.context.get_provider_by_id(provider_id)
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 取 Provider %s 失败", provider_id, exc_info=True)
            return None

    def _build_pipeline(self) -> None:
        policy = ReviewPolicy.from_config(self.cfg)
        vision = self._provider(self.cfg.get("moderation_provider_id", ""))
        if vision is None:
            # 没单独配审核模型就退到翻译那个；两者都没有就不审（会按 fail_closed 拒收）
            vision = self._provider(self.cfg.get("translate_provider_id", ""))
        moderator = (
            ProviderVision(
                vision,
                build_review_prompt(
                    policy.reject_flags, policy.nsfw_max_level, policy.ban_enabled
                ),
            )
            if vision is not None
            else None
        )
        translator_provider = self._provider(
            self.cfg.get("translate_provider_id", "")
        ) or vision
        translator = ProviderTranslator(translator_provider) if translator_provider else None

        redrawer = None
        caption_mode = str(self.cfg.get("caption_zh_mode", MODE_APPEND) or MODE_APPEND)
        if caption_mode == MODE_REDRAW:
            provider = self._provider(self.cfg.get("redraw_provider_id", ""))
            if provider is not None:
                creds = resolve_provider_credentials(
                    provider, self.cfg.get("redraw_model", "")
                )
                redrawer = OpenAIImageGenerator(
                    creds,
                    size=str(self.cfg.get("redraw_size", "") or "auto"),
                    quality=str(self.cfg.get("redraw_quality", "") or ""),
                    timeout=self._cfg_float("redraw_timeout_seconds", 120.0),
                    proxy=self._proxy(),
                )
            else:
                logger.warning("[Meme] 中文化选了 redraw 但没选画图 Provider，将只做字幕翻译")

        configured = str(self.cfg.get("moderation_provider_id", "") or "").strip()
        if configured and vision is None:
            logger.warning(
                "[Meme] 审核 Provider「%s」没找到：检查 ID 是否写错、或该提供商是否已启用。"
                "当前按 fail-closed 拒收所有投稿。",
                configured,
            )
        self._pipeline = MemePipeline(
            self.store,
            policy,
            moderator=moderator,
            translator=translator,
            redrawer=redrawer,
            caption_mode=caption_mode,
            max_bytes=int(self._cfg_float("max_image_mb", 8.0) * 1024 * 1024),
            redraw_recheck=bool(self.cfg.get("redraw_recheck", True)),
            caption_limit=self._cfg_int("caption_limit", 160),
        )

    def _weights(self) -> dict[str, float]:
        return {
            "submitted": self._cfg_float("weight_submitted", 3.0),
            "api": self._cfg_float("weight_api", 0.5),
            "generated": self._cfg_float("weight_generated", 0.0),
        }

    # --- Hub 桥 -------------------------------------------------------------

    def _get_hub(self, quiet: bool = False):
        if self._hub is not None:
            return self._hub
        try:
            star = self.context.get_registered_star(HUB_NAME)
        except Exception:  # noqa: BLE001
            star = None
        if star is None or not getattr(star, "activated", True):
            if not quiet:
                logger.info("[Meme] 未发现 QQ Official Hub，改用 AstrBot 原生发图")
            return None
        hub = getattr(star, "star_cls", None)
        if hub is None or not hasattr(hub, "send_image_message"):
            if not quiet:
                logger.info("[Meme] Hub 版本不支持图片消息，改用 AstrBot 原生发图")
            return None
        self._hub = hub
        return hub

    @staticmethod
    def _hub_module(hub: Any, name: str):
        """不硬编码包名地 import Hub 的子模块。

        AstrBot 把插件当成 ``data.plugins.<目录名>.main`` 导入，目录名在
        git clone 和 zip 安装之间会不一样，所以从 Hub 类自己的 ``__module__``
        推出来。同一招在棋类插件里已经用了两版。
        """
        module_name = type(hub).__module__
        package = module_name.rsplit(".", 1)[0]
        return importlib.import_module(f"{package}.qqofficial_hub.{name}")

    def _hub_image_file_type(self, hub) -> int:
        try:
            value = getattr(self._hub_module(hub, "passive_reply"), "IMAGE_FILE_TYPE", None)
            return int(value) if value else QQ_FILE_TYPE_IMAGE
        except Exception:  # noqa: BLE001
            return QQ_FILE_TYPE_IMAGE

    # --- 卡片（按钮）--------------------------------------------------------

    def _register_actions(self) -> None:
        """把卡片按钮的回调注册到 Hub。

        Hub 只提供机制：按钮点下去是一张一次性卡片的事件，真正的"再来一张"
        由我们的回调执行。没装 Hub / 是别的适配器时静默跳过 —— 卡片本来也
        只有 QQ 官方号能用，其它平台走纯文本。
        """
        hub = self._get_hub(quiet=True)
        if hub is None or not hasattr(hub, "actions"):
            logger.info("[Meme] 没有 QQ Official Hub，按钮卡片不可用（纯文本照常）")
            return
        try:
            ActionSpec = self._hub_module(hub, "action_registry").ActionSpec
            hub.actions.register(ActionSpec(
                action_id=ACTION_AGAIN,
                title="梗图：再来一张",
                description="由梗图卡片上的「再来一张」按钮触发。",
                owner=PLUGIN_NAME,
                default_permission="everyone",
                callback=self._act_again,
            ))
            hub.actions.register(ActionSpec(
                action_id=ACTION_HOWTO,
                title="梗图：我要投稿",
                description="由梗图卡片上的「我要投稿」按钮触发，回一张投稿说明卡。",
                owner=PLUGIN_NAME,
                default_permission="everyone",
                callback=self._act_howto,
            ))
            hub.actions.register(ActionSpec(
                action_id=ACTION_AUTO_SUB,
                title="梗图：自动订阅",
                description="由卡片上的「自动订阅」触发，回一张低/中/高三档选择卡。",
                owner=PLUGIN_NAME,
                default_permission="everyone",
                callback=self._act_auto_sub,
            ))
            for action_id, level in ACTION_AUTO_LEVELS.items():
                hub.actions.register(ActionSpec(
                    action_id=action_id,
                    title=f"梗图：自动推送{LEVEL_LABELS.get(level, level)}档",
                    description="由三档选择卡触发，把本群设成该档的每小时概率。",
                    owner=PLUGIN_NAME,
                    default_permission="everyone",
                    callback=self._act_auto_set(level),
                ))
            hub.actions.register(ActionSpec(
                action_id=ACTION_AUTO_OFF,
                title="梗图：取消自动推送",
                description="由卡片上的「取消自动」触发。服务端校验群管理/管理员。",
                owner=PLUGIN_NAME,
                default_permission="everyone",
                callback=self._act_auto_off,
            ))
            logger.info(
                "[Meme] 已注册 %s 个卡片按钮动作（再来一张、我要投稿、自动订阅/三档/取消）",
                6,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 注册卡片按钮动作失败（卡片会退化成无按钮）", exc_info=True)

    def _unregister_actions(self) -> None:
        hub = getattr(self, "_hub", None)
        if hub is None or not hasattr(hub, "actions"):
            return
        try:
            hub.actions.unregister_owner(PLUGIN_NAME)
        except Exception:  # noqa: BLE001
            logger.debug("[Meme] 注销卡片动作失败", exc_info=True)

    def _card_enabled(self) -> bool:
        return bool(self.cfg.get("card_enabled", True))

    async def _act_again(self, context, params) -> int:
        """点「再来一张」：和打 /meme 走同一条路（同样吃冷却、同样记账）。

        冷却算在**点按钮的人**头上，所以同一个人连点会被劝退，别人点不受影响。
        """
        origin = str(getattr(context, "origin", "") or "")
        sender = str(getattr(context, "member_openid", "") or "")
        wait = self._cooldown_left(origin, sender) or self._group_cooldown_left(origin)
        if wait > 0:
            # ACK 4 = 无权限：群里会看到一句「暂时不能点」的提示，而不是按钮没反应
            return 4
        item = await asyncio.to_thread(self.pipeline.pick, self._weights())
        if item is None:
            return 3
        self._touch_cooldowns(origin, sender)
        await asyncio.to_thread(self.store.mark_used, item.id)
        try:
            await self._send_item_by_origin(origin, item, context=context)
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 按钮「再来一张」发送失败", exc_info=True)
            return 1
        return 0

    async def _act_howto(self, context, params) -> int:
        """点「我要投稿」：回一张说明卡，告诉对方怎么投、投了谁会看到。"""
        hub = self._get_hub(quiet=True)
        if hub is None:
            return 1
        try:
            await self._send_hub_card(
                context.origin,
                build_howto_card(show_again=self._show_again()),
                context=context,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 按钮「我要投稿」失败", exc_info=True)
            return 1
        return 0

    # --- 自动搬石（每小时概率推） -------------------------------------------

    def _auto_pcts(self) -> dict[str, int]:
        """三档概率，每次现读配置 —— 面板改完不用重开订阅就生效。"""
        result: dict[str, int] = {}
        for level, default in AUTO_PCT_DEFAULTS.items():
            value = self._cfg_int(f"auto_push_{level}_pct", default)
            result[level] = max(0, min(100, value))
        return result

    def _auto_pct(self, level: str) -> int:
        return int(self._auto_pcts().get(str(level or ""), 0))

    def _auto_enabled(self) -> bool:
        return bool(self.cfg.get("auto_push_enabled", True))

    def _auto_level(self, umo: str) -> str:
        state = self.store.group_state(umo) or {}
        return str(state.get("auto_level") or "")

    def _auto_sent_today(self, umo: str) -> int:
        state = self.store.group_state(umo) or {}
        if str(state.get("auto_sent_date") or "") != _now_day_key(time.time()):
            return 0
        return int(state.get("auto_sent_count") or 0)

    def _auto_quiet_range(self) -> tuple[int, int]:
        """``"23:00-08:00"`` → ``(1380, 480)``（跨零点）。空串 = 不设免打扰。"""
        raw = str(
            self.cfg.get("auto_push_quiet", AUTO_QUIET_DEFAULT) or ""
        ).strip()
        if not raw:
            return (0, 0)
        for sep in ("-", "~", "–", "至"):
            if sep in raw:
                left, _, right = raw.partition(sep)
                start = _parse_slots(left.strip())
                end = _parse_slots(right.strip())
                if start and end:
                    sh, sm = (int(x) for x in start[0].split(":"))
                    eh, em = (int(x) for x in end[0].split(":"))
                    return (sh * 60 + sm, eh * 60 + em)
                break
        return (0, 0)

    def _in_auto_quiet(self, moment: dt.datetime) -> bool:
        start, end = self._auto_quiet_range()
        if start == end:
            return False
        now_minutes = moment.hour * 60 + moment.minute
        if start < end:
            return start <= now_minutes < end
        return now_minutes >= start or now_minutes < end

    def _auto_note_for(self, umo: str) -> str:
        """卡片上那行提示：只有订阅中的群才有。"""
        return AUTO_NOTE if self._auto_level(umo) else ""

    #: 每个群「本小时抽骰子的分钟」。进程内内存即可：重启后重新随机，
    #: 反正是概率游戏，谁在哪一分钟被抽到不影响公平性。
    def _auto_fire_slots(self) -> dict[str, tuple[str, int]]:
        slots = getattr(self, "_auto_slots", None)
        if slots is None:
            slots = {}
            self._auto_slots = slots
        return slots

    def _auto_now_minute_matches(self, umo: str, now: dt.datetime) -> bool:
        """这个群本小时的抽签时间到了吗（整点后的随机分钟）。"""
        slots = self._auto_fire_slots()
        hour_key = now.strftime("%Y%m%d%H")
        previous = slots.get(umo)
        if previous is None or previous[0] != hour_key:
            slots[umo] = (hour_key, self._rng.randint(0, 59))
            previous = slots[umo]
        return now.minute == previous[1]

    async def _act_auto_sub(self, context, params) -> int:
        """点「自动订阅」：回一张三档选择卡。"""
        hub = self._get_hub(quiet=True)
        if hub is None:
            return 1
        try:
            await self._send_hub_card(
                context.origin,
                build_auto_pick_card(self._auto_pcts()),
                context=context,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 按钮「自动订阅」失败", exc_info=True)
            return 1
        return 0

    def _act_auto_set(self, level: str):
        """生成「选某一档」的回调。三档各一个 action id：卡片按钮塞不了参数。"""

        async def _callback(context, params) -> int:
            origin = str(getattr(context, "origin", "") or "")
            if not origin:
                return 1
            await asyncio.to_thread(self.store.touch_group, origin, "")
            await asyncio.to_thread(self.store.set_auto_level, origin, level)
            self._auto_fire_slots().pop(origin, None)  # 换档后重新抽签
            self._reschedule_auto()
            hub = self._get_hub(quiet=True)
            pct = self._auto_pct(level)
            logger.info("[Meme] %s 开启自动推送：%s（%s%%/小时）", origin, level, pct)
            if hub is None:
                # 档位已经写进库了，回执发不出来不算失败（下次点按钮会看到状态变了）
                return 0
            try:
                await self._send_hub_card(
                    origin,
                    build_auto_done_card(
                        level,
                        pct,
                        show_again=self._show_again(),
                        show_howto=self._show_howto(),
                    ),
                    context=context,
                )
            except Exception:  # noqa: BLE001
                logger.warning("[Meme] 自动推送回执卡发送失败", exc_info=True)
                return 1
            return 0

        return _callback

    async def _is_group_manager(self, origin: str, openid: str) -> bool:
        """这个人能不能关自动推送。

        三条路，任意一条通过就放行：
          1. 配置里的 ``extra_admins``（openid 白名单）—— 我自己的号走这条；
          2. Hub 的 ``identity`` 账本里记着他是群主/群管（角色只能从消息里学到）；
          3. QQ 那边压根查不到角色信息时的**保守拒绝**（宁可错杀）。
        注意按钮点击事件只带 openid、不带角色，所以从没在群里说过话的管理员
        可能被拒 —— 拒的时候会告诉他用 ``/meme 自动 关`` 或找机器人主人。
        """
        openid = str(openid or "").strip()
        if not openid:
            return False
        admins = {str(a) for a in (self.cfg.get("extra_admins") or []) if str(a).strip()}
        try:
            global_admins = self.context.get_config().get("admins_id", []) or []
            admins |= {str(a) for a in global_admins if str(a).strip()}
        except Exception:  # noqa: BLE001
            pass
        if openid in admins:
            return True
        hub = self._get_hub(quiet=True)
        if hub is None:
            return False
        checker = getattr(hub, "is_group_manager", None)
        if checker is None:
            identity = getattr(hub, "identity", None)
            checker = getattr(identity, "is_group_manager", None)
        if checker is None:
            return False
        try:
            return bool(await checker(origin, openid))
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 查群管理身份失败", exc_info=True)
            return False

    async def _act_auto_off(self, context, params) -> int:
        """点「取消自动」。必须管理员（用户明确要求）。"""
        origin = str(getattr(context, "origin", "") or "")
        openid = str(getattr(context, "member_openid", "") or "")
        if not origin:
            return 1
        if not await self._is_group_manager(origin, openid):
            # ACK 4 = 无权限：客户端会弹一句提示，不是「按钮没反应」
            return 4
        await asyncio.to_thread(self.store.set_auto_level, origin, "")
        self._auto_fire_slots().pop(origin, None)
        self._reschedule_auto()
        logger.info("[Meme] %s 关闭自动推送（由 %s 操作）", origin, openid)
        hub = self._get_hub(quiet=True)
        if hub is None:
            return 0
        try:
            await self._send_hub_card(
                origin,
                build_auto_done_card(
                    off=True, show_again=self._show_again(), show_howto=self._show_howto()
                ),
                context=context,
            )
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 退订回执卡发送失败", exc_info=True)
        return 0

    def _reschedule_auto(self) -> None:
        """挂/更新「每分钟看一眼」的定时任务。

        为什么不是「每小时一个 cron」：抽签分钟每小时都重新随机（用户明确要求
        「整点后随机分钟」），用一个每分钟跑的轻任务比每小时重建 job 简单，
        也不会在改配置时留下重复的 job。任务里绝大多数次是空转，成本可以忽略。
        """
        if self._scheduler is None:
            return
        try:
            if self._scheduler.get_job("meme:auto") is not None:
                return
            self._scheduler.add_job(
                self._auto_tick,
                "cron",
                minute="*",
                id="meme:auto",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info("[Meme] 已挂上自动推送的每分钟抽签任务")
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 挂自动推送任务失败", exc_info=True)

    async def _auto_tick(self) -> None:
        """每分钟跑一次：看看有哪个群这一分钟该抽签了。"""
        if not self._auto_enabled():
            return
        try:
            groups = await asyncio.to_thread(self.store.auto_groups)
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 读自动推送群列表失败", exc_info=True)
            return
        if not groups:
            return
        now_dt = dt.datetime.now()
        now = now_dt.timestamp()
        day_key = _now_day_key(now)
        max_per_day = self._cfg_int("auto_push_max_per_day", AUTO_MAX_PER_DAY_DEFAULT)
        quiet = self._in_auto_quiet(now_dt)
        for record in groups:
            umo = str(record.get("umo") or "")
            level = str(record.get("auto_level") or "")
            if not umo or level not in AUTO_PCT_DEFAULTS:
                continue
            if not self._auto_now_minute_matches(umo, now_dt):
                continue
            if quiet:
                continue
            if float(record.get("blocked_until") or 0) > now:
                continue
            sent_today = (
                int(record.get("auto_sent_count") or 0)
                if str(record.get("auto_sent_date") or "") == day_key
                else 0
            )
            if max_per_day and sent_today >= max_per_day:
                continue
            pct = self._auto_pct(level)
            if pct <= 0 or self._rng.randint(1, 100) > pct:
                continue  # 抽不中：绝大多数小时的结局
            await self._auto_push_one(umo, day_key=day_key)

    async def _auto_push_one(self, umo: str, *, day_key: str) -> None:
        """真的给一个群自动发一条（复用 /meme 的整套发送链路）。"""
        item = await asyncio.to_thread(self.pipeline.pick, self._weights())
        if item is None:
            logger.info("[Meme] %s 自动推送跳过：库里没图", umo)
            return
        await asyncio.to_thread(self.store.mark_used, item.id)
        try:
            await self._send_item_by_origin(umo, item, auto=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] %s 自动推送失败：%s", umo, exc)
            text = str(exc)
            if "40034105" in text or "无权限" in text or "权限" in text:
                await asyncio.to_thread(
                    self.store.note_push,
                    umo,
                    ok=False,
                    day_key=day_key,
                    blocked_until=time.time() + PUSH_BLOCK_SECONDS,
                )
                logger.warning(
                    "[Meme] %s 不允许主动消息（群主没开「机器人主动发言」），24 小时内不再自动推",
                    umo,
                )
            return
        count = await asyncio.to_thread(self.store.note_auto_push, umo, day_key=day_key)
        logger.info("[Meme] %s 自动推送 #%s（今天第 %s 条）", umo, item.id, count)

    def _auto_button(self, origin: str) -> str:
        """卡片底部的自动推送按钮：没订阅=「自动订阅」，订阅中=「取消自动」。"""
        if not bool(self.cfg.get("card_show_auto", True)):
            return ""
        return "off" if self._auto_level(origin) else "sub"

    def _show_again(self) -> bool:
        return bool(self.cfg.get("card_show_again", True))

    def _show_howto(self) -> bool:
        return bool(self.cfg.get("card_show_howto", True))

    async def _send_hub_card(
        self, origin: str, card: dict, *, context=None, msg_id: str = ""
    ) -> None:
        """发一张卡片。

        必须做成**被动回复**，否则就是群主动消息 —— QQ 对主动消息的额度极小，
        而且群里看不到「是谁触发的」。两条被动通道：
          * 按钮点击 → ``event_id``（INTERACTION_CREATE 带的那个）；
          * 群友发命令 → ``msg_id``（就当是回复他这条消息）。
        """
        hub = self._get_hub()
        if hub is None:
            raise RuntimeError("QQ Official Hub 不可用")
        kwargs: dict[str, Any] = {"client": getattr(context, "client", None)}
        if msg_id:
            kwargs["msg_id"] = msg_id
        if context is not None:
            try:
                passive = self._hub_module(hub, "passive_reply").passive_event_id
                kwargs["event_id"] = passive(context.interaction)
            except Exception:  # noqa: BLE001
                kwargs["event_id"] = None
            kwargs["initiator_openid"] = getattr(context, "member_openid", "") or ""
        # 卡片里的图就是内容本身：转存失败时宁可明确报错再降级，也不要发裂图
        if "verify_image" in inspect.signature(hub.send_ephemeral_card).parameters:
            kwargs["verify_image"] = bool(card.get("markdown", "").count("![梗图"))
        await hub.send_ephemeral_card(origin, card, **kwargs)

    async def _release_material(self, hub, image_id: str, *, origin: str = "") -> None:
        """删掉一张素材。

        失败不影响发图，但**会**影响以后：素材攒到 100 张（每机器人上限）就再也传不
        上去，那时卡片会退到图床 URL。所以这里失败要留一行 INFO，并提示版本 ——
        Hub 0.24.0 的删除路径少了 ``/delete``，平台回 11001「不支持的调用」，
        症状是"素材明明能传却删不掉"，很难自己看出来。
        """
        cleaner = getattr(hub, "delete_image_material", None)
        if cleaner is None or not image_id:
            return
        try:
            removed = await cleaner(image_id, origin=origin)
            if removed is False:
                logger.info(
                    "[Meme] 素材 %s 没能删掉。如果你用的是 QQ Official Hub 0.24.0，"
                    "它的删除路径少了 /delete（平台回 11001），升级到 0.24.1 即可。",
                    image_id,
                )
        except Exception:  # noqa: BLE001
            logger.debug("[Meme] 回收素材出错 %s", image_id, exc_info=True)
        finally:
            await asyncio.to_thread(self.store.drop_card_material, image_id)

    async def _trim_materials(self, hub, *, origin: str = "") -> None:
        """按配置裁剪素材数量，最久没用过的先走。

        注意：删素材**不影响已经发出去的消息**（平台发消息时已把图转存了一份），
        所以这里回收只关乎"还能继续发多少新卡片"。QQ 上限 100 张，超了是
        **新的上传被拒**（40035007），不会挤掉旧的。
        """
        keep = self._cfg_int("card_material_keep", CARD_MATERIAL_KEEP)
        keep = max(5, min(keep, CARD_MATERIAL_KEEP_MAX))
        ids = await asyncio.to_thread(self.store.card_material_ids)
        for stale in ids[: max(0, len(ids) - keep)]:
            await self._release_material(hub, stale, origin=origin)

    async def _send_card_item(
        self,
        origin: str,
        item: MemeItem,
        data: bytes,
        *,
        context=None,
        msg_id: str = "",
        auto_note: str = "",
    ) -> bool:
        """发一张带按钮的梗图卡片。成功返回 True，任何一环失败返回 False。

        来源优先级（每一步失败就往下走，全部失败才交给调用方发纯图片）：
          1. QQ 素材库（``qqbot-media://``）—— 不依赖公网地址，首选；
          2. Hub 图床的公网 URL —— 老路子，素材库不可用时兜底。
        """
        hub = self._get_hub(quiet=True)
        if hub is None or not hasattr(hub, "send_ephemeral_card"):
            return False
        width, height = _image_size(data)
        caption_limit = self._cfg_int("caption_limit", 160)

        async def try_card(url: str) -> bool:
            card = build_meme_card(
                item,
                url,
                caption_limit=caption_limit,
                show_again=self._show_again(),
                show_howto=self._show_howto(),
                auto=self._auto_button(origin),
                width=width,
                height=height,
                auto_note=auto_note,
            )
            try:
                await self._send_hub_card(origin, card, context=context, msg_id=msg_id)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Meme] 带图卡片被拒（%s）：%s", url.split("?")[0], exc)
                return False

        # ① 这张图传过吗？传过就直接用旧的引用 —— 一张图只占一格额度，
        #    90 天不用的会被平台回收（40035009），那时这张卡会发失败，
        #    下面会自动退回去重传。
        sha256 = ""
        try:
            from .meme.media import sha256_of

            sha256 = sha256_of(data)
        except Exception:  # noqa: BLE001
            sha256 = ""
        cached = await asyncio.to_thread(self.store.material_for_sha, sha256) if sha256 else None
        if cached and str(cached.get("url") or ""):
            age = self._cfg_int("card_material_reuse_days", 80) * 86400
            fresh = (time.time() - float(cached.get("last_used") or 0)) < age
            if fresh and await try_card(str(cached["url"])):
                await asyncio.to_thread(self.store.touch_material, cached["image_id"])
                return True
            # 过期或已被平台删掉：把这条记账清掉，走重新上传
            logger.info("[Meme] 素材 #%s 已失效（或超过复用期），重新上传", cached.get("image_id"))
            await asyncio.to_thread(self.store.drop_material_by_sha, sha256)

        # ② 新上传：素材库优先，图床兜底
        candidates: list[tuple[str, str]] = []  # (图片地址, 素材 id)
        upload = getattr(hub, "publish_image_material", None)
        if upload is not None:
            try:
                material = await upload(data, origin=origin)
                if material.get("url"):
                    candidates.append((str(material["url"]), str(material.get("image_id") or "")))
            except Exception as exc:  # noqa: BLE001
                logger.info("[Meme] 素材库上传失败，改试图床：%s", exc)
        if not candidates:
            publisher = getattr(hub, "publish_image_checked", None)
            if publisher is not None:
                try:
                    url = await publisher(data, slot=f"meme:{origin}")
                    if url:
                        candidates.append((str(url), ""))
                except Exception as exc:  # noqa: BLE001
                    logger.info("[Meme] 图床也不可用：%s", exc)
        if not candidates:
            return False

        for url, image_id in candidates:
            if not await try_card(url):
                if image_id:
                    await self._release_material(hub, image_id, origin=origin)
                continue
            if image_id:
                await asyncio.to_thread(
                    self.store.add_card_material,
                    image_id,
                    meme_id=item.id,
                    origin=origin,
                    sha256=sha256,
                    url=url,
                )
                await self._trim_materials(hub, origin=origin)
            return True
        return False

    # --- 命令入口 -----------------------------------------------------------

    @filter.command("meme", alias={"梗图", "来张梗图"})
    async def meme_entry(self, event: AstrMessageEvent):
        """随机来一张梗图，或投稿/查库/管理。"""
        args = self._args(event)
        sub, _, rest = args.partition(" ")
        sub = sub.strip()
        rest = rest.strip()
        images = self._event_images(event)

        if sub in ("帮助", "help", "?", "？"):
            yield event.plain_result(HELP_TEXT)
            return

        if sub in ("库", "库存", "stats"):
            async for result in self._do_stats(event):
                yield result
            return

        if sub in ("投稿", "submit"):
            # 唯一的投稿入口。图库只有一份，不再分「本群」「公共」两种去处，
            # 所以「/meme 收」那种老写法一起删掉了，免得两个命令行为还要解释。
            async for result in self._do_submit(event, images):
                yield result
            return

        if images and not sub:
            # 带了图却没写子命令：不静默入库（那会让人以为「发图就自动收」），
            # 只回一句怎么写，省掉一轮「为什么没反应」。
            yield event.plain_result(
                "要投稿请打「/meme 投稿」（先把图引用上、@我）。"
            )
            return

        if sub in ("删", "下架", "del", "delete"):
            yield await self._do_delete(event, rest)
            return

        if sub in ("审核", "审", "review"):
            yield await self._do_review(event, rest)
            return

        if sub in ("自动", "自动搬石", "auto"):
            async for result in self._do_auto(event, rest):
                yield result
            return

        if sub in ("每日", "推送", "daily"):
            async for result in self._do_daily(event, rest):
                yield result
            return

        if sub in ("黑名单", "黑名单列表", "bans"):
            yield await self._do_blacklist(event)
            return

        if sub in ("解禁", "解封", "unban"):
            yield await self._do_unban(event, rest)
            return

        if sub in ("拉", "fetch"):
            async for result in self._do_fetch_api(event):
                yield result
            return

        if sub in ("测试", "test"):
            async for result in self._do_selftest(event, images):
                yield result
            return

        if sub:
            # 不认识的子命令（含「再来」「再来一张」这种给不能点按钮的客户端的
            # 同义写法）：当作闲聊输入，直接发一张，别骂用户
            pass

        result = await self._do_random(event)
        if result is not None:
            yield result

    # --- 发一张 -------------------------------------------------------------

    async def _do_random(self, event: AstrMessageEvent):
        """挑一张发出去。已经通过 Hub 发过就返回 None。"""
        umo = event.unified_msg_origin
        group_id = event.get_group_id() or ""
        sender = str(event.get_sender_id() or "")
        await asyncio.to_thread(self.store.touch_group, umo, group_id)

        wait = self._cooldown_left(umo, sender)
        if wait > 0:
            return event.plain_result(f"手速有点快，{wait:.0f} 秒后再来一张")
        group_wait = self._group_cooldown_left(umo)
        if group_wait > 0:
            return event.plain_result(f"这个群刚发过，{group_wait:.0f} 秒后再点")

        item = await asyncio.to_thread(self.pipeline.pick, self._weights())
        if item is None and self._api_configured():
            item = await self._fetch_api_meme()
        if item is None:
            return event.plain_result(
                "库里还空着。引用一张图、@我，打「/meme 投稿」就能入库；"
                "管理员也可以用「/meme 拉」从第三方接口拉几张。"
            )

        self._touch_cooldowns(umo, sender)
        await asyncio.to_thread(self.store.mark_used, item.id)
        return await self._send_item(event, item)

    async def _send_item(self, event: AstrMessageEvent, item: MemeItem):
        return await self._send_item_by_origin(event.unified_msg_origin, item, event=event)

    async def _send_item_by_origin(
        self, origin: str, item: MemeItem, *, event=None, context=None, auto: bool = False
    ):
        """把一张图发出去：优先卡片（带按钮），否则按顺序降级。

        降级链（每一级失败只写一行日志，群里绝不见报错）：
          ① 卡片 + 素材库/图床嵌图 + 两个按钮
          ② 图片消息 + 紧跟一张无图卡片（图放不进去，但按钮还在）
          ③ 纯图片消息（现状）
          ④ 纯文字（连图都读不出来时的最后兜底）
        """
        caption = compose_caption(
            item.caption, item.caption_zh, self._cfg_int("caption_limit", 160)
        )
        try:
            data = await asyncio.to_thread(self._read_file, item.file_path)
        except OSError as exc:
            logger.warning("[Meme] 读图失败 #%s: %s", item.id, exc)
            data = b""

        auto_note = AUTO_NOTE if auto else ""
        msg_id = ""
        if event is not None:
            msg_id = str(
                getattr(getattr(event, "message_obj", None), "message_id", "") or ""
            )

        if data and self._card_enabled():
            try:
                if await self._send_card_item(
                    origin, item, data, context=context, msg_id=msg_id, auto_note=auto_note
                ):
                    return True if auto else None
            except Exception:  # noqa: BLE001
                logger.warning("[Meme] 卡片发送失败，降级", exc_info=True)
            # ② 图 + 无图卡片（只有 Hub 在才做得到，它同时负责发图）
            try:
                if self._get_hub(quiet=True) is None:
                    raise RuntimeError("没有 Hub")
                await self._send_image_now(origin, data, caption)
                await self._send_hub_card(
                    origin,
                    build_meme_card(
                        item, "", caption_limit=self._cfg_int("caption_limit", 160),
                        show_again=self._show_again(), show_howto=self._show_howto(),
                        auto=self._auto_button(origin),
                        auto_note=auto_note,
                        note="<font color='grey'>（这张图走的是降级发送，按钮照常可用）</font>",
                    ),
                    context=context,
                    msg_id=msg_id,
                )
                return True if auto else None
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Meme] 图+无图卡片失败：%s", exc)

        if data:
            result = await self._send_image_now(origin, data, caption, item=item, event=event)
            return True if (auto and result is None) else result
        if auto:
            # 自动推送没有 event 可以退：这里必须抛，让上层记失败原因（尤其 40034105）
            raise RuntimeError(f"图读不出来或没有可用通道（#{item.id}）")
        return event.plain_result(
            f"这张图现在发不出来（#{item.id}）" + (f"\n{caption}" if caption else "")
        )

    async def _send_image_now(
        self, origin: str, data: bytes, caption: str, *, item=None, event=None
    ):
        """一张纯图片消息。

        先走 Hub（QQ 官方号上这是唯一稳妥的路），Hub 不行才退 AstrBot 原生。
        原生那条**返回**结果对象让命令层去 yield，不在这里 ``event.send`` ——
        命令 handler 的返回值才是 AstrBot 真正发出去的东西，自己 send 再返回
        空就是"发两次"或"一次都没发"。
        """
        hub = self._get_hub(quiet=True)
        if hub is not None:
            try:
                await hub.send_image_message(origin, data, text=caption)
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Meme] Hub 发图失败，改用原生：%s", exc)
        if event is not None:
            chain: list[Any] = []
            if item is not None and os.path.isfile(item.file_path):
                chain.append(Image.fromFileSystem(item.file_path))
            else:
                chain.append(Image.fromURL("base64://" + base64.b64encode(data).decode()))
            if caption:
                chain.append(Plain(caption))
            return event.chain_result(chain)
        raise RuntimeError("没有可用的图片发送通道")

    # --- 投稿 ---------------------------------------------------------------

    async def _do_submit(self, event: AstrMessageEvent, images: list) -> Any:
        """投稿。图库只有一份，所有群共用，所以没有「只进本群」这一说了。"""
        if not images:
            yield event.plain_result(
                "投稿方式：发图时引用它、@我，然后输入「/meme 投稿」。"
            )
            return
        umo = event.unified_msg_origin
        sender = str(event.get_sender_id() or "")

        # 被拉黑的人连审核都不跑：省一次模型调用，也让他知道拒的是"人"不是"图"
        ban = await asyncio.to_thread(self.store.blacklist_entry, sender)
        if ban is not None:
            logger.info("[Meme] 已拉黑的人尝试投稿：%s（%s）", sender, ban.get("reason", ""))
            yield event.plain_result("你的投稿权限已被关闭，这张不会入库。")
            return

        limit = self._cfg_int("daily_submit_limit", 10)
        used = self._submit_used_today(umo, sender)
        if limit and used >= limit:
            yield event.plain_result(f"今天已经投了 {used} 张，够这个群笑一天了，明天再来")
            return
        if not self._submission_cooldown_ok(umo, sender):
            yield event.plain_result("刚投过，歇 30 秒再投")
            return

        lines: list[str] = []
        accepted = 0
        banned = False
        for component in images[:MAX_IMAGES_PER_SUBMIT]:
            try:
                data = await self._component_bytes(component)
            except Exception as exc:  # noqa: BLE001
                lines.append(f"第 {len(lines) + 1} 张拿不到内容：{exc}")
                continue
            outcome = await self.pipeline.ingest(
                data,
                origin="submitted",
                contributor=sender,
            )
            if outcome.ok and outcome.meme is not None:
                accepted += 1
                tag = "重画成中文版" if outcome.redrawn else "已入库"
                dup = "（早就有了）" if outcome.duplicate else ""
                lines.append(f"#{outcome.meme.id} {tag}{dup}：{outcome.reason}")
            else:
                lines.append(f"没收：{outcome.reason}")
            if outcome.banned:
                banned = await self._ban_submitter(
                    event, sender, outcome.ban_reason or outcome.reason, data
                )
                break  # 已经定性了，这一批里后面的图不用再跑
        if accepted:
            self._note_submit(umo, sender)
        if banned:
            lines.append("投稿人已被永久禁止投稿。")
        yield event.plain_result("\n".join(lines) or "没收到图")

    async def _do_blacklist(self, event: AstrMessageEvent):
        if not self._is_admin(event):
            return event.plain_result("只有管理员能看黑名单")
        entries = await asyncio.to_thread(self.store.list_blacklist, 20)
        if not entries:
            return event.plain_result("还没有人被禁止投稿。")
        lines = [f"禁止投稿的人（{len(entries)} 个）："]
        for entry in entries:
            when = dt.datetime.fromtimestamp(entry.get("created_at") or 0).strftime("%m-%d %H:%M")
            lines.append(
                f"· {entry['sender_id']}（{when}，累计 {entry.get('hits', 1)} 次）："
                f"{entry.get('reason', '')}"
            )
        lines.append("恢复某人：/meme 解禁 <用户ID>")
        return event.plain_result("\n".join(lines))

    async def _do_unban(self, event: AstrMessageEvent, rest: str):
        if not self._is_admin(event):
            return event.plain_result("只有管理员能解禁")
        sender = rest.strip()
        if not sender:
            return event.plain_result("用法：/meme 解禁 <用户ID>（先用 /meme 黑名单 查）")
        removed = await asyncio.to_thread(self.store.remove_blacklist, sender)
        if not removed:
            return event.plain_result(f"{sender} 不在黑名单里")
        return event.plain_result(f"已恢复 {sender} 的投稿权限")

    # --- 拉黑 ---------------------------------------------------------------

    async def _ban_submitter(
        self, event: AstrMessageEvent, sender: str, reason: str, data: bytes
    ) -> bool:
        """执行永久禁止投稿。

        判定由审核模型给出（``Review.ban_submitter``），这里只做三件事：
        管理员豁免、把证据图留一份、写进黑名单。

        **只关掉这个人在本插件的投稿权**，不碰他在群里正常聊天 —— 拉黑的
        初衷是"别再往库里塞能让 bot 封号的东西"，不是替他禁言。
        """
        if not bool(self.cfg.get("ban_malicious_submitter", True)):
            logger.info("[Meme] 模型建议拉黑 %s，但配置里关了这个开关：%s", sender, reason)
            return False
        if self._is_admin(event):
            logger.info("[Meme] %s 是管理员，不拉黑（只拒收这张图）", sender)
            return False
        if not sender:
            return False

        sha = ""
        evidence_path = ""
        try:
            from .meme.media import sha256_of

            sha = sha256_of(data)
            evidence_path = await asyncio.to_thread(self._save_quarantine, sha, data)
        except Exception:  # noqa: BLE001
            logger.debug("[Meme] 留证据图失败", exc_info=True)

        await asyncio.to_thread(
            self.store.add_blacklist,
            sender,
            origin=event.unified_msg_origin,
            group_id=event.get_group_id() or "",
            reason=(reason or "恶意投稿")[:300],
            evidence_sha=sha,
            evidence_path=evidence_path,
        )
        logger.warning(
            "[Meme] 已永久禁止投稿 sender=%s 群=%s 理由=%s 证据=%s",
            sender, event.get_group_id() or "-", reason, evidence_path or "(未存)",
        )
        if bool(self.cfg.get("ban_notify_group", True)):
            try:
                await self.context.send_message(
                    event.unified_msg_origin,
                    self._plain_chain(
                        "这张不收：投稿内容属于故意投递违规图，该投稿人已被永久禁止投稿。"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug("[Meme] 拉黑通知发送失败", exc_info=True)
        return True

    def _save_quarantine(self, sha256: str, data: bytes) -> str:
        """把被判恶意的图另存一份（不进梗图库、不会被抽到）。"""
        folder = os.path.join(self.data_dir, "quarantine")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, (sha256[:16] or "unknown") + ".img")
        if not os.path.isfile(path):
            with open(path, "wb") as fh:
                fh.write(data)
        return path

    def _plain_chain(self, text: str):
        from astrbot.core.message.message_event_result import MessageChain

        return MessageChain().message(text)

    # --- 库 / 删 / 审 -------------------------------------------------------

    async def _do_stats(self, event: AstrMessageEvent) -> Any:
        stats = await asyncio.to_thread(self.store.stats)
        yield event.plain_result(
            format_stats(
                total=stats["total"],
                by_origin=stats["by_origin"],
                active=stats["active"],
                rejected=stats["rejected"],
            )
        )

    async def _do_delete(self, event: AstrMessageEvent, rest: str):
        if not self._is_admin(event):
            return event.plain_result("只有管理员能下架")
        try:
            meme_id = int(rest.strip().lstrip("#"))
        except ValueError:
            return event.plain_result("用法：/meme 删 12")
        item = await asyncio.to_thread(self.store.delete, meme_id)
        if item is None:
            return event.plain_result(f"没有 #{meme_id} 这张")
        try:
            if os.path.isfile(item.file_path):
                os.remove(item.file_path)
        except OSError:
            pass
        return event.plain_result(f"已下架 #{meme_id}")

    async def _do_review(self, event: AstrMessageEvent, rest: str):
        if not self._is_admin(event):
            return event.plain_result("只有管理员能重审")
        try:
            meme_id = int(rest.strip().lstrip("#"))
        except ValueError:
            return event.plain_result("用法：/meme 审核 12")
        item = await asyncio.to_thread(self.store.get, meme_id)
        if item is None:
            return event.plain_result(f"没有 #{meme_id} 这张")
        try:
            data = await asyncio.to_thread(self._read_file, item.file_path)
        except OSError as exc:
            return event.plain_result(f"读图失败：{exc}")
        review = await self.pipeline.review_bytes(data, item.mime)
        from .meme.review import judge

        accepted, why = judge(review, self.pipeline.policy)
        if not accepted:
            await asyncio.to_thread(self.store.set_status, meme_id, "disabled")
        detail = (
            f"#{meme_id} {'通过' if accepted else '不通过'}：{why}\n"
            f"色情等级 {review.nsfw_level}，标签 {list(review.flags) or '无'}，"
            f"语言 {review.language or '无'}，文字「{review.caption_text or '无'}」"
        )
        return event.plain_result(detail)

    # --- 第三方接口 ---------------------------------------------------------

    async def _do_fetch_api(self, event: AstrMessageEvent) -> Any:
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能拉图")
            return
        if not self._api_configured():
            yield event.plain_result("没有配置第三方接口（配置里的 third_party_api_url）")
            return
        item = await self._fetch_api_meme()
        if item is None:
            yield event.plain_result("拉图失败：接口没给图，或者图没过审（细节看日志）")
            return
        yield event.plain_result(f"已入库 #{item.id}：{item.caption or '（无文字）'}")

    def _proxy(self) -> str:
        """出站代理。

        容器里访问外网基本都要经过宿主机那个代理（AstrBot 配置的
        ``http_proxy``）；httpx 默认只看环境变量，而容器里往往一个都没有，
        结果是裸连直接被 ``Network is unreachable`` 打回。插件自己发的请求
        （拉图、下载投稿图、调画图接口）必须显式带上它。
        """
        own = str(self.cfg.get("http_proxy", "") or "").strip()
        if own:
            return own
        try:
            return str(self.context.get_config().get("http_proxy", "") or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _api_configured(self) -> bool:
        return bool(str(self.cfg.get("third_party_api_url", "") or "").strip())

    def _api_source(self) -> JsonApiImageSource:
        return JsonApiImageSource(
            str(self.cfg.get("third_party_api_url", "") or "").strip(),
            str(self.cfg.get("third_party_api_json_path", "") or "").strip(),
            self._cfg_float("third_party_api_timeout_seconds", 30.0),
            proxy=self._proxy(),
        )

    async def _fetch_api_meme(self) -> MemeItem | None:
        """拉一张、过审、入库；整条链都走同一个 pipeline。"""
        try:
            data, source_url = await self._api_source().fetch()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 第三方接口拉图失败：%s", exc)
            return None
        outcome = await self.pipeline.ingest(
            data,
            origin="api",
            source_url=source_url,
        )
        if outcome.ok and outcome.meme is not None:
            return outcome.meme
        logger.info("[Meme] 第三方图没过审：%s", outcome.reason)
        return None

    # --- 自检 ---------------------------------------------------------------

    async def _do_selftest(self, event: AstrMessageEvent, images: list) -> Any:
        """引用一张图跑一遍完整管线，把审核 JSON 原样回给管理员。

        以前没带图时会拿插件自带的内置图顶上；内置图已经全部删掉了，所以现在
        必须自己引用一张 —— 反正测的就是「这张图会不会被收」，用别的图测没有意义。
        """
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能自检")
            return
        if not images:
            yield event.plain_result("自检要先引用一张图：引用它、@我，打「/meme 测试」。")
            return
        try:
            data = await self._component_bytes(images[0])
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拿不到这张图：{exc}")
            return
        if not data:
            yield event.plain_result("这张图读出来是空的。")
            return
        review = await self.pipeline.review_bytes(data)
        from .meme.review import judge

        accepted, why = judge(review, self.pipeline.policy)
        yield event.plain_result(
            "自检结果：\n"
            f"收不收：{'收' if accepted else '不收'}（{why}）\n"
            f"is_meme={review.is_meme} nsfw={review.nsfw_level}\n"
            f"flags={list(review.flags)}\n"
            f"语言={review.language or '无'}\n"
            f"文字={review.caption_text or '无'}\n"
            f"解析成功={review.parsed} {review.error}"
        )

    # --- 每日推送 -----------------------------------------------------------

    async def _do_auto(self, event: AstrMessageEvent, rest: str) -> Any:
        """``/meme 自动``：看状态；``/meme 自动 关``：关掉（管理员）。

        为什么还留一个命令版：卡片上的「取消自动」走的是按钮回调，需要 QQ 认得出
        你是群管（角色只能从群消息里学到）。管理员从没在群里说过话时就点不动，
        这条命令用 AstrBot 的管理员身份兜住，不用等卡片。
        """
        umo = event.unified_msg_origin
        await asyncio.to_thread(self.store.touch_group, umo, event.get_group_id() or "")
        level = self._auto_level(umo)
        pcts = self._auto_pcts()
        if rest.strip() in ("关", "关掉", "off", "关闭", "取消"):
            if not self._is_admin(event):
                yield event.plain_result("只有管理员能关自动推送")
                return
            await asyncio.to_thread(self.store.set_auto_level, umo, "")
            self._auto_fire_slots().pop(umo, None)
            logger.info("[Meme] %s 关闭自动推送（命令，管理员操作）", umo)
            yield event.plain_result("已关闭本群的自动推送。")
            return
        if rest.strip() in ("低", "中", "高"):
            # 开关本身是「所有人都能点」，所以命令版也不拦权限
            picked = {"低": "low", "中": "mid", "高": "high"}[rest.strip()]
            await asyncio.to_thread(self.store.set_auto_level, umo, picked)
            self._auto_fire_slots().pop(umo, None)
            yield event.plain_result(
                f"已开启自动搬石：{LEVEL_LABELS[picked]}（{pcts[picked]}%/小时）"
            )
            return
        if not self._auto_enabled():
            yield event.plain_result(
                "自动推送被插件配置里的总开关关掉了（auto_push_enabled）。"
                "所有群都不会自动发，订阅状态还留着。"
            )
            return
        if not level:
            yield event.plain_result(
                "这个群还没开自动推送。\n"
                f"发一张梗图、点卡片上的「自动订阅」，再选一档：\n"
                f"· 低 {pcts['low']}%/小时　· 中 {pcts['mid']}%/小时　· 高 {pcts['high']}%/小时\n"
                "（也可以直接打 /meme 自动 低）"
            )
            return
        cap = self._cfg_int("auto_push_max_per_day", AUTO_MAX_PER_DAY_DEFAULT)
        sent = self._auto_sent_today(umo)
        start, end = self._auto_quiet_range()
        quiet_text = (
            f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d}"
            if start != end
            else "不设（全天都可能发）"
        )
        yield event.plain_result(
            f"本群自动推送：{LEVEL_LABELS.get(level, level)}（{pcts.get(level, 0)}%/小时）\n"
            f"今天已自动发 {sent} 条" + (f"（上限 {cap} 条）" if cap else "（不设上限）") + "\n"
            f"免打扰：{quiet_text}\n"
            "关掉：卡片上点「取消自动」，或 /meme 自动 关（管理员）"
        )

    async def _do_daily(self, event: AstrMessageEvent, rest: str) -> Any:
        umo = event.unified_msg_origin
        # 先把群号记下来，否则定时任务只知道 umo、不知道去哪儿挑本群的图
        await asyncio.to_thread(self.store.touch_group, umo, event.get_group_id() or "")
        state = await asyncio.to_thread(self.store.group_state, umo) or {}
        if not rest:
            enabled = bool(state.get("daily_enabled"))
            slots = state.get("daily_slots") or ""
            yield event.plain_result(
                "每日推送："
                + ("开着" if enabled else "关着")
                + (f"，时间 {slots}" if slots else "")
                + "\n用「/meme 每日 9:00,21:00」打开，「/meme 每日 关」关闭。"
                + "\n注意：这属于主动消息，需要群主在群设置里允许机器人主动发言。"
            )
            return
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能改每日推送")
            return
        if rest.strip() in ("关", "off", "关闭", "0"):
            await asyncio.to_thread(self.store.set_daily, umo, False, "")
            self._reschedule_daily()
            yield event.plain_result("每日推送已关。")
            return
        slots = _parse_slots(rest)
        if not slots:
            yield event.plain_result("时间没看懂，写成「9:00,21:00」这样。")
            return
        await asyncio.to_thread(self.store.set_daily, umo, True, ",".join(slots))
        self._reschedule_daily()
        yield event.plain_result("每日推送已开：" + "、".join(slots))

    def _ensure_scheduler(self) -> None:
        if self._scheduler is not None:
            return
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 没有 APScheduler，每日推送不可用")
            return
        timezone = str(self.cfg.get("timezone", "") or "").strip()
        self._scheduler = AsyncIOScheduler(timezone=timezone or None)
        self._scheduler.start()

    def _reschedule_daily(self) -> None:
        """把库里的群配置重建成定时任务。

        每次改配置就整体重建：定时任务的状态是最容易被改坏又最难排查的地方，
        与其做增量 diff，不如每次从库里重新生成一遍，代价是一次 SQL。
        """
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_all_jobs()
        except Exception:  # noqa: BLE001
            logger.warning("[Meme] 清理旧定时任务失败", exc_info=True)
        count = 0
        for row in self.store.daily_groups():
            slots = _parse_slots(row.get("daily_slots") or "")
            if not slots:
                slots = _parse_slots(str(self.cfg.get("daily_push_slots", "") or ""))
            umo = row.get("umo") or ""
            for slot in slots:
                hour, minute = (int(part) for part in slot.split(":"))
                try:
                    self._scheduler.add_job(
                        self._daily_push,
                        "cron",
                        hour=hour,
                        minute=minute,
                        args=[umo],
                        id=f"meme:{umo}:{slot}",
                        replace_existing=True,
                        misfire_grace_time=600,
                        coalesce=True,
                    )
                    count += 1
                except Exception:  # noqa: BLE001
                    logger.warning("[Meme] 添加定时任务失败 %s %s", umo, slot, exc_info=True)
        if count:
            logger.info("[Meme] 已排 %s 个每日推送任务", count)
        # remove_all_jobs() 刚把自动推送的抽签任务也清掉了，这里补回来
        self._reschedule_auto()

    async def _daily_push(self, umo: str) -> None:
        """定时推送一条。任何一步失败都只记日志，不在群里报错。"""
        if not umo:
            return
        state = await asyncio.to_thread(self.store.group_state, umo) or {}
        now = time.time()
        blocked_until = float(state.get("blocked_until") or 0)
        if blocked_until > now:
            logger.info("[Meme] %s 主动消息被平台拒绝中，跳过", umo)
            return
        day_key = _now_day_key(now)
        push_count = int(state.get("push_count") or 0) if state.get("push_date") == day_key else 0
        if push_count >= self._cfg_int("daily_push_max_per_day", 2):
            return
        if self._in_quiet_hours(dt.datetime.fromtimestamp(now)):
            logger.info("[Meme] %s 处于免打扰时段，跳过", umo)
            return

        item = await asyncio.to_thread(self.pipeline.pick, self._weights())
        if item is None:
            return
        caption = compose_caption(
            item.caption, item.caption_zh, self._cfg_int("caption_limit", 160)
        )
        try:
            data = await asyncio.to_thread(self._read_file, item.file_path)
        except OSError:
            return

        sent = False
        error_text = ""
        hub = self._get_hub(quiet=True)
        if hub is not None and hasattr(hub, "send_media_message"):
            file_type = self._hub_image_file_type(hub)
            result = await deliver(
                [
                    Attempt(
                        "hub 主动富媒体",
                        lambda: hub.send_media_message(
                            umo, data, file_type, text=caption
                        ),
                    ),
                ]
            )
            sent = result.ok
            if not sent:
                error_text = result.summary()
        if not sent:
            result = await deliver(
                [
                    Attempt(
                        "主动图片消息",
                        lambda: self._send_image_chain(umo, data, caption),
                    ),
                    Attempt("主动文字", lambda: self._send_text(umo, caption or "梗图")),
                ]
            )
            sent = result.ok
            error_text = error_text or result.summary()

        if sent:
            await asyncio.to_thread(self.store.mark_used, item.id)
            await asyncio.to_thread(self.store.note_push, umo, ok=True, day_key=day_key)
            logger.info("[Meme] 已向 %s 推送 #%s", umo, item.id)
            return
        if "40034105" in error_text or "无权限" in error_text or "权限" in error_text:
            await asyncio.to_thread(
                self.store.note_push,
                umo,
                ok=False,
                day_key=day_key,
                blocked_until=now + PUSH_BLOCK_SECONDS,
            )
            logger.warning(
                "[Meme] %s 不允许主动消息（群主没开「机器人主动在群聊内发言」），24 小时内不再尝试",
                umo,
            )
        else:
            logger.warning("[Meme] %s 推送失败：%s", umo, error_text)

    def _in_quiet_hours(self, moment: dt.datetime) -> bool:
        start = _parse_slots(str(self.cfg.get("daily_push_quiet_start", "") or ""))
        end = _parse_slots(str(self.cfg.get("daily_push_quiet_end", "") or ""))
        if not start or not end:
            return False
        now_minutes = moment.hour * 60 + moment.minute
        start_minutes = int(start[0].split(":")[0]) * 60 + int(start[0].split(":")[1])
        end_minutes = int(end[0].split(":")[0]) * 60 + int(end[0].split(":")[1])
        if start_minutes == end_minutes:
            return False
        if start_minutes < end_minutes:
            return start_minutes <= now_minutes < end_minutes
        return now_minutes >= start_minutes or now_minutes < end_minutes

    async def _send_image_chain(self, umo: str, data: bytes, caption: str) -> None:
        """主动发图（非 QQ 官方平台的退路；QQ 官方必须走 Hub）。"""
        from astrbot.core.message.message_event_result import MessageChain

        chain = MessageChain()
        chain.base64_image(base64.b64encode(data).decode())
        if caption:
            chain.message(caption)
        await self.context.send_message(umo, chain)

    async def _send_text(self, umo: str, text: str) -> None:
        from astrbot.core.message.message_event_result import MessageChain

        chain = MessageChain().message(text)
        await self.context.send_message(umo, chain)

    # --- 小工具 -------------------------------------------------------------

    def _args(self, event: AstrMessageEvent) -> str:
        text = (event.message_str or "").strip()
        if not text:
            return ""
        head, _, rest = text.partition(" ")
        if head.lstrip("/").strip() in COMMAND_NAMES:
            return rest.strip()
        return text

    @staticmethod
    def _read_file(path: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()

    @staticmethod
    def _component_type(component: Any) -> str:
        return str(getattr(component, "type", "") or "").lower()

    def _event_images(self, event: AstrMessageEvent) -> list:
        """本条消息里的图，外加被引用消息里的图（引用里能拿到就拿）。"""
        found: list[Any] = []
        message = getattr(getattr(event, "message_obj", None), "message", None) or []
        for component in message:
            kind = self._component_type(component)
            if kind.endswith("image"):
                found.append(component)
            elif kind.endswith("reply"):
                for inner in getattr(component, "chain", None) or []:
                    if self._component_type(inner).endswith("image"):
                        found.append(inner)
        return found

    async def _component_bytes(self, component: Any) -> bytes:
        path = str(getattr(component, "path", "") or "")
        if path and os.path.isfile(path):
            return await asyncio.to_thread(self._read_file, path)
        raw = str(getattr(component, "file", "") or "")
        if raw.startswith("base64://"):
            return base64.b64decode(raw[len("base64://") :])
        if raw.startswith("file://"):
            local = urllib.parse.unquote(raw[len("file://") :])
            if os.path.isfile(local):
                return await asyncio.to_thread(self._read_file, local)
        url = str(getattr(component, "url", "") or "")
        if not url and raw.startswith("http"):
            url = raw
        if url:
            try:
                return await fetch_bytes(
                    url,
                    self._cfg_float("download_timeout_seconds", 30.0),
                    proxy=self._proxy(),
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"下载失败（{type(exc).__name__}）") from exc
        raise RuntimeError("这条消息里没有可读取的图片地址")

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender = str(event.get_sender_id() or "")
        if not sender:
            return False
        admins = {str(a) for a in (self.cfg.get("extra_admins") or []) if str(a).strip()}
        try:
            global_admins = self.context.get_config().get("admins_id", []) or []
            admins |= {str(a) for a in global_admins if str(a).strip()}
        except Exception:  # noqa: BLE001
            pass
        if sender in admins:
            return True
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001
                return False
        return False

    # --- 冷却 / 计数 --------------------------------------------------------

    def _cooldown_left(self, umo: str, sender: str) -> float:
        gap = self._cfg_int("cooldown_seconds", 60)
        if gap <= 0:
            return 0.0
        last = self._cooldowns.get((umo, sender), 0.0)
        return max(0.0, gap - (time.time() - last))

    def _group_cooldown_left(self, umo: str) -> float:
        gap = self._cfg_int("send_cooldown_seconds", 5)
        if gap <= 0:
            return 0.0
        last = self._group_cooldowns.get(umo, 0.0)
        return max(0.0, gap - (time.time() - last))

    def _touch_cooldowns(self, umo: str, sender: str) -> None:
        now = time.time()
        self._cooldowns[(umo, sender)] = now
        self._group_cooldowns[umo] = now

    def _submission_cooldown_ok(self, umo: str, sender: str) -> bool:
        gap = self._cfg_int("submission_cooldown_seconds", 30)
        if gap <= 0:
            return True
        key = f"submit_cd:{umo}:{sender}"
        last = float(self.store.get_meta(key, "0") or 0)
        if time.time() - last < gap:
            return False
        self.store.set_meta(key, str(time.time()))
        return True

    def _submit_day_key(self, umo: str, sender: str) -> str:
        return f"submit:{_now_day_key()}:{umo}:{sender}"

    def _submit_used_today(self, umo: str, sender: str) -> int:
        return int(self.store.get_meta(self._submit_day_key(umo, sender), "0") or 0)

    def _note_submit(self, umo: str, sender: str) -> None:
        key = self._submit_day_key(umo, sender)
        self.store.set_meta(key, str(self._submit_used_today(umo, sender) + 1))


    # --- 网页后台（pages/meme）-----------------------------------------------

    def _thumbs_dir(self) -> str:
        return os.path.join(self.data_dir, "thumbs")

    def _thumb_of(self, item) -> str:
        from .meme.thumbs import GALLERY_THUMB, data_url, read_thumb

        try:
            raw = read_thumb(item.file_path, item.sha256, self._thumbs_dir(), GALLERY_THUMB)
        except (OSError, MemoryError):
            return ""
        return data_url(raw)

    async def web_state(
        self,
        *,
        origin: str = "",
        query: str = "",
        sort: str = "",
        status: str = "",
        limit: int = 24,
        offset: int = 0,
    ) -> dict:
        """页面首屏要的一切：候选图一页 + 统计 + 来源分布。"""
        from .meme.gallery import (
            SORT_NEW,
            build_gallery,
            build_origin_breakdown,
            filter_items,
            sort_items,
        )

        stats = await asyncio.to_thread(self.store.stats)
        items = await asyncio.to_thread(self.store.random_pool)
        # 已下架的也要能在页面上看见（否则「删掉的图」在界面里凭空消失）
        disabled = [
            item
            for item in await asyncio.to_thread(self.store.recent, 200)
            if item.status != "active"
        ]
        everything = {item.id: item for item in list(items) + list(disabled)}
        pool = filter_items(
            everything.values(), origin=origin, query=query, status=status or "all"
        )
        pool = sort_items(pool, sort or SORT_NEW)
        page = build_gallery(
            pool,
            total=stats["total"],
            stats=stats,
            thumb_of=self._thumb_of,
            file_exists=lambda path: bool(path) and os.path.isfile(path),
            limit=limit,
            offset=offset,
        )
        page["origins"] = build_origin_breakdown(everything.values())
        page["sort"] = sort or SORT_NEW
        page["filters"] = {"origin": origin or "all", "q": query, "status": status or "all"}
        page["running"] = {
            "captionMode": str(self.cfg.get("caption_zh_mode", "") or ""),
            "moderationProvider": str(self.cfg.get("moderation_provider_id", "") or ""),
            "proxy": self._proxy(),
            "dataDir": self.data_dir,
        }
        page["blacklist"] = await asyncio.to_thread(self.store.list_blacklist, 20)
        auto_state = await asyncio.to_thread(self.store.group_state, "") or {}
        page["auto"] = {
            "enabled": self._auto_enabled(),
            "pcts": self._auto_pcts(),
            "maxPerDay": self._cfg_int("auto_push_max_per_day", AUTO_MAX_PER_DAY_DEFAULT),
            "quiet": str(self.cfg.get("auto_push_quiet", AUTO_QUIET_DEFAULT) or ""),
            "groups": len(await asyncio.to_thread(self.store.auto_groups)),
        }
        page["card"] = {
            "enabled": self._card_enabled(),
            "showAgain": self._show_again(),
            "showHowto": self._show_howto(),
            "materialKeep": self._cfg_int("card_material_keep", CARD_MATERIAL_KEEP),
            "materials": len(await asyncio.to_thread(self.store.card_material_ids)),
        }
        return page

    async def web_detail(self, raw_id: str) -> dict | None:
        from .meme.thumbs import PREVIEW_SIZE, data_url, read_thumb

        try:
            meme_id = int(str(raw_id).lstrip("#"))
        except ValueError:
            return None
        item = await asyncio.to_thread(self.store.get, meme_id)
        if item is None:
            return None
        try:
            raw = await asyncio.to_thread(
                read_thumb, item.file_path, item.sha256, self._thumbs_dir(), PREVIEW_SIZE
            )
            image = data_url(raw)
        except OSError:
            image = ""
        return {
            "id": item.id,
            "image": image,
            "mime": item.mime,
            "origin": item.origin,
            "status": item.status,
            "caption": item.caption,
            "captionZh": item.caption_zh,
            "tags": list(item.tags),
            "nsfwLevel": item.nsfw_level,
            "usedCount": item.used_count,
            "contributor": item.contributor,
            "sourceUrl": item.source_url,
            "filePath": item.file_path,
            "createdAt": item.created_at,
            "usedAt": item.used_at,
        }

    def web_upload_options(self) -> dict:
        """页面上的「免审」开关，存在库里。

        为什么不用请求参数：上传走的是桥接层的二进制通道（`bridge.upload`），
        它只带文件、不带额外字段。所以页面在改动这个开关时先写一条记录，
        上传时再读回来。同一个人操作后台，不存在并发歧义。

        （「可见范围」这个选项跟着单群图库一起删了：库只有一份。）
        """
        skip = self.store.get_meta("web_upload_skip_review", "1")
        return {"skipReview": skip != "0"}

    async def web_set_upload_options(self, *, skip_review=None) -> dict:
        if skip_review is not None:
            self.store.set_meta("web_upload_skip_review", "1" if skip_review else "0")
        return {"ok": True, **self.web_upload_options()}

    async def web_upload(
        self,
        data: bytes,
        filename: str = "",
        *,
        skip_review=None,
    ) -> dict:
        """网页拖进来的图。

        ``skip_review=True``（页面默认）时**不花审核的钱**：拖进来的人就是
        坐在面板前的管理员，他看得到自己拖的是什么。想恢复「先审后收」，
        把页面上的勾去掉即可，走的就是群友投稿那条完全一样的管线。
        """
        options = self.web_upload_options()
        if skip_review is None:
            skip_review = options["skipReview"]

        if skip_review:
            outcome = await self._ingest_without_review(data)
        else:
            outcome = await self.pipeline.ingest(
                data,
                origin="submitted",
                contributor="web",
            )
        result = {
            "ok": bool(outcome.ok),
            "message": outcome.reason or ("已入库" if outcome.ok else "没收"),
            "skippedReview": bool(skip_review),
            "duplicate": bool(outcome.duplicate),
            "filename": filename,
        }
        if outcome.meme is not None:
            result["meme"] = {
                "id": outcome.meme.id,
                "origin": outcome.meme.origin,
                "caption": outcome.meme.caption,
            }
        return result

    async def web_fetch_url(self, url: str, *, skip_review=None) -> dict:
        """拖进来的只是一张图的**网址**（不是文件）→ 服务端去把它下回来。

        为什么要在服务端下：面板页面跑在沙盒 iframe 里（面板给的是
        ``sandbox="allow-scripts allow-forms allow-downloads"``，没有
        ``allow-same-origin``），页面自己去 corss-origin 取字节是取不到的；
        而拖拽「网页上的图」「聊天里的图」时，浏览器给的就是 URL。

        三道闸：
        1. 只认 http/https；
        2. 不许指向本机/内网（这就是 SSRF 的门，面板一旦被谁点开也不能拿它探内网）；
        3. 大小上限沿用 ``max_image_mb``。
        """
        from .meme.adapters import fetch_bytes
        from .meme.media import human_size

        target = str(url or "").strip()
        if not target:
            return {"ok": False, "message": "没拿到地址"}
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https"):
            return {"ok": False, "message": f"只认 http/https 地址（拿到的是「{parsed.scheme or '空'}」）"}
        host = parsed.hostname or ""
        if not host:
            return {"ok": False, "message": "这个地址里没有主机名"}
        if _looks_private_host(host):
            return {"ok": False, "message": f"这个地址指向本机/内网（{host}），不收"}
        try:
            data = await fetch_bytes(
                target,
                timeout=self._cfg_float("download_timeout_seconds", 30.0),
                headers={"User-Agent": "AstrBot-Meme/1.0"},
                proxy=self._proxy(),
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"下载失败：{type(exc).__name__}: {exc}"}
        max_bytes = int(self._cfg_float("max_image_mb", 8.0) * 1024 * 1024)
        if max_bytes and len(data) > max_bytes:
            return {
                "ok": False,
                "message": f"这张太大了（{human_size(len(data))}，上限 {human_size(max_bytes)}）",
            }
        name = (parsed.path.rsplit("/", 1)[-1] or "downloaded")[:80]
        result = await self.web_upload(data, name, skip_review=skip_review)
        result["sourceUrl"] = target
        result["message"] = f"{result.get('message', '')}（来自网址 {host}）"
        return result

    async def _ingest_without_review(self, data: bytes):
        """免审入库：仍然做格式/大小/查重，只是跳过模型审核。"""
        from .meme.media import ext_for_mime, human_size, sha256_of, sniff_image
        from .meme.models import STATUS_ACTIVE, IngestResult

        if not data:
            return IngestResult(ok=False, reason="空文件")
        sniffed = sniff_image(data)
        if sniffed is None:
            return IngestResult(ok=False, reason="这不是图片（只收 jpg/png/gif/webp）")
        mime, ext = sniffed
        max_bytes = int(self._cfg_float("max_image_mb", 8.0) * 1024 * 1024)
        if max_bytes and len(data) > max_bytes:
            return IngestResult(
                ok=False,
                reason=f"图太大了（{human_size(len(data))}，上限 {human_size(max_bytes)}）",
            )
        sha256 = sha256_of(data)
        existing = await asyncio.to_thread(self.store.by_sha, sha256)
        if existing is not None and existing.status == STATUS_ACTIVE:
            return IngestResult(ok=True, meme=existing, duplicate=True, reason="这张早就在库里了")
        path = await asyncio.to_thread(self.store.save_media, sha256, data, ext)
        meme_id, _created = await asyncio.to_thread(
            self.store.add,
            sha256=sha256,
            file_path=path,
            origin="submitted",
            mime=mime,
            caption="",
            tags=("免审",),
            contributor="web",
        )
        await asyncio.to_thread(self.store.clear_reject, sha256)
        meme = await asyncio.to_thread(self.store.get, meme_id)
        if meme is None:
            return IngestResult(ok=False, reason="入库失败（数据库异常）")
        return IngestResult(ok=True, meme=meme, reason="已免审入库")

    async def _probe_image(self) -> bytes:
        """自检用的测试图：现做一张纯色 PNG，不依赖库里有没有图、也不依赖内置图。

        以前这里读 ``assets/bundled`` 里的种子图；内置图整套删掉之后，自检不该
        再去要求「库里有图」（刚装好的机器人库里本来就是空的）。
        """
        def _make() -> bytes:
            try:
                import io as _io

                from PIL import Image as _PILImage, ImageDraw

                img = _PILImage.new("RGB", (640, 480), (32, 96, 200))
                draw = ImageDraw.Draw(img)
                draw.rectangle([40, 40, 600, 440], outline=(255, 255, 255), width=6)
                buffer = _io.BytesIO()
                img.save(buffer, "PNG")
                return buffer.getvalue()
            except Exception:  # noqa: BLE001
                # 没有 PIL：一张 1x1 的合法 PNG（够验链路能不能上传，群里又看不到）
                return base64.b64decode(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
                    "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
                )

        return await asyncio.to_thread(_make)

    async def web_card_probe(self) -> dict:
        """卡片图自检：素材库 → 图床，逐项试，结果说人话。"""
        hub = self._get_hub(quiet=True)
        if hub is None:
            return {"ok": False, "message": "没有 QQ Official Hub，卡片用不了（会退纯图片）"}
        data = await self._probe_image()
        if not data:
            return {"ok": False, "message": "生成不出测试图（PIL 不可用？）"}
        width, height = _image_size(data)
        report: dict[str, Any] = {"ok": False, "size": f"{width}x{height}", "checks": []}

        # 素材库要先知道「用哪个群的机器人」才能上传，所以拿最近活跃的真群；
        # 一个群都没有（机器人刚装）时跳过这一项，直接验图床。
        origins = await asyncio.to_thread(self.store.recent_group_umos, 5)
        upload = getattr(hub, "publish_image_material", None)
        if upload is None:
            report["checks"].append({"name": "素材库", "ok": False, "detail": "Hub 版本不支持"})
        elif not origins:
            report["checks"].append({
                "name": "素材库", "ok": False,
                "detail": "还没有任何群跟机器人说过话，拿不到 client，先跳过",
            })
        else:
            for origin in origins:
                try:
                    material = await upload(data, origin=origin)
                    image_id = str(material.get("image_id") or "")
                    url = str(material.get("url") or "")
                    await self._release_material(hub, image_id, origin=origin)
                    report["checks"].append({
                        "name": "素材库", "ok": bool(url),
                        "detail": f"用 {origin} 拿到 qqbot-media 引用，已回收额度",
                    })
                    report["ok"] = bool(url)
                    report["via"] = "素材库"
                    if report["ok"]:
                        break
                except Exception as exc:  # noqa: BLE001
                    report["checks"].append({
                        "name": "素材库", "ok": False,
                        "detail": f"{origin}：{type(exc).__name__}: {exc}",
                    })

        if not report["ok"]:
            publisher = getattr(hub, "publish_image_checked", None)
            if publisher is not None:
                try:
                    url = await publisher(data, slot="meme:probe")
                    report["checks"].append({
                        "name": "图床", "ok": bool(url), "detail": url or "空地址",
                    })
                    report["ok"] = bool(url)
                    if report["ok"]:
                        report["via"] = "图床"
                except Exception as exc:  # noqa: BLE001
                    report["checks"].append({
                        "name": "图床", "ok": False,
                        "detail": f"{type(exc).__name__}: {exc}",
                    })
        report["message"] = (
            f"卡片图能显示（走{report.get('via', '？')}）" if report["ok"]
            else "卡片带图这条路暂时不通，会降级成「图片 + 无图卡片」，按钮照常"
        )
        return report

    async def web_unban(self, sender_id: str) -> dict:
        """恢复某人的投稿权限（页面上的「解禁」）。"""
        sender = str(sender_id or "").strip()
        if not sender:
            return {"ok": False, "message": "没给用户 ID"}
        removed = await asyncio.to_thread(self.store.remove_blacklist, sender)
        if not removed:
            return {"ok": False, "message": f"{sender} 不在黑名单里"}
        logger.info("[Meme] 已恢复 %s 的投稿权限（来自页面）", sender)
        return {"ok": True, "message": f"已恢复 {sender} 的投稿权限"}

    async def web_delete(self, raw_id: str) -> dict:
        try:
            meme_id = int(str(raw_id).lstrip("#"))
        except ValueError:
            return {"ok": False, "message": "编号不对"}
        item = await asyncio.to_thread(self.store.delete, meme_id)
        if item is None:
            return {"ok": False, "message": f"没有 #{meme_id} 这张"}
        try:
            if os.path.isfile(item.file_path):
                os.remove(item.file_path)
        except OSError:
            pass
        return {"ok": True, "message": f"已下架 #{meme_id}", "id": meme_id}

    async def web_toggle(self, raw_id: str, active: bool) -> dict:
        try:
            meme_id = int(str(raw_id).lstrip("#"))
        except ValueError:
            return {"ok": False, "message": "编号不对"}
        status = "active" if active else "disabled"
        changed = await asyncio.to_thread(self.store.set_status, meme_id, status)
        if not changed:
            return {"ok": False, "message": f"没有 #{meme_id} 这张"}
        return {"ok": True, "id": meme_id, "status": status, "message": "已启用" if active else "已停用"}

    async def web_recheck(self, raw_id: str) -> dict:
        from .meme.review import judge

        try:
            meme_id = int(str(raw_id).lstrip("#"))
        except ValueError:
            return {"ok": False, "message": "编号不对"}
        item = await asyncio.to_thread(self.store.get, meme_id)
        if item is None:
            return {"ok": False, "message": f"没有 #{meme_id} 这张"}
        try:
            data = await asyncio.to_thread(self._read_file, item.file_path)
        except OSError as exc:
            return {"ok": False, "message": f"读图失败：{exc}"}
        review = await self.pipeline.review_bytes(data, item.mime)
        accepted, why = judge(review, self.pipeline.policy)
        if not accepted:
            await asyncio.to_thread(self.store.set_status, meme_id, "disabled")
        return {
            "ok": True,
            "id": meme_id,
            "accepted": accepted,
            "reason": why,
            "nsfwLevel": review.nsfw_level,
            "flags": list(review.flags),
            "language": review.language,
            "caption": review.caption_text,
            "parsed": review.parsed,
            "message": ("通过：" if accepted else "不通过（已自动停用）：") + why,
        }

    def web_settings(self) -> dict:
        from .meme.gallery import ORIGIN_LABELS
        from .meme.settings import export_settings

        payload = export_settings(self.cfg)
        payload["originLabels"] = ORIGIN_LABELS
        payload["plugin"] = {"name": PLUGIN_NAME, "version": "0.3.1"}
        return payload

    async def web_save_settings(self, values: dict) -> dict:
        """网页保存配置：写回插件配置对象 + 落盘 + 重建管线。"""
        from .meme.settings import apply_settings, auto_prob_problem

        # 先在草稿上试一遍：三档概率顺序不对就整批不写，免得群里「低/中/高」颠倒
        draft = dict(self.config)
        draft.update({k: v for k, v in (values or {}).items() if k in draft or k.startswith("auto_push_")})
        problem = auto_prob_problem(draft)
        if problem:
            return {"ok": False, "message": problem, "errors": [problem]}

        applied, errors = apply_settings(self.config, values)
        if applied:
            saver = getattr(self.config, "save_config_async", None)
            if callable(saver):
                try:
                    await saver()
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[Meme] 配置落盘失败：%s", exc)
                    return {"ok": False, "message": f"配置已改但写盘失败：{exc}", "applied": applied}
            self._pipeline = None  # 重建：阈值/模式都变了
            self._reschedule_auto()
            await asyncio.to_thread(
                self.store.set_meta, "settings_saved_at", str(int(time.time()))
            )
        if errors and not applied:
            return {"ok": False, "message": "；".join(errors), "errors": errors}
        return {
            "ok": True,
            "applied": applied,
            "errors": errors,
            "message": ("已保存 %d 项" % len(applied)) if applied else "没有改动",
        }
