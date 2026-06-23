"""Pixiv Refresh Token 获取工具（Windows 友好 / 有头浏览器）

用法：
    1. 双击 run.bat（自动处理环境和依赖）
    2. 或手动运行：python get_pixiv_token.py

功能：
    - 打开 Chrome 浏览器
    - 让用户手动登录 Pixiv
    - 自动拦截 OAuth 回调 code
    - 换取并保存 refresh_token
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
from base64 import urlsafe_b64encode
from hashlib import sha256
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import urlencode

import requests
from playwright.async_api import async_playwright


# ------------------------------------------------------------------
# Pixiv OAuth 常量（来自 gppt / pixivpy）
# ------------------------------------------------------------------
USER_AGENT = "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)"
CALLBACK_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
REDIRECT_URI = "https://accounts.pixiv.net/post-redirect"
LOGIN_URL = "https://app-api.pixiv.net/web/v1/login"
AUTH_TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"


# ------------------------------------------------------------------
# 查找系统已安装的浏览器（Windows）
# ------------------------------------------------------------------
def find_system_browser() -> str | None:
    """优先查找系统 Chrome / Edge，避免下载 Chromium。"""
    candidates: list[str] = []

    # Chrome
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

    candidates.extend([
        os.path.join(local_appdata, r"Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(program_files, r"Google\\Chrome\\Application\\chrome.exe"),
        os.path.join(program_files_x86, r"Google\\Chrome\\Application\\chrome.exe"),
    ])

    # Edge
    candidates.extend([
        os.path.join(program_files_x86, r"Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(program_files, r"Microsoft\\Edge\\Application\\msedge.exe"),
        os.path.join(local_appdata, r"Microsoft\\Edge\\Application\\msedge.exe"),
    ])

    # Chromium
    candidates.extend([
        os.path.join(local_appdata, r"Chromium\\Application\\chrome.exe"),
        os.path.join(program_files, r"Chromium\\Application\\chrome.exe"),
    ])

    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def find_playwright_browser() -> str | None:
    """查找 Playwright 已下载的 Chromium。"""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            exe = p.chromium.executable_path
            if exe and Path(exe).exists():
                return str(exe)
    except Exception:
        pass
    return None


# ------------------------------------------------------------------
# PKCE 生成
# ------------------------------------------------------------------
def oauth_pkce() -> tuple[str, str]:
    """生成 OAuth PKCE 的 code_verifier 和 code_challenge。"""
    code_verifier = token_urlsafe(32)
    code_challenge = (
        urlsafe_b64encode(sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return code_verifier, code_challenge


# ------------------------------------------------------------------
# 主流程
# ------------------------------------------------------------------
async def main() -> int:
    print("=" * 60)
    print(" Pixiv Refresh Token 获取工具")
    print("=" * 60)
    print("将打开 Chrome/Edge 浏览器，请手动登录 Pixiv。")
    print("登录成功后，本工具会自动获取 refresh_token。")
    print("=" * 60)

    code_verifier, code_challenge = oauth_pkce()
    login_params = {
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "client": "pixiv-android",
    }
    login_url = f"{LOGIN_URL}?{urlencode(login_params)}"

    # 优先使用系统自带浏览器，找不到再尝试 Playwright 内置 Chromium
    browser_path = find_system_browser() or find_playwright_browser()
    launch_kwargs: dict = {
        "headless": False,
        "args": [
            "--disable-gpu",
            "--disable-extensions",
            "--disable-infobars",
            "--disable-dev-shm-usage",
            "--start-maximized",
            "--no-sandbox",
            f"--user-agent={USER_AGENT}",
        ],
    }
    if browser_path:
        print(f"[+] 使用浏览器: {browser_path}")
        launch_kwargs["executable_path"] = browser_path
    else:
        print("[-] 未找到可用浏览器。")
        print("    请安装 Chrome/Edge，或运行以下命令下载 Chromium:")
        print("    .venv\\Scripts\\python -m playwright install chromium")
        return 1

    async with async_playwright() as p:
        print("正在启动浏览器...")
        try:
            browser = await p.chromium.launch(**launch_kwargs)
        except Exception as e:
            print(f"[-] 启动浏览器失败: {e}")
            return 1

        context = await browser.new_context()
        page = await context.new_page()

        captured_code: str | None = None

        async def handle_request(request) -> None:
            nonlocal captured_code
            url = request.url
            if not url.startswith("pixiv://"):
                return
            if m := re.search(r"code=([^&]*)", url):
                captured_code = m.group(1)
                print("[+] 已捕获授权 code")

        page.on("request", handle_request)
        await page.goto(login_url)

        print(f"[+] 已打开登录页")
        print("[!] 请在浏览器中完成登录（5分钟超时）...")

        try:
            await page.wait_for_url(
                re.compile(f"^{re.escape(REDIRECT_URI)}"),
                wait_until="networkidle",
                timeout=300000,
            )
        except Exception as e:
            print(f"[-] 等待登录超时或失败: {e}")
            await browser.close()
            return 1

        await page.wait_for_timeout(1000)
        await browser.close()

        if not captured_code:
            print("[-] 未能获取授权 code，请重试。")
            return 1

    print("[+] 正在用 code 换取 token...")
    response = requests.post(
        AUTH_TOKEN_URL,
        data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": captured_code,
            "code_verifier": code_verifier,
            "grant_type": "authorization_code",
            "include_policy": "true",
            "redirect_uri": CALLBACK_URI,
        },
        headers={
            "user-agent": USER_AGENT,
            "app-os-version": "14.6",
            "app-os": "ios",
        },
        timeout=10,
    )

    try:
        result = response.json()
    except Exception as e:
        print(f"[-] 解析 token 响应失败: {e}")
        print(f"    原始响应: {response.text}")
        return 1

    if "refresh_token" not in result:
        print(f"[-] 换取 token 失败: {result}")
        return 1

    refresh_token = result["refresh_token"]
    access_token = result.get("access_token", "")
    expires_in = result.get("expires_in", 3600)

    print("\n" + "=" * 60)
    print(" ✅ 获取成功！")
    print("=" * 60)
    print(f" refresh_token: {refresh_token}")
    print(f" access_token:  {access_token}")
    print(f" expires_in:    {expires_in}")
    print("=" * 60)

    output_file = Path("refresh_token.txt")
    output_file.write_text(refresh_token, encoding="utf-8")
    print(f" 已保存到: {output_file.absolute()}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\n[!] 用户取消")
        sys.exit(130)
