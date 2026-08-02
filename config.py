"""资金费率反弹策略 - 配置"""
import os

# === Bitget API（从环境变量/.env读取） ===
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS = os.getenv("BITGET_API_PASS", "")

# === 策略参数 ===
SINGLE_AMOUNT = 5          # 单笔开仓保证金 (USDT)
LEVERAGE = 5               # 杠杆倍数
MARGIN_MODE = "crossed"    # 全仓
ORDER_TYPE = "market"      # 市价单

FUNDING_THRESHOLD = 0.0015  # 费率阈值 (±0.15%)
SETTLE_WINDOW = 60         # 结算剩余秒数筛选 (< 60s)

EXCLUDE_SYMBOLS = ["BTC", "ETH", "BNB"]  # 排除币种

OPEN_FEE_RATE = 0.0005     # 开仓手续费 0.05%
CLOSE_FEE_RATE = 0.0005    # 平仓手续费 0.05%
TOTAL_FEE_RATE = 0.001     # 合并 0.1%
PROFIT_MULTIPLIER = 5      # 浮盈倍数阈值

CANDIDATES_FILE = os.path.join(os.path.dirname(__file__), "funding_candidates.json")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# === 带单限制币种跳过列表（自学习）===
# 某些代币在 Bitget 带单(copy-trade)列表里，普通 API 下单会被拒(code 40020/40731)。
# 开仓时遇到这类错误会自动把该币写入此黑名单，后续扫描自动跳过，避免每次整点白跑。
COPY_TRADE_BLACKLIST_FILE = os.path.join(os.path.dirname(__file__), "copy_trade_blacklist.json")

# === TG 通知 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "6155212881")
