"""资金费率反弹策略 - 配置"""
import os

# === Bitget API（从环境变量/.env读取） ===
API_KEY = os.getenv("BITGET_API_KEY", "")
API_SECRET = os.getenv("BITGET_API_SECRET", "")
API_PASS = os.getenv("BITGET_API_PASS", "")

# === 策略参数 ===
SINGLE_AMOUNT = 5          # 单笔开仓保证金 (USDT)
LEVERAGE = 5               # 杠杆倍数
MARGIN_MODE = "isolated"   # 逐仓
ORDER_TYPE = "market"      # 市价单

FUNDING_THRESHOLD = 0.0025  # 费率阈值 (±0.25%)
SETTLE_WINDOW = 60         # 结算剩余秒数筛选 (< 60s)

EXCLUDE_SYMBOLS = ["BTC", "ETH", "BNB"]  # 排除币种

OPEN_FEE_RATE = 0.0005     # 开仓手续费 0.05%
CLOSE_FEE_RATE = 0.0005    # 平仓手续费 0.05%
TOTAL_FEE_RATE = 0.001     # 合并 0.1%
PROFIT_MULTIPLIER = 5      # 浮盈倍数阈值

CANDIDATES_FILE = os.path.join(os.path.dirname(__file__), "funding_candidates.json")
LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")

# === TG 通知 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "6155212881")
