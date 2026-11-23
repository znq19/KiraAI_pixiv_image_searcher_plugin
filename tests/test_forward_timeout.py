"""pixiv_image_searcher：合并转发可配置超时 + 超时降级为逐条直发。

    python3 tests/test_forward_timeout.py
"""
import asyncio
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _mod(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Logger:
    def debug(self, m, *a): pass
    def info(self, m, *a): pass
    def warning(self, m, *a): pass
    def error(self, m, *a): pass
    def exception(self, m, *a): pass


LOGGER = _Logger()


class BasePlugin:
    def __init__(self, ctx=None, cfg=None):
        self.ctx = ctx
        self.cfg = cfg or {}


def register_tool(*a, **kw):
    if a and callable(a[0]): return a[0]
    def deco(fn): return fn
    return deco


_mod("core")
_mod("core.plugin", BasePlugin=BasePlugin, register_tool=register_tool)
_mod("core.chat")
_mod("core.chat.message_utils", KiraMessageBatchEvent=object, MessageChain=lambda els: els)
_mod("core.chat.message_elements", Image=lambda *a, **kw: a)
_mod("core.logging_manager", get_logger=lambda *a, **kw: LOGGER)
_mod("core.utils")
_mod("core.utils.path_utils", get_data_path=lambda: "/tmp/pixiv_test_data")
_mod("pixivpy3", AppPixivAPI=object)

import importlib.util
spec = importlib.util.spec_from_file_location("pixiv_main", ROOT / "main.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
Plugin = mod.PixivPlugin

FAILS = []


def check(label, cond, detail=""):
    print(("  ✓ " if cond else "  ✗ ") + label + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)
    return cond


class FakeClient:
    def __init__(self, mode="ok"):
        self.calls = []
        self.timeouts = []
        self.mode = mode

    async def send_action(self, action, params, timeout=10.0):
        self.calls.append((action, dict(params)))
        self.timeouts.append(timeout)
        if self.mode == "timeout":
            raise asyncio.TimeoutError(f"请求 {action} 超时")
        if self.mode == "conn":
            raise ConnectionError(f"NapCat WebSocket 未连接，无法执行 {action}")
        return {"status": "ok", "retcode": 0, "data": {"message_id": 1}}


class _Info:
    name = "测试Bot"


class _Adapter:
    def __init__(self, client):
        self._client = client
        self.info = _Info()

    def get_client(self):
        return self._client


class _AdapterMgr:
    def __init__(self, client):
        self._a = _Adapter(client)

    def get_adapter(self, name):
        return self._a


class FakeCtx:
    def __init__(self, client):
        self.adapter_mgr = _AdapterMgr(client)
        self.direct_sent = []

    async def send_message_chain(self, sid, chain):
        self.direct_sent.append(chain)
        return type("R", (), {"ok": True})()


class _Sess:
    sid = "qq:gm:12345"


class _EvtAdapter:
    name = "qq"


class FakeEvent:
    sid = "qq:gm:12345"
    session = _Sess()
    adapter = _EvtAdapter()
    self_id = "10001"
    messages = []


def make_plugin(client, forward_timeout=60.0):
    p = Plugin.__new__(Plugin)
    p.ctx = FakeCtx(client)
    p.send_as_forward = True
    p.forward_timeout = forward_timeout
    p.image_storage_dir = "files/pixiv"
    p.max_cache_files = 50
    p.cleanup_count = 20
    p._sent_ids = {}
    p._fwd_unsupported_logged = False
    return p


PATHS = ["/tmp/pixiv_test_data/a.png", "/tmp/pixiv_test_data/b.png"]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


print("═══ 1) 正常：转发成功，只调一次，带 60s")
c = FakeClient("ok")
p = make_plugin(c)
ok = run(p._send_forward_images(FakeEvent(), PATHS))
check("返回 True", ok is True, repr(ok))
check("只调用 1 次", len(c.calls) == 1, str([a for a, _ in c.calls]))
check("★ 显式传 60s", c.timeouts == [60.0], str(c.timeouts))

print()
print("═══ 2) ★ 超时 ⇒ 返回 False（供调用方降级）")
c2 = FakeClient("timeout")
p2 = make_plugin(c2)
ok2 = run(p2._send_forward_images(FakeEvent(), PATHS))
check("★ 超时返回 False", ok2 is False, repr(ok2))
check("只尝试 1 次", len(c2.calls) == 1, str(len(c2.calls)))

print()
print("═══ 3) 明确故障（连接断开）⇒ 同样返回 False")
c3 = FakeClient("conn")
p3 = make_plugin(c3)
ok3 = run(p3._send_forward_images(FakeEvent(), PATHS))
check("返回 False", ok3 is False, repr(ok3))

print()
print("═══ 4) forward_timeout 可配置")
c4 = FakeClient("ok")
p4 = make_plugin(c4, forward_timeout=25)
run(p4._send_forward_images(FakeEvent(), PATHS))
check("传入 25s", c4.timeouts == [25.0], str(c4.timeouts))

c5 = FakeClient("ok")
p5 = make_plugin(c5, forward_timeout=0)
run(p5._send_forward_images(FakeEvent(), PATHS))
check("0 ⇒ 用框架默认 10s", c5.timeouts == [10.0], str(c5.timeouts))

print()
print("═══ 5) 老核心（无 timeout 形参）自动退回")
class OldClient:
    def __init__(self):
        self.calls = []

    async def send_action(self, action, params):
        self.calls.append(action)
        return {"status": "ok", "retcode": 0}


c6 = OldClient()
p6 = make_plugin(c6)
ok6 = run(p6._send_forward_images(FakeEvent(), PATHS))
check("老核心下仍发送成功", ok6 is True, repr(ok6))

print()
if FAILS:
    print(f"✗ 失败 {len(FAILS)} 项: {FAILS}")
    sys.exit(1)
print("✓ 全部通过")
