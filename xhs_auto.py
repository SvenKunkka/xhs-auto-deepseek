#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xhs_auto.py — 小红书图文笔记全自动生成 + 发布（DeepSeek 文案版）

流程：
  1. 联网搜索话题相关内容做参考素材（必应，失败则跳过）
  2. DeepSeek 生成标题、正文、话题标签、图片卡片内容（去 AI 味）
  3. HTML 模板 + Playwright 截图，生成 3:4 小红书风格封面图和内容卡
  4. Playwright 打开小红书创作者中心，自动上传图片、填入标题/正文/话题
     （首次运行需扫码登录一次，之后记住登录态）

用法：
  python3 xhs_auto.py "秋冬护肤思路"
  python3 xhs_auto.py "成都周末遛娃" --pages 4 --theme tea
  python3 xhs_auto.py "话题" --no-publish        # 只生成不发布
  python3 xhs_auto.py "话题" --yes               # 发布前不再人工确认
  python3 xhs_auto.py --check-api                # 只测 DeepSeek Key 通不通

文案模型配置（环境变量 或 同目录 config_local.py，后者优先级更高）：
  DEEPSEEK_API_KEY    必填，platform.deepseek.com 申请
  DEEPSEEK_MODEL      默认 deepseek-v4-pro（想省钱可换 deepseek-v4-flash）
  DEEPSEEK_THINKING   1 = 开启思考模式（更准、更慢、更贵），默认 0
"""

import argparse
import datetime
import json
import os
import random
import re
import sys
import time

# ============================================================
# 1. 配置常量块
# ============================================================
# DeepSeek 配置：先读环境变量，再用脚本同目录的 config_local.py 覆盖
# （config_local.py 已在 .gitignore 里，别把 Key 提交上去）
# 兼容旧的 XHS_AI_* 环境变量名和旧的 AI_* 配置写法，老配置不会失效。
def _env(*names, default=""):
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return default

AI_BASE_URL = _env("DEEPSEEK_BASE_URL", "XHS_AI_BASE_URL", default="https://api.deepseek.com")
AI_API_KEY  = _env("DEEPSEEK_API_KEY", "XHS_AI_API_KEY")
AI_MODEL    = _env("DEEPSEEK_MODEL", "XHS_AI_MODEL", default="deepseek-v4-pro")

# 思考模式：关闭时可用 temperature 控制文风发散度；开启时 DeepSeek 会忽略
# temperature/top_p，输出更稳但更慢更贵。写文案默认关闭。
AI_THINKING = _env("DEEPSEEK_THINKING", default="0").lower() in ("1", "true", "yes", "on")
AI_REASONING_EFFORT = _env("DEEPSEEK_REASONING_EFFORT", default="high")   # low / high / max
AI_TEMPERATURE      = float(_env("DEEPSEEK_TEMPERATURE", default="1.3"))  # 创作类偏高更活
AI_MAX_TOKENS       = int(_env("DEEPSEEK_MAX_TOKENS", default="8192"))
AI_TIMEOUT          = int(_env("DEEPSEEK_TIMEOUT", default="180"))        # 单次请求超时秒数
AI_MAX_RETRIES      = int(_env("DEEPSEEK_MAX_RETRIES", default="3"))

# config_local.py 覆盖：用显式 getattr 而不是 `import *`。
# `import *` 只会把 DEEPSEEK_API_KEY 这个名字塞进模块，而代码读的是 AI_API_KEY，
# 结果就是配置静默失效（key 明明写了却报"没读到"）。这里两种写法都认。
try:
    import config_local as _cfg

    def _cfg_get(*names, fallback):
        for n in names:
            v = getattr(_cfg, n, None)
            if v not in (None, ""):
                return v
        return fallback

    AI_BASE_URL = _cfg_get("DEEPSEEK_BASE_URL", "AI_BASE_URL", fallback=AI_BASE_URL)
    AI_API_KEY  = _cfg_get("DEEPSEEK_API_KEY", "XHS_AI_API_KEY", "AI_API_KEY", fallback=AI_API_KEY)
    AI_MODEL    = _cfg_get("DEEPSEEK_MODEL", "AI_MODEL", fallback=AI_MODEL)
    AI_THINKING = bool(_cfg_get("DEEPSEEK_THINKING", fallback=AI_THINKING))
    AI_REASONING_EFFORT = str(_cfg_get("DEEPSEEK_REASONING_EFFORT", fallback=AI_REASONING_EFFORT))
    AI_TEMPERATURE = float(_cfg_get("DEEPSEEK_TEMPERATURE", fallback=AI_TEMPERATURE))
    AI_MAX_TOKENS  = int(_cfg_get("DEEPSEEK_MAX_TOKENS", fallback=AI_MAX_TOKENS))
    AI_TIMEOUT     = int(_cfg_get("DEEPSEEK_TIMEOUT", fallback=AI_TIMEOUT))
    AI_MAX_RETRIES = int(_cfg_get("DEEPSEEK_MAX_RETRIES", fallback=AI_MAX_RETRIES))
except ImportError:
    pass

# 思考模式会忽略 temperature，置空以免用户以为自己调的生效了
AI_TEMPERATURE_DEFAULT = AI_TEMPERATURE     # 仅用于 --help 显示
if AI_THINKING:
    AI_TEMPERATURE = None

OUT_ROOT      = os.path.expanduser("~/Downloads/xhs_auto")
def _system_chrome_profile():
    """系统安装的 Google Chrome 的默认 profile 路径；找不到返回 None。"""
    override = _env("XHS_CHROME_PROFILE")
    if override:
        return os.path.expanduser(override)
    if sys.platform == "darwin":
        p = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    elif sys.platform.startswith("win"):
        p = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")
    else:
        p = os.path.expanduser("~/.config/google-chrome")
    return p if p and os.path.isdir(p) else None


# 登录态放哪。
#
# 曾尝试默认复用「你日常那个 Chrome 的 profile」，避免反复扫码，但在
# Chrome 153 + Playwright 1.62 上实测走不通，两条路都堵死：
#   1) launch_persistent_context 指向现有大 profile：Chrome 主进程起来了，
#      但 50 秒都不创建窗口，Playwright 握手超时（全新 profile 0.9 秒就成功，
#      所以不是 channel 的问题，是这个 profile 无法被自动化启动）。
#   2) 连正在运行的 Chrome（connect_over_cdp）：Chrome 136+ 直接拒绝
#      "DevTools remote debugging requires a non-default data directory"。
# 所以默认回到独立 profile：扫码一次，登录态就存在这里，之后不用再扫。
#
# 想要共用系统 Chrome（比如你的环境能跑通）：
#   export XHS_USE_SYSTEM_CHROME=1
# 或者直接指定任意目录：
#   export XHS_PROFILE_DIR=/path/to/profile
_OWN_PROFILE = os.path.join(OUT_ROOT, "browser_profile")
SYSTEM_CHROME_PROFILE = _system_chrome_profile()
_WANT_SYSTEM = _env("XHS_USE_SYSTEM_CHROME", default="0").lower() in ("1", "true", "yes", "on")

if _env("XHS_PROFILE_DIR"):
    PROFILE_DIR = os.path.expanduser(_env("XHS_PROFILE_DIR"))
elif _WANT_SYSTEM and SYSTEM_CHROME_PROFILE:
    PROFILE_DIR = SYSTEM_CHROME_PROFILE
else:
    PROFILE_DIR = _OWN_PROFILE

USING_SYSTEM_PROFILE = (SYSTEM_CHROME_PROFILE is not None
                        and PROFILE_DIR == SYSTEM_CHROME_PROFILE)
# 用系统 Chrome 的 profile 就得用系统 Chrome 本体打开
# （内置 Chromium 较旧，打不开新版 Chrome 的 profile）
USE_CHROME_CHANNEL = USING_SYSTEM_PROFILE

XHS_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official&target=image"

IMG_W, IMG_H  = 1242, 1656          # 3:4 竖图
TITLE_MAX     = 20                  # 小红书标题上限 20 字

SEARCH_TIMEOUT  = 12
LOGIN_TIMEOUT_S = int(_env("XHS_LOGIN_TIMEOUT", default="300"))   # 等扫码登录最多 5 分钟

# 配色主题：bg / 卡片 / 主色 / 强调 / 正文色
THEMES = {
    "cream":  dict(bg="#FBF6EE", card="#FFFFFF", main="#C75B39", accent="#E8A87C", text="#3D3229", sub="#8C7B6B"),
    "tea":    dict(bg="#F3EDE4", card="#FFFDF9", main="#8C6244", accent="#C9A87C", text="#42362B", sub="#94836F"),
    "green":  dict(bg="#EFF5EC", card="#FFFFFF", main="#3E6B4F", accent="#9CBD8F", text="#2E3B30", sub="#7E907F"),
    "blue":   dict(bg="#EDF3F8", card="#FFFFFF", main="#2F5D8A", accent="#8FB6D9", text="#2A3540", sub="#7C8B99"),
    "pink":   dict(bg="#FBEFF0", card="#FFFFFF", main="#C04B5E", accent="#ECA7B0", text="#433235", sub="#9A8186"),
}

# ============================================================
# 2. Prompt 模板
# ============================================================
COPY_PROMPT = """你是一个小红书重度用户，自己运营着一个不错的账号。现在围绕话题「{TOPIC}」写一篇图文笔记。

{REFERENCE_BLOCK}

【写作要求 —— 必须严格遵守，目标是让人完全看不出是 AI 写的】
1. 用第一人称真实经历的口吻，像跟朋友发微信一样说话，可以有点小情绪、小吐槽
2. 加入具体的细节：具体数字、具体场景、具体的失败教训，宁可编得具体也不要写得空泛
3. 禁止出现这些 AI 腔：首先/其次/最后、总之/综上所述、值得注意的是、不仅...还...、赋能、闭环、攻略来啦、宝子们（最多出现一次）、家人们（最多一次）、排比句堆砌
4. 句子要短，多换行，一段不超过 3 行。全文带 3~6 个 emoji，放在段首或句尾点缀，别堆在一起
5. 标题不超过 {TITLE_MAX} 个字，要有钩子（数字、反差、悬念、利益点选一种），别用感叹号堆砌
6. 正文 350~550 字，结尾自然收住，可以抛个问题引导评论，但别写"你们觉得呢？"这种烂大街的
7. 话题标签 5~8 个，由热门大词 + 精准长尾词组成，不带 # 号
8. 不要用任何 markdown 语法：不要 **加粗**、不要 # 标题、不要 `代码`、不要 - 列表符号，
   小红书正文不渲染 markdown，写了会原样显示成一堆星号和井号

【图片卡片内容】
同时为这篇笔记设计 {N_PAGES} 张图的文字内容：
- 第 1 张是封面：一句大字主标题（10 字以内，可以和笔记标题不同、更夸张更抓眼）+ 一句小字副标题（16 字以内）+ 一个角标短词（2~6 字，如"亲测""避坑""第3期"）
- 其余是内容卡：每张一个小标题（8 字以内）+ 3~5 条要点，每条要点 14 字以内（超长会被截断），可以用 1 个贴合内容的 emoji 开头，干货密度要高

【输出格式】只输出一个 json 对象，不要任何解释、不要 markdown 代码块、不要在 json 前后加任何字：
{{
  "title": "笔记标题",
  "body": "正文（用\\n换行）",
  "topics": ["话题1", "话题2"],
  "pages": [
    {{"type": "cover", "line1": "封面主标题", "line2": "副标题", "badge": "角标"}},
    {{"type": "content", "heading": "卡片标题", "items": ["要点1", "要点2", "要点3"]}}
  ]
}}"""

# DeepSeek 的 JSON Output 要求 system/user 提示里出现 "json" 字样，这里双重保险
SYSTEM_PROMPT = (
    "你是中文社交媒体写作助手，擅长写小红书图文笔记。"
    "你的每次回复都必须是合法、可直接解析的 json，不含 markdown 代码块和多余解释。"
)

REFERENCE_TPL = """【参考素材 —— 这是从网上搜到的相关内容，提炼里面有用的信息点融进笔记，但表达必须完全是你自己的话，不许照抄句子】
{REF}
"""

# ============================================================
# 3. HTML 模板（图片卡片，__TOKEN__ 占位替换）
# ============================================================
HTML_BASE = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:__W__px; height:__H__px; overflow:hidden;
  font-family:"PingFang SC","Hiragino Sans GB",sans-serif; }
body { background:__BG__; display:flex; align-items:center; justify-content:center; }
.deco { position:absolute; border-radius:50%; opacity:.35; }
__EXTRA_CSS__
</style></head><body>__BODY__</body></html>"""

COVER_CSS = """
.wrap { width:100%; height:100%; padding:96px 88px; position:relative; overflow:hidden;
  display:flex; flex-direction:column; justify-content:center; }
.dotgrid { position:absolute; top:110px; left:88px; width:300px; height:130px; opacity:.45;
  background-image:radial-gradient(__ACCENT__ 7px, transparent 7px); background-size:48px 44px; }
.badge { position:absolute; top:96px; right:88px; background:__MAIN__; color:#fff;
  font-size:38px; font-weight:600; padding:20px 42px; letter-spacing:3px;
  border-radius:48px 48px 48px 10px; box-shadow:0 14px 34px __MAIN__44; }
.kicker { display:flex; align-items:center; color:__SUB__; font-size:40px;
  letter-spacing:8px; margin-bottom:54px; overflow:hidden; white-space:nowrap; }
.kicker::before { content:""; width:64px; height:10px; border-radius:5px;
  background:__MAIN__; margin-right:24px; flex:none; }
.line1 { font-size:__T1SIZE__px; font-weight:800; color:__TEXT__; line-height:1.24;
  letter-spacing:3px; margin-bottom:56px; max-height:2.6em; overflow:hidden; }
.line1 em { font-style:normal; color:__MAIN__;
  background:linear-gradient(transparent 70%, __ACCENT__66 70%); border-radius:4px; }
.line2box { display:inline-block; align-self:flex-start; max-width:100%; background:__CARD__;
  border:3px solid __ACCENT__; color:__SUB__; border-radius:26px; padding:28px 42px;
  font-size:46px; line-height:1.45; box-shadow:10px 10px 0 __ACCENT__55;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.foot { position:absolute; bottom:88px; left:88px; right:88px; display:flex;
  justify-content:space-between; align-items:center; color:__SUB__; font-size:36px;
  letter-spacing:2px; border-top:3px dashed __ACCENT__88; padding-top:38px; }
"""

COVER_BODY = """<div class="wrap">
<div class="deco" style="width:560px;height:560px;background:__ACCENT__;top:-200px;left:-200px;opacity:.30;"></div>
<div class="deco" style="width:420px;height:420px;background:__MAIN__;bottom:-160px;right:-140px;opacity:.14;"></div>
<div class="deco" style="width:180px;height:180px;border:14px solid __ACCENT__;background:transparent;opacity:.35;bottom:300px;right:120px;"></div>
<div class="dotgrid"></div>
<div class="badge">__BADGE__</div>
<div class="kicker">__KICKER__</div>
<div class="line1">__LINE1__</div>
<div class="line2box">__LINE2__</div>
<div class="foot"><span>持续更新中</span><span>左滑看干货 →</span></div>
</div>"""

CONTENT_CSS = """
.wrap { width:100%; height:100%; padding:72px 64px; position:relative; overflow:hidden; }
.card { width:100%; height:100%; background:__CARD__; border-radius:44px;
  padding:80px 72px; border:2px solid __ACCENT__40;
  box-shadow:0 24px 70px rgba(0,0,0,.07);
  display:flex; flex-direction:column; overflow:hidden; position:relative; }
.corner { position:absolute; top:-90px; right:-90px; width:280px; height:280px;
  border-radius:50%; background:__ACCENT__; opacity:.18; }
.head { display:flex; align-items:center; margin-bottom:30px; }
.head .num { font-size:42px; font-weight:800; color:#fff;
  background:linear-gradient(135deg,__MAIN__,__ACCENT__);
  width:92px; height:92px; border-radius:26px; display:flex; align-items:center;
  justify-content:center; margin-right:34px; flex-shrink:0;
  box-shadow:0 10px 24px __MAIN__40; }
.head .h { font-size:__HSIZE__px; font-weight:800; color:__TEXT__; line-height:1.2;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.hbar { width:150px; height:12px; border-radius:6px; margin:0 0 58px 126px;
  background:linear-gradient(90deg,__MAIN__,__ACCENT__); }
.item { background:__BG__; border-radius:30px; padding:40px 44px; margin-bottom:34px;
  display:flex; align-items:flex-start; overflow:hidden; }
.item .mark { width:20px; height:20px; border-radius:50%; background:__MAIN__;
  outline:8px solid __ACCENT__44; margin:24px 36px 0 4px; flex-shrink:0; }
.item .t { font-size:__ISIZE__px; color:__TEXT__; line-height:1.5;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }
.pagefoot { margin-top:auto; display:flex; justify-content:space-between; align-items:center;
  color:__SUB__; font-size:36px; padding-top:38px; border-top:3px dashed __ACCENT__88; }
.pagefoot .pg b { color:__MAIN__; font-size:42px; }
"""

CONTENT_BODY = """<div class="wrap">
<div class="card">
<div class="corner"></div>
<div class="head"><div class="num">__NUM__</div><div class="h">__HEADING__</div></div>
<div class="hbar"></div>
__ITEMS__
<div class="pagefoot"><span>__TOPIC__</span><span class="pg"><b>__PAGE__</b> / __TOTAL__</span></div>
</div></div>"""

# ============================================================
# 4. 工具函数
# ============================================================
def log(msg):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def pause_for_user(msg, fallback_wait=20):
    """终端等用户回车；非交互环境（stdin 不可用）则等待固定秒数后继续。
    只用于"继续了也没关系"的场合；发布确认必须用 confirm_publish。"""
    try:
        input(msg)
    except (EOFError, OSError):
        log(f"（非交互模式，{fallback_wait} 秒后自动继续）")
        time.sleep(fallback_wait)

def confirm_publish(msg):
    """发布前的确认，fail-closed：拿不到明确确认就绝不发布。

    非交互环境（nohup / cron / 管道 / 被程序调用）里 input() 会立刻
    EOFError，若沿用 pause_for_user 的"等 20 秒继续"就会在没人确认的
    情况下真的发帖。这里一律返回 False，把发布留给人工。
    """
    if not sys.stdin or not sys.stdin.isatty():
        log("!! 当前不是交互式终端，出于安全没有点击发布。")
        log("   图片和文案都已生成，可手动发布；确实想全自动请显式加 --yes")
        return False
    try:
        input(msg)
        return True
    except (EOFError, OSError):
        log("!! 读不到确认输入，出于安全没有点击发布。")
        return False
    except KeyboardInterrupt:
        log("\n已取消发布。")
        return False

def strip_tags(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = re.sub(r"&[a-zA-Z]+;", " ", html)
    return re.sub(r"\s+", " ", html).strip()

def parse_ai_json(text):
    text = re.sub(r"```(?:json)?", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("AI 输出中找不到 JSON")
    return json.loads(text[start:end + 1])

def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def fill(tpl, mapping):
    for k, v in mapping.items():
        tpl = tpl.replace(f"__{k}__", str(v))
    return tpl

def clip(s, n):
    """超长截断加省略号，防止文字溢出卡片。"""
    s = str(s or "").strip()
    return s if len(s) <= n else s[:n - 1] + "…"

def clean_text(s):
    """剥掉模型偶尔混进来的 markdown 标记。
    小红书正文和图片卡片都不渲染 markdown，**加粗** 会原样显示成星号，必须清掉。"""
    s = str(s or "")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)          # **加粗**
    s = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", s)   # *斜体*
    s = re.sub(r"__(.+?)__", r"\1", s)              # __加粗__
    s = re.sub(r"`([^`]*)`", r"\1", s)              # `代码`
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)    # 行首 "## 标题"（带空格才算，避免误伤 #话题）
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.M)  # 行首 "- 列表项"
    s = "\n".join(line.rstrip() for line in s.split("\n"))
    return s.strip()

# ============================================================
# 5. Step 函数
# ============================================================
def search_reference(topic):
    """必应搜索话题，抓取摘要 + 头部网页正文做参考素材。失败返回空串。"""
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    chunks = []
    try:
        log(f"搜索参考素材：{topic}")
        r = requests.get("https://www.bing.com/search",
                         params={"q": topic, "setlang": "zh-hans"},
                         headers=headers, timeout=SEARCH_TIMEOUT)
        blocks = re.findall(r'(?s)<li[^>]*class="b_algo[^"]*".*?</li>', r.text)
        links = []
        for b in blocks[:8]:
            m = re.search(r'(?s)<h2[^>]*><a[^>]*href="(http[^"]+)"[^>]*>(.*?)</a>', b)
            snippet = strip_tags(re.sub(r"(?s)<h2>.*?</h2>", "", b))[:200]
            if m:
                title = strip_tags(m.group(2))
                links.append(m.group(1))
                chunks.append(f"- {title}：{snippet}")
        # 抓前 2 个网页的正文片段
        for url in links[:2]:
            try:
                pr = requests.get(url, headers=headers, timeout=SEARCH_TIMEOUT)
                pr.encoding = pr.apparent_encoding or "utf-8"
                body = strip_tags(pr.text)
                if len(body) > 300:
                    chunks.append(f"- 网页正文摘录：{body[:1200]}")
            except Exception:
                continue
    except Exception as e:
        log(f"搜索失败（不影响后续，AI 将凭自身知识写作）：{e}")
    ref = "\n".join(chunks)
    if ref:
        log(f"拿到参考素材 {len(ref)} 字")
    return ref[:4000]

# ------------------------------------------------------------
# DeepSeek 调用层（OpenAI 兼容协议，直接用 requests，不额外装 SDK）
# ------------------------------------------------------------
class AIError(RuntimeError):
    """API 调用失败，带可读的中文排查提示。"""

_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}

def _http_error_hint(status, body):
    if status == 401:
        return "API Key 无效，去 platform.deepseek.com 重新生成后填进 DEEPSEEK_API_KEY"
    if status == 402:
        return "DeepSeek 账户余额不足，去 platform.deepseek.com 充值"
    if status == 429:
        return "触发限流（请求太频繁），等几秒重试或降低发布频率"
    if status == 400:
        return ("请求参数被拒，确认 DEEPSEEK_MODEL 是 deepseek-v4-pro 或 "
                "deepseek-v4-flash")
    if status == 503:
        return "DeepSeek 服务繁忙，稍后重试"
    return f"HTTP {status}：{body[:300]}"

def deepseek_chat(messages, json_mode=False, max_tokens=None, temperature=None):
    """调 DeepSeek /chat/completions，失败按指数退避重试。
    返回 (正文, usage, reasoning_content)；reasoning_content 仅思考模式有值。"""
    import requests
    if not AI_API_KEY:
        sys.exit("缺少 DeepSeek API Key：请设置环境变量 DEEPSEEK_API_KEY，"
                 "或在脚本同目录创建 config_local.py 写入 "
                 "DEEPSEEK_API_KEY = \"sk-...\"")
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": AI_MODEL,
        "messages": messages,
        "max_tokens": max_tokens or AI_MAX_TOKENS,
        "stream": False,
        # 显式声明思考模式，不依赖服务端默认值（默认是 enabled）
        "thinking": {"type": "enabled" if AI_THINKING else "disabled"},
    }
    if AI_THINKING:
        payload["reasoning_effort"] = AI_REASONING_EFFORT   # low / high / max
    elif temperature is not None:
        payload["temperature"] = temperature                # 思考模式下该参数无效
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {AI_API_KEY}",
               "Content-Type": "application/json"}
    last_err = None
    for attempt in range(1, AI_MAX_RETRIES + 1):
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=AI_TIMEOUT)
        except requests.RequestException as e:
            last_err = AIError(f"网络请求失败：{e}")
            log(f"  第 {attempt}/{AI_MAX_RETRIES} 次请求异常：{e}")
        else:
            if r.status_code == 200:
                data = r.json()
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                if usage:
                    log(f"  token：输入 {usage.get('prompt_tokens', '?')}"
                        f"（缓存命中 {usage.get('prompt_cache_hit_tokens', 0)}）"
                        f" / 输出 {usage.get('completion_tokens', '?')}")
                if choice.get("finish_reason") == "length":
                    log("!! 输出被 max_tokens 截断，json 可能不完整")
                return msg.get("content") or "", usage, msg.get("reasoning_content")
            last_err = AIError(_http_error_hint(r.status_code, r.text[:500]))
            if r.status_code not in _RETRYABLE_STATUS or attempt == AI_MAX_RETRIES:
                raise last_err
            log(f"  第 {attempt}/{AI_MAX_RETRIES} 次失败（{r.status_code}），重试中…")
        time.sleep(min(2 ** attempt, 8))
    raise last_err

def check_api():
    """自检：验证 Key、列出可用模型，再跑一次最小 json 请求。不碰小红书。"""
    import requests
    if not AI_API_KEY:
        sys.exit("没有读到 DEEPSEEK_API_KEY（环境变量或 config_local.py）")
    log(f"检查 DeepSeek 接口：{AI_BASE_URL}（模型 {AI_MODEL}）")
    try:
        r = requests.get(AI_BASE_URL.rstrip("/") + "/models",
                         headers={"Authorization": f"Bearer {AI_API_KEY}"}, timeout=30)
    except requests.RequestException as e:
        sys.exit(f"连不上 DeepSeek：{e}")
    if r.status_code == 401:
        sys.exit("Key 无效（401），去 platform.deepseek.com 重新生成")
    if r.status_code == 402:
        sys.exit("余额不足（402），去 platform.deepseek.com 充值")
    if r.status_code != 200:
        sys.exit(f"接口返回 {r.status_code}：{r.text[:300]}")
    models = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
    log(f"Key 可用。账号可用模型：{'、'.join(models) or '（接口没返回列表）'}")
    if models and AI_MODEL not in models:
        log(f"!! DEEPSEEK_MODEL={AI_MODEL} 不在上面列表里，可能拼错或没权限")
    content, _, _ = deepseek_chat(
        [{"role": "system", "content": SYSTEM_PROMPT},
         {"role": "user", "content": '只输出 json：{"ok": true}'}],
        json_mode=True, max_tokens=256, temperature=0)
    log(f"最小 json 请求返回：{content.strip()[:120] or '（空内容，DeepSeek JSON 模式的已知偶发问题，重跑即可）'}")
    log("自检通过 ✅")

def ai_generate_copy(topic, reference, n_pages):
    """调 DeepSeek 生成标题/正文/话题/卡片内容，json 解析失败会自动让它修正重来。"""
    ref_block = REFERENCE_TPL.format(REF=reference) if reference else ""
    prompt = COPY_PROMPT.format(TOPIC=topic, REFERENCE_BLOCK=ref_block,
                                TITLE_MAX=TITLE_MAX, N_PAGES=n_pages)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt}]
    mode = "思考模式 " + AI_REASONING_EFFORT if AI_THINKING else f"temperature={AI_TEMPERATURE}"
    log(f"DeepSeek 生成文案中（{AI_MODEL}，{mode}）…")

    data, raw = None, ""
    for attempt in (1, 2):
        raw, _, _ = deepseek_chat(messages, json_mode=True, temperature=AI_TEMPERATURE)
        if not raw.strip():
            # json_object 模式下 DeepSeek 偶发返回空内容，官方已知问题，直接重试
            log(f"  第 {attempt} 次返回空内容，重试…")
            continue
        try:
            data = parse_ai_json(raw)
            break
        except (ValueError, json.JSONDecodeError) as e:
            log(f"  第 {attempt} 次输出不是合法 json（{e}），要求模型修正…")
            messages += [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": "上面这段不是合法 json。"
                                            "请只输出修正后的 json 对象本身，"
                                            "不要代码块、不要任何解释。"},
            ]
    if data is None:
        os.makedirs(OUT_ROOT, exist_ok=True)
        dump = os.path.join(OUT_ROOT, "ai_raw_output.txt")
        with open(dump, "w", encoding="utf-8") as f:
            f.write(raw)
        sys.exit(f"DeepSeek 两次都没返回可用 json，原始输出已存到 {dump}")

    # 兜底校验：模型偶尔漏字段或把类型写歪
    data["title"] = clean_text(data.get("title", topic))[:TITLE_MAX]
    data["body"] = clean_text(data.get("body") or "")
    if not data["body"]:
        sys.exit("DeepSeek 返回的正文是空的，重跑一次试试")
    raw_topics = data.get("topics") or []
    if isinstance(raw_topics, str):        # 偶尔把数组写成逗号串
        raw_topics = re.split(r"[,，、\s]+", raw_topics)
    data["topics"] = [clean_text(t).lstrip("#") for t in raw_topics if str(t).strip()][:8]
    data["topics"] = [t for t in data["topics"] if t][:8]
    pages = [p for p in (data.get("pages") or []) if isinstance(p, dict)]
    if not pages or pages[0].get("type") != "cover":
        pages.insert(0, {"type": "cover", "line1": data["title"][:10],
                         "line2": topic, "badge": "干货"})
    for p in pages:
        for k in ("line1", "line2", "badge", "heading"):     # 卡片文字同样去 markdown
            if k in p:
                p[k] = clean_text(p[k])
        if p.get("type") != "cover" and not isinstance(p.get("items"), list):
            p["items"] = [p["items"]] if p["items"] else []   # items 必须是 list，否则渲染切片会炸
        if isinstance(p.get("items"), list):
            p["items"] = [clean_text(i) for i in p["items"]]
    data["pages"] = pages[:n_pages]
    body_len = len(data["body"])
    if not 300 <= body_len <= 600:
        log(f"!! 正文 {body_len} 字，偏离 350~550 字的预期区间，可重跑或调 prompt")
    log(f"文案完成：《{data['title']}》 正文 {body_len} 字，"
        f"{len(data['topics'])} 个话题，{len(data['pages'])} 张图")
    return data

def render_images(data, topic, theme_name, out_dir):
    """HTML 模板渲染成 3:4 PNG。返回图片路径列表。"""
    from playwright.sync_api import sync_playwright
    theme = THEMES[theme_name]
    pages = data["pages"]
    total = len(pages)
    paths = []
    log(f"生成图片（主题 {theme_name}，共 {total} 张）…")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width": IMG_W, "height": IMG_H})
        for i, card in enumerate(pages):
            common = dict(W=IMG_W, H=IMG_H, BG=theme["bg"], CARD=theme["card"],
                          MAIN=theme["main"], ACCENT=theme["accent"],
                          TEXT=theme["text"], SUB=theme["sub"])
            if card.get("type") == "cover":
                raw1 = clip(card.get("line1", data["title"]), 12)
                # 主标题按字数自动缩字号，避免溢出
                n = len(raw1)
                t1size = 150 if n <= 6 else 132 if n <= 8 else 112 if n <= 10 else 98
                line1 = esc(raw1)
                if n > 4:                       # 后半句加高亮色
                    cut = n // 2
                    line1 = f"{esc(raw1[:cut])}<em>{esc(raw1[cut:])}</em>"
                body_html = fill(COVER_BODY, dict(common,
                                 BADGE=esc(clip(card.get("badge", "干货"), 6)),
                                 KICKER=esc(clip(topic, 14)),
                                 LINE1=line1,
                                 LINE2=esc(clip(card.get("line2", topic), 18))))
                css = fill(COVER_CSS, dict(common, T1SIZE=t1size))
            else:
                raw_items = [clip(it, 26) for it in card.get("items", [])[:5]]
                isize = 52 if all(len(it) <= 16 for it in raw_items) else 46
                items = "".join(
                    f'<div class="item"><div class="mark"></div><div class="t">{esc(it)}</div></div>'
                    for it in raw_items)
                heading = clip(card.get("heading", ""), 12)
                hsize = 74 if len(heading) <= 8 else 62
                body_html = fill(CONTENT_BODY, dict(common,
                                 NUM=f"{i:02d}", HEADING=esc(heading),
                                 ITEMS=items, TOPIC=esc(clip(topic, 14)),
                                 PAGE=i + 1, TOTAL=total))
                css = fill(CONTENT_CSS, dict(common, HSIZE=hsize, ISIZE=isize))
            html = fill(HTML_BASE, dict(common, EXTRA_CSS=css, BODY=body_html))
            pg.set_content(html)
            pg.wait_for_timeout(150)
            path = os.path.join(out_dir, f"{i+1:02d}_{'cover' if card.get('type')=='cover' else 'card'}.png")
            pg.screenshot(path=path)
            paths.append(path)
            log(f"  生成 {os.path.basename(path)}")
        browser.close()
    return paths

def save_copy_text(data, out_dir):
    """文案落盘，自动化失败时也能手动复制粘贴。"""
    txt = (f"【标题】\n{data['title']}\n\n【正文】\n{data['body']}\n\n"
           f"【话题】\n" + " ".join("#" + t for t in data["topics"]) + "\n")
    path = os.path.join(out_dir, "文案.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)
    with open(os.path.join(out_dir, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"文案已保存：{path}")

def _cursor_to_end(page):
    """把光标强制移到编辑器全文末尾，防止话题标签插进正文中间。"""
    for combo in ("Meta+ArrowDown", "Control+End"):
        try:
            page.keyboard.press(combo)
        except Exception:
            pass

def _first_visible(page, selectors, timeout_each=3000):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=timeout_each)
            return loc
        except Exception:
            continue
    return None

def _clean_profile_locks():
    """清掉上次浏览器异常退出残留的单实例锁文件，否则 Chrome 拒绝启动。
    只对自己那套独立 profile 用；系统 Chrome 的 profile 绝不擅自删锁。"""
    if USING_SYSTEM_PROFILE:
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = os.path.join(PROFILE_DIR, name)
        try:
            if os.path.lexists(path):
                os.remove(path)
        except OSError:
            pass

def _app_running(name):
    """某个 App 是否在运行（用 pgrep，避免依赖 psutil）。"""
    import subprocess
    try:
        return subprocess.run(["pgrep", "-x", name],
                              capture_output=True).returncode == 0
    except Exception:
        return False

def _launch_browser(p):
    """启动浏览器。

    默认用独立 profile（~/Downloads/xhs_auto/browser_profile）：扫码一次后
    登录态长期保存在那里，完全不碰你日常浏览器。

    复用系统 Chrome 的 profile 需要过两道闸，因为它会造成不可逆的数据损坏。
    """
    if USING_SYSTEM_PROFILE and not _env("XHS_ACCEPT_PROFILE_RISK"):
        raise RuntimeError(
            "拒绝用 Playwright 启动你日常的 Chrome profile。\n"
            "    原因：Playwright 启动 Chrome 时会自动附加这两个参数\n"
            "      --use-mock-keychain --password-store=basic\n"
            "    于是 Chrome 改用「假钥匙串」，读不出用真实钥匙串加密的 cookie，\n"
            "    会把这些 cookie 当作无效数据清掉。实测后果是浏览器退出登录、\n"
            "    大量网站需要重新登录，且无法恢复。本项目真实踩过这个坑。\n"
            "    · 推荐：去掉 XHS_USE_SYSTEM_CHROME，改用独立 profile 扫码一次\n"
            "    · 确实要冒险：再设 XHS_ACCEPT_PROFILE_RISK=1（后果自负）")

    if USING_SYSTEM_PROFILE and _app_running("Google Chrome"):
        raise RuntimeError(
            "检测到你的 Google Chrome 正在运行，它占着 profile，自动化起不来。\n"
            "    → 请先完全退出 Chrome（⌘Q，不是只关窗口），再重跑本命令。\n"
            "    → 如果希望用一套独立 profile、完全不打扰日常浏览器：\n"
            "       去掉 XHS_USE_SYSTEM_CHROME 即可")

    kwargs = dict(headless=False, viewport={"width": 1440, "height": 900},
                  args=["--disable-blink-features=AutomationControlled"])
    if USE_CHROME_CHANNEL:
        kwargs["channel"] = "chrome"        # 用系统装的 Google Chrome 本体

    last_err = None
    for attempt in range(2):
        _clean_profile_locks()
        try:
            return p.chromium.launch_persistent_context(PROFILE_DIR, **kwargs)
        except Exception as e:
            last_err = e
            msg = str(e)
            if USE_CHROME_CHANNEL and ("channel" in msg or "chrome" in msg.lower()):
                # 系统 Chrome 不可用时退回内置 Chromium
                kwargs.pop("channel", None)
                continue
            if USING_SYSTEM_PROFILE:
                break                        # 系统 profile 不要再盲目重试
            time.sleep(2)
    raise RuntimeError(
        f"浏览器启动失败（{last_err}）\n"
        f"    当前 profile：{PROFILE_DIR}\n"
        f"    · 用系统 Chrome 时请先 ⌘Q 完全退出 Chrome\n"
        f"    · 用内置 Chromium 时如提示缺浏览器，运行: playwright install chromium")

def _page_has_visible(page, selectors):
    """页面上是否出现了任一可见元素。隐藏模板节点不能作为登录证据。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                return True
        except Exception:
            continue
    return False


# 发布页的强阳性信号。小红书新版创作者中心即使没有旧的 web_session cookie，
# 只要这些入口可见且没有登录遮罩，就已经能正常上传。
PUBLISH_READY_SELECTORS = (
    'text=上传图片', 'text=上传图文',
    'input[placeholder*="标题"]', 'div.ql-editor',
    'div[contenteditable="true"]',
)
# 明确的登录阻塞信号。不能再用泛化的 text=登录 / [class*=login]：页面菜单、
# 隐藏模板和“退出登录”也会命中，导致已登录页面被误判。
LOGIN_BLOCKER_SELECTORS = (
    'text=扫码登录', 'text=请扫码登录', 'text=手机号登录',
    'img[src*="qrcode"]', '[class*="qrcode"]',
    '[class*="login-modal"]', '[class*="login-mask"]',
)
LOGIN_PROMPT_GRACE_S = 5


def _has_login_cookie(page):
    """检查旧版创作者中心常见的 web_session 登录凭证。

    注意：creator 平台还会种 access-token-creator / x-user-id-creator /
    galaxy_creator_session_id 等 cookie，这些**未登录访客也会被种上**，
    拿它们判断登录会得出"已登录"的错误结论。反过来，新版创作者中心
    已经可能不再设置 web_session，因此缺少它也不能证明未登录。
    返回 True / False，拿不到信息时返回 None（不下结论）。
    """
    try:
        cookies = page.context.cookies("https://creator.xiaohongshu.com")
    except Exception:
        return None
    return any(c.get("name") == "web_session" and c.get("value") for c in cookies)


def _wait_for_login(page, out_dir, timeout=None):
    """等到真正登录成功为止；需要登录就提示扫码。

    返回 True 表示等到了登录（曾提示过扫码），False 表示本来就已登录。

    判定优先级：
      1. 可见的登录页/二维码遮罩是强阻塞信号，出现时必须等待。
      2. 没有阻塞信号且发布入口可见，说明页面已经可操作，直接继续。
      3. web_session 只作辅助阳性证据；没有它不能否定新版页面的登录态。
    历史上这里还犯过另一个错：只认 URL 里的 "login" 和页面上的「扫码登录」
    四个字，而发布页 URL 带 ?target=image 时两者都不成立，于是既不提示也
    不报错，一直空转到超时 —— 用户看到的就是「卡住了」且毫无输出。
    """
    timeout = LOGIN_TIMEOUT_S if timeout is None else timeout
    deadline = time.time() + timeout
    warned = False
    last_note = 0.0
    started = time.time()
    while time.time() < deadline:
        ck = _has_login_cookie(page)
        url = str(getattr(page, "url", "") or "")
        path = url.lower().split("?", 1)[0].rstrip("/")
        blocked = path.endswith("/login") or _page_has_visible(
            page, LOGIN_BLOCKER_SELECTORS)
        ready = _page_has_visible(page, PUBLISH_READY_SELECTORS)

        if ready and not blocked:
            if ck is False:
                log("    发布页面已可操作；未见旧版 web_session，按当前页面状态继续")
            return warned
        if ck is True and not blocked:
            return warned

        # 页面首屏通常要 2~4 秒才渲染出发布入口。此时 cookie 可能已经返回 False，
        # 但不能抢先提示扫码，否则会出现“先说未登录、几秒后又说已登录”的假警报。
        elapsed = time.time() - started
        need_login = blocked or (ck is False and elapsed >= LOGIN_PROMPT_GRACE_S)
        if need_login and not warned:
            log(">>> 需要登录：请在弹出的浏览器窗口里用小红书 App 扫码"
                "（只需一次，之后会记住登录态）")
            warned = True

        now = time.time()
        if now - last_note >= 15:          # 定期报状态，避免看着像卡死
            last_note = now
            try:
                title = (page.title() or "")[:30]
            except Exception:
                title = ""
            log(f"    等待中… 已等 {int(now - started)}s / {timeout}s"
                f"｜页面：{title or str(getattr(page, 'url', ''))[:40]}"
                f"{'（疑似需要扫码）' if need_login else ''}")
        time.sleep(2)

    shot = os.path.join(out_dir, "等待登录超时.png")
    try:
        page.screenshot(path=shot)
    except Exception:
        pass
    try:
        title = page.title()
    except Exception:
        title = "?"
    raise RuntimeError(
        f"等待登录超时（{timeout}s）。当前页面：{title} / {getattr(page, 'url', '?')}\n"
        f"    1) 确认浏览器窗口是不是被别的窗口挡住了"
        f"（自动化用的是独立的 Google Chrome for Testing，不是日常那个 Chrome）\n"
        f"    2) 没登录就用小红书 App 扫码；已登录仍卡住说明页面又改版了，截图见 {shot}\n"
        f"    3) 扫码来不及可加大超时：export XHS_LOGIN_TIMEOUT=600 再跑")


def check_login(timeout=None):
    """只验证小红书登录态：打开浏览器 → 等进入发布表单 → 报告结果。

    不生成文案、不发布任何内容。用来把"到底登录没有"这件事一次性问清楚，
    省得靠猜——磁盘上的 Cookies 数据库在浏览器未关闭时可能不含会话 cookie，
    这里读的是 Playwright 眼里的实时 cookie（含内存中的）。
    """
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    which = ("系统 Chrome + 你的日常 profile" if USING_SYSTEM_PROFILE
             else "内置 Chromium + 独立 profile")
    log(f"打开浏览器检查登录态（{which}，不会发布任何内容）…")
    log(f"    profile：{PROFILE_DIR}")
    with sync_playwright() as p:
        ctx = _launch_browser(p)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
            try:
                warned = _wait_for_login(page, OUT_ROOT, timeout=timeout)
            except RuntimeError as e:
                log(f"❌ 没等到登录：{e}")
                return False
            ck = _has_login_cookie(page)
            log(f"web_session：{'有（辅助证据）' if ck else '未见（新版页面可不使用）' if ck is False else '读不到（不下结论）'}")
            try:
                log(f"当前页面：{page.title()}")
            except Exception:
                pass
            log("✅ 已登录，可以发笔记了" + ("（本次刚扫码）" if warned else "（用的是已保存的登录态）"))
            return True
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def publish_to_xhs(data, image_paths, out_dir, auto_yes=False):
    """打开创作者中心，上传图片并自动填入标题/正文/话题。"""
    from playwright.sync_api import sync_playwright
    os.makedirs(PROFILE_DIR, exist_ok=True)
    log("启动浏览器，打开小红书创作者中心…")
    with sync_playwright() as p:
        ctx = _launch_browser(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")

        # ---- 等登录 ----
        warned = _wait_for_login(page, out_dir)
        if warned:
            log("登录成功，登录态已保存")
            page.goto(XHS_PUBLISH_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)

        # ---- 切到"上传图文"标签，并找到真正的图片上传框 ----
        # 注意：页面默认在"上传视频"，第一个 input[type=file] 是视频框（accept 全是视频格式），
        # 必须找 accept 含图片格式（或 multiple）的那个 input
        log(f"上传 {len(image_paths)} 张图片…")
        file_input = None
        deadline2 = time.time() + 30
        while time.time() < deadline2 and file_input is None:
            # JS 精确点击文本为"上传图文"的叶子元素（类名改版也不受影响）
            try:
                page.evaluate("""() => {
                    for (const el of document.querySelectorAll('div,span,a,li')) {
                        if (el.children.length === 0 && el.textContent.trim() === '上传图文') {
                            el.click(); return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                pass
            page.wait_for_timeout(1200)
            for el in page.locator('input[type="file"]').all():
                acc = (el.get_attribute("accept") or "").lower()
                if any(k in acc for k in ("image", "png", "jpg", "jpeg", "webp")) \
                        or el.get_attribute("multiple") is not None:
                    file_input = el
                    break
        if file_input is None:
            page.screenshot(path=os.path.join(out_dir, "上传入口未找到.png"))
            raise RuntimeError("没找到图片上传入口（已截图，页面可能改版）")
        file_input.set_input_files(image_paths)
        # 等上传完成：预览图数量达到 or 兜底等待
        try:
            page.wait_for_function(
                f'document.querySelectorAll(".img-container, .pr, .preview-item").length >= {len(image_paths)}',
                timeout=30000)
        except Exception:
            page.wait_for_timeout(3000 * len(image_paths))
        log("图片上传完成")

        # ---- 填标题 ----
        title_box = _first_visible(page, [
            'input[placeholder*="标题"]', 'div.d-input input', 'input.d-text'])
        if title_box:
            title_box.click()
            title_box.fill(data["title"])
            log(f"已填标题：{data['title']}")
        else:
            log("!! 没找到标题输入框，请手动粘贴（文案.txt 里有）")

        # ---- 填正文 ----
        editor = _first_visible(page, [
            'div.ql-editor', '#post-textarea', 'div[contenteditable="true"]'])
        if editor:
            editor.click()
            page.keyboard.insert_text(data["body"])
            log("已填正文")
            page.wait_for_timeout(800)        # 等编辑器处理完长文本
            # ---- 打话题标签：输入 #词 后点联想下拉，转成真正的话题 ----
            # 每次都先把光标跳到全文末尾，否则标签会插进正文中间
            _cursor_to_end(page)
            page.keyboard.press("Enter")
            page.keyboard.press("Enter")
            for t in data["topics"]:
                _cursor_to_end(page)
                page.keyboard.type("#" + t, delay=50)
                page.wait_for_timeout(1500)
                sug = _first_visible(page, [
                    "#creator-editor-topic-container .item",
                    ".publish-topic-item",
                    'div[class*="topic"] .item',
                    'ul[class*="topic"] li'], 2500)
                if sug:
                    sug.click()
                    page.wait_for_timeout(400)
                    _cursor_to_end(page)      # 点完下拉焦点会跳，重新归位
                else:
                    page.keyboard.type(" ")   # 没联想就留纯文本标签
            log(f"已打 {len(data['topics'])} 个话题标签")
        else:
            log("!! 没找到正文编辑器，请手动粘贴")

        page.screenshot(path=os.path.join(out_dir, "发布前预览.png"))

        # ---- 发布 ----
        # 不能用 pause_for_user：它的兜底是"读不到输入就等 20 秒继续"，
        # 对发布这一步等于 fail-open —— nohup / cron / 管道 / 任何非交互环境
        # 都会在无人确认的情况下真的把帖子发出去，--yes 这个显式开关也就白设了。
        if not auto_yes and not confirm_publish(
                ">>> 内容已全部填好，去浏览器检查一下。回车=点击发布，Ctrl+C=取消："):
            try:
                page.screenshot(path=os.path.join(out_dir, "未发布.png"))
            except Exception:
                pass
            log("已跳过发布。图片和文案都在输出目录里，可手动发。")
            ctx.close()
            return
        # 必须精确匹配"发布"二字：侧边栏有"发布笔记"菜单、表单里有"定时发布"，都不能误点
        btn = None
        try:
            loc = page.get_by_role("button", name="发布", exact=True).first
            loc.wait_for(state="visible", timeout=5000)
            btn = loc
        except Exception:
            btn = _first_visible(page, [
                'button.publishBtn',
                'button:has-text("发布"):not(:has-text("笔记")):not(:has-text("定时"))'])
        if btn:
            btn.click()
            try:
                page.wait_for_selector("text=发布成功", timeout=15000)
                log("发布成功 ✅")
            except Exception:
                page.wait_for_timeout(5000)
                log("已点击发布，但没检测到'发布成功'提示，请看截图确认")
            page.screenshot(path=os.path.join(out_dir, "发布结果.png"))
        else:
            log("!! 没找到发布按钮，请在浏览器里手动点击发布")
            pause_for_user(">>> 手动发布完成后按回车关闭浏览器：", fallback_wait=60)
        ctx.close()

# ============================================================
# 6. 主流程
# ============================================================
def run_repost(out_dir, auto_yes=False):
    """直接发布之前生成好的目录（data.json + png），不重新生成。"""
    out_dir = os.path.abspath(os.path.expanduser(out_dir))
    data_path = os.path.join(out_dir, "data.json")
    if not os.path.isfile(data_path):
        sys.exit(f"目录里没有 data.json：{out_dir}")
    with open(data_path, encoding="utf-8") as f:
        data = json.load(f)
    # 只认 01_cover.png / 02_card.png 这类编号图，排除调试截图
    image_paths = sorted(
        os.path.join(out_dir, fn) for fn in os.listdir(out_dir)
        if re.match(r"^\d{2}_.+\.png$", fn))
    if not image_paths:
        sys.exit(f"目录里没有图片：{out_dir}")
    log(f"重新发布：《{data['title']}》，{len(image_paths)} 张图")
    publish_to_xhs(data, image_paths, out_dir, auto_yes=auto_yes)


def run(topic, pages=4, theme=None, research=True, publish=True, auto_yes=False):
    theme = theme or random.choice(list(THEMES.keys()))
    stamp = datetime.datetime.now().strftime("%m%d_%H%M")
    safe_topic = re.sub(r'[\\/:*?"<>|\s]+', "_", topic)[:20]
    out_dir = os.path.join(OUT_ROOT, f"{stamp}_{safe_topic}")
    # 同一分钟内跑两次同一话题会撞目录，旧实现用 exist_ok=True 直接复用，
    # 结果两次的图片和文案互相覆盖。这里撞了就加序号另开一个。
    if os.path.exists(out_dir):
        for i in range(2, 100):
            alt = f"{out_dir}-{i}"
            if not os.path.exists(alt):
                out_dir = alt
                break
    os.makedirs(out_dir, exist_ok=False)
    log(f"输出目录：{out_dir}")

    reference = search_reference(topic) if research else ""
    data = ai_generate_copy(topic, reference, pages)
    save_copy_text(data, out_dir)
    image_paths = render_images(data, topic, theme, out_dir)

    if publish:
        try:
            publish_to_xhs(data, image_paths, out_dir, auto_yes=auto_yes)
        except KeyboardInterrupt:
            log("已取消发布。图片和文案都在输出目录里，可手动发。")
        except Exception as e:
            log(f"!! 自动发布失败：{e}")
            log("图片和文案都已生成在输出目录，可手动发布。")
    log(f"完成。所有产物在：{out_dir}")
    return out_dir

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="小红书图文笔记自动生成+发布（DeepSeek 文案）")
    # nargs="*" 而不是 "?"：不加引号时 shell 会把「deepseek 4.1」拆成两个参数，
    # 用 "?" 会直接报 unrecognized arguments，对用户很不友好。这里自动拼回去。
    ap.add_argument("topic", nargs="*",
                    help="话题，如：秋冬护肤思路（含空格时可不加引号；不传则交互式询问）")
    ap.add_argument("--pages", type=int, default=4, help="图片张数（含封面），默认 4")
    ap.add_argument("--theme", choices=list(THEMES.keys()), help="配色主题，默认随机")
    ap.add_argument("--no-research", action="store_true", help="跳过联网搜参考")
    ap.add_argument("--no-publish", action="store_true", help="只生成图片文案，不发布")
    ap.add_argument("--yes", action="store_true", help="发布前不再人工确认")
    ap.add_argument("--repost", metavar="DIR", help="跳过生成，直接发布之前生成的输出目录")
    ap.add_argument("--model", help="DeepSeek 模型，默认 deepseek-v4-pro，省钱可用 deepseek-v4-flash")
    ap.add_argument("--thinking", action="store_true",
                    help="开启 DeepSeek 思考模式（更准更慢更贵，会忽略 temperature）")
    ap.add_argument("--temperature", type=float,
                    help=f"文案发散度 0~2，越高越活，默认 {AI_TEMPERATURE_DEFAULT}")
    ap.add_argument("--check-api", action="store_true",
                    help="只自检 DeepSeek Key / 连通性 / json 输出，不生成也不发布")
    ap.add_argument("--check-login", action="store_true",
                    help="只自检小红书登录态（会打开浏览器，不生成也不发布）")
    a = ap.parse_args()

    # 命令行覆盖配置
    if a.model:
        AI_MODEL = a.model
    if a.thinking:
        AI_THINKING = True
        AI_TEMPERATURE = None          # 思考模式不吃 temperature，置空避免误导
    if a.temperature is not None:
        if AI_THINKING:
            log("!! 已开启思考模式，--temperature 会被 DeepSeek 忽略")
        else:
            AI_TEMPERATURE = max(0.0, min(a.temperature, 2.0))

    if a.check_api:
        check_api()
        sys.exit(0)
    if a.check_login:
        sys.exit(0 if check_login() else 1)
    if a.repost:
        run_repost(a.repost, auto_yes=a.yes)
        sys.exit(0)
    topic = " ".join(a.topic).strip()      # 未加引号的多词话题在这里拼回来
    while not topic:
        try:
            topic = input("请输入话题（如：秋冬护肤思路）：").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("未输入话题，退出。")
    run(topic, pages=max(2, min(a.pages, 9)), theme=a.theme,
        research=not a.no_research, publish=not a.no_publish, auto_yes=a.yes)
