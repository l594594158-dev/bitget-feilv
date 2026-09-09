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

# === 异动跟踪条件(与币安一致, 2026-09-03) ===
TRACK_START_ABS = 0.0001    # 起点下限: >= 0.01% 才可能建立起点
TRACK_START_MAX = 0.0004    # 起点上限: < 0.04% (起点/跟踪区 [0.01%, 0.04%)) [2026-09-07: 0.05%→0.04%]
TRACK_TRIGGER_ABS = 0.0006  # 触发: 费率绝对值爬到超出 > 0.06% [娜姐2026-09-07: 0.08%→0.06%]
PRICE_RISE_PCT = 0.05       # 价格相对起点价涨幅 > 5% 才触发开仓 (娜姐2026-09-07 配 MAX_OPEN_RISE_PCT: 5%<涨幅<15%)
MAX_OPEN_RISE_PCT = 0.15    # 开仓涨幅硬上限(娜姐2026-09-07新增): 费率已过触发线时, 涨幅已 >=15% 也不开仓(追不上就放弃), 与 MAX_RISE_ABANDON_PCT 构成 5%<涨<15%
MAX_RISE_3D_PCT = 0.30     # 开仓前过滤(娜姐2026-09-07: 初定40%→30%): 最近72小时累计涨幅>30%的追高热门币不开仓(≤30%允许开)
MAX_DAY_RISE_PCT = 0.15    # 开仓前过滤(娜姐2026-09-10新增日涨幅<15%): 现价 vs 24h前收盘的24h涨幅 >=15% 的热门币不开仓(堵RAY式'当日单日暴拉仍过72h平滑'盲点)
MAX_RISE_ABANDON_PCT = 0.15 # 涨幅封顶放弃(娜姐2026-09-03方案A, 与币安同步): 建起点后价涨超15%但费率未达触发线0.10% → 假蓄势, 清币弃追高位
RECENT_HIGH_WINDOW_MIN = 60 # 高位回落过滤(娜姐2026-09-03 最终拍板): 进入起点区前回溯最近60分钟费率历史
RECENT_HIGH_ABS = 0.0006    # 高位回落判定阈值: 窗口内出现过 |费率| > 0.06% 即视为高位回落假起点, 不建起点/不入监控 (娜姐2026-09-07: 0.08%→0.06% 配新开仓触发线)
MAX_TRACK_SCANS = 300       # 单个币最多跟踪轮数(防无限, 中途跌回<0.01%即重置)

# === 开仓参数(与币安一致) ===
ORDER_MARGIN_USDT = 20.0    # 初始保证金 20U (娜姐 2026-09-09 调: 25U→20U; 25U是09-06从10U调来)
LEVERAGE = 10               # 10倍杠杆
MARGIN_MODE = "crossed"     # 全仓
POSITION_SIDE = "LONG"      # 只做多(双向模式的 LONG 侧)

# === 排除币种 ===
EXCLUDE_SYMBOLS = ["BTC", "ETH", "BNB"]

# === 止盈: 开仓即挂条件单, 涨20%平50%; 一半给移动止盈(娜姐2026-09-03) ===
TP_ACTIVATE_PCT = 0.20
TP_DRAWDOWN_PCT = 0.15
TP_SERVER_PCT = 0.20    # 开仓直接挂的服务端止盈触发涨幅(涨20%触发)
TP_SERVER_FRAC = 0.70   # 该止盈单平掉仓位的 70%; 剩余30%交移动止盈(娜姐2026-09-05: 50%→70%)

# === TG 通知 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "6155212881")

# === 带单限制币黑名单(自学习, Bitget 特有坑) ===
COPY_TRADE_BLACKLIST_FILE = os.path.join(BASE_DIR, "copy_trade_blacklist.json")
