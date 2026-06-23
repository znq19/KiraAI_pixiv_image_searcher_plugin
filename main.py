import asyncio
import json
import time
from pathlib import Path
from typing import List

from core.plugin import BasePlugin, register_tool as tool
from core.chat.message_utils import KiraMessageBatchEvent, MessageChain
from core.chat.message_elements import Image
from core.logging_manager import get_logger
from core.utils.path_utils import get_data_path

from pixivpy3 import AppPixivAPI

logger = get_logger("pixiv_plugin", "cyan")

# 常用中文关键词 -> Pixiv 日文/英文 tag 映射
DEFAULT_KEYWORD_MAP = {
    "黑丝": "黒タイツ",
    "白丝": "白タイツ",
    "丝袜": "タイツ",
    "腿": "太もも",
    "大腿": "太もも",
    "脚": "足",
    "裸足": "裸足",
    "萝莉": "ロリ",
    "少女": "少女",
    "御姐": "お姉さん",
    "泳装": "水着",
    "制服": "制服",
    "女仆": "メイド",
    "兔女郎": "バニーガール",
    "和服": "着物",
    "婚纱": "ウェディングドレス",
    "猫耳": "猫耳",
    "狐耳": "狐耳",
    "原神": "原神",
    "崩坏": "崩壊",
    "崩铁": "崩壊:スターレイル",
    "星穹铁道": "崩壊:スターレイル",
    "碧蓝航线": "アズールレーン",
    "明日方舟": "アークナイツ",
    "FGO": "Fate/GrandOrder",
    "初音": "初音ミク",
}


class PixivPlugin(BasePlugin):
    def __init__(self, ctx, cfg):
        super().__init__(ctx, cfg)
        self.refresh_token = cfg.get("refresh_token", "")
        self.max_count = cfg.get("max_count", 5)
        self.min_bookmarks = cfg.get("min_bookmarks", 10000)
        self.exclude_tags = cfg.get("exclude_tags", ["AI", "R-18", "裸足"])
        self.allow_r18_in_dm = cfg.get("allow_r18_in_dm", False)
        self.allow_r18g_in_dm = cfg.get("allow_r18g_in_dm", False)
        self.r18_whitelist = list(cfg.get("r18_whitelist", []))
        self.r18g_whitelist = list(cfg.get("r18g_whitelist", []))
        self.proxy = cfg.get("proxy", "")
        self.dedup_file = Path(cfg.get("dedup_file", "pixiv_sent_ids.json"))
        self.dedup_expire_hours = cfg.get("dedup_expire_hours", 24)
        self.image_storage_dir = cfg.get("image_storage_dir", "files/pixiv")
        self.download_original = cfg.get("download_original", False)
        self.send_as_forward = cfg.get("send_as_forward", True)
        self.max_cache_files = cfg.get("max_cache_files", 50)
        self.cleanup_count = cfg.get("cleanup_count", 20)
        self.max_search_pages = cfg.get("max_search_pages", 10)
        self.keyword_map = {**DEFAULT_KEYWORD_MAP, **dict(cfg.get("keyword_map", {}))}
        self.api = None
        self._sent_ids: dict[str, float] = {}

    async def initialize(self):
        try:
            if not self.refresh_token:
                logger.warning("[PixivPlugin] 未配置 Refresh Token，插件已禁用。请在配置中填写 refresh_token 后重载。")
                return
            self._load_sent_ids()
            self._cleanup_storage()
            loop = asyncio.get_running_loop()
            success = await loop.run_in_executor(None, self._init_api)
            if success:
                logger.info("[PixivPlugin] 初始化成功")
            else:
                logger.warning("[PixivPlugin] Token 校验失败，插件已禁用。请检查 Refresh Token 是否有效。")
        except Exception as e:
            logger.warning(f"[PixivPlugin] 初始化异常，插件已禁用: {e}")

    def _init_api(self) -> bool:
        try:
            self.api = AppPixivAPI()
            if self.proxy:
                self.api.set_proxy(self.proxy)
            self.api.auth(refresh_token=self.refresh_token)
            # 验证 token
            self.api.user_detail(self.api.user_id)
            logger.info("[PixivPlugin] Token 验证通过")
            return True
        except Exception as e:
            logger.warning(f"[PixivPlugin] API 初始化/认证失败: {e}")
            self.api = None
            return False

    async def terminate(self):
        self.api = None

    # ------------------------------------------------------------------
    # 关键词转换：中文 -> 日文/英文 tag
    # ------------------------------------------------------------------
    def _normalize_keyword(self, keyword: str) -> str:
        keyword = keyword.strip()
        # 先尝试整句匹配
        if keyword in self.keyword_map:
            return self.keyword_map[keyword]
        # 再尝试分词替换（简单空格分隔）
        parts = []
        for part in keyword.split():
            parts.append(self.keyword_map.get(part, part))
        return " ".join(parts)

    # ------------------------------------------------------------------
    # 会话类型与 R-18/R-18G 权限判断
    # ------------------------------------------------------------------
    @staticmethod
    def _is_dm(event: KiraMessageBatchEvent) -> bool:
        """判断当前事件是否为私聊（Direct Message）。"""
        return not event.is_group_message()

    @staticmethod
    def _filter_whitelist_by_type(whitelist: List[str], session_type: str) -> List[str]:
        """从白名单中筛选出指定会话类型（dm/gm）的条目。"""
        marker = f":{session_type}:"
        return [entry for entry in whitelist if marker in entry]

    def _is_content_allowed(self, is_dm: bool, sid: str, content_type: str) -> bool:
        """判断指定会话是否允许发送 R-18 或 R-18G。

        规则：
        - 私聊：需开启对应开关；私聊白名单为空则所有私聊允许，否则仅白名单允许。
        - 群聊：不依赖开关；群聊白名单为空则所有群聊拒绝，仅群聊白名单允许。
        - 白名单按 :dm:/:gm: 自动分类，互不影响。
        """
        full_whitelist = self.r18_whitelist if content_type == "r18" else self.r18g_whitelist
        session_type = "dm" if is_dm else "gm"
        whitelist = self._filter_whitelist_by_type(full_whitelist, session_type)

        if is_dm:
            switch_on = self.allow_r18_in_dm if content_type == "r18" else self.allow_r18g_in_dm
            if not switch_on:
                return False
            if not whitelist:
                return True
            return sid in whitelist
        # 群聊
        if not whitelist:
            return False
        return sid in whitelist

    def _is_r18_allowed(self, is_dm: bool, sid: str, x_restrict: int) -> bool:
        """根据 x_restrict 判断作品是否允许在当前会话发送。"""
        if x_restrict <= 0:
            return True
        content_type = "r18" if x_restrict == 1 else "r18g"
        return self._is_content_allowed(is_dm, sid, content_type)

    def _effective_exclude_tags(self, is_dm: bool, sid: str) -> List[str]:
        """返回实际生效的排除标签列表；若会话允许 R-18/R-18G，则忽略对应标签。"""
        tags = list(self.exclude_tags)
        if self._is_content_allowed(is_dm, sid, "r18"):
            tags = [t for t in tags if t != "R-18"]
        if self._is_content_allowed(is_dm, sid, "r18g"):
            tags = [t for t in tags if t != "R-18G"]
        return tags

    # ------------------------------------------------------------------
    # 持久化去重（带过期时间）
    # ------------------------------------------------------------------
    def _load_sent_ids(self) -> None:
        try:
            if self.dedup_file.exists():
                data = json.loads(self.dedup_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._sent_ids = {str(k): float(v) for k, v in data.items()}
                    self._expire_sent_ids()
                    logger.info(f"已加载 {len(self._sent_ids)} 条有效发送记录")
                elif isinstance(data, list):
                    # 兼容旧格式：纯 ID 列表，视为永久有效
                    now = time.time()
                    self._sent_ids = {str(x): now for x in data}
                    logger.info(f"已加载 {len(self._sent_ids)} 条发送记录（旧格式）")
        except Exception as e:
            logger.warning(f"加载去重记录失败: {e}")
            self._sent_ids = {}

    def _expire_sent_ids(self) -> None:
        """清理已过期的去重记录。"""
        if self.dedup_expire_hours <= 0:
            return
        now = time.time()
        expire_seconds = self.dedup_expire_hours * 3600
        expired = [wid for wid, ts in self._sent_ids.items() if now - ts > expire_seconds]
        for wid in expired:
            del self._sent_ids[wid]
        if expired:
            logger.info(f"去重记录过期清理：移除 {len(expired)} 条")

    def _save_sent_ids(self) -> None:
        try:
            self.dedup_file.parent.mkdir(parents=True, exist_ok=True)
            self.dedup_file.write_text(
                json.dumps(self._sent_ids, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"保存去重记录失败: {e}")

    def _mark_sent(self, illust_id: str) -> None:
        self._sent_ids[str(illust_id)] = time.time()
        self._save_sent_ids()

    def _is_sent(self, illust_id: str) -> bool:
        wid = str(illust_id)
        if wid not in self._sent_ids:
            return False
        if self.dedup_expire_hours <= 0:
            return True
        now = time.time()
        expire_seconds = self.dedup_expire_hours * 3600
        if now - self._sent_ids[wid] > expire_seconds:
            del self._sent_ids[wid]
            self._save_sent_ids()
            return False
        return True

    # ------------------------------------------------------------------
    # 图片缓存目录 + 自动清理旧文件
    # ------------------------------------------------------------------
    def _get_storage_dir(self) -> Path:
        return Path(get_data_path()) / self.image_storage_dir

    def _cleanup_storage(self) -> None:
        try:
            storage_dir = self._get_storage_dir()
            if not storage_dir.exists():
                return

            files = [f for f in storage_dir.iterdir() if f.is_file()]
            if len(files) <= self.max_cache_files:
                return

            files.sort(key=lambda f: f.stat().st_mtime)
            to_delete = files[: self.cleanup_count]
            deleted = 0
            for f in to_delete:
                try:
                    f.unlink()
                    deleted += 1
                except Exception as e:
                    logger.warning(f"清理旧图片失败 {f}: {e}")

            logger.info(f"图片缓存清理完成：删除 {deleted} 个旧文件，当前剩余 {len(files) - deleted} 个")
        except Exception as e:
            logger.warning(f"图片缓存清理异常: {e}")

    # ------------------------------------------------------------------
    # 高级搜索：优先使用 popular_desc + bookmark_num_min，失败则回退 date_desc
    # ------------------------------------------------------------------
    def _search_illust_advanced_sync(self, keyword: str, offset: int | None = None):
        """直接调用 Pixiv API，支持 bookmark_num_min 和 popular_desc。"""
        url = "%s/v1/search/illust" % self.api.hosts
        params = {
            "word": keyword,
            "search_target": "partial_match_for_tags",
            "filter": "for_ios",
        }
        if offset:
            params["offset"] = offset
        if self.min_bookmarks > 0:
            params["bookmark_num_min"] = self.min_bookmarks

        # 先尝试 popular_desc（需要 Pixiv Premium）
        try:
            params["sort"] = "popular_desc"
            r = self.api.no_auth_requests_call("GET", url, params=params, req_auth=True)
            result = self.api.parse_result(r)
            if result and getattr(result, "illusts", None):
                logger.debug(f"关键词「{keyword}」popular_desc 搜索成功")
                return result
        except Exception as e:
            logger.debug(f"popular_desc 搜索失败: {e}")

        # 回退 date_desc + bookmark_num_min
        params["sort"] = "date_desc"
        r = self.api.no_auth_requests_call("GET", url, params=params, req_auth=True)
        return self.api.parse_result(r)

    def _fetch_ids_sync(self, keyword: str, count: int, is_dm: bool, sid: str) -> List[str]:
        if not self.api:
            raise RuntimeError("API 未初始化")

        all_illusts = []
        next_url = None

        for page in range(self.max_search_pages):
            try:
                if page == 0:
                    result = self._search_illust_advanced_sync(keyword)
                else:
                    result = self.api.parse_qs(next_url)
            except Exception as e:
                logger.error(f"search_illust 请求失败: {e}")
                raise

            if result is None:
                logger.error("search_illust 返回 None")
                break

            if isinstance(result, dict) and result.get("error"):
                logger.error(f"API 返回错误: {result['error']}")
                break

            if hasattr(result, "error") and result.error:
                logger.error(f"API 返回错误: {result.error}")
                break

            illusts = getattr(result, "illusts", None) or []
            if not illusts:
                break

            all_illusts.extend(illusts)
            next_url = getattr(result, "next_url", None)
            if not next_url:
                break

        if not all_illusts:
            logger.warning(f"关键词「{keyword}」未返回作品")
            return []

        # 本地按收藏数降序
        all_illusts.sort(key=lambda x: x.total_bookmarks, reverse=True)

        ids: List[str] = []
        for illust in all_illusts:
            if self.min_bookmarks > 0 and illust.total_bookmarks < self.min_bookmarks:
                continue

            # R-18 / R-18G 过滤：私聊需开关+白名单；群聊仅白名单
            x_restrict = getattr(illust, "x_restrict", 0)
            if not self._is_r18_allowed(is_dm, sid, x_restrict):
                continue

            effective_exclude = self._effective_exclude_tags(is_dm, sid)
            tags = [tag.name for tag in getattr(illust, "tags", [])]
            if any(exclude in tags for exclude in effective_exclude):
                continue

            illust_id = str(illust.id)
            if self._is_sent(illust_id):
                continue

            ids.append(illust_id)
            if len(ids) >= count:
                break

        return ids

    async def _fetch_ids(self, keyword: str, count: int, is_dm: bool, sid: str) -> List[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_ids_sync, keyword, count, is_dm, sid)

    # ------------------------------------------------------------------
    # 下载 Pixiv 图片到配置目录（使用 pixivpy 自带 download，自动代理+Referer）
    # ------------------------------------------------------------------
    def _download_image_sync(self, img_url: str, illust_id: str) -> str | None:
        if not self.api:
            return None

        storage_dir = self._get_storage_dir()
        storage_dir.mkdir(parents=True, exist_ok=True)
        ext = Path(img_url).suffix or ".jpg"
        if ext not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
            ext = ".jpg"
        local_path = storage_dir / f"{illust_id}{ext}"

        try:
            logger.debug(f"开始下载图片 {illust_id}: {img_url}")
            ok = self.api.download(
                img_url,
                path=str(storage_dir),
                name=f"{illust_id}{ext}",
                replace=True,
                referer="https://app-api.pixiv.net/",
            )
            if ok and local_path.exists() and local_path.stat().st_size > 0:
                return str(local_path)
            logger.warning(f"下载图片 {illust_id} 失败或文件为空")
            return None
        except Exception as e:
            logger.warning(f"下载图片 {illust_id} 异常: {e}")
            return None

    async def _download_image(self, img_url: str, illust_id: str) -> str | None:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._download_image_sync, img_url, illust_id)

    # ------------------------------------------------------------------
    # 直接发送图片到当前会话（不经过 LLM）
    # ------------------------------------------------------------------
    async def _send_image_directly(self, event: KiraMessageBatchEvent, local_path: str) -> bool:
        """使用 MessageProcessor.send_message_chain 直接推送图片，不等待 LLM。"""
        try:
            session_id = getattr(event, "sid", None)
            if not session_id:
                last_msg = event.messages[-1]
                adapter = event.adapter.name if event.adapter else "qq"
                if hasattr(last_msg, "group") and last_msg.group:
                    session_id = f"{adapter}:gm:{last_msg.group.group_id}"
                else:
                    session_id = f"{adapter}:dm:{last_msg.sender.user_id}"

            # caption 设为空字符串，避免 echoed 图片被 VLM 重复识别描述
            img = Image(local_path, caption="")
            chain = MessageChain([img])
            result = await self.ctx.message_processor.send_message_chain(session_id, chain)
            return result.ok
        except Exception as e:
            logger.error(f"直接发送图片失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 合并转发发送图片（参考 ThreePartFormatPlugin 调用 QQ 客户端 API）
    # ------------------------------------------------------------------
    async def _send_forward_images(self, event: KiraMessageBatchEvent, local_paths: List[str]) -> bool:
        """将多张图片以合并转发消息发送。即使只有 1 张也发转发。"""
        if not local_paths:
            return False
        try:
            session_id = getattr(event, "sid", None)
            if not session_id:
                last_msg = event.messages[-1]
                adapter = event.adapter.name if event.adapter else "qq"
                if hasattr(last_msg, "group") and last_msg.group:
                    session_id = f"{adapter}:gm:{last_msg.group.group_id}"
                else:
                    session_id = f"{adapter}:dm:{last_msg.sender.user_id}"

            parts = session_id.split(":")
            if len(parts) != 3:
                raise ValueError(f"无效 session_id: {session_id}")
            adapter_name, session_type, target_id = parts

            adapter_inst = self.ctx.adapter_mgr.get_adapter(adapter_name)
            if not adapter_inst:
                raise ValueError(f"无法获取适配器: {adapter_name}")
            client = adapter_inst.get_client()
            if not client:
                raise ValueError("无法获取 QQ 客户端")

            self_id = str(event.self_id) if hasattr(event, "self_id") else "0"
            bot_nick = getattr(adapter_inst.info, "name", adapter_name)

            nodes = []
            for path in local_paths:
                abs_path = str(Path(path).resolve())
                nodes.append({
                    "type": "node",
                    "data": {
                        "name": bot_nick,
                        "uin": self_id,
                        "content": [{"type": "image", "data": {"file": abs_path}}],
                    },
                })

            if session_type == "gm":
                await client.send_action("send_forward_msg", {
                    "group_id": int(target_id),
                    "messages": nodes,
                })
            else:
                await client.send_action("send_forward_msg", {
                    "user_id": int(target_id),
                    "messages": nodes,
                })
            return True
        except Exception as e:
            logger.error(f"合并转发发送失败: {e}")
            return False

    # ------------------------------------------------------------------
    # 选择图片 URL：原图 or 预览图
    # ------------------------------------------------------------------
    def _get_image_url(self, illust, use_original: bool) -> str | None:
        """根据 use_original 选择原图或预览图 URL。"""
        image_urls = getattr(illust, "image_urls", {}) or {}

        if use_original:
            # 单图作品
            meta_single = getattr(illust, "meta_single_page", {}) or {}
            original = meta_single.get("original_image_url")
            if original:
                return original
            # 多图作品第一张
            meta_pages = getattr(illust, "meta_pages", []) or []
            if meta_pages:
                page0 = meta_pages[0].get("image_urls", {})
                if page0.get("original"):
                    return page0["original"]
            # 兜底 large
            return image_urls.get("large")

        # 预览模式：优先 large，其次 medium
        return image_urls.get("large") or image_urls.get("medium")

    @staticmethod
    def _is_pixiv_id(s: str) -> bool:
        """判断字符串是否为 Pixiv 作品 ID（纯数字，长度 6-12 位）。"""
        return s.isdigit() and 6 <= len(s) <= 12

    async def _get_illust_path_by_id(self, illust_id: str, use_original: bool, is_dm: bool, sid: str) -> tuple[bool, str]:
        """根据作品 ID 下载图片。返回 (是否成功, 本地路径或错误信息)。"""
        try:
            detail = await asyncio.get_running_loop().run_in_executor(
                None, self.api.illust_detail, illust_id
            )
            if not detail or not hasattr(detail, "illust"):
                return False, f"未找到作品 {illust_id}"

            illust = detail.illust

            # R-18 / R-18G 权限检查（作品 ID 直查也要受白名单约束）
            x_restrict = getattr(illust, "x_restrict", 0)
            if not self._is_r18_allowed(is_dm, sid, x_restrict):
                content_label = "R-18" if x_restrict == 1 else "R-18G"
                return False, f"作品 {illust_id} 为 {content_label} 内容，当前会话不在白名单或未开启私聊开关"

            img_url = self._get_image_url(illust, use_original)
            if not img_url:
                return False, f"无法获取作品 {illust_id} 的图片 URL"

            local_path = await self._download_image(img_url, illust_id)
            if not local_path:
                return False, f"下载作品 {illust_id} 失败"

            return True, local_path
        except Exception as e:
            logger.error(f"处理作品 {illust_id} 失败: {e}")
            return False, str(e)
    @tool(
        "pixiv_search_and_send",
        "从 Pixiv 搜索高人气图片（按收藏数排序）并直接发送到当前聊天。支持过滤低收藏数和排除标签。默认发送预览图，可指定原图。",
        {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词，支持中文，会自动转换为 Pixiv 常用 tag"},
                "count": {"type": "integer", "default": 1, "description": "要获取的图片张数"},
                "original": {"type": "boolean", "default": False, "description": "是否发送原图（默认预览图）"},
            },
            "required": ["keyword"],
        },
    )
    async def pixiv_search_and_send(self, event: KiraMessageBatchEvent, keyword: str, count: int = 1, original: bool = False) -> str:
        if not self.api:
            return "未初始化"
        if not keyword or not keyword.strip():
            return "无关键词"

        count = min(max(1, int(count)), self.max_count)
        use_original = bool(original) or self.download_original
        is_dm = self._is_dm(event)
        sid = getattr(event, "sid", None)
        if not sid:
            last_msg = event.messages[-1]
            adapter = event.adapter.name if event.adapter else "qq"
            if hasattr(last_msg, "group") and last_msg.group:
                sid = f"{adapter}:gm:{last_msg.group.group_id}"
            else:
                sid = f"{adapter}:dm:{last_msg.sender.user_id}"

        # 每次调用时检查并清理旧缓存
        self._cleanup_storage()

        # 转换关键词
        search_keyword = self._normalize_keyword(keyword)
        if search_keyword != keyword:
            logger.info(f"关键词转换：{keyword} -> {search_keyword}")

        # 如果关键词是纯数字 Pixiv ID，直接获取该作品
        if self._is_pixiv_id(search_keyword):
            logger.info(f"检测到 Pixiv 作品 ID：{search_keyword}")
            ok, info = await self._get_illust_path_by_id(search_keyword, use_original, is_dm, sid)
            if not ok:
                return f"发送失败：{info}"
            local_path = info

            if self.send_as_forward:
                send_ok = await self._send_forward_images(event, [local_path])
            else:
                send_ok = await self._send_image_directly(event, local_path)

            if not send_ok:
                return "发送失败"

            self._mark_sent(search_keyword)
            mode_text = "原图" if use_original else "预览图"
            rel = Path(local_path).relative_to(Path(get_data_path()))
            return (
                f"已成功发送 1 张{mode_text}（作品 ID {search_keyword}）到当前聊天。\n"
                f"图片已由工具直接发送完毕，你无需再次发送。\n"
                f"发送的文件：data/{rel.as_posix()}"
            )

        try:
            ids = await self._fetch_ids(search_keyword, count, is_dm, sid)
        except Exception:
            logger.exception("Pixiv 搜索失败")
            return "搜索失败"

        if not ids:
            return "无结果"

        # 先下载所有图片
        pending: List[tuple[str, str]] = []  # (wid, local_path)
        for wid in ids[:count]:
            try:
                detail = await asyncio.get_running_loop().run_in_executor(
                    None, self.api.illust_detail, wid
                )
                if not detail or not hasattr(detail, "illust"):
                    continue

                illust = detail.illust
                img_url = self._get_image_url(illust, use_original)
                if not img_url:
                    continue

                local_path = await self._download_image(img_url, wid)
                if not local_path:
                    continue

                pending.append((wid, local_path))
            except Exception as e:
                logger.error(f"处理图片 {wid} 失败: {e}")

        if not pending:
            return "发送失败"

        # 统一发送：合并转发 或 逐条发送
        local_paths = [p[1] for p in pending]
        if self.send_as_forward:
            send_ok = await self._send_forward_images(event, local_paths)
            if not send_ok:
                # 转发失败时回退逐条发送
                send_ok = True
                for local_path in local_paths:
                    if not await self._send_image_directly(event, local_path):
                        send_ok = False
        else:
            send_ok = True
            for local_path in local_paths:
                if not await self._send_image_directly(event, local_path):
                    send_ok = False

        if not send_ok:
            return "发送失败"

        sent_ids = [p[0] for p in pending]
        for wid in sent_ids:
            self._mark_sent(wid)

        sent_paths = [
            f"data/{Path(p[1]).relative_to(Path(get_data_path())).as_posix()}"
            for p in pending
        ]
        mode_text = "原图" if use_original else "预览图"
        forward_text = "合并转发" if self.send_as_forward else "单条"
        return (
            f"已成功以{forward_text}形式发送 {len(sent_ids)} 张{mode_text}到当前聊天。\n"
            f"这些图片已由工具直接发送完毕，你无需再次发送，也无需使用 <file> 标签。\n"
            f"只需简单回应用户即可。\n"
            f"发送的文件：{', '.join(sent_paths)}"
        )