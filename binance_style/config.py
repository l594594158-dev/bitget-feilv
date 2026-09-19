"""Bitget 币安式费率蓄势做多策略 - 配置
单向下单(双向模式只开 LONG), 只做多, 抓异动趋势.
娜姐 2026-09-03: 删除原罗海"双向费率差"策略, 改照币安策略逻辑,
用 Bitget 自己的资金费率扫描/跟踪.

参数与币安(刘刚币安)策略完全一致:
- 每1分钟全费率扫描+记录 (用 Bitget 自己合约费率, 2026-09-03 从5改1)
- 费率从起点区[0.01%,0.05%)建立起点价, 爬到超出 >=0.10% 触发
- 价格相对起点价上涨 >5% 才触发开多
- 开仓: 5U/仓, 10x, 全仓, 单向多(双向模式 LONG 侧)
- 移动止盈: 涨20%激活, 回撤15%平
"""
import os

# === Bitget API（从 .env 读取, 目录在 binance_style/ 同级的 luohai_funding/.env） ===
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(BASE_DIR, "logs")
ENV_PATH = os.path.join(os.path.dirname(BASE_DIR), ".env")   # luohai_funding/.env

# === 加载 .env 到进程环境(供本模块 getenv 直接用) ===
if os.path.exists(ENV_PATH):
    for _l in open(ENV_PATH):
        _l = _l.strip()
        if _l and not _l.startswith('#'):
            _k, _, _v = _l.partition('=')
            os.environ.setdefault(_k.strip(), _v.strip())

# === 调度 ===
SCAN_INTERVAL_MIN = 1          # 费率扫描每1分钟一次(与币安一致, crontab 驱动 2026-09-03)
TRACKER_POLL_SECONDS = 3       # 跟踪止盈: 每3秒拉一次行情

# === 费率历史库 ===
RATE_DB_FILE = os.path.join(DATA_DIR, "funding_rate_history.jsonl")
RATE_SQLITE_DB = os.path.join(DATA_DIR, "funding_rate.db")    # SQLite 费率/价数据库 (娜姐2026-09-03, 与币安同步; 上涨前费率/价规律分析用)
RATE_SQLITE_RETENTION_DAYS = 31   # 数据库历史保留上限=1个月(31天), 超出自动删最旧 (娜姐2026-09-03)

# === 异动跟踪条件(与币安一致, 娜姐 2026-09-19 新方案) ===
TRACK_START_ABS = 0.0005    # 起点下限: 费率须先 <0.05%(到过低位), 首次 >=0.05% 建起点
TRACK_START_MAX = 0.0008    # 起点上限: 首次进入起点区 [0.05%, 0.08%) 建起点
TRACK_TRIGGER_ABS = 0.0020  # 触发: 费率绝对值爬到 >=0.2% (娜姐2026-09-19: 0.06%→0.2%)
MAX_CLIMB_MINUTES = 30      # 爬升窗口: 起点到触发须 <=30分钟
PRICE_RISE_PCT = 0.0        # 新方案: 取消'开仓价较起点涨>5%'条件
MAX_OPEN_RISE_PCT = 999.0   # 新方案: 取消涨幅封顶
MAX_RISE_3D_PCT = 0.30      # 保留: 72h涨幅>30% 不开仓
MAX_DAY_RISE_PCT = 0.15     # 保留: 24h涨幅>=15% 不开仓
MAX_RISE_ABANDON_PCT = 999.0 # 新方案: 取消'涨幅封顶放弃'
RECENT_HIGH_WINDOW_MIN = 0  # 新方案: 取消高位回落过滤
RECENT_HIGH_ABS = 0.0000
MAX_TRACK_SCANS = 300       # 单个币最多跟踪轮数

# === 开仓参数(与币安一致) ===
ORDER_MARGIN_USDT = 20.0    # 初始保证金 20U (娜姐 2026-09-09 调: 25U→20U; 25U是09-06从10U调来)
LEVERAGE = 5                # 5倍杠杆(娜姐 2026-09-10 调: 10→5)
MARGIN_MODE = "crossed"     # 全仓
POSITION_SIDE = "LONG"      # 只做多(双向模式的 LONG 侧)

# === 排除币种 ===
EXCLUDE_SYMBOLS = ["BTC", "ETH", "BNB"]

# === 止盈/止损(娜姐 2026-09-19 新方案定稿) ===
TP_ACTIVATE_PCT = 0.20   # 移动止盈: 涨20%激活
TP_DRAWDOWN_PCT = 0.15   # 从最高回撤15%平 (全仓平)
TP_SERVER_PCT = 0.20     # 保留字段(新方案不挂分批止盈)
TP_SERVER_FRAC = 0.0     # 新方案: 不挂服务端分批止盈, 全仓交移动止盈
HARD_SL_PCT = 0.50       # 硬止损: 亏50%平仓
NO_EXPIRY = True         # 持仓无限期, 不设到期强平

# === TG 通知 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "6155212881")

# === 带单限制币黑名单(自学习, Bitget 特有坑) ===
COPY_TRADE_BLACKLIST_FILE = os.path.join(BASE_DIR, "copy_trade_blacklist.json")
