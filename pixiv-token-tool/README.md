# Pixiv Refresh Token 获取工具

一个 Windows 友好的独立小工具，用**有头浏览器**登录 Pixiv 后自动获取 `refresh_token`，供 `pixivpy` 或 KiraAI 的 Pixiv 图片搜索插件使用。

## 使用方法

### 方式一：双击运行（推荐）

1. 确保已安装 [Python 3.10+](https://www.python.org/downloads/)
2. 双击 `run.bat`
3. 第一次运行会自动创建虚拟环境、安装依赖
4. 工具会自动检测系统已装的 Chrome/Edge，有则直接使用；无则提示下载 Chromium
5. 在打开的浏览器窗口中登录 Pixiv
6. 登录成功后，工具会自动打印并保存 `refresh_token` 到 `refresh_token.txt`

### 方式二：手动运行

```bash
cd pixiv-token-tool
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python get_pixiv_token.py
```

## 输出

成功后会显示：

```text
============================================================
✅ 获取成功！
============================================================
refresh_token: xxxxxx
access_token:  xxxxxx
expires_in:    3600
============================================================
已保存到: C:\...\pixiv-token-tool\refresh_token.txt
============================================================
```

把 `refresh_token.txt` 里的内容复制到插件配置的 `refresh_token` 中即可。

## 注意事项

- 必须使用**有头浏览器**（headless=False），因为 Pixiv 可能会检测无头浏览器
- 登录过程完全在本地浏览器中完成，工具不会收集你的账号密码
- 如果之前获取的 token 失效，重新运行本工具即可
