"""Bitget 币安式费率蓄势做多策略 - 核心逻辑
单向下单(双向模式只开 LONG), 只做多, 抓异动趋势.
2026-09-03 娜姐: 原罗海双向费率差策略删除, 按币安策略改造成此逻辑.
只 OPEN 到 Bitget 账户 LONG 侧; 费率用 Bitget 自己的合约资金费率.

流程:
1. 每1分钟扫 Bitget 全部 USDT 永续资金费率 → 记录历史
2. 费率跟踪: 起点区 [0.01%,0.05%) 建起点价 → 爬到 >=0.10% 且价较起点涨>5% → 开多
3. 开仓: 5U保证金,10x,全仓,LONG
4. tracker 每分钟拉行情做移动止盈(涨20激活/回撤15平)

⚠️ 双向模式(Bitget hedged=True)只往 LONG 下单。平 LONG 用 sell(reduce)。
"""
import os, sys, json, time, math, base64, hmac, hashlib
import requests
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import ccxt
from config import *
# 加载 .env(本模块 config 已加载过一次, 这里确保 loader 也读)
for _d in (DATA_DIR, LOG_DIR):
    os.makedirs(_d, exist_ok=True)

os.environ.setdefault("BITGET_API_KEY", os.getenv("BITGET_API_KEY", ""))
os.environ.setdefault("BITGET_API_SECRET", os.getenv("BITGET_API_SECRET", ""))
os.environ.setdefault("BITGET_API_PASS", os.getenv("BITGET_API_PASS", ""))

def _cred():
    env = {}
    if os.path.exists(ENV_PATH):
        for _l in open(ENV_PATH):
            _l = _l.strip()
            if _l and not _l.startswith('#'):
                _k, _, _v = _l.partition('=')
                env[_k.strip()] = _v.strip()
    return {
        "apiKey": env.get("BITGET_API_KEY", ""),
        "secret": env.get("BITGET_API_SECRET", ""),
        "password": env.get("BITGET_API_PASS", ""),
    }

# ═══════════════ 交易所(Bitget) ═══════════════
def get_exchange():
    c = _cred()
    return ccxt.bitget({
        "apiKey": c["apiKey"],
        "secret": c["secret"],
        "password": c["password"],
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })

def is_copy_trade_error(e) -> bool:
    s = str(e)
    return ("copy" in s.lower()) or ("40020" in s) or ("40731" in s) or ("40125" in s)

def load_copy_blacklist() -> set:
    if os.path.exists(COPY_TRADE_BLACKLIST_FILE):
        try:
            return set(json.load(open(COPY_TRADE_BLACKLIST_FILE)))
        except Exception:
            return set()
    return set()

def save_copy_blacklist(s: set):
    json.dump(sorted(s), open(COPY_TRADE_BLACKLIST_FILE, 'w'))

# ═══════════════ TG 通知 ═══════════════
def tg_send(text: str):
    token = TG_BOT_TOKEN or os.getenv("TG_BOT_TOKEN", "")
    chat_id = TG_CHAT_ID or os.getenv("TG_CHAT_ID", "")
    if not token:
        print(f"[TG] (未配置) {text[:200]}")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=10)
        print(f"[TG] 已推送: {text[:80]}")
    except Exception as e:
        print(f"[TG ERR] {e}")

# ═══════════════ 费率历史库 ═══════════════
def load_rate_db(keep_per_symbol=720):
    """载入费率历史: {symbol: [ {ts, rate, price}, ... ]} 旧→新

    注(娜姐2026-09-05 OOM修复): 流式载入时每个币只保留最近 keep_per_symbol 条,
    更早的记录直接丢弃, 避免把整份 jsonl(数百MB)全量载入内存导致 scan 进程
    膨胀到 1GB+ 反复触发 OOM 杀掉 cron/gateway/策略自身.
    分析/异动判断只用每币最近窗口(≤3000条), 不影响逻辑."""
    db = {}
    if os.path.exists(RATE_DB_FILE):
        for line in open(RATE_DB_FILE):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                bucket = db.get(rec['symbol'])
                if bucket is None:
                    db[rec['symbol']] = [rec]
                else:
                    bucket.append(rec)
                    # 只保留最近 keep 条, 控制内存
                    if len(bucket) > keep_per_symbol:
                        del bucket[:-keep_per_symbol]
            except Exception:
                continue
    return db

def append_rate(rec):
    with open(RATE_DB_FILE, 'a') as f:
        f.write(json.dumps(rec) + '\n')

# ═══════════════════════ SQLite 费率/价数据库 (娜姐2026-09-03, 与币安同构) ═══════════════════════
_TSBJ = None
def _bj(ts_ms):
    """ms时间戳→北京时间(UTC+8)字符串 'YYYY-MM-DD HH:MM' (娜姐要求:数据库记录时间用北京时间标注)"""
    global _TSBJ
    if _TSBJ is None:
        from datetime import timedelta
        _TSBJ = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(ts_ms / 1000.0, _TSBJ).strftime('%Y-%m-%d %H:%M')

def _ensure_bj_column():
    """老库历史库补 bj_time 列(已建无此列的 .db 兼容, 幂等)"""
    import sqlite3
    conn = sqlite3.connect(RATE_SQLITE_DB)
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute('PRAGMA table_info(rates)')]
        if 'bj_time' not in cols:
            c.execute('ALTER TABLE rates ADD COLUMN bj_time TEXT DEFAULT \'\'')
            conn.commit()
    except Exception:
        pass
    finally:
        conn.close()

def init_rate_sqlite():
    """建表 rates(symbol,ts,rate,price,bj_time): 唯一键防重, 索引快查; bj_time=北京时间."""
    import sqlite3
    conn = sqlite3.connect(RATE_SQLITE_DB)
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS rates (
            symbol TEXT NOT NULL,
            ts INTEGER NOT NULL,
            rate REAL,
            price REAL,
            bj_time TEXT DEFAULT '',
            PRIMARY KEY (symbol, ts))''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_rates_ts ON rates(ts)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_rates_sym ON rates(symbol)')
        conn.commit()
    finally:
        conn.close()

def write_rates_sqlite_batch(rows):
    """批量写入本轮轮询结果到 SQLite. rows: [{symbol,ts,rate,price}], 幂等(重复跳过); 附北京时间bj_time(娜姐2026-09-03)."""
    if not rows:
        return
    import sqlite3
    init_rate_sqlite()
    _ensure_bj_column()
    conn = sqlite3.connect(RATE_SQLITE_DB)
    try:
        c = conn.cursor()
        c.executemany(
            'INSERT OR IGNORE INTO rates(symbol,ts,rate,price,bj_time) VALUES(?,?,?,?,?)',
            [(r['symbol'], r['ts'], r.get('rate'), r.get('price'), _bj(r['ts'])) for r in rows])
        conn.commit()
    except Exception as e:
        print(f'  ⚠️ SQLite 写入失败: {e}')
    finally:
        conn.close()
    prune_rates_sqlite()

def prune_rates_sqlite():
    """滚动清理: 保留最近 RATE_SQLITE_RETENTION_DAYS(31)天数据, 超出一个月删最旧(娜姐2026-09-03). 每天最多一次."""
    try:
        import sqlite3
        now_ms = datetime.now(timezone.utc).timestamp() * 1000.0
        cutoff = now_ms - RATE_SQLITE_RETENTION_DAYS * 86400_000.0
        today = _bj(now_ms)[:10]
        conn = sqlite3.connect(RATE_SQLITE_DB, timeout=15)
        try:
            c = conn.cursor()
            c.execute('CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)')
            row = c.execute("SELECT v FROM meta WHERE k='pruned_on'").fetchone()
            if row and row[0] == today:
                return
            c.execute('DELETE FROM rates WHERE ts < ?', (int(cutoff),))
            c.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('pruned_on',?)", (today,))
            conn.commit()
        finally:
            conn.close()
    except Exception as _e:
        print(f'  ⚠️ SQLite 清理跳过: {_e}')


def had_recent_high(hist, window_min=None):
    """高位回落过滤(娜姐2026-09-03最终): 该币最近 window_min 分钟内是否出现过 |费率|>RECENT_HIGH_ABS(0.10%) 高位.
    True=刚从更高费率回落, 应视为'假起点', 不建起点/不入监控."""
    if window_min is None:
        window_min = RECENT_HIGH_WINDOW_MIN
    if not hist:
        return False
    latest = hist[-1]
    cur_ts = latest['ts']
    window_ms = window_min * 60 * 1000
    for rec in reversed(hist):
        if cur_ts - rec['ts'] > window_ms:
            break
        if abs(rec['rate']) > RECENT_HIGH_ABS:
            return True
    return False

# ═══════════════ 异动跟踪状态 ═══════════════
TRACK_FILE = os.path.join(DATA_DIR, "track_state.json")
def load_track():
    if os.path.exists(TRACK_FILE):
        try:
            return json.load(open(TRACK_FILE))
        except Exception:
            return {}
    return {}
def save_track(track):
    tmp = TRACK_FILE + '.tmp'
    json.dump(track, open(tmp, 'w'))
    os.replace(tmp, TRACK_FILE)

# ═══════════════ 费率扫描 + 异动检测 ═══════════════
def scan_funding_and_detect(dry_run=False):
    ex = get_exchange()
    if not ex.markets:
        ex.load_markets()
    track = load_track()
    now_ms = int(time.time() * 1000)
    ts_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

    # 1. Bitget 全市场费率(批量) + 价格
    fr = ex.fetch_funding_rates()
    tk = ex.fetch_tickers()

    # 先取一次, 记录所有 USDT 永续
    symbols = [s for s in fr if s.endswith('/USDT:USDT')]
    fetched = 0
    sql_rows = []   # 本轮新增记录, 同步写 SQLite
    for sym in symbols:
        base = sym.split('/')[0].upper()
        if any(base.startswith(e.upper()) for e in EXCLUDE_SYMBOLS):
            continue
        rate = fr[sym].get('fundingRate')
        last = None
        if sym in tk and tk[sym].get('last'):
            last = float(tk[sym]['last'])
        if rate is None:
            continue
        rec = {'symbol': sym, 'ts': now_ms, 'rate': float(rate),
               'price': last, 'time': ts_str}
        append_rate(rec)
        sql_rows.append(rec)
        fetched += 1
    print(f'[SCAN] 记录 {fetched} 个币的费率/价格 (Bitget)')
    # 本轮数据同步写入 SQLite(带索引, 供上涨前费率/价规律分析)
    try:
        write_rates_sqlite_batch(sql_rows)
    except Exception as _e:
        print(f'  ⚠️ SQLite 写入异常: {_e}')

    db = load_rate_db()   # 含本次新记录

    # 2. 每个币做异动跟踪
    triggered = []
    try:
        for sym in symbols:
            if not sym.endswith('/USDT:USDT'):
                continue
            base = sym.split('/')[0].upper()
            if any(base.startswith(e.upper()) for e in EXCLUDE_SYMBOLS):
                continue
            # 带单限制币黑名单跳过
            if base in load_copy_blacklist():
                continue
            hist = db.get(sym, [])
            if not hist:
                continue
            st = track.get(sym, {})
            new_st, did_trigger = _evaluate(sym, hist, st)
            if did_trigger and new_st:
                triggered.append((sym, dict(new_st)))
            elif did_trigger:
                print(f'  ⚠️ {sym} did_trigger=True 但 new_st=None, 忽略')
            if new_st is None:
                track.pop(sym, None)
            elif new_st.get('mode') == 'consumed':
                track.pop(sym, None)
            else:
                track[sym] = new_st
    finally:
        try:
            save_track(track)
        except Exception as e:
            print(f'  ⚠️ 保存 track 状态失败: {e}')

    # 3. 开仓前过滤: 若该币在 Bitget 已有真实多头持仓, 则跳过不重复开(娜姐2026-09-05 持仓过滤)
    #    修复 AKE 类多次费率轮回反复叠仓问题: 同一币持仓期间不再重复开多.
    #    [2026-09-09 深度加固] (a)原写法用 (sym,dict)元组对字符串set做成员判断→每笔真触发必崩
    #        TypeError unhashable, 被 except 吞掉→去重恒失效→一路重复叠开; 已改为按 sym 去重回填.
    #       (b)原 except 只打印告警不挡单→真实拉仓瞬时失败会照样开→叠仓. 现: 失败时完全回退本地
    #        open_positions 记账去重(记账=已发过开仓单的最权威凭据), 绝不让过滤被静默旁路.
    if triggered and not dry_run:
        try:
            local_held = {s for s in (load_opens() or {}).keys() if isinstance(s, str)}
        except Exception:
            local_held = set()
        try:
            held = set()
            for p in ex.fetch_positions():
                if float(p.get('contracts') or 0) != 0 and p.get('side') == 'long':
                    s = p.get('symbol')
                    if isinstance(s, str) and s:
                        held.add(s)
            held |= local_held  # 真实持仓 + 本地记账 并集: 防下单后撮合/跨轮同步滞后导致的叠开
        except Exception as e:
            held = set(local_held)  # 拉取失败→完全回退本地记账(至少已开/已下单的币挡着不开)
            print(f'  ⚠️ 拉取真实持仓失败({str(e)[:70]}), 回退本地记账去重({len(held)}个在持/已下单)')
        # 只按 sym 域名去重回填
        dup_syms = [sym for sym, _st in triggered if sym in held]
        if dup_syms:
            held_syms = set(dup_syms)
            triggered = [(sym, stt) for sym, stt in triggered if sym not in held_syms]
            for s in dup_syms:
                print(f'  ⏭ {s} 已有Bitget多头持仓或已下单, 跳过重复开仓')

    # 开仓前过滤(娜姐2026-09-07): 币近72h累计涨幅 >40% → 追高热门币不开仓
    if triggered:
        kept_t = []
        for s, _st in triggered:
            try:
                ohl = ex.fetch_ohlcv(s, '4h', limit=20)
                if not ohl:
                    raise ValueError('empty ohlcv')
                px_last = float(ohl[-1][4])
                px_72 = None
                for c in ohl:
                    if float(c[0]) <= (now_ms - 72 * 3600 * 1000):
                        px_72 = float(c[4])
                if px_72 and px_72 > 0:
                    r3 = (px_last - px_72) / px_72
                    if r3 > MAX_RISE_3D_PCT:
                        print(f'  🔒 {s} 近72h涨幅{r3*100:.1f}% > {MAX_RISE_3D_PCT*100:.0f}%, 追高过滤不开仓')
                        continue
            except Exception as e:
                print(f'  ⚠️ {s} 近72h涨幅查询失败({str(e)[:50]}), 按通过处理')
            kept_t.append((s, _st))
        if len(kept_t) != len(triggered):
            triggered = kept_t

    # 开仓前过滤(娜姐2026-09-10 新增): 日/24h涨幅过滤 <15%. 堵盲点: 当日单日暴拉(如RAY ~24%)
    #    即使起底价建在拉高后(起底后涨幅小)、72h平滑处理, 也照样追高; 这里直接以现价 vs 24h前收盘
    #    (1h K线)判 24h涨幅 >=MAX_DAY_RISE_PCT → 不开仓.
    if triggered:
        kept_t = []
        for s, _st in triggered:
            try:
                hod = ex.fetch_ohlcv(s, '1h', limit=25)
                if not hod or len(hod) < 24:
                    raise ValueError('insufficient 1h ohlcv')
                px_now = float(hod[-1][4])
                px_24 = float(hod[-24][4])
                if px_24 and px_24 > 0:
                    dr = (px_now - px_24) / px_24
                    if dr >= MAX_DAY_RISE_PCT:
                        print(f'  🔒 {s} 24h涨幅{dr*100:.1f}% >= {MAX_DAY_RISE_PCT*100:.0f}%, 日涨幅过滤不开仓')
                        continue
            except Exception as e:
                print(f'  ⚠️ {s} 24h涨幅查询失败({str(e)[:50]}), 按通过处理')
            kept_t.append((s, _st))
        if len(kept_t) != len(triggered):
            triggered = kept_t

    # 3. 开仓(单向 LONG)
    if triggered and not dry_run:
        for sym, stt in triggered:
            _open_long(ex, sym, stt)
    elif dry_run:
        print(f'[DRY] 触发 {len(triggered)} 个' if triggered else '[DRY] 实际扫描: 无异动触发')
    else:
        print(f'[SCAN] 本轮无触发')
    return triggered

# ═══════════════ 异动评估(与币安一致, 用 Bitget 费率) ═══════════════
def _evaluate(sym, hist, st):
    """费率异动跟踪评估. 规则(每轮扫描):
      - 费率绝对值 [0.01%,0.05%) : 未监控→建起点; 已监控→起点价不变
      - 费率绝对值 <0.01% : 清除监控(退出, 起点作废)
      - 费率绝对值 [0.05%,0.10%) 爬升区: 已监控→起点不变; 未监控→不建起点
      - 费率绝对值 >=0.10% 高位区:
          * 无有效起点(没经起点区直接跳高位)→ 不建、不触发
          * 有有效起点 → (现价-起点)/起点 >5% → 触发开多; 否则等待价格涨够
    """
    base = sym.split('/')[0]
    last = hist[-1]
    abs_rate = abs(last['rate'])
    cur_px = last.get('price')
    mode = (st or {}).get('mode', 'none')

    # ── 涨幅封顶放弃(方案A, 娜姐2026-09-03, 与币安同步): 价先飞、费率跟不上则清币 ──
    _sp = (st or {}).get('start_price')
    if (mode == 'watching' and _sp and cur_px and _sp > 0 and abs_rate < TRACK_TRIGGER_ABS):
        _rise = (cur_px - _sp) / _sp
        if _rise > MAX_RISE_ABANDON_PCT:
            print(f'  ⏬ {base} 价已超起点+{_rise*100:.0f}%(>{MAX_RISE_ABANDON_PCT*100:.0f}%)但费率仅{abs_rate*100:.3f}%未达触发线, 假蓄势, 放弃')
            return None, False

    if abs_rate < TRACK_START_ABS:
        return None, False
    if abs_rate < TRACK_START_MAX:
        # 高位回落过滤(娜姐2026-09-03): 最近RECENT_HIGH_WINDOW_MIN分钟内出现过|费率|>=0.10%高位→不建起点/清出监控
        if had_recent_high(hist):
            return None, False
        if mode == 'watching':
            return {'mode': 'watching', 'start_price': st.get('start_price')}, False
        else:
            return {'mode': 'watching', 'start_price': cur_px}, False
    if abs_rate < TRACK_TRIGGER_ABS:
        if mode == 'watching':
            return {'mode': 'watching', 'start_price': st.get('start_price')}, False
        else:
            return None, False
    # >= 0.10% 高位区
    if mode != 'watching' or st.get('start_price') is None:
        return None, False
    start_price = st.get('start_price')
    if start_price and cur_px and start_price > 0:
        rise = (cur_px - start_price) / start_price
        if rise > PRICE_RISE_PCT and rise < MAX_OPEN_RISE_PCT:
            return {'mode': 'consumed', 'start_price': start_price}, True
        if rise >= MAX_OPEN_RISE_PCT:
            print(f'  ⏸ {base} 价已超起点+{rise*100:.0f}%(≥{MAX_OPEN_RISE_PCT*100:.0f}%)涨幅封顶, 过触发线也不追, 放弃')
            return None, False
    return {'mode': 'watching', 'start_price': start_price}, False

# ═══════════════ 开仓(双向模式开 LONG) ═══════════════
def _open_long(ex, sym, stt):
    base = sym.split('/')[0]
    print(f'[OPEN] {sym} 费率异动触发, 准备开多(LONG)...')
    try:
        if not ex.markets:
            ex.load_markets()
        market = ex.market(sym)
        last = ex.fetch_ticker(sym)['last']
        if not last or last <= 0:
            print(f'  ❌ {sym} 价格无效'); return
        notional_usdt = ORDER_MARGIN_USDT * LEVERAGE
        # ⚠️ 数量修复(2026-09-03): create_order 传的是【base token 币数】,
        # 直接 = 目标名义/价格, 再按交易所可交易精度向下取整即可。
        # 旧代码错误地再 ÷ sizeMultiplier(如 EGLD=0.1), 把小面额币位放大10x,
        # 一台单砸出 ≈10 倍名义(EGLD 曾开出 2000U 名义/200U 保证金)。
        # FLOCK(sizeMultiplier=1) 不受影响所以看起来正常。
        qty_contract = ex.amount_to_precision(sym, notional_usdt / last)
        qty_tokens = float(qty_contract)
        min_trade = float(
            (market.get("limits") or {}).get("amount", {}).get("min")
            or market.get("info", {}).get("minTradeNum", "1")
            or 0
        )
        if qty_tokens < min_trade or qty_tokens <= 0:
            print(f'  ❌ {sym} 开仓币数 {qty_tokens} 低于最小 {min_trade}'); return

        # 杠杆(双向模式需带 side) + 全仓(带 side)
        for _try in range(2):
            try:
                ex.set_leverage(LEVERAGE, sym, {"side": "long"}); break
            except Exception:
                try:
                    ex.set_leverage(LEVERAGE, sym); break
                except Exception as e2:
                    if _try == 0: print(f'  ⚠️ 设杠杆: {str(e2)[:60]}')
        try:
            ex.set_margin_mode(MARGIN_MODE, sym, {"side": "long"})
        except Exception:
            try:
                ex.set_margin_mode(MARGIN_MODE, sym)
            except Exception as e:
                print(f'  ⚠️ 设全仓: {str(e)[:60]}')

        # 开多(双向模式 LONG 侧): 用 buy, 不传 marginMode
        order = ex.create_order(sym, "market", "buy", qty_tokens, None, {
            "hedged": True,
            "productType": "USDT-FUTURES",
        })
        if not (order and order.get("id")):
            print(f'  ❌ {sym} 下单失败: {order}'); return
        print(f'  ✅ 开多 {sym} {qty_tokens}{market.get("base")}(≈{notional_usdt:.2f}U) orderId={order.get("id")}')
        # 成交价(下单后立即查询, 用下单前 last 作为 entry 近似)
        entry_est = _fetch_entry_price(ex, sym, order.get("id"), last)
        # 开仓即挂服务端止盈: 涨 TP_SERVER_PCT(20%) 平 TP_SERVER_FRAC(50%), 剩余交移动止盈
        half = _place_half_tp(ex, sym, market, qty_tokens, entry_est)
        # 状态记录: 移动止盈只负责剩余一半(服务端止盈已覆盖最初那一半)
        _record_open(sym, half, entry_est)
        tg_send(
            f"📈 <b>Bitget币安式 · 开多</b>\n"
            f"币种: {base}\n"
            f"保证金: {ORDER_MARGIN_USDT}U ×{LEVERAGE} 全仓\n"
            f"开仓价: {entry_est}\n"
            f"名义: ≈{notional_usdt:.2f}U"
        )
    except Exception as e:
        print(f'  ❌ {sym} 开仓失败: {str(e)[:150]}')
        import traceback
        if is_copy_trade_error(e):
            b = base.upper(); bl = load_copy_blacklist()
            if b not in bl:
                bl.add(b); save_copy_blacklist(bl)
                print(f'  🚫 {sym} 带单限制币,已加入黑名单')
                tg_send(f'🚫 {sym} 带单限制币,已加入跳过黑名单')

def _fetch_entry_price(ex, sym, order_id, fallback):
    """开仓后回读成交均价, 失败用 fallback(下单前 last)"""
    try:
        time.sleep(1.2)
        od = ex.fetch_order(order_id, sym)
        avg = od.get("average")
        if avg and float(avg) > 0:
            return float(avg)
    except Exception:
        pass
    return fallback

# ═══════════════ 开仓状态记录 ═══════════════
OPEN_FILE = os.path.join(DATA_DIR, "open_positions.json")
def load_opens():
    if os.path.exists(OPEN_FILE):
        try:
            return json.load(open(OPEN_FILE))
        except Exception:
            return {}
    return {}
def save_opens(opens):
    json.dump(opens, open(OPEN_FILE, 'w'), indent=2)
def _place_half_tp(ex, sym, market, bought_qty, entry_price):
    """开仓后给 LONG 挂服务端减半止盈计划单:
    触发价 = 开仓价*(1+TP_SERVER_PCT), 数量 = bought_qty*TP_SERVER_FRAC(sell/reduce).
    返回给移动止盈留管的剩余数量(向下取整到可交易单位)。
    挂失败不阻断开仓(仅告警), 此时返回全额让移动止盈接管。
    """
    try:
        if not ex.markets:
            ex.load_markets()
        sid = (market.get("info") or {}).get("symbol") or market.get("id")
        pp = int((market.get("info") or {}).get("pricePlace", "8"))
        tp_price = entry_price * (1 + TP_SERVER_PCT)
        tp_r = math.floor(tp_price * (10 ** pp)) / (10 ** pp)
        half = bought_qty * TP_SERVER_FRAC
        half = float(ex.amount_to_precision(sym, half))
        if half <= 0 or half >= bought_qty:
            print(f'  ⚠️ {sym} 减半止盈计算异常(half={half} bq={bought_qty}), 交由移动止盈全接管')
            return bought_qty
        # 查重: 是否已有同币同触发价的 reduce sell 计划单
        dup = False
        try:
            pr = ex.private_mix_get_v2_mix_order_orders_plan_pending(
                {"productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"})
            for x in ((pr.get("data") or {}).get("entrustedList", [])):
                if str(x.get("side")) == "sell" and abs(float(x.get("triggerPrice", 0)) - tp_r) < 1e-8:
                    dup = True; break
        except Exception as e:
            print(f'  (查计划单异常 {sym}: {str(e)[:50]})')
        if dup:
            print(f'  ⏭ {sym} 已有 +{int(TP_SERVER_PCT*100)}% 减半止盈计划单 @ {tp_r}, 不重复挂')
            return half
        o = ex.create_trigger_order(sym, "market", "sell", half, None, tp_r, {
            "hedged": True, "productType": "USDT-FUTURES", "reduceOnly": True})
        if not (o and o.get("id")):
            print(f'  ⚠️ {sym} 减半止盈下单无orderId: {o}, 全额交移动止盈')
            return bought_qty
        print(f'  🧾 {sym} 已挂减半止盈 @ {tp_r} (+{int(TP_SERVER_PCT*100)}%) qty={half}, 剩余交移动止盈')
        return half
    except Exception as e:
        print(f'  ⚠️ {sym} 挂减半止盈失败: {str(e)[:120]}, 全额交移动止盈')
        return bought_qty


def _record_open(sym, qty, entry_price):
    opens = load_opens()
    opens[sym] = {
        'qty': qty, 'entry_price': entry_price,
        'activate': entry_price * (1 + TP_ACTIVATE_PCT),
        'trailing_high': entry_price, 'activated': False,
        'open_time': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
    }
    save_opens(opens)
    print(f'  📇 记录开仓状态 {sym} entry={entry_price} qty张={qty}')

# ═══════════════ 跟踪止盈巡检 ═══════════════
def _trailing_tp_check(ex):
    opens = load_opens()
    if not opens:
        return
    if not ex.markets:
        ex.load_markets()

    # 幽灵清理: 以 Bitget 真实 LONG 持仓为准
    # [2026-09-09 加固] 只有真实拉取成功(fetch_ok)才允许清幽灵; 失败/瞬断时不清理(防止把
    #    真实在持币的记账误删 → 下一轮 scan 因本地账本缺失而重复叠开, 与币安全仓侧看齐).
    fetch_ok = False
    actual_long = set()
    try:
        for p in ex.fetch_positions():
            if float(p.get('contracts') or 0) != 0 and p.get('side') == 'long':
                s = p.get('symbol')
                if isinstance(s, str) and s:
                    actual_long.add(s)
        fetch_ok = True
    except Exception as e:
        print(f'  ⚠️ 拉真实持仓失败, 跳过幽灵清理(保留记账防误删): {str(e)[:80]}')
    if fetch_ok:
        removed = [s for s in list(opens) if s not in actual_long]
        if removed:
            for s in removed:
                opens.pop(s, None)
                print(f'  🧹 {s} Bitget已无LONG持仓, 清除幽灵记录')

    for sym, st in list(opens.items()):
        try:
            last = ex.fetch_ticker(sym)['last']
            entry = st['entry_price']
            high = st['trailing_high']
            if last > high:
                st['trailing_high'] = last; high = last
            if not st['activated']:
                if last >= entry * (1 + TP_ACTIVATE_PCT):
                    st['activated'] = True
                    print(f'  🎯 {sym} TP激活 @ {last} (entry={entry})')
            if st['activated']:
                drawdown = (high - last) / high
                if drawdown >= TP_DRAWDOWN_PCT:
                    if _close_long(ex, sym, st):
                        opens.pop(sym, None)
                    continue
        except Exception as e:
            print(f'  ⚠️ {sym} TP巡检异常: {str(e)[:80]}')
        save_opens(opens)

def _close_long(ex, sym, st):
    try:
        if not ex.markets:
            ex.load_markets()
        # 双向模式 LONG 平仓 = sell reduce. Bitget 允许 reduceOnly? 参考罗海: 平仓用 create_order+reduceOnly
        order = ex.create_order(sym, "market", "sell", float(st['qty']), None, {
            "hedged": True,
            "reduceOnly": True,
            "productType": "USDT-FUTURES",
        })
        print(f'  ✅ 移动止盈平多 {sym} qty={st["qty"]} orderId={order.get("id")}')
        return True
    except Exception as e:
        print(f'  ❌ {sym} 平仓失败: {str(e)[:150]}')
        return False

# ═══════════════ CLI ═══════════════
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['scan', 'tpcheck'])
    ap.add_argument('--dry', action='store_true')
    args = ap.parse_args()
    if args.cmd == 'scan':
        scan_funding_and_detect(dry_run=args.dry)
    elif args.cmd == 'tpcheck':
        _trailing_tp_check(get_exchange())
        print('[TPCHECK] 完成')

if __name__ == '__main__':
    main()
