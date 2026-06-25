import asyncio
import json
import time
from pathlib import Path
from typing import List, Callable

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
        # 新增配置
        self.enable_title_search = cfg.get("enable_title_search", True)
        self.search_priority = cfg.get("search_priority", "tags")  # "tags" 或 "title"
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
        """初始化 API 并验证 token，返回是否成功。"""
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

    # ---------- 判断是否为认证错误 ----------
    @staticmethod
    def _is_auth_error_from_result(result) -> bool:
        """检查 API 返回结果是否包含认证错误。"""
        if result is None:
            return False
        error_msg = None
        if isinstance(result, dict) and result.get("error"):
            error_msg = result["error"]
        elif hasattr(result, "error") and result.error:
            error_msg = result.error
        if error_msg:
            if isinstance(error_msg, dict):
                error_text = error_msg.get("message", "") or str(error_msg)
            else:
                error_text = str(error_msg)
            return "invalid_grant" in error_text.lower()
        return False

    @staticmethod
    def _is_auth_exception(exc: Exception) -> bool:
        """判断异常是否为认证相关错误。"""
        msg = str(exc).lower()
        keywords = ["invalid_grant", "oauth", "access token", "authentication", "unauthorized"]
        return any(k in msg for k in keywords)

    # ---------- 自动重试（封装 API 调用） ----------
    def _call_with_reauth(self, func: Callable, *args, **kwargs):
        """执行 func，如果发生认证错误则重新认证并重试一次。"""
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if not self._is_auth_exception(e):
                raise
            logger.warning(f"[PixivPlugin] 捕获认证异常: {e}，尝试重新认证...")
            if not self._init_api():
                logger.error("[PixivPlugin] 重新认证失败，放弃请求")
                raise
            logger.info("[PixivPlugin] 重新认证成功，重试请求")
            return func(*args, **kwargs)

    # ---------- 原有业务方法 ----------
    async def terminate(self):
        self.api = None

    def _normalize_keyword(self, keyword: str) -> str:
        keyword = keyword.strip()
        if keyword in self.keyword_map:
            return self.keyword_map[keyword]
        parts = []
        for part in keyword.split():
            parts.append(self.keyword_map.get(part, part))
        return " ".join(parts)

    @staticmethod
    def _is_dm(event: KiraMessageBatchEvent) -> bool:
        return not event.is_group_message()

    @staticmethod
    def _filter_whitelist_by_type(whitelist: List[str], session_type: str) -> List[str]:
        marker = f":{session_type}:"
        return [entry for entry in whitelist if marker in entry]

    def _is_content_allowed(self, is_dm: bool, sid: str, content_type: str) -> bool:
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
        if x_restrict <= 0:
            return True
        content_type = "r18" if x_restrict == 1 else "r18g"
        return self._is_content_allowed(is_dm, sid, content_type)

    def _effective_exclude_tags(self, is_dm: bool, sid: str) -> List[str]:
        tags = list(self.exclude_tags)
        if self._is_content_allowed(is_dm, sid, "r18"):
            tags = [t for t in tags if t != "R-18"]
        if self._is_content_allowed(is_dm, sid, "r18g"):
            tags = [t for t in tags if t != "R-18G"]
        return tags

    # ---------- 持久化去重 ----------
    def _load_sent_ids(self) -> None:
        try:
            if self.dedup_file.exists():
                data = json.loads(self.dedup_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._sent_ids = {str(k): float(v) for k, v in data.items()}
                    self._expire_sent_ids()
                    logger.info(f"已加载 {len(self._sent_ids)} 条有效发送记录")
                elif isinstance(data, list):
                    now = time.time()
                    self._sent_ids = {str(x): now for x in data}
                    logger.info(f"已加载 {len(self._sent_ids)} 条发送记录（旧格式）")
        except Exception as e:
            logger.warning(f"加载去重记录失败: {e}")
            self._sent_ids = {}

    def _expire_sent_ids(self) -> None:
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

    # ---------- 图片缓存 ----------
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
            to_delete = files[:self.cleanup_count]
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

    # ---------- 高级搜索（会检查返回错误） ----------
    def _search_illust_advanced_sync(self, keyword: str, offset: int | None = None, search_target: str = "partial_match_for_tags"):
        url = "%s/v1/search/illust" % self.api.hosts
        params = {
            "word": keyword,
            "search_target": search_target,
            "filter": "for_ios",
        }
        if offset:
            params["offset"] = offset
        if self.min_bookmarks > 0:
            params["bookmark_num_min"] = self.min_bookmarks

        # 先尝试 popular_desc
        try:
            params["sort"] = "popular_desc"
            r = self._call_with_reauth(self.api.no_auth_requests_call, "GET", url, params=params, req_auth=True)
            result = self.api.parse_result(r)
            if self._is_auth_error_from_result(result):
                logger.warning("检测到认证错误（popular_desc），将抛出让上层处理")
                return result
            if result and getattr(result, "illusts", None):
                logger.debug(f"关键词「{keyword}」popular_desc 搜索成功")
                return result
        except Exception as e:
            logger.debug(f"popular_desc 搜索失败: {e}")

        # 回退 date_desc + bookmark_num_min
        params["sort"] = "date_desc"
        r = self._call_with_reauth(self.api.no_auth_requests_call, "GET", url, params=params, req_auth=True)
        result = self.api.parse_result(r)
        return result

    # ---------- 核心搜索与过滤（整合回退逻辑） ----------
    def _do_search_and_collect(self, keyword: str, search_target: str) -> List:
        """执行一次完整的搜索（含翻页），返回 illust 列表"""
        all_illusts = []
        next_url = None
        for page in range(self.max_search_pages):
            try:
                if page == 0:
                    result = self._search_illust_advanced_sync(keyword, search_target=search_target)
                else:
                    result = self.api.parse_qs(next_url)
            except Exception as e:
                logger.error(f"search_illust 请求失败: {e}")
                raise

            if result is None:
                break
            if self._is_auth_error_from_result(result):
                # 认证错误直接抛出，由外层重试
                raise RuntimeError("Authentication error in search response")
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
        return all_illusts

    def _filter_and_sort_illusts(self, illusts: List, count: int, is_dm: bool, sid: str) -> List[str]:
        """排序、过滤、去重，返回 ID 列表"""
        if not illusts:
            return []

        illusts.sort(key=lambda x: x.total_bookmarks, reverse=True)

        ids: List[str] = []
        for illust in illusts:
            if self.min_bookmarks > 0 and illust.total_bookmarks < self.min_bookmarks:
                continue
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

    def _fetch_ids_sync(self, keyword: str, count: int, is_dm: bool, sid: str) -> List[str]:
        if not self.api:
            raise RuntimeError("API 未初始化")

        # 如果关闭标题搜索，只搜一次标签
        if not self.enable_title_search:
            illusts = self._do_search_and_collect(keyword, "partial_match_for_tags")
            if not illusts:
                return []
            return self._filter_and_sort_illusts(illusts, count, is_dm, sid)

        # 开启标题搜索，根据优先级决定顺序
        priority = self.search_priority
        if priority == "tags":
            primary_target = "partial_match_for_tags"
            fallback_target = "title_and_caption"
        else:  # "title"
            primary_target = "title_and_caption"
            fallback_target = "partial_match_for_tags"

        logger.info(f"优先搜索模式: {primary_target}")
        illusts = self._do_search_and_collect(keyword, primary_target)

        # 如果优先搜索无结果，执行回退搜索
        if not illusts:
            logger.info(f"优先搜索无结果，回退到: {fallback_target}")
            illusts = self._do_search_and_collect(keyword, fallback_target)

        if not illusts:
            return []

        return self._filter_and_sort_illusts(illusts, count, is_dm, sid)

    async def _fetch_ids(self, keyword: str, count: int, is_dm: bool, sid: str) -> List[str]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._fetch_ids_sync, keyword, count, is_dm, sid)

    # ---------- 下载图片 ----------
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
            ok = self._call_with_reauth(
                self.api.download,
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

    # ---------- 直接发送 ----------
    async def _send_image_directly(self, event: KiraMessageBatchEvent, local_path: str) -> bool:
        try:
            session_id = getattr(event, "sid", None)
            if not session_id:
                last_msg = event.messages[-1]
                adapter = event.adapter.name if event.adapter else "qq"
                if hasattr(last_msg, "group") and last_msg.group:
                    session_id = f"{adapter}:gm:{last_msg.group.group_id}"
                else:
                    session_id = f"{adapter}:dm:{last_msg.sender.user_id}"

            img = Image(local_path, caption="")
            chain = MessageChain([img])
            result = await self.ctx.message_processor.send_message_chain(session_id, chain)
            return result.ok
        except Exception as e:
            logger.error(f"直接发送图片失败: {e}")
            return False

    async def _send_forward_images(self, event: KiraMessageBatchEvent, local_paths: List[str]) -> bool:
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

    # ---------- 选择 URL ----------
    def _get_image_url(self, illust, use_original: bool) -> str | None:
        image_urls = getattr(illust, "image_urls", {}) or {}

        if use_original:
            meta_single = getattr(illust, "meta_single_page", {}) or {}
            original = meta_single.get("original_image_url")
            if original:
                return original
            meta_pages = getattr(illust, "meta_pages", []) or []
            if meta_pages:
                page0 = meta_pages[0].get("image_urls", {})
                if page0.get("original"):
                    return page0["original"]
            return image_urls.get("large")

        return image_urls.get("large") or image_urls.get("medium")

    @staticmethod
    def _is_pixiv_id(s: str) -> bool:
        return s.isdigit() and 6 <= len(s) <= 12

    # ---------- 根据 ID 获取图片 ----------
    async def _get_illust_path_by_id(self, illust_id: str, use_original: bool, is_dm: bool, sid: str) -> tuple[bool, str]:
        try:
            detail = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: self._call_with_reauth(self.api.illust_detail, illust_id)
            )
            if not detail or not hasattr(detail, "illust"):
                return False, f"未找到作品 {illust_id}"

            illust = detail.illust
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

    # ---------- 工具注册 ----------
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
        # ---- 如果 API 未初始化，先尝试初始化 ----
        if not self.api and not self._init_api():
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

        self._cleanup_storage()
        search_keyword = self._normalize_keyword(keyword)
        if search_keyword != keyword:
            logger.info(f"关键词转换：{keyword} -> {search_keyword}")

        # ---- 纯数字 ID 处理（带重试） ----
        if self._is_pixiv_id(search_keyword):
            logger.info(f"检测到 Pixiv 作品 ID：{search_keyword}")
            max_retries = 2
            for attempt in range(max_retries):
                ok, info = await self._get_illust_path_by_id(search_keyword, use_original, is_dm, sid)
                if ok:
                    local_path = info
                    if self.send_as_forward:
                        send_ok = await self._send_forward_images(event, [local_path])
                    else:
                        send_ok = await self._send_image_directly(event, local_path)
                    if send_ok:
                        self._mark_sent(search_keyword)
                        mode_text = "原图" if use_original else "预览图"
                        rel = Path(local_path).relative_to(Path(get_data_path()))
                        return (
                            f"已成功发送 1 张{mode_text}（作品 ID {search_keyword}）到当前聊天。\n"
                            f"图片已由工具直接发送完毕，你无需再次发送。\n"
                            f"发送的文件：data/{rel.as_posix()}"
                        )
                    else:
                        return "发送失败"
                else:
                    if "invalid_grant" in info.lower() or "oauth" in info.lower():
                        if attempt < max_retries - 1:
                            logger.warning(f"获取作品 ID {search_keyword} 时认证错误，尝试重新认证...")
                            if self._init_api():
                                logger.info("重新认证成功，重试")
                                continue
                            else:
                                logger.error("重新认证失败")
                    return f"发送失败：{info}"
            return f"发送失败：重试后仍失败"

        # ---- 搜索模式（带重试） ----
        max_retries = 2
        last_error = None
        for attempt in range(max_retries):
            try:
                ids = await self._fetch_ids(search_keyword, count, is_dm, sid)
                if ids:
                    break
                else:
                    return f"无结果（关键词：{search_keyword}）"
            except Exception as e:
                last_error = e
                if self._is_auth_exception(e) and attempt < max_retries - 1:
                    logger.warning(f"搜索时发生认证错误: {e}，尝试重新认证...")
                    if self._init_api():
                        logger.info("重新认证成功，重试搜索")
                        continue
                    else:
                        logger.error("重新认证失败")
                        break
                else:
                    break
        else:
            if last_error:
                return f"搜索失败：{str(last_error)}"
            return "搜索失败"

        # ---- 下载并发送图片 ----
        pending: List[tuple[str, str]] = []  # (wid, local_path)
        for wid in ids[:count]:
            try:
                detail = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self._call_with_reauth(self.api.illust_detail, wid)
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
            return "发送失败（下载图片失败）"

        local_paths = [p[1] for p in pending]
        if self.send_as_forward:
            send_ok = await self._send_forward_images(event, local_paths)
            if not send_ok:
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
            return "发送失败（QQ 发送失败）"

        for wid, _ in pending:
            self._mark_sent(wid)

        sent_paths = [
            f"data/{Path(p[1]).relative_to(Path(get_data_path())).as_posix()}"
            for p in pending
        ]
        mode_text = "原图" if use_original else "预览图"
        forward_text = "合并转发" if self.send_as_forward else "单条"
        return (
            f"已成功以{forward_text}形式发送 {len(pending)} 张{mode_text}到当前聊天。\n"
            f"这些图片已由工具直接发送完毕，你无需再次发送，也无需使用 <file> 标签。\n"
            f"只需简单回应用户即可。\n"
            f"发送的文件：{', '.join(sent_paths)}"
        )
