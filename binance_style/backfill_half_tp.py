#!/usr/bin/env python3
"""给 Bitget 现存 LONG 老仓补挂 +20% 减半止盈 + 纳入移动止盈监控(方案1)。
娜姐 2026-09-03 已批准对全部老仓执行。
用法: python3 backfill_half_tp.py   -> 真挂(已授权)
"""
import os, sys, math
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
from strategy import get_exchange, load_opens, save_opens
from config import TP_SERVER_PCT, TP_SERVER_FRAC

ex = get_exchange(); ex.load_markets()
opens = load_opens()
pos = [p for p in ex.fetch_positions() if float(p.get('contracts') or 0) != 0 and p.get('side') == 'long']
print(f'=> Bitget 现存 LONG {len(pos)} 个, 开始补挂 +20% 减半止盈并接管剩下一半')

done = skip = fail = 0
for p in pos:
    sym = p['symbol']
    real_qty = float(p['contracts'])
    entry = float(p['entryPrice'])
    market = ex.market(sym)
    sid = (market.get("info") or {}).get("symbol") or market.get("id")
    pp = int((market.get("info") or {}).get("pricePlace", "8"))
    # 触发价 +20%
    tp = entry * (1 + TP_SERVER_PCT)
    tp_r = math.floor(tp * (10 ** pp)) / (10 ** pp)
    # 一半
    half = real_qty * TP_SERVER_FRAC
    half = float(ex.amount_to_precision(sym, half))
    if half <= 0 or half >= real_qty:
        print(f'  ❌ {sym} half计算异常, 跳过'); fail += 1; continue
    # 查重
    dup = False
    try:
        pr = ex.private_mix_get_v2_mix_order_orders_plan_pending(
            {"productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"})
        _d = pr.get("data") if isinstance(pr, dict) else None
        lst = (_d or {}).get("entrustedList", []) if isinstance(_d, dict) else []
        dup = any(str(x.get("side")) == "sell" and abs(float(x.get("triggerPrice", 0)) - tp_r) < 1e-8 for x in lst)
    except Exception as e:
        print(f'  (查单异常 {sym}: {str(e)[:50]})')
    if dup:
        print(f'  ⏭ {sym} 已有 +20%减半止盈计划单 @ {tp_r}, 跳过')
        skip += 1
    else:
        try:
            o = ex.create_trigger_order(sym, "market", "sell", half, None, tp_r,
                                        {"hedged": True, "productType": "USDT-FUTURES", "reduceOnly": True})
            if o and o.get("id"):
                print(f'  ✅ {sym} 补挂减半止盈 @ {tp_r} qty={half}')
                done += 1
            else:
                print(f'  ⚠️ {sym} 挂单返回无id: {o}'); fail += 1
        except Exception as e:
            print(f'  ❌ {sym} 挂单异常: {str(e)[:110]}'); fail += 1
    # 接管移动止盈: 状态记录剩下一半(half), entry=真实持仓价
    opens[sym] = {
        'qty': half, 'entry_price': entry,
        'activate': entry * (1 + TP_SERVER_PCT),
        'trailing_high': entry, 'activated': False,
        'open_time': 'backfill-origin',
    }
    print(f'  🔄 {sym} 已交由移动止盈接管剩下一半 qty={half}')
save_opens(opens)
print(f'===== 完成: 新挂 {done}, 已有 {skip}, 失败 {fail} ====')
