# -*- coding: utf-8 -*-
"""大肥鱼 GitHub Release 加速下载插件

链路：用户发 GitHub Release 链接
  → 服务器 aria2 多线程加速下载到插件数据目录
  → 起临时 HTTP 文件服务，把 URL 交给 SnowLuma 上传并回传文件
  → 确认发送后自动删除服务器上的下载残留
"""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import socket
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

import aiohttp
from aiohttp import web

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_ghfetch"

try:  # 配置页 Web API（AstrBot >= 4.24 支持插件自带页面与自定义接口）
    from .web_api import register_routes as _register_web_routes
except Exception:  # pragma: no cover
    _register_web_routes = None
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AstrBot-GHFetch/1.0"

RE_DIRECT = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/"
    r"[^\s\"'<>，。；）】\]]+"
)
RE_TAG = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases/tag/"
    r"([^\s\"'<>/?#，。；）】]+)"
)
RE_REPO = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/releases(?:/latest)?/?(?:[?#]\S*)?"
)
# 纯仓库主页链接：https://github.com/owner/repo（可带 .git / 查询串），其后不得再接子路径
RE_REPO_PLAIN = re.compile(
    r"https?://(?:www\.)?github\.com/"
    r"([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*?)"
    r"/?(?:[?#][^\s]*)?(?=$|[\s，。；）】、!！?？\"'<>])"
)

# 「下载 项目名」这类按名索取的指令
RE_NAME_CMD = re.compile(
    r"^(?:下载|下个|gh下载|ghdl|ghfetch|/dl|dl|gh)[\s:：]+(.{1,80})$", re.I
)
RE_NAME_CMD_TIGHT = re.compile(
    r"^(?:下载|gh下载|ghdl|/dl)[\s:：]*([A-Za-z0-9][A-Za-z0-9._/-]{0,79})$", re.I
)
RE_OWNER_REPO = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)$")

PLAT_PATTERNS = {
    "win": re.compile(r"(?<!dar)win|windows|\.exe$|\.msi$", re.I),
    "linux": re.compile(r"linux|musl|\.deb$|\.rpm$|appimage", re.I),
    "mac": re.compile(r"macos|darwin|osx|\.dmg$|(^|[_\-.])mac([_\-.]|$)", re.I),
    "android": re.compile(r"android|\.apk$", re.I),
}
ARCH_PATTERNS = {
    "arm64": re.compile(r"arm64|aarch64", re.I),
    "386": re.compile(r"i386|i686|386|32bit", re.I),
    "amd64": re.compile(r"x86_64|amd64|x64|64bit", re.I),
}
RE_JUNK_ASSET = re.compile(
    r"\.(sha\d*|sig|asc|md5|txt|json|ya?ml|sbom|intoto)$|checksums?|sha\d*sums?|\.sig$|\.asc$", re.I
)
DEFAULT_ALIASES = {
    "vscode": "microsoft/vscode",
    "gh": "cli/cli",
    "githubcli": "cli/cli",
    "7zip": "ip7z/7zip",
    "ffmpeg": "BtbN/FFmpeg-Builds",
    "aria2": "aria2/aria2",
    "clash": "MetaCubeX/mihomo",
    "mihomo": "MetaCubeX/mihomo",
    "singbox": "SagerNet/sing-box",
    "frp": "fatedier/frp",
    "alist": "AlistGo/alist",
    "neovim": "neovim/neovim",
}


def _fmt_size(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f%s" % (n, unit)
        n /= 1024
    return "%.1fTB" % n


@register(
    PLUGIN_NAME,
    "Marvis",
    "GitHub Release 加速下载：发链接或项目名 → 服务器 aria2 多线程下载 → 回传文件 → 自动清理残留",
    "1.1.1",
)
class GhFetchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        self.config = config if isinstance(config, dict) else {}

        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.dl_root = self.data_dir / "dl"
        self.dl_root.mkdir(parents=True, exist_ok=True)

        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._http_port = int(self._cfg("http_port", 8899))
        self._files: dict[str, Path] = {}
        self._last_hit: dict[str, float] = {}
        self._pending: dict[str, dict] = {}
        self._info_cache: dict[str, tuple[float, dict]] = {}
        self._intro_cache: dict[str, tuple[float, str]] = {}
        self._sem = asyncio.Semaphore(max(1, int(self._cfg("max_concurrent", 2))))
        self._busy: set[str] = set()

        logger.info("[ghfetch] 数据目录 %s，aria2=%s" % (self.data_dir, self._find_aria2()))

        try:
            if _register_web_routes is not None:
                _register_web_routes(
                    self.context, self, Path(__file__).resolve().parent
                )
        except Exception as e:  # pragma: no cover
            logger.warning("[ghfetch] 注册配置页 API 失败：%s" % e)

        asyncio.create_task(self._boot())

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default):
        v = self.config.get(key, default)
        if v is None:
            return default
        return v

    def _msg(self, text: str) -> MessageChain:
        return MessageChain([Plain(text)])

    # ---------------- 启动 ----------------
    async def _boot(self):
        try:
            self._find_aria2()
            if not self._cfg("enabled", True):
                return
            self._sweep_leftovers()
            await self._ensure_http()
        except Exception as e:  # pragma: no cover
            logger.warning("[ghfetch] 启动异常：%s" % e)

    def _find_aria2(self) -> str | None:
        cand = str(self._cfg("aria2_path", "aria2c") or "aria2c")
        for p in (cand, "/usr/bin/aria2c", "/usr/local/bin/aria2c"):
            if p and Path(p).exists():
                return p
        for d in ("/usr/bin", "/usr/local/bin", "/bin"):
            p = Path(d) / "aria2c"
            if p.exists():
                return str(p)
        return None

    def _sweep_leftovers(self):
        """清掉上次运行留下的下载残骸（超过 30 分钟的）。"""
        try:
            now = time.time()
            for d in self.dl_root.iterdir():
                if d.is_dir() and now - d.stat().st_mtime > 1800:
                    shutil.rmtree(d, ignore_errors=True)
        except Exception as e:
            logger.debug("[ghfetch] 清理残留失败：%s" % e)

    # ---------------- HTTP 文件服务 ----------------
    async def _ensure_http(self):
        if self._runner is not None:
            return
        app = web.Application(client_max_size=1024 * 1024)
        app.router.add_get("/f/{token}/{name}", self._handle_file)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        base = self._http_port
        last_err = None
        for offset in range(0, 20):
            port = base + offset
            try:
                site = web.TCPSite(runner, "0.0.0.0", port)
                await site.start()
                self._runner, self._site = runner, site
                self._http_port = port
                logger.info("[ghfetch] 文件服务监听 0.0.0.0:%d" % port)
                return
            except OSError as e:
                last_err = e
                continue
        await runner.cleanup()
        raise RuntimeError("回传端口全部被占用：%s" % last_err)

    async def _handle_file(self, request: web.Request) -> web.StreamResponse:
        token = request.match_info.get("token", "")
        p = self._files.get(token)
        if not p or not Path(p).exists():
            return web.Response(status=404, text="not found")
        self._last_hit[token] = time.time()
        logger.info("[ghfetch] 回传命中 %s（%s）" % (p.name, request.remote))
        return web.FileResponse(p)

    def _local_ip(self) -> str:
        host = str(self._cfg("callback_host", "") or "").strip()
        if host:
            return host
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.connect(("172.17.0.1", 80))
                return s.getsockname()[0]
            finally:
                s.close()
        except Exception:
            try:
                return socket.gethostbyname(socket.gethostname())
            except Exception:
                return "127.0.0.1"

    # ---------------- 消息入口 ----------------
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_any_message(self, event: AstrMessageEvent):
        if not self._cfg("enabled", True):
            return
        text = (event.get_message_str() or "").strip()
        if not text:
            return

        key = self._session_key(event)
        pend = self._pending.get(key)
        if pend:
            if time.time() - pend["ts"] > float(self._cfg("pending_ttl", 180)):
                self._pending.pop(key, None)
            elif pend.get("kind") == "source":
                self._pending.pop(key, None)
                event.stop_event()
                if re.fullmatch(
                    r"(y|yes|ok|1|好|好滴|是|是的|要|要的|需要|下|下载|嗯|行|可以|确认|来吧)",
                    text.strip().lower(),
                ):
                    await self._download_and_send(
                        event, str(pend.get("url") or ""), str(pend.get("fname") or "source.zip"), 0
                    )
                else:
                    await event.send(self._msg("行，源码包不下了"))
                return
            elif text in ("0", "取消", "cancel", "/cancel"):
                self._pending.pop(key, None)
                await event.send(self._msg("已取消本次选择"))
                event.stop_event()
                return
            elif pend.get("kind") == "assets" and pend.get("all") and self._plat_word(text):
                plat = self._plat_word(text)
                event.stop_event()
                head = str(pend.get("head") or "这个 Release")
                txt = self._asset_menu(event, head, list(pend.get("all") or []), plat)
                if not txt:
                    self._pending.pop(key, None)
                    await event.send(self._msg("%s 里没有 %s 的文件" % (head, plat)))
                    return
                await event.send(self._msg(txt))
                return
            elif text.isdigit():
                idx = int(text) - 1
                items = pend.get("items") or pend.get("assets") or []
                kind = pend.get("kind", "assets")
                self._pending.pop(key, None)
                event.stop_event()
                if not (0 <= idx < len(items)):
                    await event.send(self._msg("序号超范围，已取消"))
                    return
                if kind == "repos":
                    await self._release_of_repo(
                        event, str(items[idx].get("full_name") or ""), pend.get("plat"), pend.get("arch")
                    )
                else:
                    a = items[idx]
                    await self._download_and_send(
                        event, a["browser_download_url"], a["name"], int(a.get("size") or 0)
                    )
                return

        if not self._chat_allowed(event):
            return

        m = RE_DIRECT.search(text)
        if m:
            url = m.group(0).rstrip(".,);:，。；）】")
            name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download.bin"
            event.stop_event()
            await self._download_and_send(event, url, name, 0)
            return

        m = RE_TAG.search(text)
        if m:
            event.stop_event()
            await self._resolve_release(event, m.group(1), m.group(2), m.group(3))
            return

        m = RE_REPO.search(text)
        if m:
            event.stop_event()
            await self._resolve_release(event, m.group(1), m.group(2), None)
            return

        m = RE_REPO_PLAIN.search(text)
        if m:
            event.stop_event()
            owner, repo = m.group(1), m.group(2)
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            # 先把这个仓库的简介 + 一句话总结发过去，再照常走 Release 流程
            if self._cfg("repo_intro", True):
                try:
                    intro = await self._repo_intro(owner, repo)
                    if intro:
                        await event.send(self._msg(intro))
                except Exception as e:
                    logger.debug("[ghfetch] 简介发送失败：%s" % e)
            await self._resolve_release(event, owner, repo, None, allow_source=True)
            return

        plain = re.sub(r"^(?:\s*\[CQ:at,[^\]]*\]|\s*@\S+\s*)+", "", text).strip()
        m = RE_NAME_CMD.match(plain) or RE_NAME_CMD_TIGHT.match(plain)
        if m:
            event.stop_event()
            await self._resolve_by_name(event, m.group(1).strip())

    def _session_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or event.get_sender_id())

    def _chat_allowed(self, event: AstrMessageEvent) -> bool:
        group = event.get_group_id()
        if group:
            if not self._cfg("allow_group", True):
                return False
            wl = self._cfg("whitelist_groups", []) or []
            if wl and str(group) not in [str(x) for x in wl]:
                return False
        else:
            if not self._cfg("allow_private", True):
                return False
        return True

    # ---------------- 仓库简介 ----------------
    async def _repo_info(self, owner: str, repo: str) -> dict | None:
        """取仓库基础信息，带缓存省 GitHub API 配额"""
        full = "%s/%s" % (owner, repo)
        ttl = float(self._cfg("info_cache_ttl", 900))
        hit = self._info_cache.get(full.lower())
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        info = await self._gh_api("/repos/%s" % full)
        if isinstance(info, dict) and not info.get("_error"):
            self._info_cache[full.lower()] = (time.time(), info)
        return info if isinstance(info, dict) else None

    async def _readme_snippet(self, owner: str, repo: str, limit: int = 1500) -> str:
        """抓 README 并洗成纯文字，喂给模型做总结用"""
        data = await self._gh_api("/repos/%s/%s/readme" % (owner, repo))
        if not isinstance(data, dict) or data.get("_error"):
            return ""
        try:
            raw = base64.b64decode(str(data.get("content") or ""), validate=False).decode("utf-8", "ignore")
        except Exception:
            return ""
        raw = raw.replace("\r", "")
        lines: list[str] = []
        total = 0
        for ln in raw.split("\n"):
            s = ln.strip()
            if not s or s.startswith("<!--") or s.startswith("```"):
                continue
            if re.match(r"^!\[|^\[!\w+\]|^<|^[-=]{3,}$", s):
                continue
            s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)
            s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
            s = re.sub(r"<[^>]+>", "", s)
            s = re.sub(r"^#+\s*", "", s)
            s = re.sub(r"[`*_>|]", "", s).strip()
            if len(s) < 3:
                continue
            lines.append(s)
            total += len(s) + 1
            if total > limit:
                break
        return "\n".join(lines)[:limit]

    async def _llm_text(self, system_prompt: str, user_prompt: str, timeout: float = 20.0) -> str:
        try:
            provider = self.context.get_using_provider()
        except Exception:
            provider = None
        if provider is None:
            return ""
        try:
            resp = await asyncio.wait_for(
                provider.text_chat(prompt=user_prompt, system_prompt=system_prompt, request_max_retries=1),
                timeout=timeout,
            )
        except Exception as e:
            logger.warning("[ghfetch] LLM 总结失败：%s" % e)
            return ""
        try:
            txt = resp.result_chain.get_plain_text() if getattr(resp, "result_chain", None) else ""
        except Exception:
            txt = getattr(resp, "completion_text", "") or ""
        return str(txt or "").strip()

    async def _repo_intro(self, owner: str, repo: str) -> str:
        """把仓库简介拼成一条可直发的消息：名称/简介/语言/star/标签 + 大肥鱼一句话总结"""
        full = "%s/%s" % (owner, repo)
        ttl = float(self._cfg("info_cache_ttl", 900))
        hit = self._intro_cache.get(full.lower())
        if hit and time.time() - hit[0] < ttl:
            return hit[1]

        info = await self._repo_info(owner, repo)
        if not isinstance(info, dict):
            return ""
        if info.get("_error"):
            if info.get("_error") == "not_found":
                return "📦 %s\n仓库不存在或不是公开仓库" % full
            return ""

        desc = re.sub(r"\s+", " ", str(info.get("description") or "")).strip()
        lang = str(info.get("language") or "").strip()
        topics = [str(t) for t in (info.get("topics") or []) if t][:6]
        try:
            stars = int(info.get("stargazers_count") or 0)
            forks = int(info.get("forks_count") or 0)
        except Exception:
            stars = forks = 0
        pushed = str(info.get("pushed_at") or info.get("updated_at") or "")[:10]
        archived = bool(info.get("archived"))
        star_s = ("%.1fk" % (stars / 1000.0)) if stars >= 1000 else str(stars)

        lines = ["📦 %s%s" % (full, "（已归档）" if archived else "")]
        lines.append("简介：%s" % (desc[:200] if desc else "（作者没写简介）"))
        meta = []
        if lang:
            meta.append(lang)
        meta.append("⭐%s" % star_s)
        if forks:
            meta.append("分支 %s" % (("%.1fk" % (forks / 1000.0)) if forks >= 1000 else str(forks)))
        if pushed:
            meta.append("更新 %s" % pushed)
        lines.append(" ｜ ".join(meta))
        if topics:
            lines.append("标签：" + "、".join(topics))

        if self._cfg("repo_intro_llm", True):
            readme = await self._readme_snippet(
                owner, repo, int(float(self._cfg("repo_intro_readme_chars", 1500)))
            )
            system_prompt = (
                "你是在 QQ 群里随手插话的群友大肥鱼。用大白话说，60 字以内，最多两句，"
                "不用 markdown、不列点、不写产品介绍腔、不说客套话。"
            )
            user_prompt = (
                "下面是 GitHub 仓库的信息。用一两句大白话告诉群友：这仓库是做什么的、有啥亮点。\n"
                "要求：别复述 star 数字；别编造信息里没有的功能；别用「这是一个…」「该仓库…」这种模板腔；"
                "像群友看到链接随口点评一句那样说人话。\n\n"
                "仓库：%s\n简介：%s\n语言：%s\n标签：%s\nREADME 摘录：\n%s"
                % (full, desc or "（无）", lang or "（未知）", "、".join(topics) or "（无）", readme or "（没抓到）")
            )
            tldr = await self._llm_text(system_prompt, user_prompt)
            tldr = re.sub(r"^[「『\"']+|[」』\"']+$", "", tldr).strip()
            tldr = re.sub(r"\s+", " ", tldr)[:160]
            if tldr:
                lines.append("🐟 %s" % tldr)

        txt = "\n".join(lines)
        self._intro_cache[full.lower()] = (time.time(), txt)
        return txt

    # ---------------- Release 解析 ----------------
    async def _gh_api(self, path: str) -> dict | None:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
        token = str(self._cfg("github_token", "") or "").strip()
        if token:
            headers["Authorization"] = "Bearer " + token
        try:
            timeout = aiohttp.ClientTimeout(total=25)
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
                async with s.get("https://api.github.com" + path) as r:
                    if r.status == 404:
                        return {"_error": "not_found"}
                    if r.status == 403:
                        return {"_error": "rate_limited"}
                    if r.status != 200:
                        return {"_error": "http_%d" % r.status}
                    return await r.json()
        except Exception as e:
            logger.warning("[ghfetch] GitHub API 失败：%s" % e)
            return {"_error": "network"}

    async def _resolve_release(
        self, event: AstrMessageEvent, owner: str, repo: str, tag: str | None, allow_source: bool = False
    ):
        path = "/repos/%s/%s/releases/%s" % (owner, repo, ("tags/" + tag) if tag else "latest")
        data = await self._gh_api(path)
        if not data or data.get("_error"):
            err = (data or {}).get("_error", "unknown")
            if err == "not_found" and allow_source:
                await self._ask_source_zip(event, owner, repo, None)
                return
            tip = {
                "not_found": "没找到这个 Release，确认下仓库/版本号是否正确",
                "rate_limited": "GitHub API 限流了（未配置 token 每小时 60 次），稍后再试或直接发下载直链",
                "network": "连不上 GitHub API",
            }.get(err, "GitHub 返回 %s" % err)
            await event.send(self._msg("解析失败：%s" % tip))
            return

        assets = [
            a
            for a in (data.get("assets") or [])
            if a.get("browser_download_url") and not RE_JUNK_ASSET.search(str(a.get("name", "")).lower())
        ]
        if not assets:
            if allow_source:
                await self._ask_source_zip(event, owner, repo, str(data.get("tag_name") or "") or None)
                return
            await event.send(
                self._msg("这个 Release 没有可下载的资产（可能只有源码包）。源码包直链形如：\n"
                          "https://github.com/%s/%s/archive/refs/tags/%s.tar.gz" % (owner, repo, data.get("tag_name", "TAG")))
            )
            return

        if len(assets) == 1:
            a = assets[0]
            await self._download_and_send(event, a["browser_download_url"], a["name"], int(a.get("size") or 0))
            return

        # 多个平台/多个文件：一律列菜单让用户自己选，不擅自替他挑
        head = "%s %s" % (repo, data.get("tag_name", ""))
        txt = self._asset_menu(event, head, assets)
        if txt:
            await event.send(self._msg(txt))

    def _tag_of(self, name: str) -> str:
        """给文件名打上平台/架构标签，菜单里更好认"""
        low = (name or "").lower()
        plat = self._match_plat(low)
        arch = self._match_arch(low)
        label = {"win": "Windows", "linux": "Linux", "mac": "macOS", "android": "Android"}.get(plat or "", "")
        parts = [x for x in (label, arch) if x]
        return ("，" + "/".join(parts)) if parts else ""

    async def _source_zip_url(self, owner: str, repo: str, tag: str | None) -> tuple[str, str, dict | None]:
        """构造源码 zip 直链：有 tag 用 tag，否则取默认分支；顺带把仓库信息带出来"""
        full = "%s/%s" % (owner, repo)
        info = None
        if tag:
            ref = "refs/tags/" + tag
            fname = "%s-%s.zip" % (repo, tag)
        else:
            info = await self._repo_info(owner, repo)
            branch = "main"
            if isinstance(info, dict) and not info.get("_error"):
                branch = str(info.get("default_branch") or "main")
            ref = "refs/heads/" + branch
            fname = "%s-%s.zip" % (repo, branch)
        return "https://github.com/%s/archive/%s.zip" % (full, ref), fname, info

    async def _ask_source_zip(self, event: AstrMessageEvent, owner: str, repo: str, tag: str | None = None):
        """仓库没有可用 Release：先问一句要不要源码包，确认了再下"""
        url, fname, info = await self._source_zip_url(owner, repo, tag)
        if isinstance(info, dict) and info.get("_error") == "not_found":
            # 仓库本身就不存在（或私有），别白问一句源码包
            await event.send(self._msg("仓库 %s/%s 不存在或不是公开仓库，没法下" % (owner, repo)))
            return
        ttl = int(self._cfg("pending_ttl", 180))
        self._pending[self._session_key(event)] = {
            "ts": time.time(),
            "kind": "source",
            "url": url,
            "fname": fname,
        }
        await event.send(
            self._msg(
                "%s/%s 没有能下的 Release（只有源码）。要下源码包 %s 吗？\n回 1 下载 / 0 不要（%ds 内有效）"
                % (owner, repo, fname, ttl)
            )
        )

    # ---------------- 按项目名搜索 ----------------
    def _parse_query(self, raw: str) -> tuple[str, str | None, str | None]:
        """把「项目名 + 平台/架构词」拆成搜索词与偏好。"""
        toks = [t for t in re.split(r"[\s,，]+", (raw or "").strip()) if t]
        plat = arch = None
        keep: list[str] = []
        for t in toks:
            low = t.lower()
            p = self._match_plat(low)
            a = self._match_arch(low)
            if p and plat is None:
                plat = p
            elif a and arch is None:
                arch = a
            else:
                keep.append(t)
        q = (" ".join(keep) if keep else (raw or "")).strip()
        q = re.sub(r"(?:(?:帮我|请|麻烦|来个|搞个|下载|下)\s*)+", "", q).strip() or (raw or "").strip()
        return q[:60], plat, arch

    def _match_plat(self, low: str) -> str | None:
        for k, pat in PLAT_PATTERNS.items():
            if pat.search(low):
                return k
        return None

    def _match_arch(self, low: str) -> str | None:
        for k, pat in ARCH_PATTERNS.items():
            if pat.search(low):
                return k
        return None

    def _plat_bucket(self, name: str) -> str:
        """把文件归到某个平台桶里，识别不出的丢 other"""
        p = self._match_plat((name or "").lower()) or ""
        return p if p in ("win", "linux", "mac", "android") else "other"

    def _plat_word(self, text: str) -> str | None:
        """把用户随手写的平台词映射成内部桶名"""
        t = (text or "").strip().lower()
        table = {
            "win": "win", "windows": "win", "win10": "win", "win11": "win", "windows10": "win",
            "windows11": "win", "exe": "win", "win64": "win", "win32": "win", "电脑": "win",
            "安卓": "android", "android": "android", "apk": "android", "手机": "android",
            "mac": "mac", "macos": "mac", "osx": "mac", "苹果": "mac", "dmg": "mac",
            "linux": "linux", "deb": "linux", "rpm": "linux", "appimage": "linux",
        }
        if t in table:
            return table[t]
        return self._match_plat(t)

    def _asset_menu(self, event: AstrMessageEvent, head: str, full_assets: list, plat: str | None = None):
        """出一份按平台分组轮转的资产菜单，保证每个平台都露脸，不被条数上限整段挤掉"""
        pool = [a for a in full_assets if not plat or self._plat_bucket(a.get("name", "")) == plat]
        if not pool:
            return None
        limit = max(3, int(self._cfg("asset_menu_limit", 20)))
        groups: dict[str, list] = {}
        for a in pool:
            groups.setdefault(self._plat_bucket(a.get("name", "")), []).append(a)
        order = ["win", "android", "mac", "linux", "other"]
        shown: list = []
        depth = 0
        while len(shown) < limit:
            added = False
            for k in order:
                lst = groups.get(k) or []
                if depth < len(lst) and len(shown) < limit:
                    shown.append(lst[depth])
                    added = True
            if not added:
                break
            depth += 1
        ttl = int(self._cfg("pending_ttl", 180))
        tail = ""
        if len(shown) < len(pool):
            tail = "（只列了 %d 个，想看某平台就回 win / 安卓 / mac / linux）" % len(shown)
        lines = ["%s 有 %d 个文件%s" % (head, len(pool), tail),
                 "回复序号下载（0 取消，%ds 内有效）：" % ttl]
        for i, a in enumerate(shown, 1):
            lines.append("%d. %s（%s%s）" % (i, a["name"], _fmt_size(a.get("size") or 0), self._tag_of(a["name"])))
        self._pending[self._session_key(event)] = {
            "ts": time.time(),
            "kind": "assets",
            "head": head,
            "items": shown,
            "all": pool[:200],
        }
        return "\n".join(lines)

    def _aliases(self) -> dict:
        out = dict(DEFAULT_ALIASES)
        raw = self._cfg("aliases", "") or ""
        pairs: list[tuple[str, str]] = []
        if isinstance(raw, dict):
            pairs = [(str(k), str(v)) for k, v in raw.items()]
        else:
            for part in str(raw).replace(";", ",").replace("\n", ",").split(","):
                if "=" in part:
                    a, b = part.split("=", 1)
                    pairs.append((a.strip(), b.strip()))
        for k, v in pairs:
            if k and v:
                out[k.strip().lower()] = v.strip()
        return out

    async def _gh_search(self, q: str, limit: int = 5) -> list[dict]:
        """GitHub 搜索：优先 in:name，按 star 降序取候选。

        按 star 排序可避免「obsidian / typora」这类官方仓库名与关键词不完全一致的项目
        被冷门同名仓库挤掉（best match 排序会这么干）。
        """
        tries = [
            "%s in:name&sort=stars&order=desc" % q,
            "%s&sort=stars&order=desc" % q,
        ]
        for query in tries:
            data = await self._gh_api(
                "/search/repositories?q=%s&per_page=%d" % (quote(query), max(5, limit))
            )
            if isinstance(data, dict) and data.get("_error"):
                if data.get("_error") == "rate_limited":
                    return [{"_error": "rate_limited"}]
                continue
            items = (data or {}).get("items") if isinstance(data, dict) else None
            if items:
                return items
        return []

    async def _resolve_by_name(self, event: AstrMessageEvent, raw: str):
        q, plat, arch = self._parse_query(raw)
        if not q:
            return

        alias = self._aliases().get(q.lower().replace(" ", "")) or self._aliases().get(q.lower())
        if alias:
            await self._release_of_repo(event, alias, plat, arch)
            return

        m = RE_OWNER_REPO.match(q)
        if m:
            full = "%s/%s" % (m.group(1), m.group(2))
            info = await self._gh_api("/repos/" + full)
            if isinstance(info, dict) and not info.get("_error"):
                await self._release_of_repo(event, full, plat, arch)
                return

        items = await self._gh_search(q, max(3, int(self._cfg("search_candidates", 3))))
        if items and items[0].get("_error") == "rate_limited":
            await event.send(self._msg("GitHub 搜索被限流了（未配 token：搜索 10 次/分、请求 60 次/时），稍后再试或直接发链接"))
            return
        if not items:
            await event.send(
                self._msg("没搜到叫「%s」的 GitHub 项目，换个名字试试（「owner/repo」这种写法也行）" % q)
            )
            return

        norm = re.sub(r"[-_.\s]", "", q.lower())

        def _norm_name(it) -> str:
            return re.sub(r"[-_.\s]", "", str(it.get("name") or "").lower())

        def _stars(it) -> int:
            try:
                return int(it.get("stargazers_count") or 0)
            except Exception:
                return 0

        exact = [it for it in items if _norm_name(it) == norm]
        pick = max(exact, key=_stars) if exact else items[0]
        # 冷门同名仓库让位：名为「obsidian」的冷门仓库不该盖过 obsidian-releases 这种官方项目
        if exact and _stars(pick) < 200:
            better = [it for it in items if _norm_name(it).startswith(norm) and _norm_name(it) != norm]
            if better:
                top = max(better, key=_stars)
                if _stars(top) > max(10, _stars(pick) * 20):
                    pick = top
                    exact = []
        if exact or len(items) == 1 or not self._cfg("ask_on_ambiguous", True):
            await self._release_of_repo(event, str(pick.get("full_name") or ""), plat, arch)
            return

        n = max(1, int(self._cfg("search_candidates", 3)))
        lines = [
            "「%s」没有完全同名的项目，可能是指这几个（回复序号，0 取消，%ds 内有效）："
            % (q, int(self._cfg("pending_ttl", 180)))
        ]
        for i, it in enumerate(items[:n], 1):
            desc = str(it.get("description") or "").replace("\n", " ").strip()[:50]
            lines.append(
                "%d. %s ⭐%d%s"
                % (i, it.get("full_name"), int(it.get("stargazers_count") or 0), ("｜" + desc) if desc else "")
            )
        self._pending[self._session_key(event)] = {
            "ts": time.time(),
            "kind": "repos",
            "items": items[:n],
            "plat": plat,
            "arch": arch,
        }
        await event.send(self._msg("\n".join(lines)))

    def _score_asset(self, name: str, plat: str | None, arch: str | None) -> int:
        low = str(name).lower()
        if RE_JUNK_ASSET.search(low):
            return -50
        score = 0
        hits = [k for k, pat in PLAT_PATTERNS.items() if pat.search(low)]
        if plat:
            score += 6 if plat in hits else (-5 if hits else 0)
        else:
            if "win" in hits and self._cfg("win_prefer", True):
                score += 3
            if "linux" in hits:
                score += 2
            if "mac" in hits:
                score += 1
            if "android" in hits:
                score += 1
        if arch:
            ah = self._match_arch(low)
            if ah == arch:
                score += 3
            elif ah:
                score -= 1
        else:
            # 没指定架构时默认给 x64 优先：普通电脑装 arm64/386 版直接跑不起来
            ah = self._match_arch(low)
            if ah == "amd64":
                score += 2
            elif ah == "arm64":
                score -= 1
            elif ah == "386":
                score -= 2
        if re.search(r"portable|\.zip$", low):
            score += 1
        return score

    async def _release_of_repo(self, event: AstrMessageEvent, full: str, plat: str | None = None, arch: str | None = None):
        if not full:
            return
        data = await self._gh_api("/repos/%s/releases/latest" % full)
        if isinstance(data, dict) and data.get("_error") == "not_found":
            lst = await self._gh_api("/repos/%s/releases?per_page=1" % full)
            data = lst[0] if isinstance(lst, list) and lst else None

        if not isinstance(data, dict) or data.get("_error"):
            err = (data or {}).get("_error", "no_release") if isinstance(data, dict) else "no_release"
            if err == "rate_limited":
                await event.send(self._msg("GitHub API 限流了（未配 token 60 次/时），稍后再试"))
            elif err == "not_found":
                parts = [x for x in full.split("/") if x]
                if len(parts) == 2:
                    await self._ask_source_zip(event, parts[0], parts[1], None)
                else:
                    await event.send(self._msg("%s 没有已发布的 Release（可能只有源码）" % full))
            else:
                await event.send(self._msg("查 %s 的 Release 失败：%s" % (full, err)))
            return

        tag = str(data.get("tag_name") or "")
        assets = [
            a
            for a in (data.get("assets") or [])
            if a.get("browser_download_url") and not RE_JUNK_ASSET.search(str(a.get("name", "")).lower())
        ]
        if not assets:
            parts = [x for x in full.split("/") if x]
            if len(parts) == 2:
                await self._ask_source_zip(event, parts[0], parts[1], tag or None)
                return
            await event.send(self._msg("%s %s 没有可下载的二进制资产（只有源码包）" % (full, tag)))
            return

        scored = sorted(assets, key=lambda a: self._score_asset(a.get("name", ""), plat, arch), reverse=True)
        best = scored[0]
        best_score = self._score_asset(best.get("name", ""), plat, arch)
        second_score = self._score_asset(scored[1].get("name", ""), plat, arch) if len(scored) > 1 else -99
        top_count = sum(1 for a in scored if self._score_asset(a.get("name", ""), plat, arch) == best_score)
        if len(scored) == 1 or (best_score >= 3 and (best_score - second_score >= 2 or top_count == 1)):
            await event.send(self._msg("%s %s → %s" % (full, tag, best.get("name"))))
            await self._download_and_send(
                event, best["browser_download_url"], best["name"], int(best.get("size") or 0)
            )
            return

        n = max(1, int(self._cfg("asset_candidates", 8)))
        lines = [
            "%s %s 有 %d 个文件，回复序号下载（0 取消，%ds 内有效）："
            % (full, tag, len(scored), int(self._cfg("pending_ttl", 180)))
        ]
        for i, a in enumerate(scored[:n], 1):
            lines.append("%d. %s（%s）" % (i, a["name"], _fmt_size(a.get("size") or 0)))
        self._pending[self._session_key(event)] = {
            "ts": time.time(),
            "kind": "assets",
            "items": scored[:n],
        }
        await event.send(self._msg("\n".join(lines)))

    # ---------------- 下载 + 回传 ----------------
    async def _download_and_send(self, event: AstrMessageEvent, url: str, name: str, size: int = 0):
        key = self._session_key(event)
        if key in self._busy:
            await event.send(self._msg("上一个文件还在传，等它完了再来"))
            return
        max_bytes = int(float(self._cfg("max_size_mb", 500)) * 1024 * 1024)
        if size and size > max_bytes:
            await event.send(
                self._msg("文件 %s 有 %s，超过上限 %d MB，放弃。直链给你：\n%s"
                          % (name, _fmt_size(size), int(float(self._cfg("max_size_mb", 500))), url))
            )
            return

        self._busy.add(key)
        task_dir = self.dl_root / uuid.uuid4().hex[:12]
        task_dir.mkdir(parents=True, exist_ok=True)
        target = task_dir / (name or "download.bin")
        token = None
        try:
            async with self._sem:
                await event.send(
                    self._msg("收到，服务器开始加速下载：%s%s"
                              % (name, ("（%s）" % _fmt_size(size)) if size else ""))
                )
                t0 = time.time()
                ok, err = await self._fetch(url, target, size)
                if not ok:
                    await event.send(self._msg("下载失败：%s" % (err or "未知错误")))
                    return
                got = target.stat().st_size if target.exists() else 0
                if max_bytes and got > max_bytes:
                    await event.send(self._msg("文件 %s 超过上限，已放弃" % _fmt_size(got)))
                    return
                cost = max(0.1, time.time() - t0)
                speed = _fmt_size(got / cost) + "/s"

                token = await self._publish(target)
                sent = await self._deliver(event, token, target.name)
                if not sent:
                    await event.send(self._msg("文件传不出去（平台不支持文件消息），下载直链：\n%s" % url))
                    return
                await event.send(
                    self._msg("搞定：%s（%s），服务器用时 %.0fs，均速 %s"
                              % (target.name, _fmt_size(got), cost, speed))
                )
                logger.info("[ghfetch] 发送完成 %s %s" % (target.name, _fmt_size(got)))
        except Exception as e:
            logger.exception("[ghfetch] 任务异常：%s" % e)
            try:
                await event.send(self._msg("出错了：%s" % e))
            except Exception:
                pass
        finally:
            self._busy.discard(key)
            asyncio.create_task(self._cleanup(task_dir, token))

    def _mirror_list(self) -> list[str]:
        raw = self._cfg("mirrors", ["", "https://ghfast.top/", "https://gh-proxy.com/"])
        if isinstance(raw, str):
            raw = raw.split(",")
        out: list[str] = []
        for x in raw or []:
            x = str(x).strip()
            if x and not x.endswith("/"):
                x += "/"
            if x in out:
                continue
            out.append(x)
        if not out:
            out = [""]
        return out

    async def _fetch(self, url: str, target: Path, size: int = 0) -> tuple[bool, str]:
        aria = self._find_aria2()
        last = ""
        for prefix in self._mirror_list():
            real = (prefix + url) if prefix else url
            label = prefix or "直连"
            if aria:
                ok, err = await self._aria2(aria, real, target)
            else:
                ok, err = await self._py_fetch(real, target)
            if ok:
                if prefix:
                    logger.info("[ghfetch] 经镜像 %s 下载成功" % prefix)
                return True, ""
            last = "%s %s" % (label, err)
            logger.warning("[ghfetch] 源 %s 失败：%s" % (label, err))
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
        return False, last

    async def _aria2(self, aria: str, url: str, target: Path) -> tuple[bool, str]:
        conn = max(1, int(self._cfg("connections", 16)))
        log_path = target.parent / "aria2.log"
        cmd = [
            aria,
            url,
            "-x", str(conn), "-s", str(conn), "-k", "1M",
            "-c", "--file-allocation=none", "--auto-file-renaming=false",
            "--allow-overwrite=true", "--summary-interval=0",
            "--console-log-level=warn", "--check-certificate=false",
            "--connect-timeout=20", "--timeout=60", "--max-tries=3", "--retry-wait=3",
            "-d", str(target.parent), "-o", target.name,
            "--log", str(log_path), "--log-level=warn",
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            rc = await proc.wait()
        except Exception as e:
            return False, "aria2 启动失败：%s" % e
        if rc != 0:
            tail = ""
            try:
                tail = log_path.read_text(encoding="utf-8", errors="ignore").strip().splitlines()[-1]
            except Exception:
                pass
            return False, "aria2 退出码 %d %s" % (rc, tail[:120])
        if not target.exists() or target.stat().st_size == 0:
            return False, "目标文件为空"
        return True, ""

    async def _py_fetch(self, url: str, target: Path) -> tuple[bool, str]:
        conn = max(1, int(self._cfg("connections", 16)))
        try:
            timeout = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=120)
            async with aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=timeout) as s:
                async with s.get(url, allow_redirects=True) as r:
                    if r.status != 200:
                        return False, "HTTP %d" % r.status
                    total = int(r.headers.get("Content-Length") or 0)
                    ranges = "bytes" in str(r.headers.get("Accept-Ranges", "")).lower()
                if not total or not ranges or total < 8 * 1024 * 1024:
                    async with s.get(url, allow_redirects=True) as r:
                        if r.status != 200:
                            return False, "HTTP %d" % r.status
                        with open(target, "wb") as f:
                            async for chunk in r.content.iter_chunked(262144):
                                f.write(chunk)
                    return (True, "") if target.exists() and target.stat().st_size else (False, "写入失败")

                async with s.get(url, allow_redirects=True) as r:
                    real = str(r.url)
                chunk = max(256 * 1024, total // (conn * 4))
                segs = [(lo, min(total - 1, lo + chunk - 1)) for lo in range(0, total, chunk)]
                sem = asyncio.Semaphore(conn * 2)
                out = target.with_suffix(target.suffix + ".part")

                async def one(idx: int, lo: int, hi: int):
                    async with sem:
                        for attempt in range(3):
                            try:
                                async with s.get(real, headers={"Range": "bytes=%d-%d" % (lo, hi)},
                                                 allow_redirects=True) as resp:
                                    data = await resp.read()
                                with open(str(out) + ".%05d" % idx, "wb") as f:
                                    f.write(data)
                                return True
                            except Exception:
                                await asyncio.sleep(1 + attempt)
                        return False

                results = await asyncio.gather(*[one(i, lo, hi) for i, (lo, hi) in enumerate(segs)])
                if not all(results):
                    return False, "部分分片下载失败"
                with open(target, "wb") as fo:
                    for i in range(len(segs)):
                        part = Path(str(out) + ".%05d" % i)
                        with open(part, "rb") as fi:
                            shutil.copyfileobj(fi, fo, 1024 * 1024)
                for i in range(len(segs)):
                    p = Path(str(out) + ".%05d" % i)
                    try:
                        p.unlink()
                    except Exception:
                        pass
                return (True, "") if target.stat().st_size else (False, "合并失败")
        except Exception as e:
            return False, str(e)

    # ---------------- 回传 ----------------
    async def _publish(self, path: Path) -> str:
        await self._ensure_http()
        token = uuid.uuid4().hex
        self._files[token] = path
        self._last_hit[token] = time.time()
        return token

    def _file_url(self, token: str, name: str) -> str:
        return "http://%s:%d/f/%s/%s" % (self._local_ip(), self._http_port, token, quote(name))

    async def _deliver(self, event: AstrMessageEvent, token: str, name: str) -> bool:
        url = self._file_url(token, name)
        logger.info("[ghfetch] 回传 URL %s" % url)
        group = event.get_group_id()
        bot = getattr(event, "bot", None)
        try:
            if bot is not None:
                if group:
                    res = await bot.call_action("upload_group_file", group_id=int(group), file=url, name=name)
                else:
                    res = await bot.call_action(
                        "upload_private_file", user_id=int(event.get_sender_id()), file=url, name=name
                    )
                if isinstance(res, dict) and res.get("retcode") not in (0, None):
                    logger.warning("[ghfetch] 上传返回异常 %s" % res)
                    return False
                return True
        except Exception as e:
            logger.warning("[ghfetch] call_action 失败：%s" % e)
        return False

    async def _cleanup(self, task_dir: Path, token: str | None):
        delay = float(self._cfg("clean_delay", 45))
        await asyncio.sleep(delay)
        deadline = time.time() + 180
        while token and time.time() < deadline:
            last = self._last_hit.get(token, 0)
            if time.time() - last > 20:
                break
            await asyncio.sleep(5)
        if token:
            self._files.pop(token, None)
            self._last_hit.pop(token, None)
        shutil.rmtree(task_dir, ignore_errors=True)
        logger.info("[ghfetch] 已清理残留 %s" % task_dir.name)

    # ---------------- 命令 ----------------
    @filter.command("ghfetch")
    async def ghfetch_status(self, event: AstrMessageEvent):
        aria = self._find_aria2() or "未安装"
        lines = [
            "GitHub 加速下载已就绪",
            "aria2: %s" % aria,
            "并发连接: %s" % self._cfg("connections", 16),
            "大小上限: %s MB" % int(float(self._cfg("max_size_mb", 500))),
            "回传端口: %d（%s）" % (self._http_port, self._local_ip()),
            "用法:",
            "  1) 发 Release 链接 / releases/download 直链",
            "  2) 「下载 项目名」自动找最匹配项目的最新 Release，如：下载 cli、gh frp linux",
            "  3) 「下载 项目名 win」可指定平台（win/linux/mac/arm64）",
        ]
        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        try:
            if self._runner is not None:
                await self._runner.cleanup()
                self._runner = None
                self._site = None
        except Exception as e:
            logger.debug("[ghfetch] 关停 HTTP 失败：%s" % e)
        logger.info("[ghfetch] 已卸载")
