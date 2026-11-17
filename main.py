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
from .meme.chinese import MODE_APPEND, MODE_REDRAW, compose_caption
from .meme.delivery import Attempt, deliver
from .meme.models import SCOPE_GLOBAL, SCOPE_GROUP, MemeItem
from .meme.pipeline import MemePipeline, seed_bundled
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
#: 收到「主动消息无权限」后，这个群多久之内不再试。
PUSH_BLOCK_SECONDS = 24 * 3600

HELP_TEXT = """梗图玩法：
/meme —— 随机来一张（也可以发「梗图」「来张梗图」）
发一张图并附上 /meme 收 —— 投稿（先审后收）
/meme 库 —— 看看这个群有多少张能发
/meme 每日 —— 看每日推送状态
/meme 每日 9:00,21:00 —— 每天这两个点自动发一张（管理员）
/meme 每日 关 —— 关掉自动发（管理员）
/meme 删 <编号> —— 下架一张（管理员）
/meme 审核 <编号> —— 重新审一次（管理员）
/meme 拉 —— 立刻从第三方接口拉一张入库（管理员）
/meme 测试 —— 用内置图跑一遍完整管线，看审核输出（管理员）"""


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
    "0.2.1",
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
        bundled_dir = os.path.join(os.path.dirname(__file__), "assets", "bundled")
        added = await asyncio.to_thread(seed_bundled, self.store, bundled_dir)
        if added:
            logger.info("[Meme] 内置种子图入库 %s 张", added)
        self._ensure_scheduler()
        self._reschedule_daily()
        self._register_web()
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
                build_review_prompt(policy.reject_flags, policy.nsfw_max_level),
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
                    size=str(self.cfg.get("redraw_size", "1024x1024") or "1024x1024"),
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
            default_scope=str(self.cfg.get("submission_scope", SCOPE_GROUP) or SCOPE_GROUP),
            max_bytes=int(self._cfg_float("max_image_mb", 8.0) * 1024 * 1024),
            redraw_recheck=bool(self.cfg.get("redraw_recheck", True)),
            caption_limit=self._cfg_int("caption_limit", 160),
        )

    def _weights(self) -> dict[str, float]:
        return {
            "bundled": self._cfg_float("weight_bundled", 1.0),
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

        if sub in ("收", "投稿", "submit"):
            async for result in self._do_submit(event, images):
                yield result
            return

        if images and not sub:
            # 带图没写子命令，按投稿处理：少一步操作，少一堆「怎么投稿」
            async for result in self._do_submit(event, images):
                yield result
            return

        if sub in ("删", "下架", "del", "delete"):
            yield await self._do_delete(event, rest)
            return

        if sub in ("审核", "审", "review"):
            yield await self._do_review(event, rest)
            return

        if sub in ("每日", "推送", "daily"):
            async for result in self._do_daily(event, rest):
                yield result
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
            # 不认识的子命令：当作闲聊输入，直接发一张，别骂用户
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

        item = await asyncio.to_thread(self.pipeline.pick, group_id, self._weights())
        if item is None and self._api_configured():
            item = await self._fetch_api_meme(group_id)
        if item is None:
            return event.plain_result(
                "库里还空着。发一张图并附上「/meme 收」就能投稿，"
                "管理员也可以用「/meme 拉」从第三方接口拉几张。"
            )

        self._touch_cooldowns(umo, sender)
        await asyncio.to_thread(self.store.mark_used, item.id)
        return await self._send_item(event, item)

    async def _send_item(self, event: AstrMessageEvent, item: MemeItem):
        caption = compose_caption(
            item.caption, item.caption_zh, self._cfg_int("caption_limit", 160)
        )
        try:
            data = await asyncio.to_thread(self._read_file, item.file_path)
        except OSError as exc:
            logger.warning("[Meme] 读图失败 #%s: %s", item.id, exc)
            data = b""

        hub = self._get_hub(quiet=True)
        if data and hub is not None:
            try:
                await hub.send_image_message(
                    event.unified_msg_origin, data, text=caption
                )
                return None
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Meme] Hub 发图失败，改用原生：%s", exc)

        if data:
            chain: list[Any] = [Image.fromFileSystem(item.file_path)]
            if caption:
                chain.append(Plain(caption))
            return event.chain_result(chain)
        return event.plain_result(
            f"这张图现在发不出来（#{item.id}）" + (f"\n{caption}" if caption else "")
        )

    # --- 投稿 ---------------------------------------------------------------

    async def _do_submit(self, event: AstrMessageEvent, images: list) -> Any:
        if not images:
            yield event.plain_result(
                "投稿方式：把图发出来，同一条消息里带上「/meme 收」。"
            )
            return
        umo = event.unified_msg_origin
        group_id = event.get_group_id() or ""
        sender = str(event.get_sender_id() or "")
        scope = str(self.cfg.get("submission_scope", SCOPE_GROUP) or SCOPE_GROUP)
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
                group_id=group_id,
                scope=scope,
            )
            if outcome.ok and outcome.meme is not None:
                accepted += 1
                tag = "重画成中文版" if outcome.redrawn else "已入库"
                dup = "（早就有了）" if outcome.duplicate else ""
                lines.append(f"#{outcome.meme.id} {tag}{dup}：{outcome.reason}")
            else:
                lines.append(f"没收：{outcome.reason}")
        if accepted:
            self._note_submit(umo, sender)
        yield event.plain_result("\n".join(lines) or "没收到图")

    # --- 库 / 删 / 审 -------------------------------------------------------

    async def _do_stats(self, event: AstrMessageEvent) -> Any:
        group_id = event.get_group_id() or ""
        stats = await asyncio.to_thread(self.store.stats, group_id)
        yield event.plain_result(
            format_stats(
                total=stats["total"],
                by_origin=stats["by_origin"],
                group_total=stats["group_total"],
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
            if item.origin != "bundled" and os.path.isfile(item.file_path):
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
        item = await self._fetch_api_meme(event.get_group_id() or "")
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

    async def _fetch_api_meme(self, group_id: str) -> MemeItem | None:
        """拉一张、过审、入库；整条链都走同一个 pipeline。"""
        try:
            data, source_url = await self._api_source().fetch()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Meme] 第三方接口拉图失败：%s", exc)
            return None
        outcome = await self.pipeline.ingest(
            data,
            origin="api",
            group_id=group_id,
            scope=SCOPE_GLOBAL,
            source_url=source_url,
        )
        if outcome.ok and outcome.meme is not None:
            return outcome.meme
        logger.info("[Meme] 第三方图没过审：%s", outcome.reason)
        return None

    # --- 自检 ---------------------------------------------------------------

    async def _do_selftest(self, event: AstrMessageEvent, images: list) -> Any:
        """跑一遍完整管线，把审核 JSON 原样回给管理员。"""
        if not self._is_admin(event):
            yield event.plain_result("只有管理员能自检")
            return
        data = b""
        if images:
            try:
                data = await self._component_bytes(images[0])
            except Exception as exc:  # noqa: BLE001
                yield event.plain_result(f"拿不到这张图：{exc}")
                return
        if not data:
            bundled_dir = os.path.join(os.path.dirname(__file__), "assets", "bundled")
            names = sorted(os.listdir(bundled_dir)) if os.path.isdir(bundled_dir) else []
            for name in names:
                path = os.path.join(bundled_dir, name)
                if os.path.isfile(path):
                    data = await asyncio.to_thread(self._read_file, path)
                    break
        if not data:
            yield event.plain_result("没有可用于自检的图（assets/bundled 是空的）")
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

        group_id = str(state.get("group_id") or "")
        item = await asyncio.to_thread(self.pipeline.pick, group_id, self._weights())
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
        group_id: str = "",
        limit: int = 24,
        offset: int = 0,
    ) -> dict:
        """页面首屏要的一切：候选图一页 + 统计 + 来源分布 + 群列表。"""
        from .meme.gallery import (
            SORT_NEW,
            build_gallery,
            build_origin_breakdown,
            filter_items,
            sort_items,
        )

        stats = await asyncio.to_thread(self.store.stats, group_id)
        items = await asyncio.to_thread(self.store.random_pool, group_id)
        # 已下架的也要能在页面上看见（否则「删掉的图」在界面里凭空消失）
        disabled = [
            item
            for item in await asyncio.to_thread(self.store.recent, 200, group_id)
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
        page["groups"] = await asyncio.to_thread(self._known_groups)
        return page

    def _known_groups(self) -> list[dict]:
        """见过面的群（页面上的「按群筛选」用）。"""
        try:
            rows = self.store._rows("SELECT * FROM groups")  # noqa: SLF001 - 只读一处
        except Exception:  # noqa: BLE001
            return []
        result = []
        for row in rows:
            record = dict(row)
            result.append(
                {
                    "umo": record.get("umo", ""),
                    "groupId": record.get("group_id", ""),
                    "dailyEnabled": bool(record.get("daily_enabled")),
                    "dailySlots": record.get("daily_slots", ""),
                    "blockedUntil": float(record.get("blocked_until") or 0),
                    "lastPush": float(record.get("last_push") or 0),
                    "pushCount": int(record.get("push_count") or 0),
                }
            )
        return sorted(result, key=lambda item: item["groupId"])

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
            "scope": item.scope,
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
        """页面上的「免审」「可见范围」两个开关，存在库里。

        为什么不用请求参数：上传走的是桥接层的二进制通道（`bridge.upload`），
        它只带文件、不带额外字段。所以页面在改动这两个开关时先写一条记录，
        上传时再读回来。同一个人操作后台，不存在并发歧义。
        """
        scope = self.store.get_meta("web_upload_scope", "") or str(
            self.cfg.get("submission_scope", SCOPE_GROUP) or SCOPE_GROUP
        )
        skip = self.store.get_meta("web_upload_skip_review", "1")
        return {"scope": scope, "skipReview": skip != "0"}

    async def web_set_upload_options(self, *, scope: str = "", skip_review=None) -> dict:
        if scope in (SCOPE_GROUP, SCOPE_GLOBAL):
            self.store.set_meta("web_upload_scope", scope)
        if skip_review is not None:
            self.store.set_meta("web_upload_skip_review", "1" if skip_review else "0")
        return {"ok": True, **self.web_upload_options()}

    async def web_upload(
        self,
        data: bytes,
        filename: str = "",
        *,
        scope: str = "",
        skip_review=None,
    ) -> dict:
        """网页拖进来的图。

        ``skip_review=True``（页面默认）时**不花审核的钱**：拖进来的人就是
        坐在面板前的管理员，他看得到自己拖的是什么。想恢复「先审后收」，
        把页面上的勾去掉即可，走的就是群友投稿那条完全一样的管线。
        """
        options = self.web_upload_options()
        if not scope:
            scope = options["scope"]
        if skip_review is None:
            skip_review = options["skipReview"]

        if skip_review:
            outcome = await self._ingest_without_review(data, scope=scope)
        else:
            outcome = await self.pipeline.ingest(
                data,
                origin="submitted",
                contributor="web",
                scope=scope or str(self.cfg.get("submission_scope", SCOPE_GROUP) or SCOPE_GROUP),
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
                "scope": outcome.meme.scope,
                "caption": outcome.meme.caption,
            }
        return result

    async def _ingest_without_review(self, data: bytes, *, scope: str = ""):
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
            scope=scope or str(self.cfg.get("submission_scope", SCOPE_GROUP) or SCOPE_GROUP),
            group_id="",
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

    async def web_delete(self, raw_id: str) -> dict:
        try:
            meme_id = int(str(raw_id).lstrip("#"))
        except ValueError:
            return {"ok": False, "message": "编号不对"}
        item = await asyncio.to_thread(self.store.delete, meme_id)
        if item is None:
            return {"ok": False, "message": f"没有 #{meme_id} 这张"}
        try:
            if item.origin != "bundled" and os.path.isfile(item.file_path):
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
        payload["plugin"] = {"name": PLUGIN_NAME, "version": "0.2.1"}
        return payload

    async def web_save_settings(self, values: dict) -> dict:
        """网页保存配置：写回插件配置对象 + 落盘 + 重建管线。"""
        from .meme.settings import apply_settings

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
