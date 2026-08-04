#!/usr/bin/env python3
"""检查某策略账户: 持仓 + 各币种待触发计划单(止损/止盈)"""
import load_env, config, ccxt

def fmt(x, d=0):
    try:
        return f"{float(x):.{d}f}"
    except:
        return str(x)

ex = ccxt.bitget({'apiKey':config.API_KEY,'secret':config.API_SECRET,'password':config.API_PASS,'options':{'defaultType':'swap'}})
ex.load_markets()
pos = ex.fetch_positions()
op = [p for p in pos if float(p['contracts'] or 0) != 0]
print(f"===== 当前账户: 持仓 {len(op)} 个 =====")
syms = []
for p in op:
    sym = p['symbol']
    base = sym.split('/')[0]
    syms.append(base)
    print(f"  {sym:20s} {p['side']:5s} 张={fmt(p['contracts'])} 开仓={p['entryPrice']}")

print()
print("===== 各持仓币种的计划委托(止损/止盈) =====")
for base in syms:
    try:
        m = ex.market(f"{base}/USDT:USDT")
        sym_id = m['id']
        pr = ex.private_mix_get_v2_mix_order_orders_plan_pending({
            "productType": "USDT-FUTURES",
            "symbol": sym_id,
            "planType": "normal_plan",
        })
        lst = (pr.get("data") or {}).get("entrustedList") or []
        if not lst:
            print(f"  {base:10s} ❌ 无计划单(未挂止损/止盈!)")
        for o in lst:
            print(f"  {base:10s} {str(o.get('side')):5s} 触发={fmt(o.get('triggerPrice'),6)} 执行价={fmt(o.get('executePrice'),6)} planType={o.get('planType')} reduceOnly={o.get('reduceOnly')} 状态={o.get('status')}")
    except Exception as e:
        print(f"  {base:10s} 查询异常: {e}")
