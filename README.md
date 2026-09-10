# xhs-auto-deepseek 小红书图文笔记自动生成 + 发布（DeepSeek 文案版）

> **这是衍生作品**：基于 [jiawen-w/xhs-auto](https://github.com/jiawen-w/xhs-auto)（MIT）改造，把文案模型从火山方舟（豆包）换成 **DeepSeek**。原作者版权声明保留在 [LICENSE](LICENSE) 中。

输入一个话题，自动完成整条小红书图文笔记的生产链路：

```
话题 → 联网搜参考素材 → DeepSeek 生成去 AI 味文案 → 生成 3:4 图片卡片 → 自动发布
```

- **联网参考**：自动抓取必应搜索结果和头部网页正文，作为写作素材（失败自动跳过）
- **DeepSeek 写文案**：标题（≤20 字带钩子）、正文（350~550 字）、5~8 个话题标签；Prompt 内置反 AI 腔规则
- **图片生成**：HTML 模板 + Playwright 截图，封面图 + 内容卡，5 套配色主题，1242×1656（3:4）
- **自动发布**：Playwright 驱动浏览器打开小红书创作者中心，自动上传图片、填标题、正文、逐个打话题标签

## 相比原版改了什么

| | 原版 | 本版 |
|---|---|---|
| 文案模型 | 豆包 `doubao-seed-2.0-pro`（火山方舟） | **DeepSeek** `deepseek-v4-pro`（可换 `deepseek-v4-flash`） |
| 接口协议 | Anthropic Messages（`anthropic` SDK） | **OpenAI 兼容** `/chat/completions`，只用 `requests` |
| 依赖 | `anthropic` + `playwright` + `requests` | `playwright` + `requests`（少一个 SDK） |
| 结构化输出 | 靠 prompt 约束 | **`response_format={"type":"json_object"}`** 强制合法 json |
| 失败处理 | 一次调用，json 解析失败即崩 | 503/429/超时**指数退避重试**；json 坏了**让模型自我修正**；空内容自动重试 |
| 思考模式 | 无 | 支持 `--thinking`（默认关闭，可用 `temperature` 控文风） |
| 自检 | 无 | `--check-api` 一条命令验证 Key / 连通性 / 模型名 |

## 安装

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/playwright install chromium   # 如果本机没有 Chrome
```

装好后用 `./run.sh` 启动，它会自动用项目内的 `.venv`：

```bash
./run.sh "秋冬护肤思路"
```

想在任何目录直接敲 `xhs`，把它软链到 PATH 里即可：

```bash
ln -sf "$PWD/run.sh" ~/.local/bin/xhs
xhs "秋冬护肤思路"
```

## 配置 DeepSeek Key

去 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请，任选一种方式：

**方式一：环境变量**

```bash
export DEEPSEEK_API_KEY="sk-你的key"
export DEEPSEEK_MODEL="deepseek-v4-pro"      # 可选，默认就是它
```

**方式二：本地配置文件**（复制 `config_local.example.py` 为 `config_local.py`，已被 .gitignore 排除）

```python
DEEPSEEK_API_KEY = "sk-你的key"
DEEPSEEK_MODEL   = "deepseek-v4-pro"
```

可调参数（环境变量或 `config_local.py` 同名变量）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-v4-pro` | 换成 `deepseek-v4-flash` 更快更便宜 |
| `DEEPSEEK_THINKING` | `0` | `1` 开启思考模式：更准、更慢、更贵，**且会忽略 temperature** |
| `DEEPSEEK_REASONING_EFFORT` | `high` | 仅思考模式生效：`low` / `high` / `max` |
| `DEEPSEEK_TEMPERATURE` | `1.3` | 文风发散度 0~2，越高越活泼、越不像模板文 |
| `DEEPSEEK_MAX_TOKENS` | `8192` | 单次输出上限 |
| `DEEPSEEK_TIMEOUT` | `180` | 单次请求超时秒数 |
| `DEEPSEEK_MAX_RETRIES` | `3` | 重试次数（503/429/超时） |

> 旧的 `XHS_AI_API_KEY` / `XHS_AI_MODEL` / `XHS_AI_BASE_URL` 变量名仍然兼容，老配置不会失效。
> 任何 OpenAI 兼容接口都能用：把 `DEEPSEEK_BASE_URL` 指向对应地址即可。

## 先自检，再烧 token

```bash
python3 xhs_auto.py --check-api
```

会验证 Key 是否有效、列出账号可用模型、并跑一次最小 json 请求。**不生成笔记、不碰小红书。**

另外 `selftest_offline.py` 是完整的离线自检：起一个假 DeepSeek 服务，验证请求格式、重试、json 修复、错误提示、出图链路，**不联网、不花 token**。

```bash
python3 selftest_offline.py
```

## 用法

```bash
# 交互式：运行后输入话题
python3 xhs_auto.py

# 直接传话题
python3 xhs_auto.py "秋冬护肤思路"

# 常用参数
python3 xhs_auto.py "话题" --pages 5          # 图片张数（含封面），默认 4
python3 xhs_auto.py "话题" --theme cream      # 配色：cream/tea/green/blue/pink，默认随机
python3 xhs_auto.py "话题" --no-publish       # 只生成图片和文案，不发布
python3 xhs_auto.py "话题" --yes              # 发布前不再人工确认
python3 xhs_auto.py "话题" --no-research      # 跳过联网搜参考
python3 xhs_auto.py "话题" --model deepseek-v4-flash      # 本次用便宜模型
python3 xhs_auto.py "话题" --thinking                     # 本次开思考模式
python3 xhs_auto.py "话题" --temperature 1.5              # 本次调发散度
python3 xhs_auto.py --repost <输出目录>       # 重新发布之前生成过的笔记
```

所有产物输出到 `~/Downloads/xhs_auto/<时间_话题>/`：编号图片、`文案.txt`、`data.json`、发布过程截图。即使自动发布失败，图和文案也都在，可手动发布。

## 发布流程说明

- 首次发布会弹出浏览器，需要用小红书 App **扫码登录一次**，登录态保存在 `~/Downloads/xhs_auto/browser_profile/`
- 默认填完所有内容后暂停，等你在浏览器里检查、回车确认才点发布；`--yes` 跳过确认
- 话题标签会逐个输入 `#词` 并点击联想下拉，转成真正的话题标签

## 常见报错

| 报错 | 原因与处理 |
|---|---|
| `API Key 无效` (401) | Key 写错或已删除，去 platform.deepseek.com 重新生成 |
| `余额不足` (402) | 账户没余额了，充值后重试 |
| `触发限流` (429) | 请求太频繁，脚本会自动重试；持续失败就降低发文频率 |
| `连续两次没返回可用 json` | 原始输出已存到 `~/Downloads/xhs_auto/ai_raw_output.txt`，可贴回对话排查 |
| `输出被 max_tokens 截断` | 调大 `DEEPSEEK_MAX_TOKENS` 或减少 `--pages` |

## 免责声明

- 本项目仅供学习交流，使用页面自动化模拟人工操作，**请遵守小红书社区规范与服务条款**
- 建议控制发布频率（一天一两篇以内），批量高频发布可能触发平台风控，风险自负
- AI 生成内容请人工审核后再发布，对发布内容负责的始终是账号持有者

## 致谢

- 原始项目：[jiawen-w/xhs-auto](https://github.com/jiawen-w/xhs-auto) —— 整条链路设计、图片卡片 HTML 模板、Playwright 发布逻辑均出自原作者
- 本项目改造部分：DeepSeek 接入、OpenAI 兼容协议重写、重试与 json 自修复、思考模式支持、离线自检

## License

MIT，见 [LICENSE](LICENSE)。原项目版权归 jiawen-w 所有，改造部分同样以 MIT 释出。
