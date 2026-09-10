#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_offline.py — 离线自检（不联网、不花 token、不碰小红书）

起一个假的 DeepSeek 服务，验证：
  1. 请求格式对不对（模型、thinking、response_format、temperature）
  2. 429/503/超时会不会重试
  3. JSON 输出坏了会不会让模型自我修正
  4. 空内容（DeepSeek JSON 模式已知偶发问题）会不会重试
  5. 401/402 的报错提示是否清楚
  6. 出图链路能不能真的产出 PNG（需要 playwright，缺了就跳过）

用法：python3 selftest_offline.py
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import xhs_auto  # noqa: E402

NOTE = {
    "title": "秋冬护肤别乱买，这4步就够了",
    "body": "去年秋天我烂脸过一次，花了三千多才养回来。\n所以今年我学乖了。\n"
            "先说结论：保湿比美白重要。\n洗完脸别急着上精华，先拍水。",
    "topics": ["秋冬护肤", "敏感肌", "护肤步骤", "保湿", "屏障修复", "平价护肤"],
    "pages": [
        {"type": "cover", "line1": "秋冬护肤四步走", "line2": "烂脸一次才明白",
         "badge": "亲测"},
        {"type": "content", "heading": "先稳住屏障",
         "items": ["洗面奶换成氨基酸", "水温别超过体温", "洗完三分钟内上保湿"]},
        {"type": "content", "heading": "再谈功效",
         "items": ["美白先放一放", "猛药一周两次", "叠加不超过两种"]},
    ],
}

state = {"calls": [], "mode": "ok"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _completion(self, content):
        self._send(200, {
            "id": "fake", "object": "chat.completion", "model": xhs_auto.AI_MODEL,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "prompt_cache_hit_tokens": 0, "total_tokens": 150},
        })

    def do_GET(self):
        if self.path.endswith("/models"):
            self._send(200, {"data": [{"id": "deepseek-v4-pro"},
                                      {"id": "deepseek-v4-flash"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        state["calls"].append(body)
        n = len(state["calls"])
        mode = state["mode"]
        if mode == "503-then-ok" and n == 1:
            return self._send(503, {"error": {"message": "server busy"}})
        if mode == "401":
            return self._send(401, {"error": {"message": "Authentication Fails"}})
        if mode == "402":
            return self._send(402, {"error": {"message": "Insufficient Balance"}})
        if mode == "empty-then-ok" and n == 1:
            return self._completion("")
        if mode == "badjson-then-ok" and n == 1:
            return self._completion("好的，我来写：这是一篇关于秋冬护肤的笔记……")
        if mode == "markdown":
            note = dict(NOTE)
            note["body"] = "**加粗**的正文第一段\n## 小标题\n- 列表项"
            note["pages"] = [
                {"type": "cover", "line1": "**封面**", "line2": "副标题", "badge": "干货"},
                {"type": "content", "heading": "**小标题**",
                 "items": ["**要点一**", "普通要点"]}]
            return self._completion(json.dumps(note, ensure_ascii=False))
        return self._completion(json.dumps(NOTE, ensure_ascii=False))


PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✅' if cond else '❌'} {name}" + (f" — {detail}" if detail and not cond else ""))


def reset(mode="ok"):
    state["calls"].clear()
    state["mode"] = mode


def main():
    tmp = tempfile.mkdtemp(prefix="xhs_selftest_")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    # 把被测模块指向假服务
    xhs_auto.AI_BASE_URL = f"http://127.0.0.1:{port}"
    xhs_auto.AI_API_KEY = "sk-fake-for-selftest"
    xhs_auto.AI_MODEL = "deepseek-v4-pro"
    xhs_auto.AI_MAX_RETRIES = 3
    xhs_auto.AI_TIMEOUT = 10
    xhs_auto.OUT_ROOT = tmp

    try:
        print("\n[1] 请求格式（非思考模式）")
        reset()
        data = xhs_auto.ai_generate_copy("秋冬护肤思路", "参考素材：保湿优先", 3)
        p = state["calls"][0]
        check("model 传对", p["model"] == "deepseek-v4-pro", p.get("model"))
        check("thinking 显式关闭", p.get("thinking") == {"type": "disabled"}, str(p.get("thinking")))
        check("开了 json_object", p.get("response_format") == {"type": "json_object"},
              str(p.get("response_format")))
        check("temperature 生效且为正数", p.get("temperature") == xhs_auto.AI_TEMPERATURE,
              str(p.get("temperature")))
        check("stream=False", p.get("stream") is False)
        check("system 提示含 json 字样", "json" in p["messages"][0]["content"].lower())
        check("user 提示含 json 字样", "json" in p["messages"][1]["content"].lower())
        check("参考素材进了 prompt", "保湿优先" in p["messages"][1]["content"])
        check("解析出标题", data["title"] == NOTE["title"], data.get("title"))
        check("话题剥掉 # 并限 8 个", all(not t.startswith("#") for t in data["topics"])
              and len(data["topics"]) == 6)
        check("页数按 --pages 截断", len(data["pages"]) == 3, str(len(data["pages"])))

        print("\n[2] 思考模式：忽略 temperature，带上 reasoning_effort")
        reset()
        xhs_auto.AI_THINKING = True
        xhs_auto.AI_TEMPERATURE = None
        xhs_auto.ai_generate_copy("秋冬护肤思路", "", 3)
        p = state["calls"][0]
        check("thinking 显式开启", p.get("thinking") == {"type": "enabled"}, str(p.get("thinking")))
        check("带 reasoning_effort", p.get("reasoning_effort") == "high",
              str(p.get("reasoning_effort")))
        check("不带 temperature", "temperature" not in p, str(p.get("temperature")))
        xhs_auto.AI_THINKING = False
        xhs_auto.AI_TEMPERATURE = 1.3

        print("\n[3] 503 自动重试")
        reset("503-then-ok")
        xhs_auto.ai_generate_copy("话题", "", 2)
        check("重试后成功", len(state["calls"]) == 2, f"调用 {len(state['calls'])} 次")

        print("\n[4] 空内容自动重试（JSON 模式已知偶发问题）")
        reset("empty-then-ok")
        d = xhs_auto.ai_generate_copy("话题", "", 2)
        check("空内容后重试成功", len(state["calls"]) == 2 and d["title"] == NOTE["title"])

        print("\n[5] 坏 json 触发自我修正")
        reset("badjson-then-ok")
        d = xhs_auto.ai_generate_copy("话题", "", 2)
        check("第二次调用带修正指令",
              len(state["calls"]) == 2
              and "不是合法 json" in state["calls"][1]["messages"][-1]["content"])
        check("修正后拿到结构化数据", d["title"] == NOTE["title"])

        print("\n[6] 错误提示可读")
        for mode, kw in (("401", "API Key 无效"), ("402", "余额不足")):
            reset(mode)
            try:
                xhs_auto.ai_generate_copy("话题", "", 2)
                check(f"{mode} 应该抛错", False)
            except xhs_auto.AIError as e:
                check(f"{mode} 提示包含「{kw}」", kw in str(e), str(e))
        reset("401")
        try:
            xhs_auto.AI_API_KEY = ""
            xhs_auto.deepseek_chat([{"role": "user", "content": "hi"}])
            check("缺 Key 应该退出", False)
        except SystemExit as e:
            check("缺 Key 给出明确指引", "DEEPSEEK_API_KEY" in str(e), str(e))
        finally:
            xhs_auto.AI_API_KEY = "sk-fake-for-selftest"

        print("\n[7] --check-api 自检")
        reset()
        xhs_auto.check_api()
        check("自检流程跑通", len(state["calls"]) == 1)

        print("\n[8] 文案落盘")
        reset()
        d = xhs_auto.ai_generate_copy("秋冬护肤思路", "", 3)
        xhs_auto.save_copy_text(d, tmp)
        check("文案.txt 生成", os.path.isfile(os.path.join(tmp, "文案.txt")))
        check("data.json 可被 repost 复用",
              json.load(open(os.path.join(tmp, "data.json"), encoding="utf-8"))["title"] == NOTE["title"])

        print("\n[9] 出图链路（需要 playwright + chromium）")
        try:
            import playwright  # noqa: F401
            reset()
            d = xhs_auto.ai_generate_copy("秋冬护肤思路", "", 3)
            paths = xhs_auto.render_images(d, "秋冬护肤思路", "cream", tmp)
            ok = len(paths) == 3 and all(os.path.getsize(p) > 5000 for p in paths)
            check("生成 3 张 PNG 且非空", ok, str([os.path.getsize(p) for p in paths]))
        except ImportError:
            print("  ⏭  跳过：本机没装 playwright")

        print("\n[10] markdown 标记清理（小红书不渲染 markdown，** 会原样显示）")
        dirty = "**加粗**的要点\n## 小标题\n- 列表项\n`代码`\n*斜体*   "
        cleaned = xhs_auto.clean_text(dirty)
        check("去掉 ** 加粗**", "**" not in cleaned, cleaned)
        check("去掉行首 ## 标题", not cleaned.startswith("##"), cleaned)
        check("去掉行首 - 列表符", "- 列表项" not in cleaned and "列表项" in cleaned, cleaned)
        check("去掉 `反引号`", "`" not in cleaned, cleaned)
        check("去掉 *斜体*", "*" not in cleaned, cleaned)
        check("保留 #话题（不带空格的不误伤）", xhs_auto.clean_text("#护肤 #保湿") == "#护肤 #保湿")
        state["calls"].clear()
        state["mode"] = "markdown"
        d = xhs_auto.ai_generate_copy("话题", "", 3)
        body_ok = "**" not in d["body"]
        check("端到端：正文不含 md 标记", body_ok, d["body"][:60])
        check("端到端：卡片要点不含 md 标记",
              all("**" not in i for p in d["pages"] for i in p.get("items", [])))
        state["mode"] = "ok"

        print("\n[11] config_local.py 真的生效（曾因 import * 静默失效）")
        import subprocess
        here = os.path.dirname(os.path.abspath(__file__))
        for label, content, want_key, want_model in (
            ("DEEPSEEK_* 写法",
             'DEEPSEEK_API_KEY = "sk-from-config"\nDEEPSEEK_MODEL = "model-from-config"\n'
             'DEEPSEEK_TEMPERATURE = 1.7\n', "sk-from-config", "model-from-config"),
            ("旧的 AI_* 写法（兼容）",
             'AI_API_KEY = "sk-legacy-config"\nAI_MODEL = "legacy-model"\n',
             "sk-legacy-config", "legacy-model"),
        ):
            probe = tempfile.mkdtemp(prefix="xhs_cfg_")
            try:
                shutil.copy(os.path.join(here, "xhs_auto.py"), probe)
                with open(os.path.join(probe, "config_local.py"), "w", encoding="utf-8") as f:
                    f.write(content)
                env = {k: v for k, v in os.environ.items()
                       if not k.startswith(("DEEPSEEK_", "XHS_AI_"))}
                out = subprocess.run(
                    [sys.executable, "-c",
                     "import xhs_auto as x; print('|'.join([x.AI_API_KEY, x.AI_MODEL,"
                     " str(x.AI_TEMPERATURE), str(x.AI_THINKING)]))"],
                    cwd=probe, capture_output=True, text=True, env=env)
                got = (out.stdout or "").strip()
                check(f"{label} 的 Key 被读到", want_key in got, got or out.stderr[-300:])
                check(f"{label} 的模型被读到", want_model in got, got)
            finally:
                shutil.rmtree(probe, ignore_errors=True)
        # 环境变量优先级低于 config_local.py，这里顺带确认默认值没被写死
        probe = tempfile.mkdtemp(prefix="xhs_cfg_")
        try:
            shutil.copy(os.path.join(here, "xhs_auto.py"), probe)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("DEEPSEEK_", "XHS_AI_"))}
            env["DEEPSEEK_API_KEY"] = "sk-from-env"
            env["DEEPSEEK_MODEL"] = "model-from-env"
            out = subprocess.run(
                [sys.executable, "-c",
                 "import xhs_auto as x; print(x.AI_API_KEY, x.AI_MODEL)"],
                cwd=probe, capture_output=True, text=True, env=env)
            check("没有 config_local 时读环境变量",
                  "sk-from-env" in out.stdout and "model-from-env" in out.stdout,
                  out.stdout or out.stderr[-300:])
        finally:
            shutil.rmtree(probe, ignore_errors=True)

        print("\n[12] 命令行：不加引号的多词话题")
        probe = tempfile.mkdtemp(prefix="xhs_cli_")
        try:
            shutil.copy(os.path.join(here, "xhs_auto.py"), probe)
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("DEEPSEEK_", "XHS_AI_"))}
            # 故意不给 Key：解析通过的话报"缺少 Key"，解析失败会报 unrecognized arguments
            out = subprocess.run(
                [sys.executable, "xhs_auto.py", "deepseek", "4.1", "--no-publish"],
                cwd=probe, capture_output=True, text=True, env=env, timeout=60)
            blob = (out.stdout or "") + (out.stderr or "")
            check("`xhs deepseek 4.1` 不再报 unrecognized arguments",
                  "unrecognized arguments" not in blob, blob[-200:])
            check("多词话题被接受（走到缺 Key 那步）",
                  "缺少 DeepSeek API Key" in blob, blob[-200:])
        finally:
            shutil.rmtree(probe, ignore_errors=True)
    finally:
        server.shutdown()
        keep = os.path.join(tempfile.gettempdir(), "xhs_selftest_last")
        shutil.rmtree(keep, ignore_errors=True)
        try:
            shutil.copytree(tmp, keep)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        for name in FAIL:
            print(f"  失败：{name}")
        print(f"（产物保留在 {os.path.join(tempfile.gettempdir(), 'xhs_selftest_last')}）")
        sys.exit(1)
    print("全部通过 ✅")


if __name__ == "__main__":
    main()
