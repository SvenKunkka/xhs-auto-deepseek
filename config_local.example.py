# 复制为 config_local.py 后填写（config_local.py 已在 .gitignore，不会被提交）
# 也可以改用环境变量，二选一即可；本文件里的值会覆盖环境变量。

DEEPSEEK_API_KEY = "sk-你的key"          # https://platform.deepseek.com/api_keys

# 模型：deepseek-v4-pro 文笔更好；deepseek-v4-flash 更快更便宜
DEEPSEEK_MODEL = "deepseek-v4-pro"

# 一般不填，默认 https://api.deepseek.com
# DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 思考模式：True 更准更慢更贵，且会忽略 temperature；写文案建议保持 False
DEEPSEEK_THINKING = False
# DEEPSEEK_REASONING_EFFORT = "high"     # 仅思考模式生效：low / high / max

# 文风发散度 0~2，越高越活泼、越不像模板文，默认 1.3
DEEPSEEK_TEMPERATURE = 1.3

# DEEPSEEK_MAX_TOKENS = 8192
# DEEPSEEK_TIMEOUT = 180
# DEEPSEEK_MAX_RETRIES = 3
