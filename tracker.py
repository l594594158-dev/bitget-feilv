#!/usr/bin/env python3
"""
tracker.py — 资金费率反弹策略：持仓追踪平仓

每 5 秒拉一次持仓，检查总浮盈是否超过阈值，达标则全平。

crontab 每分钟触发（保证程序被拉起），内部自循环 60s。
"""
import sys, os, json, time, hmac, hashlib, base64
from datetime import datetime

# 先加载 .env 再到 config（直接内联）
_dotenv = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(_dotenv):
    with open(_dotenv) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#'):
                _k, _, _v = _line.partition('=')
                if _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import *

import requests
import ccxt

# ─── TG ────────────────────────────────────────────────────────────
def tg_send(text: str):
    if not TG_BOT_TOKEN:
        print(f"[TG] (未配置) {text[:200]}")
        return
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[TG ERR] {e}")

# ─── 交易所 ────────────────────────────────────────────────────────
def get_exchange():
    return ccxt.bitget({
        "apiKey": API_KEY,
        "secret": API_SECRET,
        "password": API_PASS,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })

# ─── 核心追踪 ──────────────────────────────────────────────────────
def check_and_close():
    ex = get_exchange()
    positions = ex.fetch_positions()

    has_positions = [p for p in positions if p.get("contracts") and float(p["contracts"]) > 0]

    if not has_positions:
        print(f"[TRACKER] {datetime.now().strftime('%H:%M:%S')} 无持仓")
        return  # 没持仓就睡

    # 计算总浮盈和总浮亏
    total_profit = 0.0   # 正浮盈总和
    total_loss = 0.0     # 负浮亏总和
    total_fee_base = 0.0
    details = []

    for p in has_positions:
        pnl = float(p.get("unrealizedPnl", 0) or 0)
        # 保证金从原始 info 的 marginSize 读取（ccxt .margin 可能为空）
        info = p.get("info", {})
        margin = float(info.get("marginSize", "0") or 0)
        lever = float(info.get("leverage", LEVERAGE))
        notional = margin * lever  # 实际成交额 = 保证金 × 杠杆
        fee_cost = notional * TOTAL_FEE_RATE  # 开平仓合并手续费 = 成交额 × 0.1%
        
        if pnl > 0:
            total_profit += pnl
        else:
            total_loss += pnl  # 负数
        total_fee_base += fee_cost

        side = "多" if p.get("side") == "long" else "空"
        details.append({
            "symbol": p["symbol"],
            "pnl": pnl,
            "margin": margin,
            "leverage": lever,
            "notional": notional,
            "side": side,
        })

    # 净浮盈 = 所有盈利仓的总和 - 所有亏损仓的总和（减去负值）
    net_pnl = total_profit + total_loss  # loss是负数，所以加上等于减去绝对值
    threshold = total_fee_base * PROFIT_MULTIPLIER

    print(f"[TRACKER] {datetime.now().strftime('%H:%M:%S')} "
          f"持仓={len(has_positions)} 盈利={total_profit:.4f}U "
          f"亏损={total_loss:.4f}U "
          f"净浮盈={net_pnl:.4f}U "
          f"手续费基数={total_fee_base:.4f}U 阈值={threshold:.4f}U")

    if net_pnl <= threshold:
        return

    # ---- 达标！平仓 ----
    print(f"[TRACKER] 🎯 净浮盈 {net_pnl:.4f} > 阈值 {threshold:.4f}，开始平仓")

    # 市价全平（直调 Bitget API，无间隔）
    base_url = "https://api.bitget.com"
    closed = []
    for d in details:
        try:
            sym_raw = d["symbol"].split("/")[0] + "USDT"
            pos = [p for p in ex.fetch_positions() if p["symbol"] == d["symbol"]]
            if not pos:
                continue
            total = float(pos[0].get("info", {}).get("total", "0"))
            if total <= 0:
                continue
            side = "sell" if d["side"] == "多" else "buy"
            body = json.dumps({
                "symbol": sym_raw,
                "productType": "USDT-FUTURES",
                "marginCoin": "USDT",
                "marginMode": "isolated",
                "side": side,
                "orderType": "market",
                "size": str(int(total)),
                "leverage": str(int(d["leverage"])),
                "reduceOnly": "YES",
            })
            t = str(int(time.time() * 1000))
            msg = t + "POST" + "/api/v2/mix/order/place-order" + body
            sig = base64.b64encode(hmac.new(API_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
            hdrs = {"ACCESS-KEY": API_KEY, "ACCESS-SIGN": sig, "ACCESS-TIMESTAMP": t,
                    "ACCESS-PASSPHRASE": API_PASS, "Content-Type": "application/json"}
            r = requests.post(base_url + "/api/v2/mix/order/place-order", headers=hdrs, data=body, timeout=10)
            resp = r.json()
            if resp.get("code") == "00000":
                closed.append(d["symbol"])
                print(f"   ✅ 平仓 {d['symbol']} 浮盈 {d['pnl']:.4f}U")
            else:
                print(f"   ❌ {d['symbol']} 平仓失败: {resp.get('code')} - {resp.get('msg')}")
        except Exception as e:
            print(f"   ❌ {d['symbol']} 平仓异常: {e}")

    # 日志
    log_entry = {
        "ts": int(time.time() * 1000),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "net_pnl": net_pnl,
        "total_profit": total_profit,
        "total_loss": total_loss,
        "threshold": threshold,
        "fee_base": total_fee_base,
        "details": details,
        "closed": closed,
    }
    os.makedirs(LOG_DIR, exist_ok=True)
    log_file = os.path.join(LOG_DIR, f"close_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(log_file, "w") as f:
        json.dump(log_entry, f, indent=2)

    # TG
    profit_entries = [d for d in details if d['pnl'] > 0]
    loss_entries = [d for d in details if d['pnl'] <= 0]
    msg = f"🏁 止盈平仓!\n"
    msg += f"净浮盈 {net_pnl:.4f}U / 阈值 {threshold:.4f}U\n"
    msg += f"盈利仓: {sum(d['pnl'] for d in profit_entries):.4f}U | 亏损仓: {sum(d['pnl'] for d in loss_entries):.4f}U\n"
    for d in details:
        pnl_str = f"+{d['pnl']:.4f}" if d['pnl'] > 0 else f"{d['pnl']:.4f}"
        msg += f"• {d['symbol']} {d['side']} {pnl_str}U\n"
    tg_send(msg)

# ─── 主循环 ────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("[TRACKER] 启动，每 5 秒检查一次…")

    # crontab 每分钟触发，内部循环 55 秒
    start_time = time.time()
    timeout = 55  # 最多跑 55 秒，给下个 cron 留余地

    while time.time() - start_time < timeout:
        try:
            check_and_close()
        except Exception as e:
            print(f"[TRACKER ERROR] {e}")
            import traceback; traceback.print_exc()
        time.sleep(3)

    print("[TRACKER] 本轮结束（将由 crontab 重新拉起）")
