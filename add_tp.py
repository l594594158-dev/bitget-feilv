#!/usr/bin/env python3
"""给当前已持仓仓位补挂 20% 止盈单(reduceOnly, 跟止损对称)。
  多仓 TP=开仓x1.2/sell平  空仓 TP=开仓x0.8/buy平
"""
import load_env, config, ccxt, math, time

def fmt(x, d=6):
    try:
        return f"{float(x):.{d}f}"
    except:
        return str(x)

ex = ccxt.bitget({'apiKey':config.API_KEY,'secret':config.API_SECRET,'password':config.API_PASS,'options':{'defaultType':'swap'}})
ex.load_markets()
pos = ex.fetch_positions()
op = [p for p in pos if float(p['contracts'] or 0) != 0]
print(f"===== 账户: 持仓 {len(op)} 个,开始补挂止盈 =====")

done, skip, fail = 0, 0, 0
for p in op:
    sym = p['symbol']
    side = p['side']
    contracts = float(p['contracts'])
    entry = float(p['entryPrice'])
    market = ex.market(sym)
    pp = int((market.get("info") or {}).get("pricePlace", "6"))
    reduce_side = "sell" if side == "long" else "buy"
    # 止盈: 多仓 x1.2, 空仓 x0.8
    tp = entry * 1.2 if side == "long" else entry * 0.8
    tp_r = math.floor(tp * (10 ** pp)) / (10 ** pp)

    # 先查该币种是否已有止盈单(避免重复挂)
    sid = market.get("id")
    try:
        pr = ex.private_mix_get_v2_mix_order_orders_plan_pending(
            {"productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"})
        lst = (pr.get("data") or {}).get("entrustedList", [])
        has_tp = any(str(x.get("side")) == reduce_side and
                     abs(float(x.get("triggerPrice", 0)) - tp_r) < 1e-8 for x in lst)
        if has_tp:
            print(f"  ⏭ {sym:20s} 已有止盈 @ {tp_r},跳过")
            skip += 1
            continue
    except Exception as e:
        print(f"  (查询计划单异常 {sym}: {str(e)[:50]}),继续尝试挂单")

    try:
        o = ex.create_trigger_order(sym, "market", reduce_side, contracts, None, tp_r, {
            "marginMode": "crossed", "productType": "USDT-FUTURES", "reduceOnly": True})
        ok = bool(o and o.get("id"))
        # 回读确认
        verified = False
        try:
            ex.load_markets()
            pr = ex.private_mix_get_v2_mix_order_orders_plan_pending(
                {"productType": "USDT-FUTURES", "symbol": sid, "planType": "normal_plan"})
            lst = (pr.get("data") or {}).get("entrustedList", [])
            verified = any(str(x.get("side")) == reduce_side and
                           abs(float(x.get("triggerPrice", 0)) - tp_r) < 1e-8 for x in lst)
        except Exception as ve:
            print(f"  (回读异常: {str(ve)[:40]})")
        if ok and verified:
            print(f"  ✅ {sym:20s} {side:5s} 止盈已挂并确认 @ {tp_r} (+20%)")
            done += 1
        elif ok:
            print(f"  ⚠️ {sym:20s} 止盈已下单 @ {tp_r} 但回读未确认")
            done += 1
        else:
            print(f"  ❌ {sym:20s} 止盈下单返回异常: {o}")
            fail += 1
    except Exception as e:
        print(f"  ❌ {sym:20s} 挂止盈异常: {str(e)[:80]}")
        fail += 1

print(f"===== 完成: 新挂 {done}, 跳过已有 {skip}, 失败 {fail} =====")
