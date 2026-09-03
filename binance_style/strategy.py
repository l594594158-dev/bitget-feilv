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
def load_rate_db():
    db = {}
    if os.path.exists(RATE_DB_FILE):
        for line in open(RATE_DB_FILE):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                db.setdefault(rec['symbol'], []).append(rec)
            except Exception:
                continue
    return db

def append_rate(rec):
    with open(RATE_DB_FILE, 'a') as f:
        f.write(json.dumps(rec) + '\n')

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
    db = load_rate_db()
    track = load_track()
    now_ms = int(time.time() * 1000)
    ts_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')

    # 1. Bitget 全市场费率(批量) + 价格
    fr = ex.fetch_funding_rates()
    tk = ex.fetch_tickers()

    # 先取一次, 记录所有 USDT 永续
    symbols = [s for s in fr if s.endswith('/USDT:USDT')]
    fetched = 0
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
        append_rate({'symbol': sym, 'ts': now_ms, 'rate': float(rate),
                     'price': last, 'time': ts_str})
        fetched += 1
    print(f'[SCAN] 记录 {fetched} 个币的费率/价格 (Bitget)')

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

    if abs_rate < TRACK_START_ABS:
        return None, False
    if abs_rate < TRACK_START_MAX:
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
        if rise > PRICE_RISE_PCT:
            return {'mode': 'consumed', 'start_price': start_price}, True
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
    try:
        actual_long = set()
        for p in ex.fetch_positions():
            if float(p.get('contracts') or 0) != 0 and p.get('side') == 'long':
                actual_long.add(p['symbol'])
        removed = [s for s in opens if s not in actual_long]
        if removed:
            for s in removed:
                opens.pop(s, None)
                print(f'  🧹 {s} Bitget已无LONG持仓, 清除幽灵记录')
    except Exception as e:
        print(f'  ⚠️ 拉真实持仓失败: {str(e)[:80]}')

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
